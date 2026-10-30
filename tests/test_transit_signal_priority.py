"""The TSP controller (transit_signal_priority.py): its configuration checks, each case of hold, truncation,
check-in and check-out, recovery and its sync with a plan alongside; and the agent's person-delay estimate."""

from traffic_simulator.transit_signal_priority import ApproachCfg, NodeCfg, PhaseCfg, TSPController


def two(**change) -> NodeCfg:
    phases = [PhaseCfg(phase_id=k, planned_green=30, min_green=10, yellow=3, all_red=3) for k in ("NS", "EW")]
    approaches = [ApproachCfg(approach_id=a, phase_id=p, x=300, v_lo=5, v_hi=12.5)
                  for a, p in (("N", "NS"), ("S", "NS"), ("E", "EW"), ("W", "EW"))]
    return NodeCfg(**{"phases": phases, "approaches": approaches, "W": 12, "v_cross": 10, "W_max": 60, **change})


def four() -> NodeCfg:
    phases = [PhaseCfg(phase_id=k, planned_green=20, min_green=8, yellow=3, all_red=3) for k in "NESW"]
    approaches = [ApproachCfg(approach_id=k, phase_id=k, x=600, v_lo=5, v_hi=12.5) for k in "NESW"]
    return NodeCfg(phases=phases, approaches=approaches, W=12, v_cross=10, W_max=120)


def checkin(bus, approach, d=300.0):
    return lambda c, t: c.on_checkin(bus, approach, d, 12.5, 0, t)


def checkout(bus):
    return lambda c, t: c.on_checkout(bus, t)


def run(c, events, t_to, t_from=0, states=None):
    states = {} if states is None else states
    for t in range(t_from, t_to):
        for event in events.get(t, ()):
            event(c, t)
        states[t] = c.step(t)
    return states


def at(c, name, phase=None):
    return [t for t, event, info in c.log if event == name and (phase is None or info.get("phase") == phase)]


def rejects(cfg, word):
    try:
        TSPController(cfg())
    except ValueError as exc:
        return word in str(exc)
    return False


def test_config_rejects_short_x():
    assert rejects(lambda: two(approaches=[ApproachCfg(approach_id="N", phase_id="NS", x=100, v_lo=5, v_hi=12.5)]), "x")


def test_config_rejects_short_all_red():
    phases = [PhaseCfg(phase_id=k, planned_green=30, min_green=10, yellow=1, all_red=1) for k in ("NS", "EW")]
    assert rejects(lambda: two(phases=phases), "all_red")  # 2 s to clear, the bus needs (12 + 12) / 10 = 2.4
    phases = [PhaseCfg(phase_id=k, planned_green=30, min_green=10, yellow=3, all_red=1) for k in ("NS", "EW")]
    TSPController(two(phases=phases))  # the change interval clears it, all-red alone needn't


def test_case_A_no_action():
    c = TSPController(two())
    run(c, {2: [checkin("b1", "N")], 28: [checkout("b1")]}, 60)
    assert not at(c, "HOLD") and not at(c, "TRUNCATE") and at(c, "RELEASE") == [28]


def test_case_B_hold_until_checkout():
    c = TSPController(two())
    states = run(c, {10: [checkin("b1", "N")], 37: [checkout("b1")]}, 50)
    assert at(c, "HOLD") == list(range(30, 37)) and at(c, "RELEASE_END") == [37] and states[37] == ("NS", "Y")


def test_case_D_ext_infeasible():
    c = TSPController(two())
    run(c, {25: [checkin("b1", "N")]}, 40)
    assert at(c, "END_EXT") == [30] and at(c, "TRUNCATE") == [31] and not at(c, "HOLD")


def test_case_C_truncate_timing():
    c = TSPController(two())
    states = run(c, {40: [checkin("b1", "N")], 67: [checkout("b1")]}, 70)
    g_star = 40 + 300 / 12.5
    expected = next(t for t in range(40, 70) if t + max(0, 10 - (t - 36)) + 3 + 3 >= g_star)
    assert at(c, "TRUNCATE") == [expected] == [58] and states[64] == ("NS", "G")


def test_request_during_own_yellow():
    c = TSPController(two())
    states = run(c, {31: [checkin("b1", "N")]}, 45)
    assert all(states[t][1] != "G" for t in range(31, 36)) and states[36] == ("EW", "G")
    assert ("NS", "G") not in [states[t] for t in range(30, 45)] and not at(c, "HOLD")


def test_early_checkout_release():
    c = TSPController(two())
    run(c, {2: [checkin("b1", "N")], 10: [checkout("b1")]}, 20)
    assert at(c, "RELEASE") == [10] and not at(c, "RELEASE_END") and c.active is None


def test_cap_hit():
    c = TSPController(two())
    states = run(c, {10: [checkin("b1", "N")]}, 50)
    holds = [info["ext_used"] for _, event, info in c.log if event == "HOLD"]
    assert max(holds) == c.cfg.E_max == len(holds) and at(c, "END_EXT") == [45] and states[45] == ("NS", "Y")


def test_timeout():
    c = TSPController(two())
    run(c, {2: [checkin("b1", "N")]}, 190)
    assert at(c, "TIMEOUT") == [183] and 183 in at(c, "RELEASE") and c.active is None


def test_same_phase_merge():
    c = TSPController(two())
    run(c, {2: [checkin("b1", "N"), checkin("b2", "S")], 20: [checkout("b1")]}, 21)
    assert c.active.bus_ids == {"b1", "b2"} and not c.queue and not at(c, "RELEASE")
    run(c, {25: [checkout("b2")]}, 30, 21)
    assert at(c, "RELEASE") == [25]


def test_conflict_fifo():
    c = TSPController(two())
    run(c, {2: [checkin("a", "E")], 3: [checkin("b", "N")]}, 4)
    assert c.active.phase_id == "EW" and [r.phase_id for r in c.queue] == ["NS"]
    run(c, {50: [checkout("a")], 80: [checkout("b")]}, 90, 4)
    assert [info["phase"] for _, event, info in c.log if event.startswith("RELEASE")] == ["EW", "NS"]


def test_back_to_back_no_starvation():
    c = TSPController(two())
    events = {}
    for i in range(10):
        events.setdefault(5 + 30 * i, []).append(checkin(f"b{i}", "N"))
        events.setdefault(31 + 30 * i, []).append(checkout(f"b{i}"))
    states = run(c, events, 400)  # _check asserts W_max every tick
    starts = [t for t in range(1, 400) if states[t] == ("EW", "G") and states[t - 1] != ("EW", "G")]
    assert len(starts) >= 5 and all(b - a <= c.cfg.W_max + 30 for a, b in zip(starts, starts[1:]))


def test_recovery_credit():
    c = TSPController(two())
    states = run(c, {40: [checkin("b1", "N")], 67: [checkout("b1")]}, 140)
    assert c.deficit["EW"] == 0 and states[92] == ("EW", "G")  # cut 8 s at 58, paid back from 92
    green = [t for t in range(92, 140) if states[t] == ("EW", "G")]
    assert len(green) == 30 + min(8, c.cfg.C_max) == 38


def test_sync_takes_over_where_the_plan_is():
    c = TSPController(two())  # a plan alongside: NS green 30 s, its green began at 100, EW's ended at 94
    c.sync(129, "NS", "G", 100, 30, {"EW": 94})
    assert c.step(129) == ("NS", "G") and c.step(130) == ("NS", "Y")  # its green ends as the plan's does
    c.sync(130, "NS", "G", 100, 30, {"EW": 94})  # a bus checked in on N now: held past the plan's end
    c.on_checkin("b1", "N", 60, 12.5, 0, 130)
    assert c.step(130) == ("NS", "G") and at(c, "HOLD") == [130]
    c = TSPController(two())
    c.sync(205, "EW", "AR", 204, 30, {"NS": 170})  # in the plan's all-red: 1 s of it gone
    assert c.interval == "AR" and c.t_green_onset == 204 - 30 - 3 and c.red_since["EW"] == 204 - 3
    assert c.step(205) == ("EW", "AR") and c.deficit == {"NS": 0.0, "EW": 0.0}


def test_set_green_moves_the_end_of_the_green_running():
    c = TSPController(two())
    run(c, {}, 5)
    c.set_green(20)  # ends at 20 instead of 30, no debt counted
    states = run(c, {}, 30, 5)
    assert states[19] == ("NS", "G") and states[20] == ("NS", "Y") and c.deficit["NS"] == 0
    c.set_green(3)  # never under the minimum green
    assert c.green_target == 10 or c.interval != "G"


def test_four_phase_right_turn():
    c = TSPController(four())  # a right-turning bus on approach N: phase N serves all of N's movements
    states = run(c, {30: [checkin("bus", "N", 600)], 81: [checkout("bus")]}, 90)
    assert at(c, "TRUNCATE", "N") == [44] and states[78] == ("N", "G") and at(c, "RELEASE", "N") == [81]
    assert [t for t in range(44, 78) if states[t] == ("S", "G")] == list(range(50, 58))  # eff_min 8 s


def test_the_person_delay_estimate_weighs_passengers_against_the_cross_streets():
    from types import SimpleNamespace  # noqa: PLC0415

    from traffic_simulator.agent_transit_signal_priority import person_delay  # noqa: PLC0415

    phases = [PhaseCfg(phase_id="A", planned_green=30, min_green=7, yellow=3, all_red=2),
              PhaseCfg(phase_id="B", planned_green=20, min_green=7, yellow=3, all_red=2)]
    controller = SimpleNamespace(cfg=SimpleNamespace(phases=phases, E_max=15.0))

    def message(arrives_on, state, eta, passengers=40, queued=10, room=True, **spat):
        return {"request": {"phase": "A", "eta_s": eta, "arrives_on": arrives_on, "passengers": passengers,
                            "exit_has_room": room},
                "spat": {"bus_phase_state": state, **spat},
                "approaches": {"bus": {"bus_approach": True, "people_queued": 99},
                               "cross": {"bus_approach": False, "people_queued": queued}}}

    held = person_delay(message("red", "green", 12, bus_green_ends_in_s=5), controller)
    # its green ends 7 s before it arrives: a 7 s hold saves the red until its next green (5 + 3 + 2 + 20 + 3 + 2 = 35 s
    # from now, so 23 s after it would have arrived); the cross streets lose the 7 s
    assert held["bus_seconds_saved"] == 23 and held["cross_seconds_lost"] == 7
    assert held["person_s_saved"] == 40 * 23 and held["person_s_lost"] == 10 * 7 and held["favours"] == "priority"
    too_far = person_delay(message("red", "green", 30, bus_green_ends_in_s=5), controller)
    assert too_far["bus_seconds_saved"] == 0 and "over the 15 s" in too_far["how"]
    early = person_delay(message("red", "red", 5, passengers=2, queued=30, bus_green_starts_in_s=20), controller)
    # waiting 15 s at a red; B's green can lose up to 20 - 7 = 13 s: 13 s saved for 2 people, 13 lost for 30
    assert early["bus_seconds_saved"] == 13 and early["favours"] == "plan" and early["net_person_s"] == 2 * 13 - 30 * 13
    full = person_delay(message("red", "red", 5, room=False, bus_green_starts_in_s=20), controller)
    assert full["bus_seconds_saved"] == 0 and "exit lane is full" in full["how"]
    green = person_delay(message("green", "green", 5, bus_green_ends_in_s=20), controller)
    assert green["net_person_s"] == 0 and green["favours"] == "plan"


if __name__ == "__main__":
    for name, check in list(globals().items()):
        if name.startswith("test_"):
            check()
    print("ok")

"""The TSP controller's own tests (in transit_signal_priority.py, under __main__), run as part of
the suite. Run with `python tests/test_transit_signal_priority.py` (or the test monitor)."""

import subprocess
import sys
from pathlib import Path

CONTROLLER = Path(__file__).resolve().parents[1] / "src" / "traffic_simulator" / "transit_signal_priority.py"


def test_every_controller_test_passes():
    result = subprocess.run([sys.executable, str(CONTROLLER)], capture_output=True, text=True, timeout=120)
    passed = [line for line in result.stdout.splitlines() if line.startswith("PASS ")]
    assert result.returncode == 0 and len(passed) == 17, result.stdout + result.stderr


def test_the_person_delay_estimate_weighs_passengers_against_the_cross_streets():
    from types import SimpleNamespace  # noqa: PLC0415

    from traffic_simulator.agent_transit_signal_priority import person_delay  # noqa: PLC0415
    from traffic_simulator.transit_signal_priority import PhaseCfg  # noqa: PLC0415

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
    test_every_controller_test_passes()
    test_the_person_delay_estimate_weighs_passengers_against_the_cross_streets()
    print("ok")

"""Signal timing: Webster, TRANSYT, Synchro, SCATS and SCOOT (signal_timing.py) against hand-worked
numbers and their published behaviour; offsets and coordinated groups in a run; the loop detectors;
adaptive plans changing as a run goes, with TSP following them; the junction editor's settings.
Run with `python tests/test_signal_timing.py` (or the test monitor)."""

import os
from math import isclose

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from traffic_simulator.mode_behavior import PALETTE, VehicleMode  # noqa: E402
from traffic_simulator.network_generator import generate_network  # noqa: E402
from traffic_simulator.signal_timing import (  # noqa: E402
    MAX_CYCLE_S, MIN_GREEN_S, SPAN_S, STANDARD_SPACE_S, Approach, ScatsJunction, ScatsRegion, ScootJunction, ScootLink, ScootRegion, TimingModel, cyclic_queue,
    degree_of_saturation, disperse, fit_greens, hcm_delay, hill_climb, incremental_delay, percentile_greens,
    clearance, effective, realigned_length, synchro_cycle, webster)
from traffic_simulator.simulation import Loop, PairDemand, Simulation, Turn  # noqa: E402

APP = QApplication.instance() or QApplication([])
CAR = [VehicleMode("Car", PALETTE[1])]


def pair(origin, destination, flow):
    return PairDemand(origin, destination, flow, None, [], "Poisson", (100,))


def test_a_platoon_disperses_and_keeps_its_vehicles():
    platoon = [1.0] * 10 + [0.0] * 50  # 10 s at 1 veh/s, then nothing, round a 60 s cycle
    spread = disperse(platoon, 20.0)  # lag 0.8 x 20 = 16 s, F = 1 / (1 + 0.35 x 16)
    assert isclose(sum(spread), 10.0, rel_tol=1e-4)  # every vehicle arrives (steady to 0.01 %)
    assert max(spread) < 1.0 and spread.index(max(spread)) >= 16  # later, and flatter
    assert isclose(spread[16], 1 / (1 + 0.35 * 16) + (1 - 1 / (1 + 0.35 * 16)) * spread[15], rel_tol=1e-4)  # Robertson


def test_the_cyclic_queue_matches_the_hand_worked_delay():
    arrivals, green = [0.2] * 60, [False] * 30 + [True] * 30  # 0.2 veh/s, 30 s red, 30 s effective green, 0.5 veh/s
    queue = cyclic_queue(arrivals, green, 0.5)
    uniform = 0.2 * 30 ** 2 / (2 * (1 - 0.2 / 0.5))  # q·r² / 2(1 − q/s) = 150 veh·s a cycle
    assert isclose(queue.saturation, 0.8) and isclose(sum(queue.departures), 12.0)
    assert isclose(queue.delay, uniform + 12 * incremental_delay(0.8, 900.0))  # plus the HCM's random delay
    assert isclose(queue.stops, 6.0 + 4.0)  # the red's 6, and 4 more arriving while its queue clears (20 s)
    over = cyclic_queue([0.4] * 60, green, 0.5)  # 24 arrive, 15 can go: over capacity
    assert isclose(over.saturation, 1.6) and isclose(sum(over.departures), 15.0) and over.delay > 10 * queue.delay


def test_the_hcm_delay_matches_a_hand_calculation():
    # 600 veh/h, 1800 veh/h saturation, 30 s effective green in 60 s: X = 2/3.
    d1 = 0.5 * 60 * (1 - 0.5) ** 2 / (1 - (2 / 3) * 0.5)  # 11.25 s
    d2 = 900 * 0.25 * ((2 / 3 - 1) + ((2 / 3 - 1) ** 2 + 8 * 0.5 * 1 * (2 / 3) / (900 * 0.25)) ** 0.5)
    assert isclose(hcm_delay(600, 1800, 30, 60), d1 + d2) and 15.0 < d1 + d2 < 15.2


def test_greens_fill_the_cycle_and_keep_their_minimum():
    greens = fit_greens([0.3, 0.1], 60)
    assert isclose(sum(greens) + 2 * SPAN_S, 60) and isclose(effective(greens[0]) / effective(greens[1]), 3)  # 3:1
    tight = fit_greens([0.5, 0.01, 0.01], 60)
    assert isclose(tight[1], 7.0) and isclose(tight[2], 7.0) and isclose(sum(tight) + 3 * SPAN_S, 60)  # the minimum
    assert isclose(sum(fit_greens([0.2, 0.2], 10)) + 2 * SPAN_S, 24)  # too short a cycle: lengthened to the minimums


def test_webster_keeps_its_minimum_greens_within_the_longest_cycle():
    for ratios in ([0.94, 0.0], [0.94, 0.0, 0.0, 0.0], [0.9, 0.04]):  # one phase saturated, the rest on their minimum
        cycle, greens = webster(ratios)
        assert isclose(cycle, MAX_CYCLE_S) and min(greens) >= MIN_GREEN_S - 1e-9, (ratios, cycle, greens)
    cycle, greens = webster([0.5, 0.1])  # C0 = (1.5 x 8 + 5) / 0.4 = 42.5 s: the minimum taken inside it
    assert isclose(cycle, 42.5) and isclose(greens[1], MIN_GREEN_S)
    try:
        webster([0.1] * 4, span=25.0)  # 4 x (7 + 25) s can't fit in 120 s
        raise AssertionError("an impossible minimum cycle was accepted")
    except ValueError as error:
        assert "120" in str(error)


def test_a_junctions_clearance_follows_its_approach_speed_and_width():
    assert clearance(50 / 3.6, 21.8) == (3.3, 2.0)  # ITE: 1 + 13.9 / 6.1 s amber, (21.8 + 6.1) / 13.9 s all-red
    assert clearance(120 / 3.6, 65.5) == (6.0, 2.1)  # 6.5 s amber held to MUTCD's 6 s
    assert clearance(20 / 3.6, 10.0) == (3.0, 2.9)  # at least 3 s amber
    fast = Simulation(generate_network(1, 1, lanes_per_direction=3, speed_limit_kmh=70), [], CAR)
    signal = fast.graph.signals["1,1"]
    assert signal.amber > 4.0 and signal.all_red >= 1.0  # faster and wider than the 3 + 2 s default
    link = next(iter(signal.phase_of))
    green = signal.phases[signal.phase_of[link]][1]
    assert signal.state(link, green + signal.amber - 0.1) == "Y" and signal.state(link, green + signal.amber + 0.1) == "R"
    assert isclose(signal.cycle, sum(green for _, green in signal.phases) + len(signal.phases) * signal.span)


def test_scats_measures_saturation_and_moves_the_cycle():
    assert degree_of_saturation(30, 30, 0) == 0.0  # nothing came
    assert isclose(degree_of_saturation(30, 15, 15, 1.0), 1.0)  # 15 vehicles, each leaving just the standard space
    assert isclose(degree_of_saturation(30, 20, 5, 1.0), 0.5)
    region = ScatsRegion(60, 40, 120)
    junction = ScatsJunction("A", region, [[1, 1], [1, 1], [2, 1], [1, 2]], 0.0, 0.9)
    lanes = ["east", "north"]
    junction.add_lane("east", 0)
    junction.add_lane("north", 1)

    class Signal:  # just what a controller touches
        phases = [(None, 27.5), (None, 22.5)]

        def set_greens(self, greens):
            self.phases = [(None, green) for green in greens]

    signal, cycles, plans = Signal(), [], []
    for cycle in range(6):  # the east phase saturated, north barely used
        for lane, (green, empty, cars) in zip(lanes, ((27.5, 26 * STANDARD_SPACE_S, 26), (22.5, 20.0, 3))):  # DS 1, 0.2
            for _ in range(int(green * 2)):
                junction.measure(lane, True, False, 0, 0.5)
            junction.lanes[lane][2:] = [empty, cars]
        junction.cycle_starts(signal, 60.0 * (cycle + 1))
        cycles.append(region.cycle)
        plans.append(junction.plan)
    assert cycles == sorted(cycles) and cycles[-1] - cycles[0] >= 18 and all(b - a <= 6 for a, b in zip(cycles, cycles[1:]))
    assert plans[0] == 0 and plans[-1] == 2  # voted for the plan that favours east, chosen on its second vote
    assert signal.phases[0][1] > 1.8 * signal.phases[1][1]
    quiet = ScatsRegion(90, 40, 120)
    quiet.report("A", 0.3, 0.0)
    assert quiet.cycle == 84  # under its target: shorter, by at most 6 s a cycle


def test_scoot_moves_green_to_the_busier_phase_and_the_cycle_to_its_target():
    region = ScootRegion(60, 40, 120)
    links = [ScootLink("east", 0, 0.5, 10.0), ScootLink("north", 1, 0.5, 10.0)]
    junction = ScootJunction("A", region, [1.0, 1.0], 0.0, 0.9, links)
    links[0].profile, links[1].profile = [0.4] * 60, [0.05] * 60  # 24 and 3 vehicles a cycle

    class Signal:
        phases = [(None, 25.0), (None, 25.0)]

        def set_greens(self, greens):
            self.phases = [(None, green) for green in greens]

    signal = Signal()
    junction.phase_starts(signal, 0, 0.0)
    assert [green for _, green in signal.phases] == [29.0, 21.0]  # 4 s more for east, the cycle kept
    assert junction.shares[0] > junction.shares[1]  # and the stored split moved 1 s its way
    junction.saturation = junction.saturations([29.0, 21.0])  # east is over 90 %: the cycle lengthens
    region.review(300.0)
    assert region.cycle == 64
    region.reviewed = 0.0
    links[0].profile = links[1].profile = [0.01] * 60
    junction.saturation = junction.saturations([30.0, 25.0])
    region.review(600.0)
    assert region.cycle == 56  # well under target: shorter, 8 s steps above 64 s


def test_realigning_holds_an_offset():
    # Cycles due to start at 30, 90, 150... (offset 30 in a 60 s cycle).
    assert realigned_length(90.0, 0.0, 60.0, 30.0) == 60.0  # on time
    assert realigned_length(92.0, 0.0, 60.0, 30.0) == 58.0  # 2 s late: 2 s shorter
    assert realigned_length(100.0, 0.0, 60.0, 30.0) == 56.0  # 10 s late: shorter, by at most 4 s
    assert realigned_length(80.0, 0.0, 60.0, 30.0) == 64.0  # 10 s early: longer, by at most 4 s


def test_transyt_times_a_corridor_for_its_platoons():
    # Four junctions 500 m apart, 900 veh/h eastbound: the lights should turn green as the platoon comes.
    network = generate_network(1, 4)
    for node in network.nodes.values():
        node.signal_method = "transyt"
    simulation = Simulation(network, [pair("1W", "1E", 900), pair("1E", "1W", 150)], CAR)
    plans = [simulation.signal_plans[f"{i},1"] for i in range(1, 5)]
    cycle = plans[0]["cycle_s"]
    assert {plan["cycle_s"] for plan in plans} == {cycle} and 40 <= cycle <= 120  # one common cycle
    assert plans[0]["pi_after"] < plans[0]["pi_before"] and plans[0]["finished"]  # the hill-climb found better
    travel = simulation.graph.links["1,1-2,1>"].length / simulation.graph.links["1,1-2,1>"].speed
    for upstream, downstream in zip(plans, plans[1:]):  # each green starts about as the platoon arrives
        lag = (downstream["offset_s"] - upstream["offset_s"] - travel) % cycle
        assert min(lag, cycle - lag) <= 10, (upstream["offset_s"], downstream["offset_s"], travel, cycle)
    signal = simulation.graph.signals["2,1"]
    assert signal.offset == plans[1]["offset_s"] and signal.state("1,1-2,1>", signal.offset + 0.5) == "G"
    assert isclose(signal.cycle_start(signal.offset + signal.cycle + 3), signal.offset + signal.cycle)  # at its offset


def test_a_hill_climb_never_makes_the_plan_worse():
    approaches = [Approach("A", "X", 0, 0.5, 0.25, 30.0, [], 0.25, 10.0),
                  Approach("B", "Y", 0, 0.5, 0.25, 30.0, [("A", 1.0)], 0.0, 10.0),
                  Approach("C", "Y", 1, 0.5, 0.05, 30.0, [], 0.05, 10.0)]
    model = TimingModel(approaches, {"X": [30.0, 20.0], "Y": [30.0, 20.0]}, 60)
    model.offsets = {"X": 0.0, "Y": 0.0}
    model.solve()
    before = model.pi()
    assert hill_climb(model) and model.pi() <= before
    model.progression()  # B's green starts as A's platoon arrives, 30 s after A's
    assert (model.offsets["Y"] - model.offsets["X"]) % 60 == 30.0


def test_synchro_picks_its_cycle_by_percentile_delay():
    light = {"X": [(300.0, 3600.0), (200.0, 3600.0)]}
    heavy = {"X": [(1500.0, 3600.0), (1200.0, 3600.0)]}
    assert synchro_cycle(light, 40, 150) < synchro_cycle(heavy, 40, 150)  # busier: a longer cycle
    greens = percentile_greens([(900.0, 1800.0), (300.0, 1800.0)], 90)
    assert greens[0] > greens[1] and isclose(sum(greens) + 2 * SPAN_S, 90)
    network = generate_network(1, 4)
    for node in network.nodes.values():
        node.signal_method = "synchro"
    plan = Simulation(network, [pair("1W", "1E", 900)], CAR).signal_plans["2,1"]
    assert plan["method"] == "synchro" and plan["pi_after"] <= plan["pi_before"] and plan["cycle_s"] % 1 == 0


def test_loops_count_the_traffic_and_see_it_standing():
    network = generate_network(1, 1)
    network.node("1,1").signal_method = "fixed"
    simulation = Simulation(network, [pair("1W", "1E", 1200)], CAR, seed=2)
    lanes = simulation.graph.links["1W>"].lanes
    upstream = [Loop(lane, 15.0, 17.0, simulation._out[lane]) for lane in lanes]
    stop_lines = [Loop(lane, lane.length - 4.5, lane.length, simulation._out[lane]) for lane in lanes]
    counted, occupied_on_red, crossed, seen = 0, 0, 0, set()
    while simulation.time < 600:
        states = simulation.signal_states()
        simulation.step()
        readings = [loop.read() for loop in stop_lines]
        crossed += sum(passed for _, passed in readings)
        counted += sum(loop.read()[1] for loop in upstream)
        occupied_on_red += readings[0][0] and states[("1,1", "1W")] == "R"
        seen |= {vehicle.id for vehicle in simulation.vehicles if isinstance(vehicle.segment, Turn)
                 and vehicle.segment.lane_in in lanes}
    between = sum(vehicle.s >= 17.0 for lane in lanes for vehicle in lane.vehicles)
    assert crossed == len(seen) > 30  # every vehicle that went on over a stop line, once, queued there or not
    assert abs(counted - (crossed + between)) <= 2  # the upstream loops saw each of them, and those still between
    assert occupied_on_red > 100  # a queue stands on the stop-line loop while it's red


def test_every_method_runs_the_traffic_safely_and_the_adaptive_ones_adapt():
    demand = [pair("1W", "1E", 700), pair("1E", "1W", 500), pair("2W", "2E", 600), pair("1S", "1N", 300),
              pair("2S", "2N", 350), pair("3N", "3S", 250)]
    for method in ("webster", "transyt", "synchro", "scats", "scoot", "fixed"):
        network = generate_network(3, 4)
        for node in network.nodes.values():
            node.signal_method = method
        simulation = Simulation(network, demand, CAR, seed=5)
        start = dict(simulation.signal_plans["2,2"])
        red = overlaps = 0
        while simulation.time < 900:
            before, states = {v.id: v.segment for v in simulation.vehicles}, simulation.signal_states()
            simulation.step()
            red += sum(isinstance(v.segment, Turn) and before.get(v.id) not in (None, v.segment)
                       and states.get((v.segment.node, v.segment.lane_in.link.road)) == "R" for v in simulation.vehicles)
            for segment in {v.segment for v in simulation.vehicles}:
                overlaps += sum(front.s - front.length_m < rear.s - 1e-6
                                for rear, front in zip(segment.vehicles, segment.vehicles[1:]))
        now = simulation.frame().plans["2,2"]
        assert red == 0 and overlaps == 0 and simulation.arrived > 400, (method, red, overlaps, simulation.arrived)
        assert start["method"] == now["method"] == method
        if method in ("scats", "scoot"):  # changed as it went, from what its detectors saw
            assert now != start and "saturation" in now, (method, start, now)
        else:
            assert now == start, method


def test_scats_keeps_a_short_cycle_at_moderate_load():
    # A corridor at DS 0.6-0.8 (target 0.9): each phase's DS weighted by its lanes' vehicles and smoothed over cycles;
    # the worst single lane of one cycle had pushed the cycle from 40 s to its 120 s cap (audit 2026-10-13, M1)
    network = generate_network(1, 3)
    for node in network.nodes.values():
        node.signal_method = "scats"
    car = [VehicleMode("Car", PALETTE[1])]
    demand = [PairDemand(o, d, q, None, [], "Poisson", (100,)) for o, d, q in (("1W", "1E", 1500), ("1E", "1W", 1500))]
    demand += [PairDemand(f"{c}S", f"{c}N", 150, None, [], "Poisson", (100,)) for c in (1, 2, 3)]
    simulation = Simulation(network, demand, car, seed=4)
    while simulation.time < 1200:
        simulation.step()
    cycles = {plan.get("cycle_s") for plan in simulation.signal_plans.values()}
    assert cycles and max(cycles) < 70, cycles


def test_max_pressure_never_starves_a_light_arm():
    from traffic_simulator.signal_timing import MAX_RED_S

    # 4 phases, two busy arms facing each other and a light one: the busy two passed the green between them and
    # the light arm's vehicles waited 300 s (then teleported) before the maximum red
    network = generate_network(1, 1)
    node = network.node("1,1")
    node.signal_method, node.signal_phases, node.green_times_s = "max_pressure", 4, [20.0] * 4
    car = [VehicleMode("Car", PALETTE[1])]
    demand = [PairDemand(o, d, q, None, [], "Poisson", (100,)) for o, d, q in (("1W", "1E", 1000), ("1E", "1W", 1000),
                                                                                 ("1S", "1N", 40))]
    simulation, worst = Simulation(network, demand, car, seed=1), 0.0
    while simulation.time < 1200:
        simulation.step()
        worst = max([worst] + [v.waiting_s for v in simulation.vehicles if v.origin == "1S"])
    assert simulation.teleports == 0 and worst < MAX_RED_S + 25, worst  # its red, then the busy green's amber


def test_max_pressure_serves_a_lone_side_road_car_soon():
    # Pressure counts the vehicles near the line (PRESSURE_ZONE_M), not the whole 500 m approach: a main road's cars
    # still half a minute away had held a lone side-road car 52 s
    network = generate_network(1, 1)
    network.node("1,1").signal_method = "max_pressure"
    demand = [PairDemand(o, d, q, None, [], "Poisson", (100,)) for o, d, q in (("1W", "1E", 300), ("1S", "1N", 20))]
    simulation, worst = Simulation(network, demand, [VehicleMode("Car", PALETTE[1])], seed=1), 0.0
    while simulation.time < 1800:
        simulation.step()
        worst = max([worst] + [v.waiting_s for v in simulation.vehicles if v.origin == "1S"])
    assert worst < 25, worst


def test_tsp_follows_an_adaptive_plan():
    from pathlib import Path

    from traffic_simulator.clock import SimulationClock
    from traffic_simulator.extensions import ExtensionsPanel
    from traffic_simulator.network_scene import NetworkGraphicsScene

    network = generate_network(2, 2)
    for node in network.nodes.values():
        node.signal_method = "scoot"
    panel = ExtensionsPanel(NetworkGraphicsScene(network), SimulationClock(), Path(__file__).parents[1] / "extensions")
    tsp = next(extension for extension in panel.extensions if extension.path.name == "transit_signal_priority.py")
    tsp.check.setChecked(True)
    bus = VehicleMode("Bus", PALETTE[0], length_m=12.0, width_m=2.55, max_speed_kmh=100, accel=1.2, decel=4.0)
    simulation = Simulation(network, [PairDemand("1W", "1E", 400, None, [], "Poisson", (20, 80)),
                                      pair("1S", "1N", 300)], [bus, CAR[0]], seed=4)
    simulation.signal_override = overrides = panel.signal_override()
    while simulation.time < 600:
        simulation.step()
    here = tsp.module._latest.junctions["1,1"]
    method_greens = [phase["green_s"] for phase in simulation._junction_view("1,1", simulation.graph.signals["1,1"])["phases"]]
    controller_greens = [phase.planned_green for phase in here.controller.cfg.phases]
    assert not overrides.failed and controller_greens == [max(green, 7.0) for green in method_greens]
    tsp.check.setChecked(False)


def test_the_junction_editor_shows_each_methods_settings():
    from traffic_simulator.properties_panel import PropertiesPanel

    network = generate_network(2, 2)
    panel = PropertiesPanel()
    panel.set_network(network)
    node = network.node("1,1")
    panel.show_object("node", node)
    methods = [panel.timing_method.itemData(i) for i in range(panel.timing_method.count())]
    assert methods == ["webster", "transyt", "synchro", "scats", "scoot", "actuated", "max_pressure", "fixed"]
    shown = {}
    for method in methods:
        panel.timing_method.setCurrentIndex(methods.index(method))
        shown[method] = {label.text() for label, _ in panel.timing_settings.items() if label.isVisibleTo(panel)}
        assert panel.green_times[0][1].isEnabled() == (method == "fixed")  # the greens entered: fixed time only
    assert shown["webster"] == shown["fixed"] == shown["actuated"] == shown["max_pressure"] == set()
    assert shown["synchro"] == {"Cycle limits"}
    assert shown["transyt"] == {"Cycle limits", "Stop weight"} and shown["scats"] == {"Cycle limits", "Target saturation"}
    assert shown["scoot"] == {"Cycle limits", "Target saturation", "Stop weight"}
    panel.cycle_min.setValue(150)  # past the longest: the longest follows
    panel.target_saturation.setValue(85)
    assert (node.cycle_min_s, node.cycle_max_s, node.target_saturation) == (150, 150, 0.85)
    network.validate()
    panel.timing_method.setCurrentIndex(methods.index("scoot"))
    panel.show_plans({"1,1": {"method": "scoot", "cycle_s": 64, "greens_s": [30, 24], "offset_s": 12, "group": 4,
                              "saturation": [0.8, 0.5]}})
    assert "Now: cycle 64 s, greens 30 / 24 s, offset 12 s, 4 junctions; saturation 80% / 50%." in panel.signal_note.text()


if __name__ == "__main__":
    for name, check in list(globals().items()):
        if name.startswith("test_"):
            check()
    print("ok")

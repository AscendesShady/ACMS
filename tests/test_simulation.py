"""The traffic simulation: vehicles from their types, car following, signals, gap acceptance,
lane changing, road geometry and spawning from the OD matrix.
Run with `python tests/test_simulation.py` (or the test monitor)."""

import os
import random
import time
from statistics import mean, pvariance

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from traffic_simulator.mode_behavior import PALETTE, VehicleMode  # noqa: E402
from traffic_simulator.network_generator import generate_network  # noqa: E402
from traffic_simulator.signal_timing import (AMBER_USED_S, SATURATION_PER_LANE_VPH, START_UP_LOST_S,  # noqa: E402
                                             STANDARD_SPACE_S, STOP_LINE_LOOP_M)
from traffic_simulator.simulation import (DT, Lane, Loop, PairDemand, Runner, Simulation, Turn, arrivals,  # noqa: E402
                                          webster)
from traffic_simulator.vehicle import create_vehicle, free_speed, next_speed, safe_speed  # noqa: E402

APP = QApplication.instance() or QApplication([])  # the window test needs one; widgets crash without it
MODES = [VehicleMode("Bus", PALETTE[0], length_m=12.0, width_m=2.5, max_speed_kmh=85, accel=1.2, decel=4.0),
         VehicleMode("Car", PALETTE[1]),  # SUMO's defaults for each (mode_behavior.VehicleMode)
         VehicleMode("Bike", PALETTE[2], length_m=1.6, width_m=0.65, max_speed_kmh=50, desired_max_kmh=20,
                     accel=1.2, decel=3.0, min_gap_m=0.5)]


def pair(origin, destination, flow, pattern="Poisson", split=(10, 85, 5)):
    return PairDemand(origin, destination, flow, None, [], pattern, split)


def run(simulation, seconds, each_step=None):
    while simulation.time < seconds:
        before = {vehicle.id: vehicle.segment for vehicle in simulation.vehicles}
        states = simulation.signal_states()
        simulation.step()
        if each_step:
            each_step(simulation, before, states)


def test_vehicles_are_made_from_their_type():
    bus = create_vehicle(MODES[0], "Bus1")
    assert (bus.mode, bus.colour, bus.length_m, bus.width_m, bus.speed_factor) == ("Bus", PALETTE[0], 12.0, 2.5, 1.0)
    assert abs(bus.max_speed - 85 / 3.6) < 1e-9 and (bus.accel, bus.decel) == (1.2, 4.0)
    rng = random.Random(3)  # SUMO's speed factors, normc(1, 0.1, 0.2, 2): about 95 % within 0.8-1.2
    factors = [create_vehicle(MODES[1], f"Car{i}", rng=rng).speed_factor for i in range(2000)]
    assert abs(mean(factors) - 1) < 0.01 and abs(pvariance(factors) ** 0.5 - 0.1) < 0.01
    assert 0.93 < sum(0.8 <= factor <= 1.2 for factor in factors) / 2000 < 0.97 and 0.2 <= min(factors)
    bike = create_vehicle(MODES[2], "Bike1")
    assert free_speed(bike, 50 / 3.6) == 20 / 3.6 and free_speed(create_vehicle(MODES[1], "Car1"), 50 / 3.6) == 50 / 3.6


def test_krauss_never_hits_a_stopped_leader():
    car, rng = create_vehicle(MODES[1], "Car1"), random.Random(1)
    car.speed, gap = 50 / 3.6, 80.0  # the gap to a standing leader, less the minimum gap
    for _ in range(200):
        car.speed = next_speed(car, safe_speed(car, gap, 0.0), 50 / 3.6, DT, rng)
        gap -= car.speed * DT
        assert gap > -1e-6
    assert car.speed < 0.1 and gap < 3  # stopped, close behind


def test_lanes_follow_the_road_on_the_driving_side():
    for side, sign in (("left", -1), ("right", 1)):
        network = generate_network(1, 2, driving_side=side)  # 1,1 -> 2,1 runs east (+x), y down
        graph = Simulation(network, [], MODES).graph
        road = network.road("1,1-2,1")
        east, y = graph.links["1,1-2,1>"], network.node("1,1").y
        assert abs(east.lanes[0].points[0][1] - (y + sign * 1.5 * 3.5)) < 1e-6  # kerbside lane, keep side
        assert abs(east.lanes[1].points[0][1] - (y + sign * 0.5 * 3.5)) < 1e-6
        assert abs(east.lanes[0].length - (road.length_m - 2 * graph._trim("1,1", road))) < 1e-6  # stop lines


def test_arrival_patterns_follow_their_distributions():
    rng = random.Random(7)
    counts = {pattern: [arrivals(rng, pattern, 5.0) for _ in range(4000)]
              for pattern in ("Random", "Poisson", "Binomial", "Negative binomial")}
    for pattern, sample in counts.items():
        assert abs(mean(sample) - 5.0) < 0.2, pattern
    assert pvariance(counts["Random"]) == 0  # exactly the flow's count, at random times
    assert pvariance(counts["Binomial"]) < pvariance(counts["Poisson"]) < pvariance(counts["Negative binomial"])
    assert abs(pvariance(counts["Poisson"]) - 5.0) < 0.6


def test_spawning_follows_the_flow_and_bus_headways():
    network = generate_network(3, 4)
    modes = [VehicleMode(**{**vars(mode), "headway_min": 10 if mode.name == "Bus" else 0}) for mode in MODES]
    simulation = Simulation(network, [pair("1W", "1E", 600), pair("1S", "3N", 300, "Binomial")], modes, seed=2)
    run(simulation, 1800)
    assert simulation.spawned_by_mode["Bus"] == 6  # every 10 min on each of the two pairs, for 30 min
    assert abs(simulation.spawned - 450) < 60  # 900 veh/h for half an hour, buses included
    assert simulation.spawned_by_mode["Car"] > 8 * simulation.spawned_by_mode["Bike"]  # 85 % against 5 %


def test_daily_demand_follows_its_curve():
    network = generate_network(2, 2)
    shares = [0.0] * 24
    shares[0] = 1.0  # the whole day's trips in the first hour
    daily = PairDemand("1W", "1E", None, 480, shares, "Random", (0, 100, 0))
    simulation = Simulation(network, [daily], MODES, seed=3)
    run(simulation, 1800)
    assert abs(simulation.spawned - 240) <= 8  # 480 veh in that hour: 240 in half of it
    simulation.time, simulation._next_count = 3600.0, 3600.0
    spawned = simulation.spawned + sum(at < 3600 for at, *_ in simulation.departures)  # the first hour's still due
    run(simulation, 5400)
    assert simulation.spawned == spawned  # none in the second hour


def test_demand_changes_reach_a_run_under_way():
    modes = [VehicleMode(**{**vars(mode), "headway_min": 5 if mode.name == "Bus" else 0}) for mode in MODES]
    simulation = Simulation(generate_network(2, 2), [pair("1W", "1E", 600)], modes, seed=9)
    departed = {}  # vehicle id -> (pair, mode, departure time)

    def record(sim, before, states):
        for vehicle in [*sim.vehicles, *(v for queue in sim.pending.values() for v in queue)]:
            departed.setdefault(vehicle.id, (f"{vehicle.origin}->{vehicle.destination}", vehicle.mode, vehicle.depart_s))

    run(simulation, 600, record)
    simulation.set_demand([pair("1S", "2N", 1200), pair("1N", "9X", 100)])  # 1W->1E gone; one pair with no route
    run(simulation, 1800, record)
    after = [(route, mode, at) for route, mode, at in departed.values() if at >= 660]  # from the next minute
    assert not [trip for trip in after if trip[0] == "1W->1E"]  # its flow and its bus timetable both stopped
    new = [trip for trip in after if trip[0] == "1S->2N"]
    assert abs(len(new) - 1200 * (1800 - 660) / 3600) < 60  # the new pair at its flow, buses included
    assert 3 <= sum(mode == "Bus" for _, mode, _ in new) <= 4  # its own timetable: a bus every 5 min
    assert simulation.unreachable == ["1N->9X"] and simulation.signal_plans["1,1"]["method"] == "webster"


def test_traffic_obeys_signals_and_never_overlaps():
    network = generate_network(3, 4)
    network.node("2,2").control = "roundabout"
    network.node("3,2").control = "uncontrolled"
    demand = [pair(o, d, 450) for o, d in (("1W", "1E"), ("1S", "3N"), ("2E", "1N"), ("3W", "4S"),
                                            ("2W", "2E"), ("2S", "2N"), ("4N", "2W"))]
    simulation = Simulation(network, demand, MODES, seed=4)
    seen = {"red": 0, "green": 0, "roundabout": 0, "uncontrolled": 0, "overlaps": 0, "lane changes": 0}

    def check(sim, before, states):
        for vehicle in sim.vehicles:
            was, now = before.get(vehicle.id), vehicle.segment
            if isinstance(now, Turn) and was is not None and was is not now:
                state = states.get((now.node, now.lane_in.link.road))
                key = "roundabout" if now.ring else "uncontrolled" if state is None else "red" if state == "R" else "green"
                seen[key] += 1
            if isinstance(now, Lane) and isinstance(was, Lane) and was is not now and was.link is now.link:
                seen["lane changes"] += 1
        for segment in {vehicle.segment for vehicle in sim.vehicles}:
            for rear, front in zip(segment.vehicles, segment.vehicles[1:]):
                if front.s - front.length_m < rear.s - 1e-6:
                    seen["overlaps"] += 1

    run(simulation, 1200, check)
    assert seen["red"] == 0 and seen["overlaps"] == 0, seen
    assert seen["green"] > 500 and seen["roundabout"] > 50 and seen["uncontrolled"] > 50, seen
    assert seen["lane changes"] > 100 and simulation.arrived > 300, seen


def test_a_mirror_image_on_the_other_side_performs_alike():
    # The physics is side-neutral: a network, and its mirror image driven on the other side (the demand
    # and junctions mirrored east-west), clear the same traffic in the same time, give or take the
    # randomness. The same demand on the other side is a different mix of kerb and offside turns.
    pairs = (("1W", "1E"), ("1S", "3N"), ("2E", "1N"), ("3W", "4S"), ("2W", "2E"), ("2S", "2N"), ("4N", "2W"))

    def mirror(end):  # across the grid's north-south centre line: W and E swap, column c becomes 5 - c
        number, side = end[:-1], end[-1]
        return number + {"W": "E", "E": "W"}[side] if side in "WE" else f"{5 - int(number)}{side}"

    def run_side(side, mirrored, seed):
        network = generate_network(3, 4)
        network.set_driving_side(side)
        roundabout, free = ("3,2", "2,2") if mirrored else ("2,2", "3,2")
        network.node(roundabout).control, network.node(free).control = "roundabout", "uncontrolled"
        demand = [pair(mirror(o), mirror(d), 450) if mirrored else pair(o, d, 450) for o, d in pairs]
        simulation = Simulation(network, demand, MODES, seed=seed)
        run(simulation, 1200)
        return simulation.arrived

    as_given = sum(run_side("left", False, seed) for seed in (4, 5))
    mirrored = sum(run_side("right", True, seed) for seed in (4, 5))
    other_side = sum(run_side("right", False, seed) for seed in (4, 5))
    assert abs(mirrored - as_given) < 0.07 * as_given, (as_given, mirrored)
    assert other_side > 1.15 * as_given, (as_given, other_side)  # kerb turns for the busiest movements


def test_two_phase_signals_detour_offside_turns_by_a_u_turn():
    def nodes(origin, destination, network):
        return [(link.start, link.end) for link in Simulation(network, [], MODES).graph.route(origin, destination)]

    network = generate_network(3, 3)  # every junction a two-phase signal, driving on the left
    graph = Simulation(network, [], MODES).graph
    assert not [turn for link in graph.links.values() for turn in link.turns.values() if turn.kind == "offside"]
    # Right at 1,1: straight on to 1,2, U-turn there, back, and left at 1,1.
    assert nodes("1S", "1E", network) == [("1S", "1,1"), ("1,1", "1,2"), ("1,2", "1,1"), ("1,1", "2,1"),
                                          ("2,1", "3,1"), ("3,1", "1E")]
    # Straight ahead is a boundary end: left to 2,1, U-turn there, back and straight over.
    assert nodes("1N", "1W", network) == [("1N", "1,3"), ("1,3", "1,2"), ("1,2", "1,1"), ("1,1", "2,1"),
                                          ("2,1", "1,1"), ("1,1", "1W")]
    route = graph.route("1S", "1E")  # lanes: 0 kerbside, 1 offside
    first, out, back = (Simulation.reach(route, leg) for leg in range(3))
    assert first[1] > first[0] and out[1] > out[0] == route[1].lanes[0].length and back[0] > back[1]
    uturn = route[1].turns[(1, route[2].id)]
    assert uturn.kind == "uturn" and uturn.permitted and not route[1].turns[(0, route[2].id)].permitted
    assert nodes("1S", "1W", generate_network(3, 3, driving_side="right")) == [  # mirrored: left is offside
        ("1S", "1,1"), ("1,1", "1,2"), ("1,2", "1,1"), ("1,1", "1W")]
    network.node("1,1").signal_phases, network.node("1,1").green_times_s = 4, [20.0] * 4
    assert nodes("1S", "1E", network)[:2] == [("1S", "1,1"), ("1,1", "2,1")]  # its own phase: turns right


def test_lane_changes_prepare_for_the_u_turn_and_the_turn_back():
    network = generate_network(3, 3)
    cars = (0, 100, 0)  # right from 1S at 1,1, through heavy straight traffic both ways on column 1
    demand = [pair("1S", "1E", 300, split=cars), pair("1S", "1N", 900, split=cars), pair("1N", "1S", 900, split=cars)]
    simulation = Simulation(network, demand, MODES, seed=8)
    seen = {"uturn": 0, "uturn on amber": 0, "kerb": 0, "wrong lane": 0, "red": 0, "to offside": 0, "to kerb": 0,
            "away": 0}
    detour = {("1,1", "1,2"): 1, ("1,2", "1,1"): 0}  # out to the U-turn in the offside lane, back in the kerb lane

    def check(sim, before, states):
        for vehicle in sim.vehicles:
            was, now = before.get(vehicle.id), vehicle.segment
            if isinstance(now, Turn) and isinstance(was, Lane):
                state = states.get((now.node, was.link.road))
                seen["red"] += state == "R"
                seen["wrong lane"] += not now.permitted
                if now.kind in ("uturn", "kerb"):
                    seen[now.kind] += 1
                    seen["uturn on amber"] += now.kind == "uturn" and state == "Y"
            if isinstance(now, Lane) and isinstance(was, Lane) and was.link is now.link and was is not now \
                    and vehicle.destination == "1E" and (now.link.start, now.link.end) in detour:
                wanted = detour[(now.link.start, now.link.end)]
                seen["away" if now.index != wanted else "to offside" if wanted else "to kerb"] += 1

    run(simulation, 1200, check)
    # Every U-turn from the offside lane, every left from the kerb (drivers coming back let in by the
    # saturated platoons beside them), and no change on the way that is undone.
    assert seen["wrong lane"] == seen["red"] == seen["away"] == 0, seen
    assert seen["uturn"] > 40 and seen["kerb"] > 40 and seen["to offside"] > 10 and seen["to kerb"] > 40, seen  # 67 62 24 64
    assert seen["uturn on amber"] > 0, seen  # waited all green for a gap in the oncoming traffic: goes as it stops
    assert not [vehicle for vehicle in simulation.vehicles if vehicle.waiting_s > 60]


def test_the_timing_methods_are_calibrated_to_the_simulated_drivers():
    # The HCM's field method on SUMO's default car at a signal with a standing queue: the saturation
    # headway (from the fifth queued vehicle on), the start-up lost time (the first four's excess over
    # it), the amber used (vehicles crossing in it, times the headway) and the unoccupied time a
    # stop-line loop sees per vehicle at that flow, against what the timing methods design with.
    network = generate_network(1, 1)
    node = network.node("1,1")
    node.signal_method, node.green_times_s = "fixed", [50.0, 20.0]
    simulation = Simulation(network, [pair("1W", "1E", 3000, "Random", (0, 100, 0))], MODES, seed=1)
    lanes = simulation.graph.links["1W>"].lanes
    loops = {lane: Loop(lane, lane.length - STOP_LINE_LOOP_M, lane.length, simulation._out[lane]) for lane in lanes}
    queued, crossed, unoccupied, headways, lost, spaces, in_amber = {}, {}, {}, [], [], [], []
    stopped_on_green, was, green_start, amber_count = 0, None, 0.0, 0
    while simulation.time < 3600:  # an hour: the amber's few vehicles need that many cycles
        state = simulation.signal_states()[("1,1", "1W")]
        if state == "G" and was != "G":  # the green starts: the last green's queue, and this one's
            for lane in lanes:
                order = [t for vehicle, t in crossed.get(lane, []) if vehicle in queued.get(lane, [])]
                if len(order) >= 8:
                    gaps = [order[0] - green_start] + [b - a for a, b in zip(order, order[1:])]
                    headways += gaps[4:]
                    lost.append(sum(gap - mean(gaps[4:]) for gap in gaps[:4]))
                    marks = unoccupied.get(lane, [])
                    fifth = next(e for n, e in marks if n >= 5)
                    spaces.append((next(e for n, e in marks if n >= len(order)) - fifth) / (len(order) - 5))
                queued[lane], crossed[lane], unoccupied[lane] = [v.id for v in reversed(lane.vehicles) if v.speed < 0.1], [], []
            green_start = simulation.time
        if state == "Y" and was != "Y":
            amber_count = 0
        if state == "R" and was == "Y":
            in_amber.append(amber_count / len(lanes))
        was = state
        before = {vehicle.id: (vehicle.segment, vehicle.speed) for vehicle in simulation.vehicles}
        simulation.step()
        for vehicle in simulation.vehicles:
            lane, speed = before.get(vehicle.id, (None, 0.0))
            if lane in lanes and isinstance(vehicle.segment, Turn):
                crossed[lane].append((vehicle.id, simulation.time))
                amber_count += state == "Y"
            elif state == "G" and lane in lanes and vehicle.segment is lane and speed > 1 and vehicle.speed < 0.1 \
                    and lane.length - vehicle.s < 1 and vehicle.id in queued.get(lane, []):
                stopped_on_green += 1  # came up to the line in the discharging queue, and stopped there
        for lane, loop in loops.items():
            occupied, _ = loop.read()
            if state == "G":
                marks = unoccupied[lane]
                passed = sum(vehicle in queued[lane] for vehicle, _ in crossed[lane])
                marks.append((passed, (marks[-1][1] if marks else 0.0) + (0.0 if occupied else DT)))
    headway = mean(headways)
    assert len(headways) > 500 and abs(3600 / headway - SATURATION_PER_LANE_VPH) < 0.04 * SATURATION_PER_LANE_VPH, headway
    assert abs(mean(lost) - START_UP_LOST_S) < 0.3, mean(lost)
    assert abs(mean(in_amber) * headway - AMBER_USED_S) < 0.4, mean(in_amber) * headway
    assert abs(mean(spaces) - STANDARD_SPACE_S) < 0.2, mean(spaces)
    assert stopped_on_green == 0  # none held at the line by the vehicle ahead of it in the same queue


def test_no_lane_changing_where_the_road_forbids_it():
    network = generate_network(3, 4)
    for road in network.all_roads():
        road.lane_changing = False
    simulation = Simulation(network, [pair("1W", "1E", 600), pair("1S", "3N", 600)], MODES, seed=5)
    changes = []

    def check(sim, before, states):
        for vehicle in sim.vehicles:
            was = before.get(vehicle.id)
            if isinstance(was, Lane) and isinstance(vehicle.segment, Lane) and was.link is vehicle.segment.link \
                    and was is not vehicle.segment:
                changes.append(vehicle.id)

    run(simulation, 900, check)
    assert not changes and simulation.arrived > 80  # about as many as with lane changing (116)


def test_webster_times_signals_from_the_demand():
    # L = 2 x (1.8 start-up + 2 all-red + 0.8 amber unused) = 9.2 s: C0 = (1.5 x 9.2 + 5) / (1 - 0.5) = 37.6 s
    cycle, greens = webster([0.3, 0.2])
    assert round(cycle, 6) == 37.6 and [round(green, 1) for green in greens] == [16.6, 11.0]  # (37.6 - 9.2) y/Y - 0.4
    cycle, greens = webster([0.0, 0.0])  # no demand: minimum greens
    assert round(cycle, 6) == 24.0 and [round(green, 6) for green in greens] == [7.0, 7.0]
    assert round(webster([0.6, 0.5])[0], 6) == 120.0  # over capacity: the longest cycle
    network = generate_network(3, 4)
    network.node("2,2").signal_method = "fixed"  # keeps its entered greens
    network.node("2,2").green_times_s = [40.0, 20.0]
    demand = [pair("1W", "1E", 1200), pair("2W", "2E", 900), pair("1S", "1N", 200)]
    simulation = Simulation(network, demand, MODES)
    plan = simulation.signal_plans["1,1"]  # east-west 1200 veh/h on 2 lanes, north-south 200
    ratios = [round(1200 / (2 * SATURATION_PER_LANE_VPH), 3), round(200 / (2 * SATURATION_PER_LANE_VPH), 3)]
    assert plan["method"] == "webster" and plan["flow_ratios"] == ratios
    assert plan["greens_s"][0] > plan["greens_s"][1] + 5  # the busy phase gets the long green (the other its minimum)
    signal = simulation.graph.signals["1,1"]
    assert abs(signal.cycle - plan["cycle_s"]) < 0.1  # the plan is rounded to 0.1 s
    east_west = next(link for link in signal.phases[0][0])
    assert signal.state(east_west, plan["greens_s"][0] - 0.1) == "G"  # the run uses the plan
    assert signal.state(east_west, plan["greens_s"][0] + 0.1) == "Y"
    assert simulation.signal_plans["2,2"]["greens_s"] == [40.0, 20.0]
    assert simulation.frame().records()["signal_plans"]["1,1"] == plan  # an AI can read the plans too


def test_the_window_runs_and_stops_the_traffic():
    from traffic_simulator.main import MainWindow
    from traffic_simulator.od_matrix import ODCell

    window = MainWindow()
    window.od_matrix.set_ends(*window._od_ends())
    window.od_matrix.cells[("1W", "1E")] = ODCell(flow=900, split=(10, 85, 5))
    window.speed_combo.setCurrentIndex(window.speed_combo.findData(60))
    window.select("node", "1,1")
    window.run_action.trigger()
    assert "Last run: cycle" in window.properties.signal_note.text()  # Webster's plan, worked out at Run
    assert not window.properties.green_times[0][1].isEnabled()
    QTest.qWait(1500)  # 1.5 s real at 60x: the physics keeps pace on its own thread
    frame = window.runner.frame
    assert frame.time >= 60 and frame.spawned > 10 and window.scene.frame is not None
    assert abs(window.runner.target() - frame.time) < 6  # in step with the wall clock (0.1 s real), not waiting
    lamp, _idle = window.scene.signal_lamps[next(iter(frame.signals))]
    assert lamp.brush().color().name() in ("#b6e21c", "#ffb000", "#ff2020")
    seen = window._traffic({"road": "1W", "limit": 5})  # what an AI reads: the latest frame, live
    assert seen["time_s"] >= 60 and all(v["road"] == "1W" and v["type"] in ("Bus", "Car", "Bike")
                                        and "speed_kmh" in v and "x_m" in v for v in seen["vehicles"])
    assert seen["shown"] <= 5 and set(seen["signals"]) == {node for node, _ in frame.signals}
    runner = window.runner
    window.od_matrix.cells[("1W", "1E")] = ODCell(flow=300, split=(10, 85, 5))
    window.od_panel.changed.emit(None)  # a demand edit (the user's or the assistant's): the run carries on
    assert window.runner is runner and [pair.hourly for pair in runner.simulation._handed] == [300]
    window.undo()  # undoing a demand edit carries on too, back to the demand before it
    assert window.runner is runner and [pair.hourly for pair in runner.simulation._handed] == []
    window.properties.edited.emit(None)  # a network edit during the run stops it
    assert window.runner is None and window.clock.time_s == 0 and not window.clock.running
    assert "error" in window._traffic({})
    window.close()


def test_the_runner_keeps_real_time_and_publishes_frames():
    simulation = Simulation(generate_network(3, 4), [pair("1W", "1E", 900), pair("1S", "3N", 900)], MODES, seed=6)
    runner = Runner(simulation, speed=30)
    try:
        runner.play()
        time.sleep(1.0)
        # In step with the wall clock whenever it's read (however long the sleep really was):
        # never ahead, and behind by no more than a step or two and the latest publish.
        assert runner.frame.time >= 25 and -0.1 <= runner.target() - runner.frame.time <= 2.0
        runner.pause()
        held = runner.frame.time
        time.sleep(0.2)
        assert runner.frame.time == held and not runner.running
        runner.set_speed(60)
        runner.play()
        time.sleep(0.5)
        assert runner.frame.time >= held + 25  # 0.5 s at 60x
        record = runner.frame.records(limit=3)
        assert record["on_road"] == len(runner.frame.vehicles) and record["shown"] <= 3
    finally:
        runner.stop()
    assert not runner._thread.is_alive()


def test_junction_saturation_by_control():
    """Degree of saturation at a roundabout (measured entry capacity against the circulating flow), a
    priority junction (against the flows it gives way to) and a signal (lane by lane, turns slower)."""
    from traffic_simulator.signal_timing import KERB_TURN_PER_LANE_VPH
    from traffic_simulator.simulation import (PRIORITY_CAPACITY_VPH, ROUNDABOUT_ENTRY_VPH, Graph,
                                              measured_capacity)

    assert measured_capacity(ROUNDABOUT_ENTRY_VPH, 0) == 1450 and measured_capacity(ROUNDABOUT_ENTRY_VPH, 300) == 1015
    assert measured_capacity(PRIORITY_CAPACITY_VPH, 5000) == 120  # held at the last point beyond the table
    network = generate_network(3, 3)
    network.nodes["2,2"].control = "roundabout"
    network.nodes["2,1"].control = "uncontrolled"
    graph = Graph(network)
    inbound, outbound = graph.arriving["2,2"], graph.leaving["2,2"]
    entry = inbound[0]
    alone = {(entry.id, next(link.id for link in outbound if link.road != entry.road)): 600.0}
    x = graph.saturation("2,2", {}, alone)
    assert abs(x - 600 / 1450) < 1e-9  # nothing circulating; the entry's lanes share one path round
    others = {(link.id, out.id): 300.0 for link in inbound if link is not entry for out in outbound if out.road != link.road}
    assert graph.saturation("2,2", {}, {**alone, **others}) > x  # circulating traffic in front: less capacity

    major = graph._major_arms("2,1")
    minor = next(link for link in graph.arriving["2,1"] if link.id not in major)
    across = next(turn.lane_out.link.id for turn in minor.turns.values() if turn.kind == "straight")
    x = graph.saturation("2,1", {}, {(minor.id, across): 200.0})
    assert abs(x - 200 / 1990 / len(minor.lanes)) < 1e-9  # no priority traffic to wait for
    through = {(link.id, out.id): 600.0 for link in graph.arriving["2,1"] if link.id in major
               for out in graph.leaving["2,1"] if out.id in major and out.road != link.road}
    assert graph.saturation("2,1", {}, {(minor.id, across): 200.0, **through}) > x

    arm = graph.arriving["1,1"][0]  # a signal's two-lane arm
    by_kind = {turn.kind: after for (_, after), turn in arm.turns.items()}
    straight = graph.arm_ratio(arm.id, {(arm.id, by_kind["straight"]): 1000.0})
    assert abs(straight - 1000 / (2 * SATURATION_PER_LANE_VPH)) < 1e-9  # shared by both lanes
    kerb = graph.arm_ratio(arm.id, {(arm.id, by_kind["kerb"]): 500.0})
    assert abs(kerb - 500 / KERB_TURN_PER_LANE_VPH) < 1e-9  # one lane, slower
    both = graph.arm_ratio(arm.id, {(arm.id, by_kind["straight"]): 1000.0, (arm.id, by_kind["kerb"]): 500.0})
    assert abs(both - (1000 / SATURATION_PER_LANE_VPH + kerb) / 2) < 1e-9  # straight-on fills round the turners


def _held_queue(control: str, conflicting: float, lanes: int = 1, minutes: float = 20.0) -> float:
    """veh/h entering a junction from one approach held saturated against `conflicting` veh/h of priority
    (or circulating) traffic, on a one-junction network, cars only."""
    from math import pi

    from traffic_simulator.simulation import Graph

    network = generate_network(1, 1)
    network.node("1,1").control = control
    for road in network.all_roads():
        road.lanes_per_direction = lanes
    graph = Graph(network)
    if control == "uncontrolled":
        major = graph._major_arms("1,1")
        entry = next(link for link in graph.arriving["1,1"] if link.id not in major)
        others = [(link.start, next(out.end for out in graph.leaving["1,1"] if out.id in major and out.road != link.road))
                  for link in graph.arriving["1,1"] if link.id in major]
        exit_ = next(out.end for out in graph.leaving["1,1"] if out.id not in major and out.road != entry.road)
    else:
        entry, exit_ = next(link for link in graph.arriving["1,1"] if link.start == "1N"), "1S"
        ring = next(turn for turn in entry.turns.values() if turn.ring)
        others = sorted({(link.start, turn.lane_out.link.end) for link in graph.arriving["1,1"] if link is not entry
                         for turn in link.turns.values() if turn.lane_out.link.end != link.start
                         and ((ring.ring[2] - turn.ring[2]) * ring.ring[4]) % (2 * pi) < (turn.ring[3] - turn.ring[2]) * ring.ring[4]})
    demand = [pair(entry.start, exit_, 4000, "Random", (0, 100, 0))]
    demand += [pair(o, d, conflicting / len(others), "Random", (0, 100, 0)) for o, d in others]
    simulation = Simulation(network, demand, MODES, seed=1)
    lanes_in = set(map(id, simulation.graph.links[entry.id].lanes))
    entered = 0
    while simulation.time < (5 + minutes) * 60:
        before = {vehicle.id: vehicle.segment for vehicle in simulation.vehicles}
        simulation.step()
        if simulation.time > 300:
            entered += sum(1 for vehicle in simulation.vehicles if id(before.get(vehicle.id)) in lanes_in
                           and isinstance(vehicle.segment, Turn))
    return entered * 60 / minutes


def test_give_way_capacities_are_calibrated_to_the_simulated_drivers():
    """The measured capacities Graph.saturation sizes demand by, re-measured: a minor arm and a roundabout
    entry held saturated against priority traffic (within 10 %)."""
    from traffic_simulator.simulation import PRIORITY_CAPACITY_VPH, ROUNDABOUT_ENTRY_VPH, measured_capacity

    for control, table, conflicting in (("uncontrolled", PRIORITY_CAPACITY_VPH, 400), ("roundabout", ROUNDABOUT_ENTRY_VPH, 400)):
        measured, expected = _held_queue(control, conflicting), measured_capacity(table, conflicting)
        assert abs(measured - expected) < 0.1 * expected, (control, measured, expected)



def test_vehicles_know_what_they_are_heading_for():
    from traffic_simulator.interchanges import build_slip_road

    network = generate_network(2, 2)  # 1,1 signalised, 2,2 a roundabout, a slip road past 1,1
    network.nodes["2,2"].control = "roundabout"
    slip = build_slip_road(network, "1W", "1,2-1,1")
    simulation = Simulation(network, [pair("1W", "1N", 600), pair("1S", "2N", 600)], MODES[1:2], seed=1)
    graph = simulation.graph
    assert [(kind, at) for _, kind, at, _ in graph.landmarks(graph.route("1W", "1N"))] == \
        [("slip road", slip), ("signal", "1,2")]
    kinds = [kind for _, kind, _, _ in graph.landmarks(graph.route("1S", "2N"))]
    assert kinds[0] == "signal" and kinds[-1] == "roundabout"
    traces: dict[str, list] = {}
    while simulation.time < 150:
        simulation.step()
        for vehicle in simulation.frame().vehicles:
            traces.setdefault(vehicle.id, []).append((vehicle.heading_to, vehicle.heading_to_id, vehicle.heading_to_m,
                                                      vehicle.place))
    for trace in traces.values():
        for (kind, at, metres, _), (next_kind, next_at, next_metres, place) in zip(trace, trace[1:]):
            # Closing in on it (a turn's curve can read a little long), unless it has just driven
            # through it and comes back to it later (a U-turn detour).
            if (kind, at) == (next_kind, next_at) and kind and not place.startswith(f"junction {at}:"):
                assert next_metres <= metres + 1.0
    slip_users = [trace for trace in traces.values() if trace[0][:2] == ("slip road", slip)]
    assert slip_users and all(("signal", "1,2") in [step[:2] for step in trace] for trace in slip_users if len(trace) > 300)
    assert all(trace[0][2] > 0 for trace in slip_users)
    record = simulation.frame().records(limit=None)["vehicles"]
    assert any(item.get("heading_to", {}).get("kind") == "roundabout" for item in record)


def test_emergency_vehicles_are_dispatched_once_or_every_so_often():
    simulation = Simulation(generate_network(2, 3), [], [])
    for origin, destination in (("1W", "1W"), ("1W", "nowhere")):
        try:
            simulation.dispatch(origin, destination)
        except ValueError as exc:
            assert "No route" in str(exc)
        else:
            raise AssertionError((origin, destination))
    simulation.dispatch("1W", "2E", every_s=60)  # a boundary end to another, every minute
    simulation.dispatch("2,2", "1S")  # from a junction, once
    simulation.dispatch("2W", "1E", after_s=150)  # once, two and a half minutes from now
    while simulation.time < 200:
        simulation.step()
    trips = simulation.emergency_trips
    assert [(t["from"], t["to"], t["dispatched_s"]) for t in trips.values()] == [
        ("1W", "2E", 0.0), ("2,2", "1S", 0.0), ("1W", "2E", 60.0), ("1W", "2E", 120.0), ("2W", "1E", 150.0),
        ("1W", "2E", 180.0)]
    assert trips["Emergency2"]["arrived_s"] and simulation.spawned_by_mode["Emergency"] == 6


def test_a_lane_obeys_its_own_signal_and_an_emergency_vehicle_keeps_to_the_kerbside_lane():
    for side in ("left", "right"):  # the kerbside lane, lane 0, whichever side traffic drives on
        network = generate_network(1, 3, driving_side=side)  # one road west to east through three signals
        demand = [PairDemand("1W", "1E", 900, None, [], "Poisson", (0, 100, 0))]
        simulation = Simulation(network, demand, MODES, seed=4)
        # an override that sets the kerbside lane of each arm apart: green for it alone, red for the rest
        simulation.signal_override = lambda node, road, state, context: ("R", {0: "G"})
        simulation.dispatch("1W", "1E", every_s=120)
        crossed, kept = set(), set()
        for _ in range(1200):
            simulation.step()
            for vehicle in simulation.vehicles:
                segment = vehicle.segment
                if isinstance(segment, Turn) and segment.node in simulation.graph.signals:
                    crossed.add(segment.lane_in.index)
                elif vehicle.emergency and isinstance(segment, Lane) and segment.link.end in simulation.graph.signals                         and segment.length - vehicle.s < 100:
                    kept.add(segment.index)
        assert crossed == {0}, (side, crossed)  # only the lane given green went through
        assert kept == {0}, (side, kept)  # the emergency vehicles kept to it before each signal
        assert all(index == 0 and state == "G" for (_, _, index), state in simulation.lane_states().items())
        assert any(trip["arrived_s"] for trip in simulation.emergency_trips.values())


def test_an_emergency_vehicle_obeys_red_unless_its_arm_has_a_lane_green():
    simulation = Simulation(generate_network(1, 2), [], MODES)
    link = next(link for link in simulation.graph.arriving["2,1"] if len(link.lanes) == 2)
    kerbside, offside = link.lanes
    car = create_vehicle(MODES[1], "car", [link])
    ambulance = create_vehicle(MODES[1], "ambulance", [link])
    ambulance.emergency = True
    simulation._states = {**simulation._states, link.id: "R"}
    simulation._lane_states = {}
    assert simulation._signal_for(ambulance, offside) == simulation._signal_for(car, offside) == "R"  # red: it waits
    simulation._lane_states = {(link.id, 0): "G"}  # DEVL's flashing green on the kerbside lane
    assert simulation._signal_for(car, kerbside) == "G" and simulation._signal_for(car, offside) == "R"
    assert simulation._signal_for(ambulance, offside) == "G"  # whichever lane it is in
    simulation._lane_states = {(link.id, 0): "Y"}  # the lane's amber: red again for it beside it
    assert simulation._signal_for(ambulance, offside) == "R"

def test_closed_lanes_are_left_in_good_time_and_a_blocked_road_stops_traffic():
    network = generate_network(1, 1)
    cars = [VehicleMode("Car", PALETTE[1])]

    def run(**close):
        simulation = Simulation(network, [pair("1W", "1E", 1200, "Random", (100,))], cars, seed=3)
        made = simulation.close("1W", direction="forward", **close)
        inside, per_minute, zone_speeds = [], {}, []
        while simulation.time < 1500:
            before = simulation.arrived
            simulation.step()
            per_minute[int(simulation.time // 60)] = per_minute.get(int(simulation.time // 60), 0) + simulation.arrived - before
            for closure in made:
                if not closure.start_s <= simulation.time < closure.end_s:
                    continue
                for vehicle in simulation.vehicles:
                    lane = vehicle.segment
                    if isinstance(lane, Lane) and lane.link.id == closure.link and closure.from_m < vehicle.s < closure.to_m:
                        zone_speeds.append(vehicle.speed * 3.6)
                        if lane.index in closure.lanes and vehicle.speed > 0.1:
                            inside.append(simulation.time)
        return made, inside, per_minute, zone_speeds

    made, inside, per_minute, _ = run(lanes=1, length_m=80, start_s=300, end_s=900)
    assert made[0].lanes == (0,) and made[0].to_m - made[0].from_m == 80  # the kerbside lane
    assert all(t < 320 for t in inside)  # only those already in the section when it closed drive on through it
    during = sum(per_minute[m] for m in range(7, 14)) / 7
    assert during > 10  # one lane of two still carries the traffic
    _, _, blocked, _ = run(lanes=2, length_m=40, start_s=300, end_s=900)
    assert sum(blocked[m] for m in range(7, 15)) == 0 and sum(blocked[m] for m in range(16, 20)) > 0  # stopped, then on
    _, _, _, speeds = run(lanes=0, length_m=150, speed_kmh=20, start_s=0, end_s=1500)
    assert sum(speeds) / len(speeds) < 22 and max(speeds) < 20 * 1.4  # a work zone's limit (each driver's speed factor)
    for kwargs, reason in (({"lanes": 3}, "lanes is 1 to 2"), ({"lanes": 0}, "lanes is 1 to 2"),
                           ({"start_s": 50, "end_s": 10}, "end after it starts"), ({"speed_kmh": 200}, "5 to 130"),
                           ({"from_m": 0, "length_m": 20}, "too near its ends")):
        try:
            Simulation(network, [], cars).close("1W", **kwargs)
        except ValueError as exc:
            assert reason in str(exc), (kwargs, exc)
        else:
            raise AssertionError(kwargs)
    simulation = Simulation(network, [], cars)
    try:
        simulation.close("nowhere")
    except ValueError as exc:
        assert "No road" in str(exc) and not simulation.closures  # refused: nothing closed
    else:
        raise AssertionError("an unknown road is refused")


def test_actuated_and_max_pressure_signals_are_safe_and_serve_demand():
    from traffic_simulator.signal_timing import ACTUATED_MAX_GREEN_S, MIN_GREEN_S  # noqa: PLC0415

    cars = [VehicleMode("Car", PALETTE[1])]

    def watch(method, demand, seconds=1200):
        """Each arm's greens (lengths) and whether every green ended in amber."""
        network = generate_network(1, 1)
        network.node("1,1").signal_method = method
        simulation = Simulation(network, demand, cars, seed=2)
        greens, began, before, straight_to_red = {}, {}, {}, 0
        while simulation.time < seconds:
            simulation.step()
            for arm, state in simulation.signal_states().items():
                if state == "G" and before.get(arm) != "G":
                    began[arm] = simulation.time
                if before.get(arm) == "G" and state != "G":
                    greens.setdefault(arm[1], []).append(simulation.time - began[arm])
                    straight_to_red += state != "Y"
                before[arm] = state
        return simulation, greens, straight_to_red

    for method in ("actuated", "max_pressure"):
        busy = [pair("1W", "1E", 900, "Random", (100,)), pair("1S", "1N", 250, "Random", (100,))]
        simulation, greens, straight_to_red = watch(method, busy)
        assert simulation.signal_plans["1,1"]["method"] == method and straight_to_red == 0
        assert min(g for found in greens.values() for g in found) >= MIN_GREEN_S - 1e-6
        assert max(g for found in greens.values() for g in found) <= ACTUATED_MAX_GREEN_S + 1.0
        assert sum(greens["1W"]) > sum(greens["1S"])  # the busy approach gets more green
        assert simulation.arrived > 300
    # actuated with traffic one way only: it rests in that green (no call elsewhere), never cycling
    _, greens, _ = watch("actuated", [pair("1W", "1E", 600, "Random", (100,))], 600)
    assert not greens.get("1S") and len(greens.get("1W", [])) <= 1


def test_a_ramp_meter_holds_its_ramp_back_by_alinea_or_a_fixed_rate():
    from traffic_simulator.simulation import METER_K_R, METER_RATES_VPH  # noqa: PLC0415

    network = generate_network(1, 1)
    network.node("1,1").control = "uncontrolled"  # a merge: 1S comes in beside the main road west to east
    cars = [VehicleMode("Car", PALETTE[1])]

    def run(**meter):
        simulation = Simulation(network, [pair("1W", "1E", 2400, "Random", (100,)), pair("1S", "1E", 700, "Random", (100,))],
                                cars, seed=5)
        made = simulation.meter("1S", **meter) if meter else None
        crossed = set()
        while simulation.time < 1800:
            simulation.step()
            crossed |= {v.id for v in simulation.vehicles if isinstance(v.segment, Turn) and v.origin == "1S"}
        return made, len(crossed)

    _, free = run()
    fixed, held = run(rate_vph=300)
    assert held < free and held <= 300 / 2 + 5  # at most one a green: 300 an hour, half an hour
    alinea, adaptive = run(target_occupancy_percent=20, detector_road="1E")
    rates = [rate for _, rate, _ in alinea.history]
    assert len(rates) >= 25 and min(rates) < METER_RATES_VPH[1] and adaptive < free  # it metered when the road filled
    for (_, before, _), (_, after, occupancy) in zip(alinea.history, alinea.history[1:]):  # each: its period's measure
        expected = min(max(before + METER_K_R * (20 - occupancy), METER_RATES_VPH[0]), METER_RATES_VPH[1])
        assert abs(after - expected) <= 5  # ALINEA: r(k) = r(k-1) + K_R (target - occupancy), from rounded history
    for kwargs, reason in (({}, "one of them"), ({"rate_vph": 5}, "rate_vph must be"),
                           ({"target_occupancy_percent": 90}, "5 to 50"),
                           ({"rate_vph": 300, "detector_road": "nowhere"}, "detector_road is one of")):
        try:
            Simulation(network, [], cars).meter("1S", **kwargs)
        except ValueError as exc:
            assert reason in str(exc), (kwargs, exc)
        else:
            raise AssertionError(kwargs)
    signalised = generate_network(1, 1)  # a signalised junction isn't a merge
    try:
        Simulation(signalised, [], cars).meter("1S", rate_vph=300)
    except ValueError as exc:
        assert "isn't an on-ramp" in str(exc)
    else:
        raise AssertionError("a ramp into a signal is refused")


def test_a_fork_runs_on_alone_and_a_rollout_compares_actions():
    network = generate_network(2, 2)
    cars = [VehicleMode("Car", PALETTE[1])]
    demand = [pair(o, d, 200, "Random", (100,)) for o in ("1W", "1S") for d in ("2E", "2N")]
    simulation = Simulation(network, demand, cars, seed=4)
    while simulation.time < 300:
        simulation.step()
    fork = simulation.fork()
    assert fork is not simulation and fork.signal_override is None and fork.observed is None
    for _ in range(120):
        simulation.step()
        fork.step()
    assert [(v.id, round(v.s, 6)) for v in simulation.vehicles] == [(v.id, round(v.s, 6)) for v in fork.vehicles]  # the same
    before = [(v.id, v.s) for v in simulation.vehicles]
    for _ in range(40):
        fork.step()  # running the fork on leaves the run itself where it was
    assert [(v.id, v.s) for v in simulation.vehicles] == before and fork.time > simulation.time

    network.node("1,1").signal_method = "actuated"
    actuated = Simulation(network, demand, cars, seed=4)
    while actuated.time < 200:
        actuated.step()
    copy = actuated.fork()
    assert type(copy.graph.signals["1,1"].controller).__name__ == "_Held" and not copy._loops
    assert type(actuated.graph.signals["1,1"].controller).__name__ == "ActuatedJunction" and actuated._loops
    actuated.step()  # the run's own controller still works after a fork

    watched = next(v.id for v in simulation.vehicles)

    def all_red(node, road, state, view):
        return "R"

    found = simulation.rollout({"plan": None, "every signal red": all_red}, 40, watch=[watched])
    assert set(found) == {"plan", "every signal red"}
    for outcome in found.values():
        assert outcome["person_delay_s"] == outcome["vehicle_delay_s"]  # cars carry one person each here
        assert watched in outcome["watched"]
    assert found["every signal red"]["person_delay_s"] > found["plan"]["person_delay_s"]  # holding everyone costs more
    assert [(v.id, v.s) for v in simulation.vehicles] == before  # rollouts leave the run itself alone


def test_weather_lowers_capacity_as_the_hcm_reports():
    # HCM 2000 Ch 22: heavy rain 14-15 % less capacity, heavy snow 30 %: the queue discharge rate at a signal
    from traffic_simulator.experiments import discharge  # noqa: PLC0415

    def saturation(weather):
        network = generate_network(1, 1)
        network.node("1,1").signal_method, network.node("1,1").green_times_s = "fixed", [50.0, 20.0]
        simulation = Simulation(network, [pair("1W", "1E", 3000, "Random", (100,))], [VehicleMode("Car", PALETTE[1])], seed=1)
        simulation.set_weather(weather)
        simulation.observe()
        while simulation.time < 2400:
            simulation.step()
        rows = [r["saturation_flow_vphpl"] for r in discharge(simulation.observations()) if r["link"] == "1W>"]
        return mean(rows)

    clear = saturation("clear")
    for weather, low, high in (("heavy rain", 10, 19), ("heavy snow", 25, 37)):
        loss = 100 * (1 - saturation(weather) / clear)
        assert low < loss < high, (weather, loss)
    try:
        Simulation(generate_network(1, 1), [], MODES).set_weather("hail")
    except ValueError as exc:
        assert "weather is one of" in str(exc)
    else:
        raise AssertionError("an unknown weather is refused")


if __name__ == "__main__":
    for name, check in list(globals().items()):
        if name.startswith("test_"):
            check()
    print("ok")

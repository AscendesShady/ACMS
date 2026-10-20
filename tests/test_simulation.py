"""The traffic simulation: vehicles from their types, car following, signals, gap acceptance,
lane changing, road geometry and spawning from the OD matrix.
Run with `python tests/test_simulation.py` (or the test monitor)."""

import hashlib
import subprocess
import sys
import os
import random
import tempfile
import time
from pathlib import Path
from itertools import accumulate
from math import cos, pi, sin
from statistics import mean, pvariance

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from traffic_simulator.mode_behavior import PALETTE, VehicleMode  # noqa: E402
from traffic_simulator.network_generator import generate_network  # noqa: E402
from traffic_simulator.signal_timing import (AMBER_USED_S, SATURATION_PER_LANE_VPH, START_UP_LOST_S, effective,  # noqa: E402
                                             STANDARD_SPACE_S, STOP_LINE_LOOP_M)
from traffic_simulator.simulation import (DT, FILTER_SPEED, TIME_TO_TELEPORT_S, TURN_SATURATION_VPH, Lane, Loop, PairDemand, Runner,  # noqa: E402
                                          Simulation, Turn, _clear, arrivals, webster)
from traffic_simulator.vehicle import (MAX_SPEED_LAT, VEHICLE_CLASSES, create_vehicle, free_speed, next_speed,  # noqa: E402
                                       safe_speed, secure_gap)
from traffic_simulator.geometry import polyline_length  # noqa: E402
from traffic_simulator.interchanges import curve_speed_kmh  # noqa: E402
from traffic_simulator.models import LANE_WIDTH_M, Network, Node, Road  # noqa: E402

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


def test_a_reaction_time_under_a_step_collides_and_is_counted():
    # Krauss keeps its gap only if the driver reacts no faster than a step (Mode Behavior refuses less): with
    # tau 0.1 s and no standstill gap, a car 1 m behind one standing at the red runs into it, and the run counts
    # the collision rather than hiding it. At tau = DT it doesn't.
    for tau, collides in ((0.1, True), (DT, False)):
        mode = VehicleMode("Car", PALETTE[1], tau_s=tau, min_gap_m=0.0)
        simulation = Simulation(generate_network(1, 1, lanes_per_direction=1), [], [mode])
        route = simulation.graph.route("1S", "1N")
        lane = route[0].lanes[0]
        for name, s in (("leader", lane.length - 0.3), ("follower", lane.length - 6.3)):
            vehicle = create_vehicle(mode, name, route, origin="1S", destination="1N")
            vehicle.segment, vehicle.s = lane, s
            lane.vehicles.insert(0, vehicle)  # rearmost first
            simulation.vehicles.append(vehicle)
        run(simulation, 7)
        assert (simulation.collisions > 0) == collides, (tau, simulation.collisions)


def test_emergency_braking_is_bounded_recorded_and_what_it_cant_avoid_counted():
    # SUMO's emergencyDecel: however hard Krauss asks, no step slows a car by more than 9 m/s² × DT, and each step
    # harder than its comfortable 4.5 is recorded, with where. A car at 50 km/h 5 m behind one standing still can't
    # stop in time: it runs into it, counted as a same-lane collision, not stopped dead.
    car = create_vehicle(MODES[1], "Car1")
    car.speed = 50 / 3.6
    assert abs(next_speed(car, 0.0, 50 / 3.6, DT, random.Random(1)) - (car.speed - 9.0 * DT)) < 1e-9
    simulation = Simulation(generate_network(1, 1, lanes_per_direction=1), [], MODES[1:2])
    simulation.observe()
    route = simulation.graph.route("1S", "1N")
    lane = route[0].lanes[0]
    for name, s, speed in (("standing", 60.0, 0.0), ("fast", 50.0, 50 / 3.6)):
        vehicle = create_vehicle(MODES[1], name, route, origin="1S", destination="1N")
        vehicle.segment, vehicle.s, vehicle.speed, vehicle.max_speed = lane, s, speed, speed
        lane.vehicles.insert(0, vehicle)  # rearmost first
        simulation.vehicles.append(vehicle)
    fast, speeds = simulation.vehicles[1], [50 / 3.6]
    while fast.speed > 0 and simulation.time < 10:
        simulation.step()
        speeds.append(fast.speed)
    assert max(a - b for a, b in zip(speeds, speeds[1:])) <= 9.0 * DT + 1e-9, speeds
    observed = simulation.observations()
    assert simulation.collision_kinds["same lane"] > 0 and {e["kind"] for e in observed["collisions"]} == {"same lane"}
    braking = [e for e in observed["emergency_braking"] if e["vehicle"] == "fast"]
    assert len(braking) >= 2 and all(4.5 < e["decel"] <= 9.0 + 1e-6 and e["where"] == lane.link.road for e in braking)
    assert observed["emergency_braking_count"] == simulation.emergency_braking >= len(braking)


def test_the_ballistic_update_moves_at_the_mean_speed_and_stops_part_way():
    # SUMO's step-method.ballistic, opt-in: a step moves (v + v')/2·Δt, and a car braking to a stop within the step
    # goes only as far as it takes to stop (v²/2a), not on at its mean speed; Euler, the default, moves v'·Δt. Krauss's
    # safe speed in its ballistic form keeps a car from hitting a standing leader. Other steps or methods are refused.
    network = generate_network(1, 1, lanes_per_direction=1)
    for method, speeds, moved in (("euler", (10.0, 12.0), 6.0), ("ballistic", (10.0, 12.0), 5.5),
                                  ("ballistic", (10.0, -10.0), 1.25)):
        simulation = Simulation(network, [], MODES[1:2], step_method=method)
        lane = simulation.graph.route("1S", "1N")[0].lanes[0]
        car = create_vehicle(MODES[1], "car", [lane.link])
        car.segment, car.s, (car.speed, car.next_speed) = lane, 10.0, speeds
        lane.vehicles.append(car)
        simulation._move(car)
        assert abs(car.s - 10.0 - moved) < 1e-9 and car.speed == max(speeds[1], 0.0), (method, speeds, car.s)
    car, rng = create_vehicle(MODES[1], "Car1"), random.Random(1)
    car.speed, gap = 50 / 3.6, 80.0
    for _ in range(200):
        new = next_speed(car, safe_speed(car, gap, 0.0, dt=DT), 50 / 3.6, DT, rng, ballistic=True)
        gap -= (car.speed + new) / 2 * DT if new >= 0 else car.speed ** 2 * DT / (2 * (car.speed - new))
        car.speed = max(new, 0.0)
        assert gap > -1e-6
    assert car.speed < 0.1 and gap < 3  # stopped, close behind
    for bad in ({"step_s": 0.3}, {"step_method": "runge-kutta"}):
        try:
            Simulation(network, [], MODES[1:2], **bad)
        except ValueError as error:
            assert "step" in str(error)
        else:
            raise AssertionError(f"{bad} must be refused")


def test_a_shorter_step_clears_about_the_same_traffic():
    # A step-sensitivity check: the same seed at DT and DT/2 (step_s, for headless runs) clears about the same traffic
    # through a signal; a 0.1 s step keeps its clock exact.
    arrived = []
    for step in (DT, DT / 2):
        simulation = Simulation(generate_network(1, 1), [pair("1W", "1E", 900), pair("1S", "1N", 600)], MODES, seed=3,
                                step_s=step)
        run(simulation, 900)
        arrived.append(simulation.arrived)
    assert arrived[0] > 250 and abs(arrived[0] - arrived[1]) < 0.05 * arrived[0], arrived
    simulation = Simulation(generate_network(1, 1), [], MODES, step_s=0.1)
    run(simulation, 10)
    assert simulation.time == 10.0


def test_vehicles_over_a_crossing_point_at_once_are_a_crossing_collision():
    # Detection only, SUMO's action "warn": two vehicles on crossing turns both over their conflict point (each front
    # past it by less than its length) are a crossing collision at that junction; short of it or clear of it, not.
    network = generate_network(1, 1)
    network.node("1,1").control = "uncontrolled"
    simulation = Simulation(network, [], MODES[1:2])
    simulation.observe()
    turn, (other, here, there, _) = next((t, c) for link in simulation.graph.arriving["1,1"] for t in link.turns.values()
                                         for c in t.conflicts if c[0].lane_out is not t.lane_out and c[2] > 1.0)
    a, b = create_vehicle(MODES[1], "a"), create_vehicle(MODES[1], "b")
    for vehicle, segment, s in ((a, turn, here + 1.0), (b, other, there + 1.0)):
        vehicle.segment, vehicle.s = segment, s
        segment.vehicles.append(vehicle)
        simulation.vehicles.append(vehicle)
    simulation._crossing_collisions()
    assert simulation.collisions == 1 and simulation.collision_kinds == {"crossing": 1}
    assert simulation.observations()["collisions"] == [{"kind": "crossing", "where": "1,1", "t": 0.0, "vehicles": ["a", "b"]}]
    for s in (there - 1.0, there + b.length_m + 0.5):  # short of the point, and its rear past it
        b.s = s
        simulation._crossing_collisions()
    assert simulation.collisions == 1


def test_vehicles_enter_only_as_fast_as_they_can_safely_brake():
    # SUMO's departSpeed "max": 10 m short of a red stop line on a 120 km/h road, a car enters slowly enough to
    # stop comfortably; behind a car at 50 km/h, slowly enough to keep the secure gap, not at its speed.
    network = generate_network(1, 1, lanes_per_direction=1, speed_limit_kmh=120)
    network.set_road_length("1S", 20)
    simulation = Simulation(network, [], MODES[1:2])
    simulation._spawn(pair("1S", "1N", 100, "Random", (100,)), 0)
    simulation._insert()
    car = simulation.vehicles[0]
    entered = car.speed
    simulation.step()
    assert entered > 0 and entered - car.speed <= car.decel * DT + 1e-9, (entered, car.speed)
    simulation = Simulation(generate_network(1, 1, lanes_per_direction=1), [], MODES[1:2])
    route = simulation.graph.route("1W", "1E")
    leader = create_vehicle(MODES[1], "leader", route, origin="1W", destination="1E")
    leader.segment, leader.s, leader.speed = route[0].lanes[0], 12.5, 50 / 3.6
    leader.segment.vehicles.append(leader)
    simulation.vehicles.append(leader)
    simulation._spawn(pair("1W", "1E", 100, "Random", (100,)), 0)
    simulation._insert()
    car = simulation.vehicles[-1]
    gap = leader.s - leader.length_m - car.s
    assert 0 < car.speed and gap >= secure_gap(car, car.speed, leader.speed, leader.decel) - 1e-9, (gap, car.speed)


def test_a_bend_caps_the_lane_speed_however_high_the_limit():
    # A 10 m radius quarter circle edited up to 50 km/h: its lane keeps to the bend's curve speed, 15 km/h (about
    # 1.7 m/s² sideways); a tighter bend is slower still, with no floor faster than it allows.
    radius = 10
    points = [(radius * sin(pi * i / 24), radius - radius * cos(pi * i / 24)) for i in range(13)]
    network = Network(horizontal_corridors=1, vertical_corridors=1)
    network.nodes = {"A": Node("A", *points[0], None, None, control="uncontrolled"),
                     "B": Node("B", *points[-1], None, None, control="uncontrolled")}
    network.roads = {"A-B": Road("A-B", "A", "B", polyline_length(points), lanes_per_direction=1, speed_limit_kmh=50,
                                 direction="one-way", shape=points[1:-1], curvature=[1 / radius] * 12, single_lane=True)}
    network.validate()
    lane = Simulation(network, [], MODES).graph.links["A-B>"].lanes[0]
    assert abs(lane.speed * 3.6 - 15) < 1e-9 and curve_speed_kmh(radius) == 15 and curve_speed_kmh(1) < 10


def test_lanes_follow_the_road_on_the_driving_side():
    for side, sign in (("left", -1), ("right", 1)):
        network = generate_network(1, 2, driving_side=side)  # 1,1 -> 2,1 runs east (+x), y down
        graph = Simulation(network, [], MODES).graph
        road = network.road("1,1-2,1")
        east, y = graph.links["1,1-2,1>"], network.node("1,1").y
        assert abs(east.lanes[0].points[0][1] - (y + sign * 1.5 * 3.5)) < 1e-6  # kerbside lane, keep side
        assert abs(east.lanes[1].points[0][1] - (y + sign * 0.5 * 3.5)) < 1e-6
        assert abs(east.lanes[0].length - (road.length_m - 2 * graph._trim("1,1", road))) < 1e-6  # stop lines


def _pocket_run(network, demand, seconds, each_step=None):
    simulation = Simulation(network, [pair(o, d, flow, "Poisson", (100,)) for o, d, flow in demand], MODES[1:2], seed=3)
    run(simulation, seconds, each_step)
    return simulation


def test_turn_pockets_follow_the_driving_side_and_take_their_turns():
    # A kerb pocket opens 40 m back from the stop line beside the kerbside lane; an offside pocket at the centre,
    # the lanes moving over to the widened kerb side along the taper. Each mirrors with the driving side.
    for side, sign in (("left", -1), ("right", 1)):
        network = generate_network(1, 1, lanes_per_direction=1, driving_side=side)  # 1W -> 1,1 runs east, y down
        network.road("1W").pockets = {"1,1": {"lanes": 1, "length_m": 40.0, "side": "kerb"}}
        network.road("1E").pockets = {"1,1": {"lanes": 1, "length_m": 40.0, "side": "offside"}}
        network.validate()
        graph = Simulation(network, [], MODES[1:2]).graph
        y = network.node("1,1").y
        east, west = graph.links["1W>"], graph.links["1E<"]
        pocket, through = east.lanes
        assert east.pockets == (1, 0) and abs(pocket.start - (pocket.length - 40.0)) < 1e-9 and through.start == 0
        assert abs(pocket.points[-1][1] - (y + sign * 1.5 * 3.5)) < 1e-6  # beyond the kerbside lane
        assert abs(through.points[-1][1] - (y + sign * 0.5 * 3.5)) < 1e-6
        kinds = {(index, turn.kind): turn.permitted for (index, _), turn in east.turns.items()}
        assert kinds[(0, "kerb")] and not kinds[(0, "straight")] and kinds[(1, "straight")] and not kinds[(1, "kerb")]
        through, pocket = west.lanes  # westbound: the kerb is on the other side of y
        assert west.pockets == (0, 1) and pocket.start > 0 and through.start == 0
        assert abs(through.points[0][1] - (y - sign * 0.5 * 3.5)) < 1e-6  # moved over along the taper
        assert abs(through.points[-1][1] - (y - sign * 1.5 * 3.5)) < 1e-6 and abs(pocket.points[-1][1] - (y - sign * 0.5 * 3.5)) < 1e-6
        # Signal timing loads a pocket's lanes with its turns and the other lanes with straight-on traffic.
        kerb_into = next(after for (index, after), turn in east.turns.items() if turn.kind == "kerb")
        moves = {("1W>", kerb_into): 900.0, ("1W>", "1E>"): 600.0}
        assert abs(graph.arm_ratio("1W>", moves) - max(900 / TURN_SATURATION_VPH["kerb"], 600 / SATURATION_PER_LANE_VPH)) < 1e-12


def test_turners_use_their_pocket_and_straight_on_traffic_passes():
    network = generate_network(1, 1, lanes_per_direction=1)
    network.road("1W").pockets = {"1,1": {"lanes": 1, "length_m": 40.0, "side": "kerb"}}
    seen = {"turners in the pocket": set(), "others in it": 0, "turned from it": 0, "turned": 0}

    def watch(simulation, before, _states):
        pocket = simulation.graph.links["1W>"].lanes[0]
        for vehicle in pocket.vehicles:
            assert vehicle.s >= pocket.start - 1e-6  # only past where the pocket opens
            if vehicle.destination == "1N":
                seen["turners in the pocket"].add(vehicle.id)
            else:
                seen["others in it"] += 1
        for vehicle in simulation.vehicles:
            if isinstance(vehicle.segment, Turn) and before.get(vehicle.id) is not vehicle.segment \
                    and vehicle.segment.lane_in.link.id == "1W>" and vehicle.destination == "1N":
                seen["turned"] += 1
                seen["turned from it"] += vehicle.segment.lane_in is pocket

    simulation = _pocket_run(network, [("1W", "1N", 200), ("1W", "1E", 300)], 900, watch)
    assert seen["others in it"] == 0 and len(seen["turners in the pocket"]) > 30
    assert seen["turned from it"] >= 0.9 * seen["turned"] > 0, seen
    assert simulation.arrived > 0.9 * simulation.spawned - 10 and simulation.collisions == 0


def test_a_full_turn_pocket_blocks_the_lane_behind_it():
    # Concept 15's turn-pocket overflow: at a priority T junction, offside turners give way to heavy oncoming
    # traffic and fill a short pocket; the next turner waits in the through lane short of the pocket, and the
    # straight-on traffic behind it waits too (one lane each way, so no way past).
    network = generate_network(1, 1, lanes_per_direction=1)
    network.delete_road("1N")
    network.node("1,1").control = "uncontrolled"
    network.road("1W").pockets = {"1,1": {"lanes": 1, "length_m": 15.0, "side": "offside"}}
    blocked = []

    def watch(simulation, _before, _states):
        through, pocket = simulation.graph.links["1W>"].lanes
        for behind, ahead in zip(through.vehicles, through.vehicles[1:]):
            if ahead.destination == "1S" and ahead.s < pocket.start + 1.0 and ahead.speed < 0.1 \
                    and behind.destination == "1E" and behind.speed < 0.1:
                blocked.append(simulation.time)

    simulation = _pocket_run(network, [("1W", "1S", 300), ("1W", "1E", 300), ("1E", "1W", 1300)], 600, watch)
    assert len(pocket_vehicles := simulation.graph.links["1W>"].lanes[1].vehicles) >= 1 and blocked, pocket_vehicles
    assert simulation.collisions == 0


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


def test_a_pairs_departures_ignore_changes_elsewhere():
    """Common random numbers: each pair draws its arrivals, types, timetable, routes and drivers from streams of its
    own, seeded from the run's seed, so slowing another pair's road and adding a pair (which changes every vehicle's
    dawdling) leaves the pair's departures, types and speed factors exactly as they were."""
    modes = [VehicleMode(**{**vars(mode), "headway_min": 10 if mode.name == "Bus" else 0}) for mode in MODES]

    def departures(slower: bool, extra: bool):
        network = generate_network(2, 2)
        if slower:
            for link in Simulation(network, [], modes).graph.route("1S", "1N"):
                network.road(link.road).speed_limit_kmh = 20
        demand = [pair("1W", "1E", 600), pair("1S", "1N", 600)] + ([pair("1E", "1W", 400)] if extra else [])
        simulation, seen = Simulation(network, demand, modes, seed=11), {}

        def record(sim, before, states):
            for vehicle in [*sim.vehicles, *(v for queue in sim.pending.values() for v in queue)]:
                if vehicle.origin == "1W":
                    seen[vehicle.id] = (round(vehicle.depart_s, 6), vehicle.mode, round(vehicle.speed_factor, 6))

        run(simulation, 900, record)
        return simulation, sorted(seen.values())

    (base, alone), (changed, beside) = departures(False, False), departures(True, True)
    assert len(alone) > 100 and alone == beside and base.spawned < changed.spawned  # the same, though more ran
    assert any(mode == "Bus" for _, mode, _ in alone)  # its timetable too


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


def test_hourly_demand_follows_its_peak_hour_factor():
    from collections import Counter
    from traffic_simulator.daily_profile import phf_quarters
    network = generate_network(2, 2)
    peaked = PairDemand("1W", "1E", 480, None, [], "Random", (0, 100, 0), quarters=phf_quarters(0.5))
    simulation = Simulation(network, [peaked], MODES, seed=3)
    for minute in range(60):  # each minute's arrivals, at the rate of its 15 minutes
        simulation._count_arrivals(minute * 60.0)
    counts = Counter(int(at // 900) for at, *_ in simulation.departures)
    assert abs(counts[1] - 240) <= 4 and all(abs(counts[q] - 80) <= 4 for q in (0, 2, 3)), counts  # V/PHF for 15 min
    daily = PairDemand("1W", "1E", None, 480, [1 / 24] * 24, "Random", (0, 100, 0), quarters=phf_quarters(0.5))
    assert Simulation.rate_now(daily, 1000) == Simulation.rate(daily, 1000) == 20  # no PHF on a daily curve


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
    assert seen["red"] == 0 and seen["overlaps"] == 0 and simulation.collisions == 0, (seen, simulation.collision_kinds)
    assert seen["green"] > 500 and seen["roundabout"] > 50 and seen["uncontrolled"] > 50, seen
    assert seen["lane changes"] > 100 and simulation.arrived > 300, seen


def test_roundabout_entrants_are_never_stopped_dead_at_the_line():
    # The audit's case: entrants let in from the other lanes are anticipated, and one already too close to stop
    # comfortably goes unless a vehicle would be beside it, so no one brakes harder than SUMO's emergency
    # braking for a car (9 m/s²) at the give-way line.
    network = generate_network(1, 1)
    network.node("1,1").control = "roundabout"
    car = VehicleMode("Car", PALETTE[1], sigma=0.0, speed_dev=0.0)
    demand = [pair(o, d, 180, "Poisson", (100,)) for o, d in (("1W", "1E"), ("1S", "1N"), ("1E", "1W"), ("1N", "1S"))]
    simulation, hardest = Simulation(network, demand, [car], seed=19), 0.0
    while simulation.time < 300:
        before = {vehicle.id: vehicle.speed for vehicle in simulation.vehicles}
        simulation.step()
        hardest = max([hardest] + [(before[v.id] - v.speed) / DT for v in simulation.vehicles if v.id in before])
    assert hardest <= 9.0 and simulation.arrived > 40 and simulation.collisions == 0, (hardest, simulation.arrived)


def test_a_mirror_image_on_the_other_side_performs_alike():
    # The physics is side-neutral: a network, and its mirror image driven on the other side (the demand
    # and junctions mirrored east-west), clear the same traffic in the same time, give or take the
    # randomness. The same demand on the other side is a different mix of kerb and offside turns.
    pairs = (("1W", "1E"), ("1S", "3N"), ("2E", "1N"), ("3W", "4S"), ("2W", "2E"), ("2S", "2N"), ("4N", "2W"))

    def mirror(end):  # across the grid's north-south centre line: W and E swap, column c becomes 5 - c
        number, side = end[:-1], end[-1]
        return number + {"W": "E", "E": "W"}[side] if side in "WE" else f"{5 - int(number)}{side}"

    def run_side(side, mirrored, seed, seconds=1200):
        network = generate_network(3, 4)
        network.set_driving_side(side)
        roundabout, free = ("3,2", "2,2") if mirrored else ("2,2", "3,2")
        network.node(roundabout).control, network.node(free).control = "roundabout", "uncontrolled"
        demand = [pair(mirror(o), mirror(d), 450) if mirrored else pair(o, d, 450) for o, d in pairs]
        simulation = Simulation(network, demand, MODES, seed=seed)
        run(simulation, seconds)
        turns = [[next(turn.kind for (_, to), turn in link.turns.items() if to == after.id)
                  for link, after in zip(route, route[1:])]
                 for route in (simulation.graph.route(p.origin, p.destination) for p in demand)]
        return simulation.arrived, turns

    (given_4, turns), (given_5, _) = run_side("left", False, 4), run_side("left", False, 5)
    (mirrored_4, mirrored_turns), (mirrored_5, _) = run_side("right", True, 4), run_side("right", True, 5)
    as_given, mirrored = given_4 + given_5, mirrored_4 + mirrored_5
    assert abs(mirrored - as_given) < 0.07 * as_given, (as_given, mirrored)
    # Routes are side-neutral too: the mirror image's make the same turns. The same demand on the other side makes
    # other ones (junction delay in route costs steers each side round its dearer turns, so neither side now clears
    # clearly more than the other).
    assert mirrored_turns == turns and run_side("right", False, 4, 0)[1] != turns


def test_a_share_of_trips_ends_part_way_along_the_route():
    # Drop-offs (ODCell.drops): 30 % of the cars end a quarter of the way, 20 % three quarters; the rest reach 1E
    def trips(drops):
        simulation = Simulation(generate_network(3, 1), [PairDemand("1W", "1E", 900, None, [], "Random", (0, 100, 0),
                                                                    drops=drops)], MODES, seed=3)
        departures, ended = [], []
        while simulation.time < 900:
            before = {vehicle.id: (vehicle, vehicle.s) for vehicle in simulation.vehicles}
            simulation.step()
            departures += [(v.id, v.depart_s) for v in simulation.vehicles if v.id not in before]
            now = {vehicle.id for vehicle in simulation.vehicles}
            ended += [(vehicle, s) for key, (vehicle, s) in before.items() if key not in now]
        return simulation, departures, ended

    plain, departures, ended = trips(())
    whole = max(sum(link.length for link in vehicle.route) for vehicle, _ in ended)
    dropped, departed, ended = trips(((1, 0.25, 30), (1, 0.75, 20)))
    assert departed == departures and dropped.spawned == plain.spawned  # its own draws: the others are as they were
    early = [(vehicle, s) for vehicle, s in ended if sum(link.length for link in vehicle.route) < whole - 1
             or vehicle.drop_s is not None]
    assert 0.38 < len(early) / len(ended) < 0.62, len(early) / len(ended)
    assert all(s <= vehicle.drop_s for vehicle, s in early if vehicle.drop_s is not None)  # none past its point
    assert {len(vehicle.route) for vehicle, _ in early} == {1, 2}  # a quarter: the first link; three quarters: the second
    assert dropped.arrived == len(ended)  # counted as arrived


def test_bikes_keep_to_a_cycle_lane_and_cars_never_use_one():
    # A cycle lane (Road.cycle_lanes) is bikes only: on its road every bike rides it and no car does; bikes turn out
    # of it into roads without one and into it from them; nothing jams or collides
    network = generate_network(3, 2)
    for index, road in enumerate(network.all_roads()):
        if index % 2 == 0:
            road.cycle_lanes = list(road.arrival_ends())
    network.validate()
    demand = [PairDemand("1W", "1E", 900, None, [], "Random", (0, 70, 30)),
              PairDemand("1W", "1S", 400, None, [], "Random", (0, 70, 30)),
              PairDemand("2N", "1W", 400, None, [], "Random", (0, 70, 30))]
    from traffic_simulator.mode_behavior import VehicleModes

    simulation = Simulation(network, demand, VehicleModes().modes, seed=2)  # Bike is of the bicycle class
    seen, moves = set(), set()
    while simulation.time < 600:
        simulation.step()
        for vehicle in simulation.vehicles:
            segment = vehicle.segment
            if isinstance(segment, Lane):
                if segment.link.cycle is not None:
                    assert (segment.index < 0) == vehicle.cycles, (vehicle.id, segment.name)
                seen.add((vehicle.cycles, segment.index < 0))
            elif isinstance(segment, Turn) and vehicle.cycles:
                moves.add((segment.lane_in.index < 0, segment.lane_out.index < 0))
    assert seen == {(False, False), (True, False), (True, True)}  # cars only on car lanes; bikes on both
    assert {(True, False), (False, True)} <= moves  # out of a cycle lane into a road without, and into one
    assert simulation.teleports == 0 and simulation.collisions == 0 and simulation.arrived > 100
    cycle = next(link.cycle for link in simulation.graph.links.values() if link.cycle is not None)
    assert all(lane is not cycle for link in simulation.graph.links.values() for lane in link.lanes)  # never offered


def test_pedestrians_walk_their_way_at_their_own_speed_and_leave_the_traffic_as_it_was():
    # SUMO's NonInteracting model: each walker along its shortest walk at its own speed all the way; its own draws,
    # so the vehicles of the same run are exactly as without them
    from traffic_simulator.geometry import polyline_length
    from traffic_simulator.metro import WALK_KMH

    def run(people):
        network = generate_network(3, 2)
        for road in network.all_roads():
            road.footpaths = ["left", "right"]
        network.walk_demand = {"1W->1E": people} if people else {}
        simulation = Simulation(network, [pair("1W", "1E", 600, "Random", (0, 100, 0))], MODES, seed=4)
        poses, walked = [], {}
        while simulation.time < 900:
            simulation.step()
            poses.append(tuple((v.id, v.pose) for v in simulation.vehicles))
            for walker in simulation.walkers:
                walked.setdefault(walker.id, (walker, simulation.time))
        return simulation, poses, walked

    plain, poses, _ = run(0)
    walking, walking_poses, walked = run(1200)
    assert walking_poses == poses  # not a vehicle moved differently
    assert 250 < walking.walk_spawned < 350 and walking.walk_arrived and not walking.walk_unreachable  # ~1200 an hour
    speeds = [walker.speed for walker, _ in walked.values()]
    assert abs(sum(speeds) / len(speeds) - WALK_KMH / 3.6) < 0.05 and max(speeds) - min(speeds) > 0.2  # each its own
    walker = next(iter(walked.values()))[0]
    assert abs(polyline_length(walker.points) - walker.length) < 1e-9
    frame = walking.frame()
    assert len(frame.walkers) == len(walking.walkers) and len(frame.walkers[0]) == 5


def test_traffic_stops_for_walkers_on_a_zebra_and_walkers_wait_for_gaps_and_cars_that_cant_stop():
    # A zebra gives walkers right of way (HCM 2000 p18-13): no vehicle passes its stop line while one is on it, and a
    # walker steps off only when no vehicle is too near to stop; at a junction's arm, only in a gap of
    # tc = L / Sp + ts (Eq 18-17); an overbridge holds no one up
    from traffic_simulator.mode_behavior import VehicleModes
    from traffic_simulator.simulation import Walker
    from traffic_simulator.walking import Crossed

    def run(kind):
        network = generate_network(2, 1)
        for road in network.all_roads():
            road.footpaths = ["left", "right"]
        road = network.roads["1,2-1,1"]
        if kind:
            network.add_crossing(road.id, network.road_geometry_length(road) / 2, kind)
        network.walk_demand = {"2W->1E": 300.0}
        simulation = Simulation(network, [pair("1N", "1S", 700, "Random", (0, 100, 0))], VehicleModes().modes, seed=5)
        passed, waited = 0, []
        while simulation.time < 900:
            before = {v.id: (v.segment, v.s) for v in simulation.vehicles}
            held = {crossing for crossing, count in simulation._on_zebra.items() if count}
            simulation.step()
            for vehicle in simulation.vehicles:
                was = before.get(vehicle.id)
                if was and was[0] is vehicle.segment and isinstance(vehicle.segment, Lane):
                    passed += sum(was[1] < at < vehicle.s for at, crossing in  # over its stop line
                                  simulation._zebra_stops.get(vehicle.segment.link.id, ()) if crossing in held)
            waited += [walker.waited_s for walker in simulation.walkers]
        return simulation, passed, waited

    zebra, passed, zebra_waits = run("zebra")
    assert passed == 0 and zebra.collisions == 0 and zebra.walk_arrived  # nobody drove over someone on it
    gaps, _, gap_waits = run(None)
    assert gaps.walk_arrived and max(gap_waits) > max(zebra_waits)  # without right of way, the waits are longer
    bridge, _, bridge_waits = run("overbridge")
    assert not bridge._zebra_stops and bridge.walk_arrived
    # At a zebra's kerb, alone on the road with one car: the walker waits for it only when it couldn't stop
    for link in zebra.graph.links.values():
        for lane in link.lanes:
            lane.vehicles.clear()
        for turn in link.all_turns():
            turn.vehicles.clear()
    link = zebra.graph.links["1,2-1,1>"]
    at = zebra._along_link(link, link.length / 2)
    walker = Walker("w", [(0.0, 0.0), (20.0, 0.0)], 20.0, 1.4, 0.0,
                    [Crossed(0.0, 14.0, "zebra", "1,2-1,1", link.length / 2, 14.0, "X1")])
    on_zebra = walker.crossings[0]
    assert zebra._may_cross(walker, on_zebra)  # nothing coming
    car = create_vehicle(VehicleModes().modes[1], "car", [link], 0.0, "1N", "1S")
    car.segment, car.s, car.speed = link.lanes[0], at - link.cut_start - 6.0, 13.0
    link.lanes[0].vehicles.append(car)
    assert not zebra._may_cross(walker, on_zebra)  # 6 m short at 47 km/h: it couldn't stop
    car.s = at - link.cut_start - 80.0
    assert zebra._may_cross(walker, on_zebra)  # far enough to stop comfortably: the walker goes, the car will stop
    assert not zebra._may_cross(walker, on_zebra._replace(kind="gap"))  # 80 m at 13 m/s: under 14 / 1.4 + 3 s
    car.speed = 0.0
    assert zebra._may_cross(walker, on_zebra._replace(kind="gap"))  # standing short of it: no threat


def test_a_vehicle_stuck_for_the_teleport_time_jumps_on_along_its_route():
    # A gridlock's cycle (Dhanmondi: three queues each waiting on the next) froze whole runs; as SUMO does after
    # time-to-teleport, the front vehicle stuck that long leaves the road and waits to enter its route's next link
    network = generate_network(3, 1)
    simulation = Simulation(network, [PairDemand("1W", "1E", 600, None, [], "Random", (0, 100, 0))], MODES, seed=1)
    while not simulation.vehicles:
        simulation.step()
    vehicle = simulation.vehicles[0]
    lane, leg = vehicle.segment, vehicle.leg
    vehicle.waiting_s = TIME_TO_TELEPORT_S - simulation.dt
    simulation._teleport_jammed()
    assert vehicle in simulation.vehicles and simulation.teleports == 0  # not yet
    vehicle.waiting_s = TIME_TO_TELEPORT_S
    simulation.time_to_teleport_s = 0  # off: a run showing its gridlock as it is
    simulation._teleport_jammed()
    assert vehicle in simulation.vehicles
    simulation.time_to_teleport_s = TIME_TO_TELEPORT_S
    simulation._teleport_jammed()
    assert vehicle not in simulation.vehicles and vehicle not in lane.vehicles and simulation.teleports == 1
    assert vehicle.leg == leg + 1 and vehicle in simulation.pending[vehicle.route[leg + 1].id]  # off the road, on its way
    inserted = vehicle.inserted_s
    while vehicle not in simulation.vehicles:
        simulation.step()
    assert vehicle.segment.link is vehicle.route[leg + 1] and vehicle.inserted_s == inserted  # its trip keeps its start


def test_the_major_road_of_an_uncontrolled_junction_is_the_higher_class():
    # An OpenStreetMap trunk road gave way to a residential lane that happened to line up better (Dhanmondi
    # gridlocked); as SUMO's netconvert does, the road class decides first, the geometry only between equals
    for major, minor in (("trunk", "residential"), ("residential", "trunk")):
        network = generate_network(3, 3)
        network.node("2,2").control = "uncontrolled"
        for road in network.all_roads():
            if "2,2" in (road.from_node, road.to_node):
                vertical = network.node(road.from_node).x == network.node(road.to_node).x
                road.road_type = major if vertical else minor
        graph = Simulation(network, [], MODES).graph
        chosen = {graph.links[link].road for link in graph._major_arms("2,2")}
        assert all(network.road(road).road_type == "trunk" for road in chosen) and len(chosen) == 2, chosen


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


def test_path_size_logit_shares_routes():
    from types import SimpleNamespace

    from traffic_simulator.simulation import path_size_logit

    a, b, c = (SimpleNamespace(id=name, length=100.0) for name in "abc")
    assert path_size_logit([[a], [b]], [60.0, 60.0], 0.5) == [0.5, 0.5]  # alike: even
    assert path_size_logit([[a], [b]], [60.0, 120.0], 0.5)[0] > 0.6  # a minute faster: e^0.5 to 1
    shared = path_size_logit([[a, c], [b, c], [a]], [60.0, 60.0, 60.0], 0.5)  # b-c overlaps least (PS 0.75, not 0.5)
    assert abs(sum(shared) - 1) < 1e-12 and shared[1] > shared[0] and abs(shared[0] - shared[2]) < 1e-12


def test_route_costs_count_junction_delay_once():
    from traffic_simulator.simulation import PRIORITY_CAPACITY_VPH, Graph
    network = generate_network(3, 3)  # two-phase signals, driving on the left
    network.node("2,2").control = "uncontrolled"
    graph = Graph(network)
    # Free flow: each move's control delay with no traffic at the greens entered, the turn's own length left out.
    signal = graph.signals["1,1"]
    into = graph.arriving["1,1"][0]
    red = signal.cycle - effective(signal.phases[signal.phase_of[into.id]][1])  # the effective red, C - g
    assert {graph.control_delay[(into.id, after)] for (_, after), turn in into.turns.items()
            if turn.kind != "uturn"} == {red * red / (2 * signal.cycle)}
    give_way = 3600 / PRIORITY_CAPACITY_VPH[0][1] + 5  # HCM 2000 Eq 17-38 with nothing to give way to
    moves = {(turn.kind, link.id in graph._major_arms("2,2")): graph.control_delay[(link.id, after)]
             for link in graph.arriving["2,2"] for (_, after), turn in link.turns.items()}
    assert moves[("straight", True)] == moves[("kerb", True)] == 0  # the major road
    assert moves[("offside", True)] == moves[("straight", False)] == moves[("offside", False)] == give_way
    leg, after = into, graph.links[next(after for _, after in into.turns)]
    assert graph.free_move_time(leg, after) == leg.length / leg.speed + graph.control_delay[(leg.id, after.id)]
    # Measured: a move's own time, else its link's over all moves; never both, nor the prior on top.
    graph.choice = {"link_times_s": {leg.id: 100.0}, "turn_times_s": {leg.id: {after.id: 40.0}}}
    other = next(graph.links[a] for _, a in leg.turns if a != after.id)
    assert graph.move_time(leg, after) == 40.0 and graph.move_time(leg, other) == 100.0
    assert graph.path_time([leg, after]) == 40.0 + after.length / after.speed  # its last link: no junction left
    # A run measures each move: the link's traversals split by the next link taken.
    simulation = Simulation(network, [pair("1W", "1E", 300), pair("2S", "2N", 300), pair("2W", "1N", 200)],
                            MODES, seed=3)
    simulation.observe()
    run(simulation, 600)
    seen = simulation.observations()
    for link, (count, mean_s) in seen["traversals"].items():
        moves = seen["turn_traversals"][link].values()
        assert sum(n for n, _ in moves) == count and abs(sum(n * s for n, s in moves) / count - mean_s) < 1e-6


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
    # L = 2 x (1.8 start-up + 2 all-red + 0.6 amber unused) = 8.8 s: C0 = (1.5 x 8.8 + 5) / (1 - 0.5) = 36.4 s
    cycle, greens = webster([0.3, 0.2])
    assert round(cycle, 6) == 36.4 and [round(green, 1) for green in greens] == [16.0, 10.4]  # (36.4 - 8.8) y/Y - 0.6
    cycle, greens = webster([0.0, 0.0])  # no demand: the shortest cycle, MIN_CYCLE_S, shared evenly
    assert round(cycle, 6) == 30.0 and [round(green, 6) for green in greens] == [10.0, 10.0]
    assert round(webster([0.6, 0.5])[0], 6) == 120.0  # over capacity: the longest cycle
    network = generate_network(3, 4)
    network.node("2,2").signal_method = "fixed"  # keeps its entered greens
    network.node("2,2").green_times_s = [40.0, 20.0]
    demand = [pair("1W", "1E", 1200), pair("2W", "2E", 900), pair("1S", "1N", 200)]
    simulation = Simulation(network, demand, MODES)
    plan = simulation.signal_plans["1,1"]  # east-west 1200 veh/h on 2 lanes, north-south 200
    ratios = [round(1200 / (2 * SATURATION_PER_LANE_VPH), 3), round(200 / (2 * SATURATION_PER_LANE_VPH), 3)]
    assert plan["method"] == "webster" and plan["flow_ratios"] == ratios
    # the busy phase gets the longer green, the other its minimum, inside Webster's cycle (it used to grow past it)
    assert plan["greens_s"][0] > plan["greens_s"][1] == 7.0
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
    assert seen["shown"] <= 5 and "signals" not in seen  # a road asked: its cars, not every signal
    assert set(window._traffic({"limit": 5})["signals"]) == {node for node, _ in frame.signals}
    runner = window.runner
    window.od_matrix.cells[("1W", "1E")] = ODCell(flow=300, split=(10, 85, 5))
    window.od_panel.changed.emit(None)  # a demand edit (the user's or the assistant's): the run carries on
    assert window.runner is runner and [pair.hourly for pair in runner.simulation._handed] == [300]
    window.undo()  # undoing a demand edit carries on too, back to the demand before it
    assert window.runner is runner and [pair.hourly for pair in runner.simulation._handed] == []
    assert runner.simulation.graph.network is not window.network  # the run drives its own copy
    window.results_folder = Path(tempfile.mkdtemp())  # the stopped run's workbook: not into the user's Documents
    window.network.nodes["1,1"].control = "roundabout"  # an edit changes the network, then stops the run
    window.properties.edited.emit(None)  # a network edit during the run stops it
    assert window.runner is None and window.clock.time_s == 0 and not window.clock.running
    assert window.last_kpis["observed"]["controls"]["1,1"] == "signalised"  # the results: the network it drove
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
        before = runner.target()
        runner.set_speed(20)  # mid-run: carries on from where it stands, no jump (the pace swapped whole)
        assert runner.speed == 20 and 0 <= runner.target() - before < 1.0
        runner.pause()
        held = runner.frame.time
        time.sleep(0.2)
        assert runner.frame.time == held and not runner.running
        runner.set_speed(60)
        runner.play()
        time.sleep(0.5)
        assert runner.frame.time >= held + 25  # 0.5 s at 60x
        record = runner.frame.records(limit=3)
        assert record["on_road_network"] == len(runner.frame.vehicles) and record["shown"] <= 3
        assert record["matching_stopped"] <= record["matching"] and "signals" in record
        assert "signals" not in runner.frame.records("1,1-2,1")  # a road asked: its cars, not every signal plan
    finally:
        runner.stop()
    assert not runner._thread.is_alive()


def test_the_runner_says_when_its_physics_fails_or_has_not_stopped():
    import threading

    simulation = Simulation(generate_network(1, 1), [], MODES)
    simulation.step = lambda: 1 / 0
    runner = Runner(simulation, speed=60)
    runner.play()
    runner._thread.join(2)
    assert not runner.running and "ZeroDivisionError" in runner.error  # said, not left "running"
    runner.play()
    assert not runner.running and runner.stop(0.1)  # stopped for good
    simulation, entered, release = Simulation(generate_network(1, 1), [], MODES), threading.Event(), threading.Event()
    step = simulation.step
    simulation.step = lambda: (entered.set(), release.wait(5), step())
    runner = Runner(simulation, speed=60)
    runner.play()
    assert entered.wait(2)
    assert runner.stop(0.1) is False  # a step still going: not ended
    release.set()
    assert runner.stop(2) and simulation.time == DT  # it ended after that step, none after


def test_a_frame_shares_no_plan_with_the_run_or_another_frame():
    simulation = Simulation(generate_network(1, 1), [], MODES)
    frame, other = simulation.frame(), simulation.frame()
    node = next(iter(frame.plans))
    cycle = simulation.signal_plans[node]["cycle_s"]
    frame.plans[node]["cycle_s"] = 1
    frame.plans[node]["greens_s"].append(1)
    frame.records()["signal_plans"][node]["greens_s"].append(1)
    assert simulation.signal_plans[node]["cycle_s"] == cycle == other.plans[node]["cycle_s"]
    assert frame.records()["signal_plans"][node]["greens_s"] == frame.plans[node]["greens_s"]  # records: a copy
    assert other.plans[node]["greens_s"] == simulation.signal_plans[node]["greens_s"]
    empty = type(frame)(0.0, (), {}, 0, 0)
    try:
        empty.plans["x"] = 1  # the default: read-only, so never one shared between frames
    except TypeError:
        pass
    else:
        raise AssertionError("a frame's default plans can be changed")


def test_give_way_drivers_slow_to_look_and_minor_turns_give_way_by_rank():
    from traffic_simulator.simulation import GIVE_WAY_KMH, Graph

    graph = Graph(_uncontrolled_crossing())
    major = graph._major_arms("1,1")
    turns = [turn for link in graph.arriving["1,1"] for turn in link.turns.values()]
    giving = [turn for turn in turns if any(gives for *_, gives in turn.conflicts)]
    assert giving and all(turn.speed <= GIVE_WAY_KMH / 3.6 + 1e-9 for turn in giving)  # slowed at the line
    on_major = [turn for turn in turns if turn.lane_in.link.id in major and turn.lane_out.link.id in major]
    assert on_major and not any(turn in giving for turn in on_major)  # the major road gives way to no one
    # HCM's ranks: a minor offside turn gives way to the other minor arm's straight-on traffic (rank 4 to rank 3)
    offside = next(turn for turn in turns if turn.kind == "offside" and turn.lane_in.link.id not in major)
    yields_to = {(other.lane_in.link.id, other.kind) for other, _, _, gives in offside.conflicts if gives}
    assert any(link not in major and kind == "straight" for link, kind in yields_to), yields_to


def _uncontrolled_crossing():
    network = generate_network(1, 1)
    network.node("1,1").control = "uncontrolled"
    return network


def test_junction_saturation_by_control():
    """Degree of saturation at a roundabout (measured entry capacity against the circulating flow), a
    priority junction (against the flows it gives way to) and a signal (lane by lane, turns slower)."""
    from traffic_simulator.signal_timing import KERB_TURN_PER_LANE_VPH
    from traffic_simulator.simulation import (PRIORITY_CAPACITY_VPH, ROUNDABOUT_ENTRY_VPH, Graph,
                                              measured_capacity)

    assert measured_capacity(ROUNDABOUT_ENTRY_VPH, 0) == 1455 and measured_capacity(ROUNDABOUT_ENTRY_VPH, 300) == 1092.5
    assert measured_capacity(PRIORITY_CAPACITY_VPH, 5000) == 105  # held at the last point beyond the table
    network = generate_network(3, 3)
    network.nodes["2,2"].control = "roundabout"
    network.nodes["2,1"].control = "uncontrolled"
    graph = Graph(network)
    inbound, outbound = graph.arriving["2,2"], graph.leaving["2,2"]
    entry = inbound[0]
    alone = {(entry.id, next(link.id for link in outbound if link.road != entry.road)): 600.0}
    x = graph.saturation("2,2", {}, alone)
    assert abs(x - 600 / 1455) < 1e-9  # nothing circulating; the entry's lanes share one path round
    others = {(link.id, out.id): 300.0 for link in inbound if link is not entry for out in outbound if out.road != link.road}
    assert graph.saturation("2,2", {}, {**alone, **others}) > x  # circulating traffic in front: less capacity

    major = graph._major_arms("2,1")
    minor = next(link for link in graph.arriving["2,1"] if link.id not in major)
    across = next(turn.lane_out.link.id for turn in minor.turns.values() if turn.kind == "straight")
    x = graph.saturation("2,1", {}, {(minor.id, across): 200.0})
    assert abs(x - 200 / 1265 / len(minor.lanes)) < 1e-9  # no priority traffic to wait for: still slowed to look
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


def test_roundabout_entry_capacity_follows_the_hcm_while_the_ring_has_room():
    """A single-lane entry held saturated against 0 and 800 veh/h circulating: within 15 % of the HCM 6 entry
    capacity (Eq 22-1, roundabouts.hcm_entry_capacity). Above about 900 veh/h the one-lane ring itself is full."""
    from traffic_simulator.roundabouts import hcm_entry_capacity

    for circulating in (0, 800):
        measured, hcm = _held_queue("roundabout", circulating), hcm_entry_capacity(circulating)
        assert abs(measured - hcm) < 0.15 * hcm, (circulating, measured, hcm)



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
    on_road = {vehicle.id for vehicle in simulation.vehicles}
    arrived = [trip["id"] for trip in trips.values() if trip["arrived_s"] is not None]
    assert arrived and not set(arrived) & on_road  # arrived (a junction or an end): off the network
    assert "Emergency2" in arrived  # its destination an end; Emergency1's journey ends at a junction's stop line too


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


def test_a_driver_stops_at_the_amber_when_it_can_brake_in_time():
    simulation = Simulation(generate_network(1, 2), [], MODES)
    link = simulation.graph.links["1,1-2,1>"]
    turn = next(turn for (index, after), turn in link.turns.items() if simulation.graph.links[after].end == "1E")
    car = create_vehicle(MODES[1], "car", [link, turn.lane_out.link])
    car.segment, car.speed = turn.lane_in, 50 / 3.6  # SUMO's car: 4.5 m/s² braking, 21.4 m to stop from 50 km/h
    simulation._states = {**simulation._states, link.id: "Y"}
    simulation._lane_states = {}
    car.s = turn.lane_in.length - 28.0  # room to stop: it stops (SUMO's rule, no reaction distance)
    assert not simulation._may_enter(car, turn)
    car.s = turn.lane_in.length - 18.0  # too close to stop at its braking: it goes on
    assert simulation._may_enter(car, turn)

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


MOTO = VehicleMode("Moto", PALETTE[3], vehicle_class="motorcycle", **VEHICLE_CLASSES["motorcycle"])  # filters: by its class


def _lateral_fingerprint(**options) -> str:
    """A fixed-seed mixed run (motorcycles among the types) frozen: every vehicle's place, speed and pose, and the counts."""
    modes = [MODES[0], MODES[1], MOTO]
    demand = [pair(o, d, 600, split=(10, 50, 40)) for o, d in (("1W", "1E"), ("1S", "2N"), ("2E", "1W"))]
    simulation = Simulation(generate_network(2, 2), demand, modes, seed=7, **options)
    simulation.observe()
    run(simulation, 300)
    state = [(v.id, v.segment.name, v.s, v.speed, v.pose) for v in simulation.vehicles]
    counts = (simulation.spawned, simulation.arrived, simulation.collisions, simulation.emergency_braking,
              len(simulation.observations()["conflicts"]))
    return hashlib.sha256(repr((state, counts)).encode()).hexdigest()


def test_lateral_position_off_leaves_every_run_as_it_was():
    # The run's every position, speed and count, bit for bit, as before lateral position existed (the digest was taken
    # then); change it only for a deliberate change to the default model. On, with no type filtering, the traffic is the
    # same too (only drawn at its lane's kerb side).
    before = "cca0bc19915d235be4f51df6b1df07cf64cb0cebc6bc86a9248ba090bd58acf6"  # 2026-10-18: turn floors, U-turn rate, minimum cycle, SUMO amber rule
    assert _lateral_fingerprint() == _lateral_fingerprint(lateral=False) == before
    plain = Simulation(generate_network(2, 2), [pair("1W", "1E", 900, split=(10, 50, 40))], [MODES[0], MODES[1], MOTO], seed=3)
    still = Simulation(generate_network(2, 2), [pair("1W", "1E", 900, split=(10, 50, 40))],
                       [MODES[0], MODES[1], VehicleMode(**{**MOTO.__dict__, "filters": False})], seed=3, lateral=True)
    run(plain, 240)
    run(still, 240)
    assert [(v.id, v.s, v.speed) for v in plain.vehicles] == [(v.id, v.s, v.speed) for v in still.vehicles]
    assert plain.arrived == still.arrived > 20 and all(v.lat_m == 0 for v in plain.vehicles)
    assert all(abs(v.lat_m) == LANE_WIDTH_M / 2 - v.width_m / 2 for v in still.vehicles)


def _red_queue(lateral: bool, kinds, red_s: float = 90.0):
    """One lane to a signal held red for `red_s`, vehicles of `kinds` sent in that order: the simulation and a helper
    adding one more."""
    simulation = Simulation(generate_network(1, 1, lanes_per_direction=1), [], [MODES[1], MOTO], lateral=lateral)
    simulation.signal_override = lambda node, road, state, context: "R" if simulation.time < red_s else "G"
    route = simulation.graph.route("1W", "1E")

    def add(mode, name):
        vehicle = create_vehicle(mode, name, route, simulation.time, "1W", "1E")
        vehicle.ahead, vehicle.route_m = simulation.graph.landmarks(route), list(accumulate(link.length for link in route))
        simulation.pending[route[0].id].append(vehicle)
        return vehicle

    return simulation, [add(mode, f"{mode.name}{i}") for i, mode in enumerate(kinds)], add


def test_motorcycles_filter_past_a_red_queue_to_the_stop_line_and_leave_first():
    # Lateral position (SL2015 at its smallest): five cars queue at a red; three motorcycles behind them move sideways, no
    # faster than SUMO's maxSpeedLat, ride past the queue within the lane (at most FILTER_SPEED while beside it) to the
    # stop line, and are through before the third car. Without it they wait at the back and leave last.
    order = {}
    for lateral in (False, True):
        simulation, vehicles, _ = _red_queue(lateral, [MODES[1]] * 5 + [MOTO] * 3)
        cars, motos = vehicles[:5], vehicles[5:]
        entered, sideways = [], 0.0
        while simulation.time < 150:
            lats = {v.id: v.lat_m for v in simulation.vehicles}
            passing = [moto for moto in motos if moto in simulation.vehicles and isinstance(moto.segment, Lane) and any(
                car in simulation.vehicles and car.segment is moto.segment and car.s - car.length_m < moto.s <= car.s
                for car in cars)]  # beside a car with its front still ahead, as this step is planned
            simulation.step()
            assert all(moto.speed <= FILTER_SPEED + 1e-9 for moto in passing), [(m.id, m.speed) for m in passing]
            for v in simulation.vehicles:
                if isinstance(v.segment, Lane):
                    sideways = max(sideways, abs(v.lat_m - lats.get(v.id, v.lat_m)))
                    assert abs(v.lat_m) + v.width_m / 2 <= LANE_WIDTH_M / 2 + 1e-9  # within its lane
                if isinstance(v.segment, Turn) and v.id not in entered:
                    entered.append(v.id)
            if abs(simulation.time - (90 - DT)) < 1e-9 and lateral:  # the end of the red
                assert motos[0].s >= cars[0].s - 0.5 and min(m.s for m in motos) > cars[2].s, \
                    [(v.id, round(v.s, 1), v.lat_m) for v in vehicles]
                assert all(_clear(m.lat_m, m.width_m, car) for m in motos for car in cars)
        assert simulation.collisions == 0 and len(entered) == 8
        order[lateral] = entered
        assert sideways <= MAX_SPEED_LAT * DT + 1e-9 and (sideways > 0) == lateral
    assert order[False][-3:] == ["Moto5", "Moto6", "Moto7"]
    assert set(order[True][:5]) >= {"Moto5", "Moto6", "Moto7"} and "Moto5" in order[True][:2], order[True]


def test_side_by_side_in_a_lane_is_no_collision_and_overlapping_is():
    # Lateral runs count a same-lane collision only when two vehicles overlap sideways as well (SUMO's sublane check);
    # without lateral position, two vehicles at one place in a lane always collide.
    counted = {}
    for lateral, lats in ((False, (0.0, 0.0)), (True, (-1.3, 1.3)), (True, (0.0, 0.5))):
        simulation = Simulation(generate_network(1, 1, lanes_per_direction=1), [], [MOTO], lateral=lateral)
        lane = simulation.graph.route("1W", "1E")[0].lanes[0]
        for name, lat in zip("ab", lats):
            vehicle = create_vehicle(MOTO, name)
            vehicle.segment, vehicle.s, vehicle.lat_m, vehicle.startup_delay = lane, 50.0, lat, 5.0  # stands still
            lane.vehicles.append(vehicle)
            simulation.vehicles.append(vehicle)
        simulation.step()
        counted[(lateral, lats)] = simulation.collisions
        a, b = simulation.frame().vehicles  # drawn at their lateral places, and read_traffic says them
        assert abs(((a.x - b.x) ** 2 + (a.y - b.y) ** 2) ** 0.5 - abs(lats[0] - lats[1])) < 1e-9
        assert [v.get("lateral_m", 0.0) for v in simulation.frame().records()["vehicles"]] == list(lats)
    assert counted == {(False, (0.0, 0.0)): 1, (True, (-1.3, 1.3)): 0, (True, (0.0, 0.5)): 1}, counted


def test_a_car_follows_a_motorcycle_it_overlaps_and_passes_one_clear_of_it():
    # A car keeps behind a motorcycle ahead in its lane that it overlaps sideways, at the kerb side as it is; one at the
    # far side of the lane, clear of it with SUMO's minGapLat, is no leader: the car comes up beside it at the stop line.
    for offside in (False, True):
        simulation, (moto,), add = _red_queue(True, [MOTO], red_s=1e9)
        run(simulation, 40)  # the motorcycle waits at the line
        if offside:
            moto.lat_m = simulation.graph.keep * (LANE_WIDTH_M / 2 - moto.width_m / 2)
        car = add(MODES[1], "Car")
        run(simulation, 120)
        assert moto.speed < 0.1 and car.speed < 0.1 and simulation.collisions == 0
        if offside:
            assert car.s > moto.s - moto.length_m and _clear(car.lat_m, car.width_m, moto)  # beside it
        else:
            assert not _clear(car.lat_m, car.width_m, moto) and moto.s - moto.length_m - car.s >= car.min_gap - 0.5


def test_the_lane_graph_needs_no_simulation():
    """Routes and turns come from lane_graph.Graph alone: it, the assistant's tools, the design audits and the
    analyses load without the physics, and nothing builds a Simulation only to read its graph."""
    for module in ("lane_graph", "network_tools", "design_checks", "analysis"):
        loaded = subprocess.run([sys.executable, "-c", f"import sys, traffic_simulator.{module}; print(sorted(sys.modules))"],
                                capture_output=True, text=True, timeout=60, check=True).stdout
        assert "traffic_simulator.simulation" not in loaded, module
    root = Path(__file__).resolve().parents[1]
    built = [path.name for folder in ("src", "extensions", "tools") for path in (root / folder).rglob("*.py")
             if "Simulation(self.network, [], []).graph" in path.read_text(encoding="utf-8")]
    assert not built, built
    network = generate_network(2, 2, 200.0)
    assert Simulation(network, [], []).graph.route("1W", "2E") is not None  # the same routes either way
    ends = [(a, b) for a in network.approach_nodes for b in network.approach_nodes if a != b]
    from traffic_simulator.lane_graph import Graph
    alone, run = Graph(network), Simulation(network, [], []).graph
    assert all([link.id for link in alone.route(*pair) or []] == [link.id for link in run.route(*pair) or []]
               for pair in ends)


if __name__ == "__main__":
    for name, check in list(globals().items()):
        if name.startswith("test_"):
            check()
    print("ok")

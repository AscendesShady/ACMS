"""Interchange checks. Run with `python tests/test_interchanges.py` (or the test monitor)."""

from math import atan2, degrees, hypot, pi

from traffic_simulator.interchanges import (
    build_diamond,
    build_slip_road,
    fit_slip_roads,
    build_trumpet,
    curve_speed_kmh,
    guess_stem,
    pair_legs,
    roads_left_at_hub,
)
from traffic_simulator.models import LANE_WIDTH_M
from traffic_simulator.network_generator import generate_network

# 3 x 4 grid, 500 m spacing. Junction 2,2 has neighbours 1,2 (W), 3,2 (E), 2,1 (S), 2,3 (N).
FREEWAY, CROSSROAD = ("1,2", "3,2"), ("2,3", "2,1")


def reachable(network, start):
    """Nodes reachable from start, obeying one-way roads."""
    step = {}
    for road in network.all_roads():
        step.setdefault(road.from_node, set()).add(road.to_node)
        if road.direction == "two-way":
            step.setdefault(road.to_node, set()).add(road.from_node)
    seen, todo = {start}, [start]
    while todo:
        for nxt in step.get(todo.pop(), ()):
            if nxt not in seen:
                seen.add(nxt)
                todo.append(nxt)
    return seen


def loop_of(network):
    """The trumpet loop: the curved ramp that turns the most (about 270 degrees)."""
    return max((road for road in network.roads.values() if road.shape),
               key=lambda road: abs(sum(turning(network.road_points(road)))))


def ramps(network, name):
    """One-way roads that start at one of the interchange's nodes."""
    return [road for road in network.roads.values()
            if road.direction == "one-way" and road.from_node.startswith(name + ".")]


def test_diamond_replaces_the_junction_and_exits_on_the_keep_side():
    for side, south_is_keep in (("right", True), ("left", False)):
        network = generate_network(3, 4, driving_side=side)
        centre = network.nodes["2,2"]
        name = build_diamond(network, FREEWAY, CROSSROAD)
        assert "2,2" not in network.nodes  # replaced: hub had nothing else left
        nodes = [node for node in network.nodes.values() if node.id.startswith(name + ".")]
        assert len(nodes) == 4 and sum(node.control == "signalised" for node in nodes) == 2
        west = min(nodes, key=lambda node: node.x)  # eastbound traffic diverges here
        exit_road = next(road for road in network.roads.values() if road.from_node == west.id
                         and road.direction == "one-way")
        terminal = network.nodes[exit_road.to_node]
        # Screen y points down: eastbound keeps south (y > centre) when driving on the right.
        assert (terminal.y > centre.y) == south_is_keep, side
        assert len(ramps(network, name)) == 4
        for leg in (*FREEWAY, *CROSSROAD):
            assert set(FREEWAY) | set(CROSSROAD) <= reachable(network, leg)


def test_a_diamonds_ramps_join_and_leave_only_their_own_carriageway():
    from traffic_simulator.simulation import Graph

    for side in ("left", "right"):
        network = generate_network(3, 4, driving_side=side)
        name = build_diamond(network, FREEWAY, CROSSROAD)
        graph = Graph(network)
        for node in (node for node in network.nodes.values() if node.id.startswith(name + ".")):
            if node.control == "uncontrolled":  # on the freeway: nothing crosses the other carriageway or turns back
                kinds = {turn.kind for link in graph.arriving[node.id] for turn in link.turns.values()}
                assert kinds <= {"straight", "kerb"}, (side, node.id, kinds)
        ends = sorted(network.approach_nodes)
        assert all(graph.route(o, d) is not None for o in ends for d in ends if o != d)  # every trip still has a way


def test_spui_meets_at_one_signal():
    network = generate_network(3, 4)
    name = build_diamond(network, FREEWAY, CROSSROAD, single_point=True)
    signals = [node for node in network.nodes.values()
               if node.id.startswith(name + ".") and node.control == "signalised"]
    assert len(signals) == 1
    point = signals[0].id
    assert sum(point in (road.from_node, road.to_node) and road.direction == "one-way"
               for road in network.roads.values()) == 4  # all ramps meet there
    assert {"2,1", "2,3"} <= {road.from_node for road in network.roads.values() if road.to_node == point} \
        | {road.to_node for road in network.roads.values() if road.from_node == point}
    assert len([i for i in network.nodes if i.startswith(name + ".")]) == 5  # curves, not bend nodes
    for road in network.roads.values():
        if road.shape:
            assert max(map(abs, turning(network.road_points(road)))) < 12, road.id


def test_trumpet_near_exit_is_upstream_and_loop_is_on_the_far_side():
    for side in ("right", "left"):
        network = generate_network(3, 4, driving_side=side)
        centre = network.nodes["2,2"]
        # Freeway along row 2 through 2,2; the terminating road comes from 2,3 (north).
        name = build_trumpet(network, FREEWAY, "2,3")
        stem_road = next(road for road in network.roads.values() if "2,3" in (road.from_node, road.to_node)
                         and (road.from_node.startswith(name) or road.to_node.startswith(name)))
        split = stem_road.to_node if stem_road.from_node == "2,3" else stem_road.from_node
        into_split = [road for road in network.roads.values() if road.to_node == split
                      and road.direction == "one-way"]
        assert len(into_split) == 1
        exit_start = network.nodes[into_split[0].from_node]
        # North carriageway traffic heads west when driving on the right, so it exits east.
        assert (exit_start.x > centre.x) == (side == "right"), side
        loop = loop_of(network)
        assert all(y > centre.y - 1e-6 for _, y in loop.shape)  # far (south) side of the freeway
        assert len([i for i in network.nodes if i.startswith(name + ".")]) == 5  # curves, not bend nodes
        for leg in (*FREEWAY, "2,3"):
            assert {*FREEWAY, "2,3"} <= reachable(network, leg)
        network.validate()


def turning(points):
    """Heading changes (degrees, signed) between consecutive segments."""
    headings = [atan2(by - ay, bx - ax) for (ax, ay), (bx, by) in zip(points, points[1:])]
    return [degrees((b - a + pi) % (2 * pi) - pi) for a, b in zip(headings, headings[1:])]


def test_trumpet_curves_are_smooth_and_follow_drags():
    network = generate_network(3, 4)
    build_trumpet(network, FREEWAY, "2,3")
    loop = loop_of(network)
    for road in network.roads.values():
        if road.shape:
            steps = turning(network.road_points(road))
            assert max(map(abs, steps)) < 12, (road.id, max(map(abs, steps)))  # no corners
    total = sum(turning(network.road_points(loop)))
    assert 255 < abs(total) < 285, total  # a true 270-degree loop
    # Dragging the loop's end keeps the curve attached and the same shape.
    start = network.nodes[loop.from_node]
    before = [(x - start.x, y - start.y) for x, y in loop.shape]
    network.move_nodes([loop.from_node, loop.to_node], 30, -20)
    after = [(x - start.x, y - start.y) for x, y in loop.shape]
    assert all(abs(a - c) < 1e-6 and abs(b - d) < 1e-6 for (a, b), (c, d) in zip(before, after))
    # Length edits scale a curve exactly.
    network.set_road_length(loop.id, loop.length_m * 1.2)
    assert abs(network.road_geometry_length(loop) - loop.length_m) < 1e-6
    network.validate()


def stored_turn(network, road):
    """Total turning (degrees) from the stored curvature: sum of curvature x segment length."""
    points = network.road_points(road)
    lengths = [hypot(bx - ax, by - ay) for (ax, ay), (bx, by) in zip(points, points[1:])]
    return degrees(sum(kappa * length for kappa, length in zip(road.curvature, lengths)))


def test_stored_curvature_matches_the_drawn_curve():
    for side in ("left", "right"):
        for stem in ("2,3", "2,1"):  # mirrored layouts flip every sign
            network = generate_network(3, 4, driving_side=side)
            build_trumpet(network, FREEWAY, stem)
            curved = [road for road in network.roads.values() if road.shape]
            assert curved and all(len(road.curvature) == len(road.shape) + 1 for road in curved)
            for road in curved:
                drawn = sum(turning(network.road_points(road)))
                assert abs(stored_turn(network, road) - drawn) < 3, (side, stem, road.id, drawn)
                points = network.road_points(road)
                for i, step in enumerate(turning(points)):  # every bend turns the stored way
                    kappa = (road.curvature[i] + road.curvature[i + 1]) / 2
                    if abs(step) > 1:
                        assert (kappa > 0) == (step > 0), (road.id, i, step, kappa)
            loop = loop_of(network)
            circle = [kappa for kappa in loop.curvature if abs(abs(kappa) - 1 / 55) < 1e-12]
            assert len(circle) > 50  # the loop's circle is stored as exactly 1/R
    network = generate_network(3, 4)
    build_diamond(network, FREEWAY, CROSSROAD, single_point=True)
    for road in (road for road in network.roads.values() if road.shape):
        assert abs(stored_turn(network, road) - sum(turning(network.road_points(road)))) < 3, road.id


def test_curvature_follows_edits():
    network = generate_network(3, 4)
    build_trumpet(network, FREEWAY, "2,3")
    loop = loop_of(network)
    turn, radius = stored_turn(network, loop), 1 / max(map(abs, loop.curvature))
    network.set_road_length(loop.id, loop.length_m * 1.2)  # scales the curve
    assert abs(1 / max(map(abs, loop.curvature)) - radius * 1.2) < 1e-6
    assert abs(stored_turn(network, loop) - turn) < 1e-6  # same bend, just bigger
    network.reverse_road(loop.id)
    assert abs(stored_turn(network, loop) + turn) < 1e-6  # driven the other way: opposite sign
    assert abs(sum(turning(network.road_points(loop))) + turn) < 3  # and the points still form the curve
    node = network.add_node(*loop.shape[len(loop.shape) // 2])
    first, second = network.split_road(loop.id, node.id)
    parts = [network.road(first), network.road(second)]
    assert abs(sum(stored_turn(network, part) for part in parts) + turn) < 3
    network.validate()


def test_ramp_curves_are_driveable():
    for lanes in (2, 6):
        network = generate_network(3, 4)
        build_trumpet(network, FREEWAY, "2,3", freeway_lanes=lanes)
        loop = loop_of(network)
        kappas = [abs(kappa) for kappa in loop.curvature]
        # Spiral lead-in: the bend builds up from straight to the circle, never jumping.
        assert kappas[0] < 0.1 / 55 and max(kappas) - 1 / 55 < 1e-12
        top = kappas.index(max(kappas))
        assert all(a <= b + 1e-12 for a, b in zip(kappas[:top], kappas[1:top + 1]))
        for road in (road for road in network.roads.values() if road.shape):
            radius = 1 / max(map(abs, road.curvature))
            assert radius > 50, (lanes, road.id, radius)  # no tight kinks, bell ramp included
            assert road.speed_limit_kmh <= curve_speed_kmh(radius), road.id


def test_curves_survive_save_and_load():
    import tempfile
    from pathlib import Path

    from traffic_simulator.network_io import load_network, save_network

    network = generate_network(3, 4)
    build_trumpet(network, FREEWAY, "2,3")
    path = Path(tempfile.mkdtemp()) / "trumpet.json"
    save_network(network, path)
    loaded = load_network(path)
    for road_id, road in network.roads.items():
        assert len(loaded.roads[road_id].shape) == len(road.shape)
        assert loaded.roads[road_id].curvature == road.curvature
        assert abs(loaded.road_geometry_length(loaded.roads[road_id]) - road.length_m) < 1e-6


def test_keep_leaves_existing_roads():
    network = generate_network(3, 4)
    before = set(network.roads)
    build_diamond(network, FREEWAY, CROSSROAD, replace=False)
    assert before <= set(network.roads) and "2,2" in network.nodes


def test_failed_build_changes_nothing():
    network = generate_network(3, 4, default_spacing=90)
    before = (set(network.nodes), set(network.roads))
    for build in (lambda: build_diamond(network, FREEWAY, CROSSROAD),
                  lambda: build_trumpet(network, FREEWAY, "2,3")):
        try:
            build()
        except ValueError as exc:
            assert "Not enough room" in str(exc)
        else:
            raise AssertionError("a 90 m grid is too tight for an interchange")
    assert (set(network.nodes), set(network.roads)) == before


def test_hub_kept_for_unselected_roads():
    network = generate_network(3, 4)
    assert roads_left_at_hub(network, [*FREEWAY, *CROSSROAD]) == ("2,2", [])
    hub, others = roads_left_at_hub(network, [*FREEWAY, "2,3"])  # trumpet: 2,1 not selected
    assert (hub, others) == ("2,2", ["2,1"])
    build_trumpet(network, FREEWAY, "2,3")
    assert "2,2" in network.nodes and any({"2,2", "2,1"} == {r.from_node, r.to_node}
                                          for r in network.roads.values())


def test_leg_guessing():
    network = generate_network(3, 4)
    assert guess_stem(network, ["1,2", "2,3", "3,2"]) == "2,3"
    first, second = pair_legs(network, ["1,2", "2,1", "3,2", "2,3"])
    assert {frozenset(first), frozenset(second)} == {frozenset(FREEWAY), frozenset(CROSSROAD)}


def layout(network):
    """Every road by where it runs (a two-way road either way), its curvature and its settings."""
    roads = set()
    for road in network.all_roads():
        points = tuple((round(x, 3), round(y, 3)) for x, y in network.road_points(road))
        curvature = tuple(round(kappa, 6) for kappa in road.curvature)
        if road.direction == "two-way" and points[::-1] < points:
            points, curvature = points[::-1], tuple(-kappa for kappa in curvature[::-1])
        roads.add((road.direction, road.road_type, points, curvature, road.lanes_per_direction,
                   round(road.speed_limit_kmh, 3), road.overpass))
    return roads


BUILDS = {"diamond": lambda network: build_diamond(network, FREEWAY, CROSSROAD),
          "SPUI": lambda network: build_diamond(network, FREEWAY, CROSSROAD, single_point=True),
          "trumpet": lambda network: build_trumpet(network, FREEWAY, "2,3")}


def test_a_new_driving_side_mirrors_each_interchange_into_that_sides_build():
    import tempfile
    from pathlib import Path

    from traffic_simulator.network_io import load_network, save_network

    for kind, build in BUILDS.items():
        for side, other in (("left", "right"), ("right", "left")):
            network = generate_network(3, 4, driving_side=side)
            name = build(network)
            original = layout(network)
            assert network.set_driving_side(other) == [name] and network.driving_side == other
            network.validate()
            fresh = generate_network(3, 4, driving_side=other)
            build(fresh)
            assert layout(network) == layout(fresh), kind  # exactly the interchange built for that side
            assert all(road.road_type == "ramp" for road in network.all_roads() if road.direction == "one-way")
            network.set_driving_side(side)
            assert layout(network) == original  # and back
    network = generate_network(3, 4)
    build_trumpet(network, FREEWAY, "2,3")
    with tempfile.TemporaryDirectory() as folder:
        save_network(network, Path(folder) / "trumpet.json")
        loaded = load_network(Path(folder) / "trumpet.json")
    assert loaded.interchanges == network.interchanges and loaded.set_driving_side("right") == ["TR1"]
    network.interchanges["TR1"] = [500.0, 500.0, 2.0, 0.0]  # not a unit direction
    try:
        network.validate()
    except ValueError:
        pass
    else:
        raise AssertionError("a bad interchange axis must not validate")
    network = generate_network(3, 4)
    assert network.set_driving_side("right") == [] and network.set_driving_side("right") == []  # none, and no change


def test_traffic_drives_a_flipped_interchange_as_a_fresh_one():
    from traffic_simulator.mode_behavior import PALETTE, VehicleMode
    from traffic_simulator.simulation import PairDemand, Simulation

    modes = [VehicleMode("Car", PALETTE[1])]
    pairs = [("2W", "2N"), ("2N", "2E"), ("2E", "2W"), ("2W", "2E"), ("2N", "2W"), ("2E", "2N")]
    results = []
    for flipped in (True, False):
        network = generate_network(3, 4, driving_side="left" if flipped else "right")
        build_trumpet(network, FREEWAY, "2,3")
        if flipped:
            network.set_driving_side("right")
        simulation = Simulation(network, [PairDemand(o, d, 300, None, [], "Poisson", (0, 100, 0)) for o, d in pairs],
                                modes, seed=3)
        overlaps = 0
        while simulation.time < 1200:
            simulation.step()
            for segment in {vehicle.segment for vehicle in simulation.vehicles}:
                overlaps += sum(front.s - front.length_m < rear.s - 1e-6
                                for rear, front in zip(segment.vehicles, segment.vehicles[1:]))
        results.append((simulation.arrived, overlaps))
    (flipped_arrived, flipped_overlaps), (fresh_arrived, fresh_overlaps) = results
    assert flipped_overlaps == fresh_overlaps == 0, results
    assert fresh_arrived > 400 and abs(flipped_arrived - fresh_arrived) <= 0.05 * fresh_arrived, results


def test_a_slip_road_takes_the_kerb_side_turn_from_the_kerbside_lane_only():
    from traffic_simulator.mode_behavior import PALETTE, VehicleMode
    from traffic_simulator.simulation import PairDemand, Simulation

    # Junction 1,1: 1W comes in from the west, 1,2-1,1 leaves north. Driving on the left that's the
    # kerb-side (left) turn; on the right the kerb side is the other way round.
    cases = (("left", ("1,2-1,1", "1W"), ("1W", "1N"), False), ("right", ("1W", "1,2-1,1"), ("1N", "1W"), False),
             ("left", ("1W", "1,2-1,1"), ("1W", "1N"), True))
    for side, roads, trip, single in cases:
        network = generate_network(2, 2, driving_side=side)
        before = len(network.roads)
        slip = network.roads[build_slip_road(network, *roads, single)]  # either order picks the same turn
        assert (slip.direction, slip.road_type, slip.lanes) == ("one-way", "ramp", 1 if single else 2)
        assert len(network.roads) == before + 3 and slip.width_m == slip.lanes * LANE_WIDTH_M
        assert all(network.nodes[node].control == "uncontrolled" for node in (slip.from_node, slip.to_node))
        network.validate()
        points = network.road_points(slip)  # a curve, not a straight line kinked at its nodes
        assert min(abs(kappa) for kappa in slip.curvature) > 0.005 and slip.length_m > 1.1 * hypot(
            points[-1][0] - points[0][0], points[-1][1] - points[0][1])
        for node_id, (a, b) in ((slip.from_node, points[:2]), (slip.to_node, points[-2:])):  # tangent to its roads
            for other in (r for r in network.all_roads() if r is not slip and node_id in (r.from_node, r.to_node)):
                line = network.road_points(other)
                p, q = line[:2] if other.from_node == node_id else line[-2:]
                u, v = (b[0] - a[0], b[1] - a[1]), (q[0] - p[0], q[1] - p[1])
                assert abs(u[0] * v[1] - u[1] * v[0]) / (hypot(*u) * hypot(*v)) < 0.1
        simulation = Simulation(network, [PairDemand(*trip, 600, None, [], "Poisson", (0, 100, 0))],
                                [VehicleMode("Car", PALETTE[1])], seed=1)
        graph = simulation.graph
        assert f"{slip.id}>" in [link.id for link in graph.route(*trip)]
        into = [turn for link in graph.arriving[slip.from_node] for (lane, out), turn in link.turns.items()
                if out == f"{slip.id}>" and turn.kind == "kerb"]
        assert [(turn.lane_in.index, turn.permitted) for turn in into] == [(0, True), (1, False)]
        merge = [(lane, turn.kind, turn.permitted) for (lane, _), turn in graph.links[f"{slip.id}>"].turns.items()
                 if turn.kind == "kerb"]
        assert merge == ([(0, "kerb", True)] if single else [(0, "kerb", True), (1, "kerb", False)])
        while simulation.time < 600:
            simulation.step()
        assert simulation.arrived > 50


def test_a_slip_road_is_refused_without_a_kerb_side_turn_or_room():
    network = generate_network(2, 2)
    before = network.as_records()
    for roads, why in ((("1W", "1,1-2,1"), "kerb-side turn"),  # straight on
                       (("1W", "2,2-2,1"), "meet at one junction"),
                       (("1,2-1,1", "1S"), "kerb-side turn")):  # left driving: north arm to south end is straight on
        try:
            build_slip_road(network, *roads)
        except ValueError as exc:
            assert why in str(exc), exc
        else:
            raise AssertionError(roads)
    short = generate_network(2, 2, default_spacing=50.0)
    records = short.as_records()
    try:
        build_slip_road(short, "1,2-1,1", "1W")  # the kerb-side turn, but 50 m roads leave no room
    except ValueError as exc:
        assert "room" in str(exc), exc
    else:
        raise AssertionError("built on 50 m roads")
    assert short.as_records() == records
    assert network.as_records() == before  # nothing changed
    road = network.roads["1,1-2,1"]
    road.single_lane = True  # only a one-way road of one lane per direction can be
    try:
        network.validate()
    except ValueError as exc:
        assert "single-lane" in str(exc)
    else:
        raise AssertionError("a two-way single-lane road validated")



def test_a_slip_road_moves_out_of_a_new_roundabout_and_traffic_flows_through_both():
    from collections import Counter

    from traffic_simulator.mode_behavior import PALETTE, VehicleMode
    from traffic_simulator.simulation import PairDemand, Simulation

    network = generate_network(2, 2)
    slip = build_slip_road(network, "1W", "1,2-1,1")
    network.nodes["1,1"].control = "roundabout"
    assert fit_slip_roads(network, "1,1") == [slip] and fit_slip_roads(network, "1,1") == []  # once is enough
    network.validate()
    hub, road = network.nodes["1,1"], network.roads[slip]
    circle = network.lane_inset("1,1", network.roads["1,1-2,1"])  # the give-way line, just outside the circle
    for node_id in (road.from_node, road.to_node):  # the arms keep 40 m of lane between the slip and the circle
        assert hypot(network.nodes[node_id].x - hub.x, network.nodes[node_id].y - hub.y) >= circle + 7 + 40
    assert min(hypot(x - hub.x, y - hub.y) for x, y in network.road_points(road)) > circle + LANE_WIDTH_M
    assert len(network.roads) == 7 and all(road.length_m > 40 for road in network.all_roads())
    pairs = [PairDemand(o, d, 400, None, [], "Poisson", (0, 100, 0))
             for o, d in (("1W", "1N"), ("1W", "1E"), ("1S", "1N"), ("1N", "1W"))]
    simulation = Simulation(network, pairs, [VehicleMode("Car", PALETTE[1])], seed=2)  # seed 1 draws 174 for 1W->1N
    assert f"{slip}>" in [link.id for link in simulation.graph.route("1W", "1N")]
    arrived = Counter()
    while simulation.time < 1800:
        before = {vehicle.id: vehicle for vehicle in simulation.vehicles}
        simulation.step()
        still = {vehicle.id for vehicle in simulation.vehicles}
        arrived.update((v.origin, v.destination) for key, v in before.items() if key not in still)
    assert all(arrived[(pair.origin, pair.destination)] > 170 for pair in pairs), arrived  # about 200 each

    short = generate_network(2, 2, default_spacing=100.0)  # room for a slip at a signal, not at a roundabout
    build_slip_road(short, "1W", "1,2-1,1")
    records = short.as_records()
    short.nodes["1,1"].control = "roundabout"
    try:
        fit_slip_roads(short, "1,1")
    except ValueError as exc:
        assert "room" in str(exc), exc
    else:
        raise AssertionError("moved on 100 m roads")
    short.nodes["1,1"].control = "signalised"
    assert short.as_records() == records


def test_a_refused_interchange_or_slip_road_leaves_the_network_untouched():
    # validate() ran only after the old roads were gone and the new ones built, so a refusal kept them; a slip road
    # never validated, so a turn pocket could end up on a stub too short for it.
    network = generate_network(3, 4)
    network.nodes["1,2"].control = "roundabout"  # a 4-lane freeway into it: more entry lanes than NCHRP 672 designs
    network.validate()
    records = network.as_records()
    for build in (lambda: build_trumpet(network, FREEWAY, "2,1", freeway_lanes=4),
                  lambda: build_diamond(network, FREEWAY, CROSSROAD, freeway_lanes=4),
                  lambda: build_diamond(network, FREEWAY, CROSSROAD, freeway_lanes=4, single_point=True)):
        try:
            build()
        except ValueError:
            pass
        else:
            raise AssertionError("built into a roundabout it overloads")
        assert network.as_records() == records
    pocketed = generate_network(3, 4)
    pocketed.roads["1,2-2,2"].pockets = {"2,2": {"lanes": 1, "length_m": 120.0, "side": "kerb"}}
    pocketed.validate()
    records = pocketed.as_records()
    try:
        build_slip_road(pocketed, "1,2-2,2", "2,3-2,2")
    except ValueError as exc:
        assert "turn pockets" in str(exc), exc
    else:
        raise AssertionError("a 120 m pocket on the slip road's stub")
    assert pocketed.as_records() == records


if __name__ == "__main__":
    for name, check in list(globals().items()):
        if name.startswith("test_"):
            check()
    print("ok")

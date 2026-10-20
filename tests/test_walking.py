"""The walking network (walking.py) and its parts in the model: footpaths beside roads, paths of their own, the
pedestrian OD matrix. Run with `python tests/test_walking.py` (or the test monitor)."""

import json
from math import hypot
import tempfile
from copy import deepcopy
from pathlib import Path

from traffic_simulator.geometry import distance_to_segment, polyline_length
from traffic_simulator.models import CYCLE_LANE_M, FOOTPATH_M
from traffic_simulator.network_generator import generate_network
from traffic_simulator.network_io import load_network, save_network
from traffic_simulator.walking import WalkGraph


def refused(network, edit, words: str) -> None:
    trial = deepcopy(network)
    edit(trial)
    try:
        trial.validate()
    except ValueError as exc:
        assert words in str(exc), str(exc)
    else:
        raise AssertionError(f"accepted, though {words!r}")


def test_footpaths_lie_beside_the_road_and_its_cycle_lane_and_swap_sides_when_it_turns_round():
    network = generate_network(2, 2)
    road = network.roads["1,1-2,1"]
    road.footpaths = ["left", "right"]
    network.validate()
    a, b = network.road_points(road)[0], network.road_points(road)[-1]

    def beside(side: str) -> float:
        line = network.footpath_line(road, side)
        return distance_to_segment(line[len(line) // 2], a, b)

    assert abs(beside("left") - (road.width_m / 2 + FOOTPATH_M / 2)) < 1e-6
    road.cycle_lanes = [road.to_node]  # its traffic's kerb side moves that footpath out by the cycle lane
    side = network.kerb_side(road, road.to_node)
    assert abs(beside(side) - (road.width_m / 2 + CYCLE_LANE_M + FOOTPATH_M / 2)) < 1e-6
    road.footpaths = ["left"]
    line = network.footpath_line(road, "left")
    network.reverse_road(road.id)  # the same paving, now on its "right"
    assert road.footpaths == ["right"] and network.footpath_line(road, "right")[::-1] == line
    refused(network, lambda n: setattr(n.roads["1,1-2,1"], "footpaths", ["left", "left"]), "footpaths go on")
    refused(network, lambda n: setattr(n.roads["1,1-2,1"], "footpaths", ["kerb"]), "footpaths go on")


def test_walks_take_the_shortest_way_on_footpaths_and_paths_and_none_where_nothing_joins():
    network = generate_network(3, 3)
    graph = WalkGraph(network)
    assert graph.route("1W", "3E") is None  # no footpath anywhere
    for road in network.all_roads():
        road.footpaths = ["left"]
    line = WalkGraph(network).route("1W", "3E")[0]
    ends = network.node("1W"), network.node("3E")
    assert hypot(line[0][0] - ends[0].x, line[0][1] - ends[0].y) < 10  # from a corner at each end
    assert hypot(line[-1][0] - ends[1].x, line[-1][1] - ends[1].y) < 10
    along_roads = polyline_length(line)
    corner, far = network.node("1,1"), network.node("3,3")
    by_roads = polyline_length(WalkGraph(network).route("1,1", "3,3")[0])
    way = network.add_walkway("1,1", "3,3", [[(corner.x + far.x) / 2, (corner.y + far.y) / 2]])  # a short cut
    network.validate()
    assert way.id == "P1" and polyline_length(WalkGraph(network).route("1,1", "3,3")[0]) < by_roads
    back = polyline_length(WalkGraph(network).route("3,3", "1,1")[0])  # the path, from corner to corner
    assert polyline_length(network.walkway_points(way)) - 40 < back <= polyline_length(network.walkway_points(way))
    assert polyline_length(WalkGraph(network).route("1W", "3E")[0]) <= along_roads + 1e-6
    for bad in (lambda: network.add_walkway("1,1", "1,1"), lambda: network.add_walkway("1,1", "2,2", [[float("nan"), 0]])):
        try:
            bad()
        except ValueError:
            pass
        else:
            raise AssertionError("a bad path was added")
    try:
        network.add_walkway("1,1", "9,9")
    except KeyError:
        pass
    else:
        raise AssertionError("a path to an unknown node")
    assert list(network.walkways) == ["P1"]


def test_paths_and_pedestrian_demand_are_checked_saved_and_go_with_their_nodes():
    network = generate_network(2, 2)
    network.add_walkway("1,1", "2,2", [[30.0, 40.0]])
    network.walk_demand = {"1W->2E": 600.0}
    network.validate()
    for bad, words in (({"1W->1W": 5.0}, "two different boundary ends"), ({"1,1->2E": 5.0}, "boundary ends"),
                       ({"1W->2E": -1.0}, "0 to"), ({"1W->2E": 1e9}, "0 to")):
        refused(network, lambda n, bad=bad: setattr(n, "walk_demand", bad), words)
    refused(network, lambda n: setattr(n.walkways["P1"], "end", "9,9"), "Path P1")
    path = Path(tempfile.mkdtemp()) / "walking.json"
    save_network(network, path)
    loaded = load_network(path)
    assert loaded.walkways == network.walkways and loaded.walk_demand == network.walk_demand
    document = json.loads(path.read_text(encoding="utf-8"))
    for key in ("walkways", "walk_demand"):
        del document[key]  # a file from before pedestrians
    path.write_text(json.dumps(document), encoding="utf-8")
    assert not load_network(path).walkways and not load_network(path).walk_demand
    network.delete_node("2,2")  # its path goes with it
    assert not network.walkways
    network.delete_road(next(road.id for road in network.approach_roads.values() if "1W" in (road.from_node, road.to_node)))
    assert not network.walk_demand  # and the demand from an end that's gone
    network.validate()


def test_walkers_keep_to_their_corner_and_cross_an_arm_in_a_gap_or_on_a_crossing():
    network = generate_network(2, 1)  # one road, 1,2 to 1,1, with ends west and east of each junction
    for road in network.all_roads():
        road.footpaths = ["left", "right"]
    graph = WalkGraph(network)
    road = network.roads["1,2-1,1"]
    length = network.road_geometry_length(road)
    line, crossed = graph.route("2W", "1E")  # from the west of the road's one end to the east of its other
    assert [(event.kind, event.road) for event in crossed] == [("gap", road.id)]  # across it once
    event = crossed[0]
    assert 0 < event.s0 < event.s1 <= polyline_length(line) and abs(event.s1 - event.s0 - event.length_m) < 1e-6
    assert min(event.distance_m, length - event.distance_m) < 20  # at a junction's arm, not part way along
    assert all(e.road != road.id for e in graph.route("2W", "2E")[1])  # the same side all the way: never across
    network.add_crossing(road.id, length / 2, "zebra")  # right of way, no wait: dearer to walk round to than a gap?
    line, crossed = WalkGraph(network).route("2W", "1E")
    assert [(event.kind, event.crossing, round(event.distance_m)) for event in crossed] == [
        ("zebra", "X1", round(length / 2))]
    network.pedestrian_crossings["X1"].kind = "overbridge"  # its stairs: dearer than the gap at the junction
    assert [event.kind for event in WalkGraph(network).route("2W", "1E")[1]] == ["gap"]
    for other in network.all_roads():
        if other.id != road.id:
            other.footpaths = ["left"]  # one side only: the arms are still crossed in gaps
    assert WalkGraph(network).route("2W", "1E") is not None


def test_crossings_are_checked_follow_their_road_and_are_saved():
    network = generate_network(3, 4)
    road = network.roads["1,2-2,2"]
    length = network.road_geometry_length(road)
    try:
        network.add_crossing(road.id, 100.0)
    except ValueError as exc:
        assert "footpaths on both sides" in str(exc)
    else:
        raise AssertionError("a crossing between no footpaths")
    road.footpaths = ["left", "right"]
    zebra = network.add_crossing(road.id, 100.0)
    for at, kind, words in ((5.0, "zebra", "at least 20 m"), (length - 5.0, "zebra", "at least 20 m"),
                            (105.0, "zebra", "Too near crossing X1"), (300.0, "tunnel", "zebra or an overbridge")):
        try:
            network.add_crossing(road.id, at, kind)
        except ValueError as exc:
            assert words in str(exc), str(exc)
        else:
            raise AssertionError(at)
    assert list(network.pedestrian_crossings) == ["X1"]
    refused(network, lambda n: setattr(n.roads["1,2-2,2"], "footpaths", ["left"]), "both sides")  # it needs both
    network.reverse_road(road.id)  # where it was, measured from the other end
    assert abs(zebra.distance_m - (length - 100.0)) < 1e-6
    first, second = network.split_road(road.id, network.add_node(250, 500).id)
    assert zebra.road in (first, second) and network.crossing_problem(zebra) is None
    network.validate()
    path = Path(tempfile.mkdtemp()) / "crossings.json"
    save_network(network, path)
    assert load_network(path).pedestrian_crossings == network.pedestrian_crossings
    network.delete_road(zebra.road)  # it goes with its road
    assert not network.pedestrian_crossings


def test_spawn_points_join_their_nearest_footpath_and_walk_between_them():
    from traffic_simulator.mode_behavior import VehicleModes
    from traffic_simulator.simulation import Simulation
    from traffic_simulator.walking import footpath_join

    network = generate_network(3, 3)
    a = network.node("1,1")
    s1, s2 = network.add_spawn_point(a.x + 120, a.y + 40), network.add_spawn_point(a.x + 300, a.y + 30)
    assert (s1.id, s2.id) == ("S1", "S2") and WalkGraph(network).route((s1.x, s1.y), (s2.x, s2.y)) is None  # no footpath
    for road in network.all_roads():
        road.footpaths = ["left", "right"]
    graph = WalkGraph(network)
    join = footpath_join(network, s1.x, s1.y)
    line, crossed = graph.route((s1.x, s1.y), (s2.x, s2.y))
    assert line[0] == (s1.x, s1.y) and line[1] == join and line[-1] == (s2.x, s2.y)  # straight to its footpath
    # both beside the same stretch of footpath: along it between their joins, no detour to its ends
    assert not crossed and abs(polyline_length(line) - (hypot(*(join[0] - s1.x, join[1] - s1.y)) + 180
                                                         + hypot(*(footpath_join(network, s2.x, s2.y)[0] - s2.x,
                                                                   footpath_join(network, s2.x, s2.y)[1] - s2.y)))) < 1e-6
    far = network.add_spawn_point(network.node("3,3").x - 150, network.node("3,3").y - 70)
    assert WalkGraph(network).route((s1.x, s1.y), (far.x, far.y))[1]  # across roads on the way: crossed in gaps
    network.spawn_demand = {"S1->S3": 600.0}
    network.validate()
    simulation = Simulation(network, [], VehicleModes().modes, seed=2)
    while simulation.time < 300:
        simulation.step()
    assert simulation.walk_spawned and not simulation.walk_unreachable
    assert all(walker.points[0] == (s1.x, s1.y) and walker.points[-1] == (far.x, far.y) for walker in simulation.walkers)
    for bad, words in (({"S1->S1": 5.0}, "two different spawn points"), ({"S1->S9": 5.0}, "two different spawn points"),
                       ({"S1->S3": -1.0}, "0 to")):
        refused(network, lambda n, bad=bad: setattr(n, "spawn_demand", bad), words)
    path = Path(tempfile.mkdtemp()) / "spawn.json"
    save_network(network, path)
    loaded = load_network(path)
    assert loaded.spawn_points == network.spawn_points and loaded.spawn_demand == network.spawn_demand
    network.delete_spawn_point("S3")  # its walking goes with it
    assert "S3" not in network.spawn_points and not network.spawn_demand


if __name__ == "__main__":
    for name, check in list(globals().items()):
        if name.startswith("test_"):
            check()
    print("ok")

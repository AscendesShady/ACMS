"""Model checks. Run with `python tests/test_models.py` (or pytest)."""

from math import hypot

from traffic_simulator.network_generator import generate_network


def test_graph_numbering_and_end_names():
    network = generate_network(3, 4)
    bottom_left = network.nodes["1,1"]
    assert (bottom_left.row, bottom_left.column) == (2, 0)
    assert network.nodes["1,3"].row == 0  # top-left
    assert {"1E", "3E", "1W", "1N", "4N", "1S", "4S"} <= set(network.approach_nodes)
    assert network.approach_nodes["1E"].y == bottom_left.y  # 1E is the bottom row


def test_short_edge_forms_trapezoid():
    network = generate_network(2, 2)
    approach_before = network.road("2E").length_m
    network.set_road_length("1,2-2,2", 100)  # top edge
    assert round(network.road("1,2-2,2").length_m) == 100
    assert round(network.road("1,1-2,1").length_m) == 500  # bottom edge untouched
    assert round(network.road("2,2-2,1").length_m) == round(hypot(400, 500))  # side angles
    assert round(network.road("1,2-1,1").length_m) == 500
    assert network.road("2E").length_m == approach_before  # end moved with its node
    network.validate()


def test_add_and_delete_edge():
    network = generate_network(2, 2)
    network.delete_road("1,1-2,1")
    assert "1,1-2,1" not in network.roads
    road = network.add_road("1,1", "2,2")
    assert round(road.length_m) == round(hypot(500, 500))
    for bad in (("2,2", "1,1"), ("1,1", "1,1"), ("1,1", "1E")):
        try:
            network.add_road(*bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"add_road{bad} should fail")
    network.delete_road("1E")
    assert "1E" not in network.approach_nodes and "1E" not in network.approach_roads
    network.validate()


def test_lane_defaults_reach_every_road():
    network = generate_network(2, 2, lanes_per_direction=3, lane_changing=False)
    assert all(road.lanes_per_direction == 3 and not road.lane_changing
               for road in network.all_roads())
    network.delete_road("1,1-2,1")
    road = network.add_road("1,1", "2,1")  # edits inherit the creation defaults
    assert road.lanes_per_direction == 3 and not road.lane_changing


def test_drag_stretches_only_connecting_roads():
    network = generate_network(2, 2)
    before = {node.id: (node.x, node.y) for node in network.all_nodes()}
    assert network.move_nodes(["2,2"], 100, -50)
    moved = network.nodes["2,2"]
    assert (moved.x, moved.y) == (before["2,2"][0] + 100, before["2,2"][1] - 50)
    assert all((node.x, node.y) == before[node.id]
               for node in network.all_nodes() if node.id != "2,2")  # rest of the model stays
    assert round(network.road("1,2-2,2").length_m) == round(hypot(600, 50))
    assert round(network.road("2E").length_m) == round(hypot(240 - 100, 50))  # end stays put
    assert round(network.road("1,1-2,1").length_m) == 500
    # Dragging onto a neighbour is refused and leaves the model untouched.
    target = network.nodes["2,1"]
    assert not network.move_nodes(["2,2"], target.x - moved.x, target.y - moved.y)
    assert (moved.x, moved.y) == (before["2,2"][0] + 100, before["2,2"][1] - 50)
    network.validate()


def test_added_nodes_are_numbered_and_connectable():
    import tempfile
    from pathlib import Path

    from traffic_simulator.network_io import load_network, save_network

    network = generate_network(2, 2)
    first, second = network.add_node(250, -300), network.add_node(250, -600)
    assert (first.id, second.id) == ("N1", "N2")
    assert network.node_label(first) == "N1" and network.node_label(network.nodes["1,1"]) == "(1,1)"
    network.add_road("1,2", "N1")
    between = network.add_road("N1", "N2")
    network.set_road_length(between.id, 150)  # off-grid: only the far node slides
    assert round(between.length_m) == 150 and network.nodes["N1"].y == -300
    path = Path(tempfile.mkdtemp()) / "net.json"
    save_network(network, path)
    loaded = load_network(path)
    assert loaded.nodes["N2"].row is None and "N1-N2" in loaded.roads
    assert loaded.add_node(0, 0).id == "N3"  # numbering continues after reload


def test_a_generated_network_times_its_signals_by_its_default_method():
    import tempfile
    from pathlib import Path

    from traffic_simulator.network_io import load_network, save_network

    network = generate_network(2, 2, signal_method="transyt")
    assert {node.signal_method for node in network.nodes.values()} == {"transyt"}
    assert network.add_node(0, 0).signal_method == "transyt"  # new junctions get the default too
    path = Path(tempfile.mkdtemp()) / "net.json"
    save_network(network, path)
    assert load_network(path).default_signal_method == "transyt"
    try:
        generate_network(2, 2, signal_method="guess")
    except ValueError as exc:
        assert "webster" in str(exc)
    else:
        raise AssertionError("an unknown timing method must be refused")


def test_new_ends_are_named_by_side_and_position():
    network = generate_network(3, 4)  # east ends: 1E at y=1000, 2E at y=500, 3E at y=0
    east_x = network.nodes["4,2"].x + 600
    assert network.add_end("4,2", east_x, 750).id == "1.5E"  # between 1E and 2E
    assert network.add_end("4,2", east_x, 875).id == "1.25E"  # between 1E and 1.5E
    assert network.add_end("4,3", east_x, -200).id == "4E"  # past the last
    assert network.add_end("4,1", east_x, 1200).id == "0.5E"  # before the first
    assert network.add_end("2,3", 750, -400).id == "2.5N"  # north ends count from the left
    assert network.add_end("1,2", -600, 250).id == "2.5W"
    assert network.add_end("4,2", 1740, 750).id == "5S"  # mostly south, past 4S
    end = network.approach_roads["1.5E"]
    assert end.from_node == "4,2" and round(end.length_m) == round(hypot(600, 250))
    network.validate()


def test_delete_node_takes_its_roads_and_dangling_ends():
    network = generate_network(3, 4)
    network.delete_node("1,1")  # corner: 2 grid roads and ends 1W, 1S
    assert "1,1" not in network.nodes
    assert not any("1,1" in (road.from_node, road.to_node) for road in network.all_roads())
    assert "1W" not in network.approach_nodes and "1S" not in network.approach_nodes
    assert "1E" in network.approach_nodes and len(network.roads) == 15
    network.delete_node("3E")  # an end on its own
    assert "3E" not in network.approach_nodes and "3E" not in network.approach_roads
    network.validate()


def test_signal_placeholder_settings_are_validated():
    network = generate_network(2, 2)
    node = network.nodes["1,1"]
    assert (node.signal_phases, node.signal_method, node.green_times_s) == (2, "webster", [30.0, 30.0])
    node.signal_phases, node.green_times_s = 4, [30.0, 25.0, 30.0, 25.0]
    network.validate()
    for phases, greens in ((3, [30.0] * 3), (4, [30.0, 30.0]), (2, [30.0, 0.0])):
        node.signal_phases, node.green_times_s = phases, greens
        try:
            network.validate()
        except ValueError:
            continue
        raise AssertionError(f"{phases} phases with {greens} should be rejected")


def test_only_finite_numbers_pass_validation_or_a_file():
    import json
    import tempfile
    from copy import deepcopy
    from pathlib import Path

    from traffic_simulator.models import BusStop
    from traffic_simulator.network_io import load_network, save_network

    network = generate_network(2, 2)
    network.bus_stops["BS1"] = BusStop("BS1", "1,1-2,1", 100.0)
    network.roads["1,1-2,1"].shape, network.roads["1,1-2,1"].curvature = [(250.0, 10.0)], [0.0, 0.0]
    network.validate()
    places = {"node": lambda n: n.nodes["1,1"], "road": lambda n: n.roads["1,1-2,1"], "stop": lambda n: n.bus_stops["BS1"],
              "background": lambda n: n.background, "network": lambda n: n}
    for bad in (float("nan"), float("inf"), float("-inf"), True):
        for place, name, value in (("node", "x", bad), ("node", "supply_vph", bad), ("node", "cycle_max_s", bad),
                                   ("node", "green_times_s", [30.0, bad]), ("road", "length_m", bad),
                                   ("road", "speed_limit_kmh", bad), ("road", "shape", [(250.0, bad)]),
                                   ("road", "curvature", [0.0, bad]), ("stop", "dwell_s", bad),
                                   ("background", "opacity", bad), ("network", "default_spacing", bad)):
            changed = deepcopy(network)
            setattr(places[place](changed), name, value)
            try:
                changed.validate()
            except ValueError:
                continue
            raise AssertionError(f"{place} {name} = {value} passed")
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "network.json"
        save_network(network, path)
        document = json.loads(path.read_text(encoding="utf-8"))
        document["nodes"][0]["x"] = float("nan")
        path.write_text(json.dumps(document), encoding="utf-8")  # NaN, as Python writes it
        try:
            load_network(path)
        except ValueError as exc:
            assert "NaN" in str(exc)
        else:
            raise AssertionError("a file with NaN opened")


def test_crossings_are_found_but_shared_junctions_are_not():
    network = generate_network(3, 4)  # rows y = 0, 500, 1000; columns x = 0, 500, 1000, 1500
    a, b = network.nodes["1,1"], network.nodes["2,3"]  # (0, 1000) -> (500, 0)
    found = network.crossings([(a.x, a.y), (b.x, b.y)], {"1,1", "2,3"})
    assert [road_id for *_, road_id in found] == ["1,2-2,2"], found  # crosses row y=500 once
    distance, x, y, _ = found[0]
    assert abs(x - 250) < 1e-6 and abs(y - 500) < 1e-6 and abs(distance - hypot(250, 500)) < 1e-6
    # Along an existing road, sharing its nodes: not a crossing.
    c, d = network.nodes["1,2"], network.nodes["2,2"]
    assert network.crossings([(c.x, c.y), (d.x, d.y)], {"1,2", "2,2"}) == []


def test_split_road_keeps_settings_and_direction():
    network = generate_network(3, 4)
    road = network.roads["1,2-2,2"]
    road.direction, road.lanes_per_direction, road.speed_limit_kmh = "one-way", 3, 70
    node = network.add_node(200, 500)
    first, second = network.split_road("1,2-2,2", node.id)
    assert "1,2-2,2" not in network.roads and (first, second) == ("1,2-N1", "N1-2,2")
    for part in (network.roads[first], network.roads[second]):
        assert (part.direction, part.lanes_per_direction, part.speed_limit_kmh) == ("one-way", 3, 70)
    assert round(network.roads[first].length_m) == 200 and round(network.roads[second].length_m) == 300
    # A boundary road keeps its name on the part that reaches its end.
    end = network.nodes["4,2"]
    cut = network.add_node(end.x + 100, end.y)
    inner, boundary = network.split_road("2E", cut.id)
    assert boundary == "2E" and network.approach_roads["2E"].from_node == cut.id
    assert network.roads[inner].from_node == "4,2" and network.roads[inner].to_node == cut.id
    network.validate()


def test_turn_pockets_are_validated_and_stay_at_their_ends():
    from copy import deepcopy

    network = generate_network(3, 4)  # 500 m blocks
    road = network.roads["1,2-2,2"]
    road.pockets = {"2,2": {"lanes": 1, "length_m": 60.0, "side": "kerb"},
                    "1,2": {"lanes": 2, "length_m": 40.0, "side": "offside"}}
    network.validate()
    assert road.arrival_ends() == ("2,2", "1,2")

    def pocket(**changes):
        return {"lanes": 1, "length_m": 50.0, "side": "kerb", **changes}

    def refused(edit, words: str) -> None:
        trial = deepcopy(network)
        edit(trial, trial.roads["1,2-2,2"])
        try:
            trial.validate()
        except ValueError as exc:
            assert words in str(exc), str(exc)
        else:
            raise AssertionError(f"accepted, though {words!r}")

    refused(lambda n, r: r.pockets.update({"2,2": pocket(length_m=200.0)}), "15 to 150 m")
    for bad in (pocket(lanes=3), pocket(lanes=True), pocket(lanes=1.0), pocket(side="left"), {"lanes": 1}):
        refused(lambda n, r: r.pockets.update({"2,2": bad}), "needs lanes (1 or 2)")
    refused(lambda n, r: n.approach_roads["1W"].pockets.update({"1W": pocket()}), "not at 1W")  # a boundary end
    refused(lambda n, r: setattr(n.nodes["2,2"], "control", "roundabout"), "not at 2,2")
    refused(lambda n, r: setattr(r, "lane_changing", False), "needs lane changing")
    refused(lambda n, r: setattr(r, "direction", "one-way"), "doesn't arrive at 1,2")  # its traffic leaves 1,2
    refused(lambda n, r: r.pockets.update({"2,2": pocket(length_m=150.0), "1,2": pocket(length_m=150.0)})
            or n.set_road_length(r.id, 300.0), "too short")
    refused(lambda n, r: setattr(n.add_bus_stop(r.id), "distance_m", 420.0), "bay is beside")  # move_bus_stop refuses it
    # Kept by the end it is at: reversing the road keeps them; splitting it leaves each on the part reaching its end.
    network.reverse_road("1,2-2,2")
    network.validate()
    assert road.pockets["2,2"]["side"] == "kerb" and road.pockets["1,2"]["side"] == "offside"
    first, second = network.split_road("1,2-2,2", network.add_node(200, 500).id)
    assert network.roads[first].pockets == {"2,2": pocket(length_m=60.0)}
    assert network.roads[second].pockets == {"1,2": {"lanes": 2, "length_m": 40.0, "side": "offside"}}
    network.validate()
    network.set_driving_side("right")  # relative to the driving side: the same pockets serve the same turns
    assert network.roads[first].pockets == {"2,2": pocket(length_m=60.0)}
    network.validate()


def test_turn_pockets_are_saved_and_older_files_open_without_them():
    import json
    import tempfile
    from pathlib import Path

    from traffic_simulator.network_io import load_network, save_network

    network = generate_network(2, 2)
    network.roads["1,1-2,1"].pockets = {"2,1": {"lanes": 2, "length_m": 45.5, "side": "offside"}}
    path = Path(tempfile.mkdtemp()) / "pockets.json"
    save_network(network, path)
    assert load_network(path).roads["1,1-2,1"].pockets == network.roads["1,1-2,1"].pockets
    document = json.loads(path.read_text(encoding="utf-8"))
    for record in document["roads"] + document["approach_roads"]:
        del record["pockets"]  # a file from before turn pockets
    path.write_text(json.dumps(document), encoding="utf-8")
    assert all(road.pockets == {} for road in load_network(path).all_roads())


def test_cycle_lanes_are_validated_kept_through_splits_and_saved():
    import json
    import tempfile
    from copy import deepcopy
    from pathlib import Path

    from traffic_simulator.geometry import distance_to_segment
    from traffic_simulator.models import CYCLE_LANE_M, LANE_WIDTH_M
    from traffic_simulator.network_io import load_network, save_network

    network = generate_network(3, 4)
    road = network.roads["1,2-2,2"]
    road.cycle_lanes = ["2,2", "1,2"]
    network.validate()
    line = network.cycle_lane_line(road, "2,2")  # beyond the kerb lane of the traffic arriving at 2,2
    centre = network.road_points(road)
    assert abs(distance_to_segment(line[len(line) // 2], centre[0], centre[-1])
               - (road.lanes_each_way * LANE_WIDTH_M + CYCLE_LANE_M / 2)) < 1e-6
    for bad in (["9,9"], ["2,2", "2,2"]):
        trial = deepcopy(network)
        trial.roads["1,2-2,2"].cycle_lanes = bad
        try:
            trial.validate()
        except ValueError as exc:
            assert "cycle lane" in str(exc)
        else:
            raise AssertionError(bad)
    trial = deepcopy(network)
    trial.roads["1,2-2,2"].direction = "one-way"  # its traffic no longer arrives at 1,2
    try:
        trial.validate()
    except ValueError as exc:
        assert "cycle lane" in str(exc)
    else:
        raise AssertionError("a cycle lane for traffic that doesn't come")
    first, second = network.split_road("1,2-2,2", network.add_node(200, 500).id)  # both parts keep both ways
    middle = network.roads[first].from_node if network.roads[first].from_node not in ("1,2", "2,2") \
        else network.roads[first].to_node
    assert sorted(network.roads[first].cycle_lanes + network.roads[second].cycle_lanes) == \
        sorted(["1,2", "2,2", middle, middle])
    network.validate()
    path = Path(tempfile.mkdtemp()) / "cycle.json"
    save_network(network, path)
    assert load_network(path).roads[first].cycle_lanes == network.roads[first].cycle_lanes
    document = json.loads(path.read_text(encoding="utf-8"))
    for record in document["roads"] + document["approach_roads"]:
        record.pop("cycle_lanes", None)  # a file from before cycle lanes
    path.write_text(json.dumps(document), encoding="utf-8")
    assert all(road.cycle_lanes == [] for road in load_network(path).all_roads())


def test_a_network_with_no_ends_reopens_with_none():
    import json
    import tempfile
    from pathlib import Path

    from traffic_simulator.network_io import load_network, save_network

    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "net.json"
        network = generate_network(2, 2)
        for road_id in list(network.approach_roads):
            network.delete_road(road_id)
        save_network(network, path)
        assert not load_network(path).approach_nodes  # saved with none: opens with none
        network.delete_node("1,1")  # a grid position gone too
        save_network(network, path)
        assert "1,1" not in load_network(path).nodes
        document = json.loads(path.read_text(encoding="utf-8"))
        del document["approach_nodes"], document["approach_roads"]  # a file from before ends were saved
        path.write_text(json.dumps(document), encoding="utf-8")
        assert sorted(load_network(path).approach_nodes) == ["1E", "1N", "2E", "2N", "2S", "2W"]  # not 1,1's 1W, 1S


def test_counts_must_be_whole_numbers():
    from copy import deepcopy

    from traffic_simulator.models import MetroLine, metro_line_problem

    network = generate_network(2, 2)
    for place, name, value in (("road", "lanes_per_direction", 1.5), ("road", "lanes_per_direction", 2.0),
                               ("node", "signal_phases", 2.0), ("background", "page", 0.5)):
        changed = deepcopy(network)
        item = {"road": changed.roads["1,1-2,1"], "node": changed.nodes["1,1"], "background": changed.background}[place]
        setattr(item, name, value)
        try:
            changed.validate()
        except ValueError as exc:
            assert name in str(exc) and "whole" in str(exc), str(exc)
        else:
            raise AssertionError(f"{place} {name} = {value} passed")
    assert "whole" in metro_line_problem(MetroLine("ML1", ["M1", "M2"], cars_per_train=5.5))
    assert metro_line_problem(MetroLine("ML1", ["M1", "M2"])) is None


def test_a_file_saved_with_a_byte_order_mark_opens():
    import tempfile
    from pathlib import Path

    from traffic_simulator.network_io import load_network, save_network, saved_conversation, saved_project

    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "net.json"
        save_network(generate_network(2, 2), path, {"messages": []}, {"run": {"seed": 7, "speed": 1.0}})
        path.write_text("﻿" + path.read_text(encoding="utf-8"), encoding="utf-8")  # as Notepad may save it
        assert len(load_network(path).nodes) == 4 and saved_project(path)["run"]["seed"] == 7
        assert saved_conversation(path) == {"messages": []}


def test_a_background_image_on_another_computer_is_refused():
    import json
    import tempfile
    from pathlib import Path

    from traffic_simulator.network_io import load_network, save_network

    network = generate_network(2, 2)
    network.background.kind = "image"
    for local in ("", r"C:\plans\site.png", "plans/site.png"):
        network.background.image = local
        network.validate()
    for remote in (r"\\198.51.100.7\share\plan.png", "//server/share/plan.png", r"\\?\UNC\server\share\plan.png",
                   r"/\server\share\plan.png"):
        network.background.image = remote
        try:
            network.validate()
        except ValueError as exc:
            assert "this computer" in str(exc), str(exc)
        else:
            raise AssertionError(f"{remote} passed: drawing it would contact that server")
    with tempfile.TemporaryDirectory() as folder:  # opening someone's project: refused before anything reads it
        path = Path(folder) / "net.json"
        network.background.image = ""
        save_network(network, path)
        document = json.loads(path.read_text(encoding="utf-8"))
        document["background"]["image"] = r"\\198.51.100.7\share\plan.png"
        path.write_text(json.dumps(document), encoding="utf-8")
        try:
            load_network(path)
        except ValueError as exc:
            assert "this computer" in str(exc)
        else:
            raise AssertionError("a project pointing at a share opened")


def test_a_move_that_brings_a_curves_ends_together_is_refused_unchanged():
    # Moving 2,2 onto 1,2 left the curve's chord zero: a ZeroDivisionError with curvature values, or (without) a curve
    # collapsed to a point that the undo skipped.
    from copy import deepcopy

    from traffic_simulator import network_tools

    for curvature in ([0.002, 0.002], []):
        network = generate_network(3, 4)
        road = network.roads["1,2-2,2"]
        road.shape, road.curvature = [(250.0, 440.0)], curvature
        network.validate()
        before = deepcopy(network)
        try:
            network_tools.run_tool(network, "move_junctions", {"junctions": ["2,2"], "dx": -500, "dy": 0})
        except network_tools.ToolError:
            pass
        else:
            raise AssertionError("a curve's ends met")
        assert network == before, curvature
        assert not network.move_nodes(["2,2"], -500, 0) and network == before


def test_manual_windows_follow_a_road_split_or_renamed_and_stale_ones_are_refused():
    from traffic_simulator.interchanges import build_diamond

    network = generate_network(3, 4)
    node = network.nodes["2,2"]
    node.signal_method = "manual"
    node.manual_greens_s = {road: [30.0 * i + 1, 30.0 * (i + 1)] for i, (road, _, _) in
                            enumerate(network.signal_arms("2,2"))}
    network.validate()
    window = node.manual_greens_s["1,2-2,2"]
    network.split_road("1,2-2,2", network.add_node(250, 500).id)  # the arm is N1-2,2 now: its window with it
    assert node.manual_greens_s.get("N1-2,2") == window and "1,2-2,2" not in node.manual_greens_s
    assert set(node.manual_greens_s) == {road for road, _, _ in network.signal_arms("2,2")}
    network.validate()
    network.delete_road("2,2-3,2")  # a deleted arm's window goes with it
    assert "2,2-3,2" not in node.manual_greens_s
    network.validate()
    node.manual_greens_s["1,1-2,1"] = [100.0, 120.0]  # a road elsewhere
    try:
        network.validate()
    except ValueError as exc:
        assert "1,1-2,1" in str(exc), exc
    else:
        raise AssertionError("a window for a road not at the node validated")

    mirrored = generate_network(3, 4)  # set_driving_side re-ties the roads into an interchange: renamed
    build_diamond(mirrored, ("1,2", "3,2"), ("2,3", "2,1"))
    outside = mirrored.nodes["1,2"]
    outside.signal_method = "manual"
    outside.manual_greens_s = {road: [30.0 * i + 1, 30.0 * (i + 1)] for i, (road, _, _) in
                               enumerate(mirrored.signal_arms("1,2"))}
    mirrored.validate()
    assert mirrored.set_driving_side("left" if mirrored.driving_side == "right" else "right")
    assert set(outside.manual_greens_s) == {road for road, _, _ in mirrored.signal_arms("1,2")}
    mirrored.validate()


def test_a_west_or_north_road_keeps_its_stops_and_crossings_from_the_junction():
    # 1W and 1N run from their boundary end to the junction: what is measured from that outer end stays put from the
    # junction when the road's length changes.
    network = generate_network(3, 4)
    for name in ("1W", "1N"):
        road = network.road(name)
        road.footpaths = ["left", "right"]
        stop = network.add_bus_stop(name)
        stop.distance_m = road.length_m - 125.0
        crossing = network.add_crossing(name, road.length_m - 60.0)
        network.set_road_length(name, 400)
        assert abs(network.road(name).length_m - 400) < 1e-6
        assert abs(400 - stop.distance_m - 125.0) < 1e-6 and abs(400 - crossing.distance_m - 60.0) < 1e-6, name
    stop = network.add_bus_stop("1E")  # measured from the junction: nothing to move
    stop.distance_m = 125.0
    network.set_road_length("1E", 400)
    assert stop.distance_m == 125.0
    network.validate()


def test_junctions_off_the_map_are_refused():
    from copy import deepcopy

    from traffic_simulator import network_tools

    network = generate_network(2, 2)
    before = deepcopy(network)
    for name, args in (("add_junction", {"x": 1e300, "y": 0}), ("add_junction", {"x": 0, "y": float("inf")}),
                       ("move_junctions", {"junctions": ["1,1"], "dx": 1e15, "dy": 0})):
        try:
            network_tools.run_tool(network, name, args)
        except network_tools.ToolError as exc:
            assert "off the map" in str(exc) or "number" in str(exc), exc  # inf: not a number, refused before it runs
        else:
            raise AssertionError(args)
        assert network == before
    network.add_node(999_000.0, -999_000.0)  # far, but on the map
    network.validate()


if __name__ == "__main__":
    for name, check in list(globals().items()):
        if name.startswith("test_"):
            check()
    print("ok")


def test_an_older_file_with_a_window_for_a_gone_road_opens_without_it():
    """Files saved before windows followed split roads kept them under the old id: they open, the stale window gone."""
    import json
    import tempfile
    from pathlib import Path

    from traffic_simulator.network_io import load_network, save_network

    network = generate_network(3, 4)
    node = network.nodes["2,2"]
    node.signal_method = "manual"
    node.manual_greens_s = {road: [30.0 * i + 1, 30.0 * (i + 1)] for i, (road, _, _) in
                            enumerate(network.signal_arms("2,2"))}
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "net.json"
        save_network(network, path)
        record = json.loads(path.read_text(encoding="utf-8"))
        nodes = record["nodes"]
        saved = next(item for item in nodes if item["id"] == "2,2")
        saved["manual_greens_s"]["9,9-2,2"] = [1.0, 30.0]  # a road split away long ago
        path.write_text(json.dumps(record), encoding="utf-8")
        loaded = load_network(path).nodes["2,2"].manual_greens_s
    assert "9,9-2,2" not in loaded and loaded == node.manual_greens_s

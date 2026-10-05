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


if __name__ == "__main__":
    for name, check in list(globals().items()):
        if name.startswith("test_"):
            check()
    print("ok")

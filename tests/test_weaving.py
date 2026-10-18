"""Freeway weaving (weaving.py, HCM 2000 Chapter 24): the method against the HCM's worked examples, the segments
found on a network, their flows from the demand, the design check and a run's own figures."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from traffic_simulator import design_checks, experiments, weaving  # noqa: E402
from traffic_simulator.interchanges import build_diamond  # noqa: E402
from traffic_simulator.mode_behavior import VehicleModes  # noqa: E402
from traffic_simulator.models import Network  # noqa: E402
from traffic_simulator.network_generator import generate_network  # noqa: E402
from traffic_simulator.simulation import Graph, PairDemand  # noqa: E402


def weave(main_in=2, segment=4, main_out=2, ramp_in=2, ramp_out=2, length=400.0) -> Network:
    """A one-way freeway 1W -> N1 -> N2 -> 1E, an on-ramp from 2W at N1 and an off-ramp to 2E at N2, both on the
    kerb side; lanes as given (1, or an even number: a one-way road's)."""
    network = Network()
    first, second = network.add_node(500, 0), network.add_node(500 + length, 0)
    for node in (first, second):
        node.control = "uncontrolled"
    middle = network.add_road(first.id, second.id)
    west, east = network.add_end(first.id, 0, 0), network.add_end(second.id, 1000 + length, 0)
    on, off = network.add_end(first.id, 250, -80), network.add_end(second.id, 750 + length, -80)  # left-hand: north
    for road, lanes, inward, kmh in ((west, main_in, True, 100), (middle, segment, False, 100),
                                     (east, main_out, False, 100), (on, ramp_in, True, 60), (off, ramp_out, False, 60)):
        if inward:  # the end's road runs from the end to the junction
            road.from_node, road.to_node = road.to_node, road.from_node
        road.direction, road.speed_limit_kmh = "one-way", kmh
        road.road_type = "ramp" if road in (on, off) else "urban"
        road.lanes_per_direction, road.single_lane = max(lanes // 2, 1), lanes == 1
    network.synchronize_road_lengths()
    return network


def test_the_method_gives_the_hcms_worked_examples():
    one = {"vo1": 2095, "vo2": 1497, "vw1": 1197, "vw2": 799}  # Example Problem 1: Type B, 4 lanes, 450 m, 110 km/h
    found = weaving.operate(one, 4, 450, 110, "B")
    assert (found["weaving_speed_kmh"], found["nonweaving_speed_kmh"], found["speed_kmh"]) == (81.0, 88.6, 85.7)
    assert found["lanes_weaving_need"] == 1.64 and found["operation"] == "unconstrained"
    assert found["density_pc_km_ln"] == 16.3 and found["los"] == "C" and found["volume_ratio"] == 0.357
    assert abs(weaving.capacity(one, 4, 450, 110, "B")["capacity_pch"] - 8421) < 0.01 * 8421  # Exhibit 24-8(F)
    two = {"vo1": 4000, "vo2": 100, "vw1": 600, "vw2": 300}  # Example Problem 2: Type A, 4 lanes, 300 m, 120 km/h
    found = weaving.operate(two, 4, 300, 120, "A")
    assert abs(found["weaving_speed_kmh"] - 79.3) < 0.15 and found["nonweaving_speed_kmh"] == 97.8  # W rounded there
    assert found["lanes_weaving_need"] == 1.02 and found["density_pc_km_ln"] == 13.3 and found["los"] == "C"
    assert abs(weaving.capacity(two, 4, 300, 120, "A")["capacity_pch"] - 8474) < 0.01 * 8474  # Exhibit 24-8(A)
    heavy = weaving.operate({k: v * 1.6 for k, v in two.items()}, 4, 300, 120, "A")
    assert heavy["density_pc_km_ln"] > found["density_pc_km_ln"] and heavy["speed_kmh"] < found["speed_kmh"]
    assert weaving.operate(one, 4, 150, 110, "B")["speed_kmh"] < 85.7  # shorter: slower
    assert weaving.capacity({"vo1": 0, "vo2": 0, "vw1": 3000, "vw2": 3000}, 4, 450, 110, "B")["capacity_bound"] == \
        "weaving flow"  # all weaving: the field limit of 4,000 pc/h binds
    assert weaving.basic_capacity(120, False) == 2400 and weaving.basic_capacity(90, False) == 2250
    assert weaving.basic_capacity(100, True) == 2200 and weaving.basic_capacity(70, True) == 1900


def test_the_type_follows_the_lane_changes():
    table = {(1, 1): "A", (0, 0): "B", (0, 1): "B", (1, 0): "B", (0, 2): "C", (3, 0): "C", (1, 2): None, (2, 2): None}
    assert all(weaving.configuration(*changes) == kind for changes, kind in table.items())  # Exhibit 24-5
    assert weaving._lanes_between(3, 4, 3, 1, 1) == (1, 1)  # a ramp-weave: one auxiliary lane
    assert weaving._lanes_between(3, 4, 3, 1, 2) == (0, 1)  # a two-lane exit with lane balance: Type B
    assert weaving._lanes_between(3, 4, 2, 1, 3) == (0, 2)  # Type C


def test_segments_are_found_between_a_merge_and_a_diverge():
    found = weaving.segments(Graph(weave()))  # 2 lanes in, 4 between (two-lane ramps), 2 out
    assert len(found) == 1 and found[0]["type"] == "A" and found[0]["lanes"] == 4 and found[0]["length_m"] == 400
    assert (found[0]["from"], found[0]["joining"], found[0]["to"], found[0]["leaving"]) == ("1W>", "2W>", "1E>", "2E>")
    assert weaving.segments(Graph(weave(1, 2, 1, 1, 1)))[0]["type"] == "A"
    assert weaving.segments(Graph(weave(2, 2, 2, 1, 1)))[0]["type"] == "ramp junctions"  # no auxiliary lane
    assert weaving.segments(Graph(weave(2, 4, 2, 1, 1)))[0]["type"] is None  # two lane changes each way
    assert weaving.segments(Graph(weave(length=800))) == []  # beyond 750 m: a merge and a diverge
    signals = weave()
    signals.nodes["N2"].control = "signalised"
    assert weaving.segments(Graph(signals)) == []
    network = generate_network(3, 4)
    assert weaving.segments(Graph(network)) == []  # junctions, not merges
    build_diamond(network, ("1,2", "3,2"), ("2,3", "2,1"))
    assert weaving.segments(Graph(network)) == []  # its off-ramp leaves before its on-ramp joins


def test_the_demand_on_the_four_movements_is_analysed():
    network = weave()
    rates = {("1W", "1E"): 2000.0, ("2W", "2E"): 100.0, ("1W", "2E"): 400.0, ("2W", "1E"): 600.0}
    graph = Graph(network)
    row = weaving.report(graph, rates)[0]
    assert row["movements_pch"] == {"vo1": 2000, "vo2": 100, "vw1": 400, "vw2": 600}
    assert row["flow_pch"] == 3100 and row["volume_ratio"] == round(1000 / 3100, 3) and row["los"] in "ABCDEF"
    assert row["capacity_pch"] > row["flow_pch"] and 0 < row["v_c"] < 1 and row["max_volume_ratio"] == 0.35
    busy = weaving.report(graph, {pair: rate * 3 for pair, rate in rates.items()})[0]
    assert busy["density_pc_km_ln"] > row["density_pc_km_ln"] and "warnings" in busy  # 3,000 pc/h weaving: over 2,800
    audit = design_checks.audit(network, VehicleModes().modes)
    assert audit["counts"]["weaving"] == 1 and "Type A" in audit["findings"]["weaving"][0]["note"]
    plain = design_checks.audit(weave(2, 2, 2, 1, 1), VehicleModes().modes)["findings"]["weaving"][0]
    assert "no auxiliary lane" in plain["note"]

    demand = [PairDemand(o, d, rate / 2, None, [], "Random", (0, 100, 0)) for (o, d), rate in rates.items()]
    simulation, _ = experiments.run_headless(network, demand, VehicleModes().modes, 1, 600, observe=True)
    observed = simulation.observations()
    simulated = weaving.measured(row, observed, observed["observed_s"] / 3600)["simulated"]
    assert simulated["traversals"] > 100 and 30 < simulated["speed_kmh"] <= 101 and simulated["density_veh_km_ln"] > 0



def test_the_simulation_weaves_where_the_lanes_say():
    graph = Graph(weave())  # 2 lanes in, 4 between, 2 out; two-lane ramps: Type A
    segment = graph.links["N1-N2>"]
    lands = {(link.id, turn.lane_out.index) for link in graph.arriving["N1"] for turn in link.turns.values()}
    assert lands == {("1W>", 2), ("1W>", 3), ("2W>", 0), ("2W>", 1)}  # the road offside, the ramp kerbside
    leaves = {(after, index) for (index, after), turn in segment.turns.items() if turn.permitted}
    assert leaves == {("2E>", 0), ("2E>", 1), ("1E>", 2), ("1E>", 3)}  # the off-ramp only from the kerbside lanes
    rates = {("1W", "1E"): 1400.0, ("2W", "2E"): 100.0, ("1W", "2E"): 500.0, ("2W", "1E"): 500.0}
    speeds = {}
    for length in (150, 600):
        network = weave(length=length)
        demand = [PairDemand(o, d, rate, None, [], "Random", (0, 100, 0)) for (o, d), rate in rates.items()]
        simulation, _ = experiments.run_headless(network, demand, VehicleModes().modes, 1, 900, observe=True)
        observed = simulation.observations()
        row = {"link": "N1-N2>", "length_m": length, "lanes": 4}
        speeds[length] = weaving.measured(row, observed, observed["observed_s"] / 3600)["simulated"]["speed_kmh"]
        assert simulation.teleports <= 1, (length, simulation.teleports)
    assert speeds[150] < 0.6 * speeds[600]  # a short weave is slow (with a mapping that needed no change: alike)


def test_the_assistant_grades_the_weaves_with_a_runs_figures():
    import os
    import tempfile

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from traffic_simulator.main import MainWindow

    _app = QApplication.instance() or QApplication([])
    window = MainWindow()
    window._ask_to_save, window.results_folder = False, Path(tempfile.mkdtemp())
    run = window.ai_mode._execute
    assert "No weaving segments" in run("weaving_los", {})["note"]  # the starting grid has none
    window.install_network(weave())
    window._commit()
    run("set_od_demand", {"pairs": ["1W->1E", "1W->2E", "2W->1E", "2W->2E"], "demand": "hourly", "value": 400})
    test = run("run_simulation", {"duration_min": 5, "seed": 1})["run"]
    row = run("weaving_los", {"id": test})["segments"][0]
    assert row["type"] == "A" and row["movements_pch"]["vw1"] == 424  # 400 veh/h with 10 % buses at 2 PCU
    assert row["operation"] == "constrained" and "warnings" in row  # half of it weaves: over Type A's 0.35
    assert row["simulated"]["traversals"] > 50 and row["simulated"]["speed_kmh"] > 0
    window.close()


if __name__ == "__main__":
    for name, check in list(globals().items()):
        if name.startswith("test_"):
            check()
    print("ok")

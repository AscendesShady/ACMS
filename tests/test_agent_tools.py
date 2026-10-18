"""The agent's statistical tools on the simulation: route choice (path-size logit, saved, undone), what a
headless run observes (detectors, link times, conflicts, bus stops, stop lines) and the analyses of it,
network indices, travel conditions, critical links, and the window's tools (assign_routes, optimise_signals,
read_detectors, analyse_safety, forecast, regress, critical_links, check_calibration's envelopes,
choose_modes' nests). Run with `python tests/test_agent_tools.py` (or the test monitor)."""

import os
import random
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from traffic_simulator import experiments, network_tools as tools, results  # noqa: E402
from traffic_simulator.mode_behavior import VehicleModes  # noqa: E402
from traffic_simulator.network_generator import generate_network  # noqa: E402
from traffic_simulator.network_io import load_network, save_network  # noqa: E402
from traffic_simulator.simulation import Graph, PairDemand  # noqa: E402

APP = QApplication.instance() or QApplication([])
DEMAND = [PairDemand(o, d, q, None, [], "Poisson", (0, 100, 0)) for o, d, q in
          (("1W", "1E", 800), ("1E", "1W", 800), ("1N", "1S", 600), ("2W", "1E", 400))]


def observed_run(network, minutes=15, seed=3, modes=None):
    modes = modes or VehicleModes().modes
    with tempfile.TemporaryDirectory() as folder:
        recorder = results.RunRecorder(network, modes, folder=Path(folder) / "run")
        simulation, _ = experiments.run_headless(network, DEMAND, modes, seed, minutes * 60, recorder=recorder, observe=True)
        kpis = recorder.kpis()
        kpis["observed"] = {**recorder.observed(), **simulation.observations()}
    return kpis


def refused(call, reason):
    try:
        call()
    except (ValueError, tools.ToolError) as exc:
        assert reason in str(exc), (reason, exc)
        return
    raise AssertionError(f"should have been refused: {reason}")


def test_route_choice_shares_routes_by_path_size_logit():
    network = generate_network(3, 4)
    assert len(Graph(network).route_shares("2W", "1E")) == 1  # off: the fastest route alone
    network.route_choice = {"theta_per_min": 0.5, "routes": 3}
    shares = Graph(network).route_shares("2W", "1E")
    assert len(shares) == 3 and abs(sum(p for p, _ in shares) - 1) < 1e-9
    assert len({tuple(link.id for link in path) for _, path in shares}) == 3  # different routes
    ids = [{link.id for link in path} for _, path in shares]
    unique = next(link for link in shares[0][1] if all(link.id not in other for other in ids[1:]))
    slower = {unique.id: unique.length / unique.speed + 60}  # the three tie on this symmetric grid: slow one down

    def shares_at(theta, times):
        network.route_choice = {"theta_per_min": theta, "routes": 3, "link_times_s": times}
        return sum(p for p, path in Graph(network).route_shares("2W", "1E") if unique in path)  # 0: no longer offered

    assert shares_at(5, slower) <= shares_at(0.5, slower) < 1 / 3  # higher theta: fewer down the slow route
    assert shares_at(0.5, {unique.id: 9999}) < 1e-6
    flows, _ = Graph(network).assign({("2W", "1E"): 600})
    assert abs(sum(v for (node, link), v in flows.items() if link.startswith("2W")) - 600) < 1e-6
    for bad in ({"routes": 9}, {"theta_per_min": 0}, {"link_times_s": {"x": -1}}, {"colour": 1}):
        network.route_choice = bad
        refused(network.validate, "Route choice")
    network.route_choice = {"theta_per_min": 0.7, "routes": 2, "link_times_s": {"1W>": 40.0}}
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "net.json"
        save_network(network, path)
        assert load_network(path).route_choice == network.route_choice  # saved with the network


def test_a_headless_run_observes_and_is_analysed():
    network = generate_network(3, 4)
    network.add_bus_stop("2,1-3,1", "both")
    modes = VehicleModes().modes
    modes[0].headway_min = 4
    kpis = observed_run(network, minutes=30, modes=modes)  # long enough for 3+ queued cycles at a stop line
    observed = kpis["observed"]
    assert observed["passages"] and observed["traversals"] and observed["stop_lines"] and observed["stops"]
    assert kpis["tti"] >= 1
    analysed = experiments.analyse_run(kpis)
    assert analysed["steady_state"]["warm_up_min"] >= 0 and analysed["steady_state"]["throughput_vph"]["mean"] > 0
    assert 0 <= analysed["flow_gini"] < 1
    stop = analysed["bus_stops"][0]
    assert stop["scheduled_min"] == 4 and stop["los"] in "ABCDEF" and 2 < stop["mean_headway_min"] < 7  # one line
    found = experiments.detectors(kpis)
    row = found["detectors"][0]
    assert row["space_mean_kmh"] <= row["time_mean_kmh"] + 1e-6  # harmonic ≤ arithmetic
    assert row["v15_kmh"] <= row["v50_kmh"] <= row["v85_kmh"] and row["flow_vph"] > 0
    assert all(1500 <= r["saturation_flow_vphpl"] <= 2600 for r in found["stop_lines"])  # few cycles here: rough
    # The stop-line discharge on an approach held saturated, enough cycles to measure: the calibrated ~2,060 per lane
    single = generate_network(1, 1)
    saturated = [PairDemand("1W", "1E", 1800, None, [], "Random", (0, 100, 0)),
                 PairDemand("1N", "1S", 500, None, [], "Random", (0, 100, 0))]
    simulation, _ = experiments.run_headless(single, saturated, modes, 7, 900, observe=True)
    discharge = [r for r in experiments.discharge(simulation.observations()) if r["cycles"] >= 3]
    assert discharge and all(1900 <= r["saturation_flow_vphpl"] <= 2250 for r in discharge), discharge
    refused(lambda: experiments.detectors(kpis, "9,9-8,8"), "No vehicle passed")
    safety = experiments.safety(kpis, 3.0)
    assert safety["conflicts"] >= experiments.safety(kpis)["conflicts"]  # a looser threshold finds more
    refused(lambda: experiments.safety(kpis, 9), "ttc_s")
    flow = experiments.series(kpis, "flow", "1,1-2,1", 5)
    assert len(flow) == 6 and all(v >= 0 for v in flow) and len(experiments.series(kpis, "throughput", None, 5)) == 6
    refused(lambda: experiments.series(kpis, "flow", None), "no road")


def test_network_indices_travel_conditions_and_critical_links():
    network = generate_network(3, 4)
    found = tools.run_tool(network, "network_indices", {})
    assert (found["vertices"], found["edges"], found["cyclomatic"]) == (26, 31, 6) and found["detour_index"] >= 1
    assert found["most_accessible"] and list(found["most_accessible"])[0] in ("2,2", "3,2", "2,3", "3,3")
    rng = random.Random(4)
    days = [{"label": f"day {i}", "values": {"volume": volume + rng.gauss(0, 50), "rain_mm": rain}}
            for i, (volume, rain) in enumerate([(3000, 0), (5000, 0), (7000, 10)] * 8)]
    clusters = tools.run_tool(network, "cluster_conditions", {"days": days})
    assert clusters["k"] == 3 and sorted(c["size"] for c in clusters["clusters"]) == [8, 8, 8]
    assert abs(sum(c["weight"] for c in clusters["clusters"]) - 1) < 0.01  # rounded to 3 places
    refused(lambda: tools.run_tool(network, "cluster_conditions", {"days": days[:2]}), "4 or more")
    critical = tools.critical_links_plan(network, {("1W", "1E"): 600, ("2W", "1E"): 300})
    assert not {row["road"] for row in critical["critical"]} & set(network.approach_roads)  # an end's road: trivial
    inner = next(row for row in critical["critical"] if row["road"] == "1,1-2,1")
    assert inner["cut_off_vph"] == 0 and inner["extra_veh_h_per_h"] > 0  # a way round costs time


def test_the_window_assigns_routes_and_optimises_signals():
    from traffic_simulator.main import MainWindow

    window = MainWindow()
    window.results_folder = Path(tempfile.mkdtemp())
    window._ask_to_save = False
    window.install_network(generate_network(3, 4))
    window._commit()
    window._od_command("set_od_demand", {"pairs": ["1W->1E", "1E->1W", "2W->1E", "1N->1S"], "demand": "hourly", "value": 500})
    refused(lambda: window._runs_command("assign_routes", {"off": True}, check_only=True), "off already")
    steps = len(window.history.undo_stack)
    found = window._runs_command("assign_routes", {"iterations": 2, "duration_min": 6, "routes": 3})
    assert [row["iteration"] for row in found["iterations"]] == [1, 2] and found["link_times_measured"] > 10
    assert window.network.route_choice["routes"] == 3 and len(window.history.undo_stack) == steps + 1
    assert any(len(routes) > 1 for routes in found["busiest_pairs"].values())
    window.undo()
    assert window.network.route_choice == {}
    refused(lambda: window._runs_command("assign_routes", {"iterations": 20}, check_only=True), "iterations")
    refused(lambda: window._runs_command("optimise_signals", {"junctions": ["1W"], "duration_min": 2}, check_only=True),
            "isn't a signalised")
    refused(lambda: window._runs_command("optimise_signals", {"junctions": ["2,1"], "duration_min": 2, "seeds": [1, 2, 3, 4, 5],
                                                              "population": 10, "generations": 5}, check_only=True), "at most 120")
    best = window._runs_command("optimise_signals", {"junctions": ["2,1"], "duration_min": 3, "seeds": [1, 2],
                                                     "population": 4, "generations": 2})
    bests = [row["best"] for row in best["history"]]
    assert best["candidates_scored"] >= 4 and bests == sorted(bests, reverse=True)  # the elite never gets worse
    assert best["apply_with"][0]["tool"] == "set_signal_timing" and window.network.nodes["2,1"].signal_method != "fixed"
    tools.run_tool(window.network, "set_signal_timing", best["apply_with"][0]["args"])  # it applies as given

    run = window._runs_command("run_simulation", {"duration_min": 20, "seed": 2})
    assert "conflicts" in run
    assert window._runs_command("read_detectors", {"road": "1,1-2,1"})["detectors"]
    assert "ranked" in window._runs_command("analyse_safety", {})
    forecast = window._runs_command("forecast", {"what": "on_road", "horizon": 4})
    assert len(forecast["forecast"]) == 4 and forecast["forecast"][0]["low"] <= forecast["forecast"][0]["value"]
    flows = window._runs_command("forecast", {"what": "flow", "road": "1,1-2,1", "interval_min": 2, "horizon": 1})["recent"]
    days = [[v * f for v in flows] for f in (0.9, 1.0, 1.1, 0.95)]
    checked = window._runs_command("check_calibration", {"profiles": [{"what": "flow", "road": "1,1-2,1",
                                                                       "interval_min": 2, "days": days}]})
    profile = checked["profiles"][0]
    assert set(profile["criteria"]) == {"I", "II", "III", "IV"} and profile["representative_day"] in range(4)
    refused(lambda: window._runs_command("check_calibration", {"profiles": [{"what": "flow", "road": "1,1-2,1",
                                                                             "interval_min": 2, "days": days[:2]}]}), "3 or more")
    critical = window._runs_command("critical_links", {"limit": 3})
    assert len(critical["critical"]) == 3
    data = [{"q": q, "t": 60 * (1 + 0.15 * (q / 1800) ** 4)} for q in (400, 800, 1200, 1600, 2000)]
    fit = window._runs_command("regress", {"data": data, "y": "t", "x": ["q"]})
    assert fit["n"] == 5 and fit["coefficients"][1]["coefficient"] > 0
    refused(lambda: window._runs_command("regress", {"data": data, "y": "t", "x": ["speed"]}), "No speed")

    alternatives = [{"mode": "Car", "cost_per_km": 25}, {"mode": "Bike", "constant": -1}, {"mode": "Bus", "cost": 150}]
    plain = window._od_command("choose_modes", {"alternatives": alternatives})
    nested = window._od_command("choose_modes", {"alternatives": alternatives, "nests": [
        {"name": "sustainable", "modes": ["Bike", "Bus"], "scale": 0.4}]})
    assert plain["model"] == "multinomial logit" and nested["model"] == "nested logit"
    assert plain["value_of_time_per_hour"] == 300 and plain["elasticities"]["Car"]["cost"] < 0  # 60 × 0.025/0.005
    refused(lambda: window._od_command("choose_modes", {"alternatives": alternatives, "nests": [
        {"modes": ["Bike"], "scale": 0.5}]}, check_only=True), "Each nest")
    window.close()


if __name__ == "__main__":
    for name, check in list(globals().items()):
        if name.startswith("test_"):
            check()
    print("ok")

"""Network testing: the statistics, scenarios, headless runs and experiments (experiments.py), the
window's run tools, the new edit tools (signal timing, bus stops, institutions, the map's place,
demand calibration, vehicle types) and the planning journal.
Run with `python tests/test_experiments.py` (or the test monitor)."""

import json
import os
import tempfile
from copy import deepcopy
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel, QLineEdit  # noqa: E402

from test_results import read_xlsx  # noqa: E402
from traffic_simulator import experiments, network_tools as tools  # noqa: E402
from traffic_simulator.institutions import place  # noqa: E402
from traffic_simulator.mode_behavior import VehicleModes  # noqa: E402
from traffic_simulator.network_generator import generate_network  # noqa: E402
from traffic_simulator.simulation import PairDemand  # noqa: E402

APP = QApplication.instance() or QApplication([])


def refused(network, name, **args) -> str:
    before = deepcopy(network)
    try:
        tools.run_tool(network, name, args)
    except tools.ToolError as exc:
        assert network.nodes.keys() == before.nodes.keys() and network.roads.keys() == before.roads.keys()
        return str(exc)
    raise AssertionError(f"{name} {args} should have been refused")


def window_with_demand(value=400):
    from traffic_simulator.main import MainWindow

    window = MainWindow()
    window.results_folder = Path(tempfile.mkdtemp())
    window._ask_to_save = False
    window.install_network(generate_network(3, 4))
    window._commit()
    window._od_command("set_od_demand", {"pairs": ["1W->1E", "1E->1W", "1N->1S"], "demand": "hourly", "value": value})
    return window


def test_the_eye_the_research_log_and_a_batch_each_leave_a_dated_folder():
    import json

    window = window_with_demand()
    window._extension("research_log").check.setChecked(True)
    window.watch_action.setChecked(True)
    done = window.run_batch([1], 2)  # no AI Mode step at all
    window.watch_action.setChecked(False)
    folders = sorted(path.name for path in window.results_folder.iterdir() if path.is_dir())
    kinds = [next(kind for kind in ("ACMS activity ", "ACMS batch ", "ACMS research log ") if name.startswith(kind))
             for name in folders]
    assert kinds == ["ACMS activity ", "ACMS batch ", "ACMS research log "], folders  # three dated folders, one each
    activity = next(window.results_folder.glob("ACMS activity *"))
    assert (activity / "activity.csv").is_file()
    session = next(window.results_folder.glob("ACMS research log *"))
    step = json.loads((session / "steps" / "0001" / "step.json").read_text(encoding="utf-8"))
    assert step["kind"] == "batch" and step["calls"][0]["tool"] == "run_batch" and step["calls"][0]["decision"] == "applied"
    artifacts = json.loads((session / "steps" / "0001" / "artifacts.json").read_text(encoding="utf-8"))
    paths = {Path(item["path"]).name for item in artifacts}
    assert Path(done["file"]).name in paths and any(name.endswith("no-ai.xlsx") for name in paths)  # each with its SHA-256
    assert not list((session / "steps" / "0001").glob("*.png"))  # a batch changes neither network nor demand: no picture
    window._extension("research_log").module.end_session()
    window.close()


def test_batch_runs_seeds_override_the_menu_bars():
    window = window_with_demand()
    window.show()
    batch = window._extension("batch_run")
    labels = [label.text() for label in window.menuBar().cornerWidget().findChildren(QLabel)]
    assert "Seed" in labels and window.seed_box.prefix() == ""  # its name beside the box, not in it
    assert window.seed_box.isVisible() and not window.seed_overridden.isVisible()
    batch.check.setChecked(True)  # Batch Run's own seeds count: the box gives way
    APP.processEvents()
    assert not window.seed_box.isVisible() and window.seed_overridden.isVisible()
    assert window.seed_overridden.text() == "Overridden" and window.seed_overridden.toolTip() == "Batch Run's seeds"
    batch.check.setChecked(False)
    APP.processEvents()
    assert window.seed_box.isVisible() and not window.seed_overridden.isVisible() and window.seed_box.value() == 42
    window.close()


def test_a_batch_goes_seed_by_seed_and_keeps_a_checkpoint():
    window = window_with_demand()
    window.results_folder = Path(tempfile.mkdtemp())
    saved, write = [], experiments.batch_workbook

    def checkpoint(path, batch, inputs):  # each save, then Stop once two runs have finished
        saved.append([(run["scenario"], run["seed"]) for run in batch["runs"]])
        if len(batch["runs"]) == 2:
            window._cancel = True
        return write(path, batch, inputs)

    experiments.batch_workbook = checkpoint
    try:
        done = window.run_batch([1, 2], 1, test_tsp=True)  # the two baselines, Never and Always preempt
    finally:
        experiments.batch_workbook = write
    agents = {scenario for scenario, _ in saved[1]}
    assert saved[0] == saved[1][:1] and len(saved[1]) == 2  # a checkpoint after every run
    assert {seed for _, seed in saved[1]} == {1} and len(agents) == 2  # seed 1 for both agents before seed 2
    assert done["runs"] == 2 and done["of"] == 4
    finished = next(row[1] for row in read_xlsx(Path(done["file"]))["Read me"] if row and row[0] == "Runs finished")
    assert finished.startswith("2 of 4") and not list(window.results_folder.glob("*(saving)*"))
    window.close()


def test_the_statistics():
    one = experiments.summary([1, 2, 3])
    assert one["mean"] == 2 and one["sd"] == 1 and abs(one["ci95"] - 4.303 / 3 ** 0.5) < 1e-3
    assert experiments.summary([5])["ci95"] is None and experiments.summary([None])["n"] == 0
    far = experiments.difference(one, experiments.summary([11, 12, 13]))
    assert far["diff"] == 10 and far["clear"] and far["percent"] == 500
    near = experiments.difference(one, experiments.summary([1.5, 2.5, 3.5]))
    assert near["clear"] is False  # its interval straddles zero
    assert experiments.difference(experiments.summary([1]), experiments.summary([9]))["clear"] is None  # one seed: open
    assert experiments.seeds_from("1-3, 7, 2") == [1, 2, 3, 7] and experiments.seeds_from(4) == [4]
    for bad in ("5-1", "1-99", "two"):
        try:
            experiments.seeds_from(bad)
        except ValueError:
            continue
        raise AssertionError(bad)
    rows = experiments.compare([{"scenario": "A", "measures": {"delay_veh_h": v}} for v in (10, 11, 12)] +
                               [{"scenario": "B", "measures": {"delay_veh_h": v}} for v in (5, 6, 7)], ["A", "B"])
    b = next(row for row in rows if row["scenario"] == "B")
    assert b["vs_baseline_diff"] == -5 and b["vs_baseline_clear"] and b["better"] is True  # less delay is better
    assert b["vs_baseline_test"] == "Welch" and b["confidence"] == 0.95  # no seeds to pair
    # The same seeds in both: paired. Seed-to-seed swings that hide a small, steady gain from Welch don't hide it
    # from the paired test; a third scenario widens both intervals (Bonferroni)
    swings = {1: 10.0, 2: 20.0, 3: 30.0}
    runs = [{"scenario": name, "seed": seed, "measures": {"delay_veh_h": value - shift}}
            for name, shift in (("A", 0), ("B", 1), ("C", 0.5)) for seed, value in swings.items()]
    two = experiments.compare([run for run in runs if run["scenario"] != "C"], ["A", "B"])
    b = next(row for row in two if row["scenario"] == "B")
    assert b["vs_baseline_test"].startswith("paired") and b["vs_baseline_clear"] and b["vs_baseline_p"] < 1e-6
    assert not experiments.difference(experiments.summary([10, 20, 30]), experiments.summary([9, 19, 29]))["clear"]
    three = experiments.compare(runs, ["A", "B", "C"])
    assert {row["confidence"] for row in three if row["scenario"] != "A"} == {0.975}
    assert all(row["replications_needed"] > experiments.UNSTABLE_RUNS for row in three)  # 10-30: far too spread


def test_a_scenario_changes_only_a_copy():
    network = generate_network(3, 4)
    scenario = experiments.scenario_from({"name": "One lane", "changes": [
        {"tool": "update_roads", "args": {"roads": ["1,1-2,1"], "lanes_per_direction": 1}}]}, 1)
    changed = experiments.scenario_network(network, scenario, tools.SCENARIO_TOOLS)
    assert changed.road("1,1-2,1").lanes_per_direction == 1 and network.road("1,1-2,1").lanes_per_direction == 2
    for spec, reason in (({"changes": [{"tool": "generate_grid", "args": {}}]}, "can't be part of a scenario"),
                         ({"changes": [{"tool": "update_roads", "args": {"roads": ["9,9-8,8"]}}]}, "change 1"),
                         ({"demand_scale": 50}, "demand_scale"), ({"changes": "wider"}, "changes are")):
        try:
            experiments.scenario_network(network, experiments.scenario_from(spec, 0), tools.SCENARIO_TOOLS)
        except ValueError as exc:
            assert reason in str(exc), (reason, exc)
            continue
        raise AssertionError(spec)
    demand = [PairDemand("1W", "1E", 300.0, None, [], "Random", (0, 100, 0))]
    assert experiments.scaled(demand, 2)[0].hourly == 600 and demand[0].hourly == 300


def test_demand_on_short_links_is_sized_by_what_they_hold():
    # A grid of 40 m give-way links: each junction alone holds X 0.85, but the queues fill the short links and spill
    # back; sizing stops when a link's 95th-percentile queue reaches STORAGE_LIMIT of its length, and that holds
    network = generate_network(2, 2, 40.0)
    for node in network.nodes.values():
        node.control = "uncontrolled"
    ends = sorted(network.approach_nodes)
    plan, info = tools.demand_plan(network, [(o, d) for o in ends for d in ends if o != d],
                                   {"demand": "hourly", "degree_of_saturation": 0.85})
    assert "spill back" in info["sized_by"]
    demand = [PairDemand(o, d, value, None, [], "Random", (0, 100, 0)) for (o, d), value in plan.items()]
    assert experiments.holds(network, demand, VehicleModes().modes, seed=1)["holds"]
    long, _ = tools.demand_plan(generate_network(2, 2), [(o, d) for o in ends for d in ends if o != d],
                                {"demand": "hourly", "degree_of_saturation": 0.85})
    assert sum(long.values()) > sum(plan.values())  # 500 m links: the junctions' X binds, as before


def test_the_demand_check_judges_the_busiest_quarter_of_an_uneven_hour():
    from traffic_simulator.daily_profile import phf_quarters

    network = generate_network(2, 2)
    ends = sorted(network.approach_nodes)
    pairs = [(o, d) for o in ends for d in ends if o != d]
    found = []
    for x, phf in ((0.85, 0.7), (0.6, 0.85)):  # its peak quarter at X 1.21: over; at X 0.71: fine
        plan, _ = tools.demand_plan(network, pairs, {"demand": "hourly", "degree_of_saturation": x})
        demand = [PairDemand(o, d, v, None, [], "Random", (0, 100, 0), quarters=phf_quarters(phf)) for (o, d), v in plan.items()]
        found.append(experiments.holds(network, demand, VehicleModes().modes, seed=1))
    assert found[0]["holds"] is False and found[0]["peak_quarter_build_up"] > experiments.HOLD_BUILD_UP
    assert found[1]["holds"] is True and "peak_quarter_build_up" in found[1]


def test_mode_choice_moves_people_so_the_vehicles_change_with_them():
    # A pair's people (vehicles x occupancy now) ride the new split: a shift from cars (1.5 aboard) to a bus (20)
    # takes cars off the road; before, the vehicles stayed and only their split changed (audit 2026-10-13, M6)
    network, modes = generate_network(2, 2), VehicleModes().modes
    pair = ("1W", "1E")
    alternatives = [{"mode": "Car", "cost_per_km": 25}, {"mode": "Bus", "cost": 50, "constant": 1.0}]
    plan, info = tools.mode_plan(network, [pair], {"alternatives": alternatives}, modes, {pair: 1000.0},
                                 current={pair: {"Bus": 0.0, "Car": 1.0, "Bike": 0.0}})
    people = info["pairs"][0]["people_percent"]
    expected = 1000 * 1.5 * (people["Car"] / 100 / 1.5 + people["Bus"] / 100 / 20)  # Bus untimetabled here
    assert people["Bus"] > 0 and abs(info["values"][pair] - expected) < 1 and info["vehicles_after"] < 1000  # % rounded
    _, kept = tools.mode_plan(network, [pair], {"alternatives": alternatives}, modes, {pair: 1000.0})
    assert "values" not in kept  # no split given: the vehicles are left as they are


def test_a_grid_of_give_way_junctions_holds_the_demand_sized_for_it():
    # Sized to X 0.85 by the capacity model, a 2 x 2 grid of uncontrolled junctions jammed (it held only to about
    # X 0.4) until give-way drivers slowed to look, minor offside turns gave way by HCM rank and the table was
    # re-measured (audit 2026-10-13, H1)
    network = generate_network(2, 2)
    for node in network.nodes.values():
        node.control = "uncontrolled"
    ends = sorted(network.approach_nodes)
    plan, _ = tools.demand_plan(network, [(o, d) for o in ends for d in ends if o != d],
                                {"demand": "hourly", "degree_of_saturation": 0.85})
    demand = [PairDemand(o, d, value, None, [], "Random", (0, 100, 0)) for (o, d), value in plan.items()]
    assert experiments.holds(network, demand, VehicleModes().modes, seed=1)["holds"]


def test_a_warm_up_is_left_out_of_what_is_observed_too():
    from traffic_simulator.results import RunRecorder

    network, modes = generate_network(2, 2), VehicleModes().modes
    demand = [PairDemand("1W", "1E", 900.0, None, [], "Random", (0, 100, 0)),
              PairDemand("1N", "1S", 600.0, None, [], "Random", (0, 100, 0))]
    whole = experiments.run_headless(network, demand, modes, 5, 600, recorder=RunRecorder(network, modes),
                                     observe=True)[0].observations()
    warm = experiments.run_headless(network, demand, modes, 5, 600, observe=True,
                                    recorder=RunRecorder(network, modes, measure_from_s=300))[0].observations()
    assert whole["observed_from_s"] == 0 and warm["observed_from_s"] == 300 and abs(warm["observed_s"] - 300) < 1
    assert all(passage[3] >= 300 for passage in warm["passages"])  # detectors, fuel, conflicts: after it only
    assert 0 < sum(warm["fuel"]["by_type_ml"].values()) < sum(whole["fuel"]["by_type_ml"].values())
    assert sum(n for n, _ in warm["traversals"].values()) < sum(n for n, _ in whole["traversals"].values())


def test_a_sensitivity_sweep_gives_each_parameters_elasticity():
    network, modes = generate_network(2, 2), VehicleModes().modes
    demand = [PairDemand("1W", "1E", 900.0, None, [], "Random", (0, 100, 0)),
              PairDemand("1N", "1S", 600.0, None, [], "Random", (0, 100, 0))]
    found = experiments.parameter_sensitivity(network, demand, modes, [3], 600, ("tau_s", "ring_timegap_s"))
    assert found["runs"] == 5 and set(found["parameters"]) == {"tau_s", "ring_timegap_s"}
    ring = found["parameters"]["ring_timegap_s"]  # no roundabout here: the same seed runs the same, elasticity 0
    assert all(ring[key]["elasticity"] == 0 for key in experiments.SENSITIVITY_MEASURES)
    assert found["parameters"]["tau_s"]["mean_speed_kmh"]["elasticity"] < 0  # longer headways: slower
    assert experiments.parameter_sensitivity(network, demand, modes, [3], 600, cancelled=lambda: True) is None
    tau = experiments.varied(modes, "tau_s", 0.1)[0]
    assert all(mode.tau_s >= experiments.DT for mode in tau) and modes[0].tau_s == VehicleModes().modes[0].tau_s


def test_a_headless_run_is_repeatable_and_recorded():
    from traffic_simulator import results

    network, modes = generate_network(3, 4), VehicleModes().modes
    demand = [PairDemand("1W", "1E", 900.0, None, [], "Random", (0, 100, 0)),
              PairDemand("1N", "1S", 600.0, None, [], "Random", (0, 100, 0))]
    figures = []
    for _ in range(2):
        with tempfile.TemporaryDirectory() as folder:
            recorder = results.RunRecorder(network, modes, folder=Path(folder) / "run")
            simulation, same = experiments.run_headless(network, demand, modes, 7, 300, recorder=recorder)
            assert same is recorder and simulation.time >= 300
            figures.append(recorder.kpis())
    assert figures[0]["arrived"] > 0 and figures[0]["time_s"] == 300
    assert experiments.measures_of(figures[0]) == experiments.measures_of(figures[1])  # the same seed, the same run
    simulation, _ = experiments.run_headless(network, demand, modes, 7, 300, cancelled=lambda: True)
    assert simulation.time == 0  # cancelled before the first step


def test_a_headless_run_that_raises_still_leaves_its_workbook():
    window = window_with_demand()
    asked = []

    def cancelled() -> bool:  # the run itself raises after 30 steps (each step asks)
        asked.append(1)
        if len(asked) > 30:
            raise RuntimeError("the run broke")
        return False

    for broken in ("run", "analysis"):
        folder = Path(tempfile.mkdtemp())
        observed = window._observed_kpis
        if broken == "analysis":
            cancelled = lambda: False  # noqa: E731
            window._observed_kpis = lambda *args: 1 / 0
        try:
            window._one_run(window.network, window._pair_demand(), window.vehicle_modes.modes, 1, 120, None, [], [],
                            broken, lambda text: None, cancelled, folder)
        except (RuntimeError, ZeroDivisionError):
            pass
        else:
            raise AssertionError("its error goes on up")
        finally:
            window._observed_kpis = observed
        written = list(folder.glob("*.xlsx"))
        assert len(written) == 1, broken
        run = {row[0]: row[1] for row in read_xlsx(written[0])["Run"][1:]}
        assert ("the run broke" if broken == "run" else "ZeroDivisionError") in run["Incomplete"]
    window.close()


def test_signal_timing_bus_stops_institutions_and_the_map():
    network = generate_network(3, 4)
    timed = tools.run_tool(network, "set_signal_timing", {"junctions": ["2,2"], "method": "fixed", "phases": 2,
                                                          "green_times_s": [30, 25]})
    node = network.nodes["2,2"]
    assert (node.signal_method, node.signal_phases, node.green_times_s) == ("fixed", 2, [30.0, 25.0]) and "2,2" in timed["timed"]
    assert "method must be" in refused(network, "set_signal_timing", junctions=["2,2"], method="guess")
    network.nodes["3,2"].control = "uncontrolled"
    assert "is not signalised" in refused(network, "set_signal_timing", junctions=["3,2"], method="fixed")

    stop = tools.run_tool(network, "add_bus_stop", {"road": "1,1-2,1", "dwell_s": 25})
    assert stop["added"] == "BS1" and network.bus_stops["BS1"].dwell_s == 25
    moved = tools.run_tool(network, "update_bus_stop", {"stop": "BS1", "dwell_s": 40, "direction": "both"})
    assert moved["dwell_s"] == 40
    assert "direction must be" in refused(network, "update_bus_stop", stop="BS1", direction="sideways")
    assert "No bus stop" in refused(network, "update_bus_stop", stop="BS9", dwell_s=1)

    x = (network.nodes["1,1"].x + network.nodes["2,1"].x) / 2
    y = (network.nodes["1,1"].y + network.nodes["1,2"].y) / 2
    school = place(network, "School", x, y, 6000, 100)
    updated = tools.run_tool(network, "update_institution", {"id": school.id, "people": 900, "radius_m": 400})
    assert network.institutions[school.id].people == 900 and updated["reaches"]
    assert "Unknown id" in refused(network, "update_institution", id="IN99", people=1)

    asked = []
    tools.place_download = lambda url: asked.append(url) or json.dumps(
        [{"display_name": "Test Town", "lat": "51.5", "lon": "-0.12"}]).encode()
    try:
        placed = tools.run_tool(network, "place_map", {"place": "Test Town test_experiments"})
    finally:
        tools.place_download = None
    assert placed["placed"] == "Test Town" and network.background.kind == "map" and network.background.lat == 51.5
    assert len(asked) == 1 and "Test%20Town" in asked[0]
    assert "Give a place" in refused(network, "place_map", lat=51.0)


def test_calibration_meets_the_counts():
    network = generate_network(3, 4)
    flows = {("1W", "1E"): 300.0, ("1N", "1S"): 200.0}
    road = next(r for r in tools._routes(network, [("1W", "1E")])[("1W", "1E")] if r in network.roads)
    plan, info = tools.calibration_plan(network, flows, {"counts": [{"road": road, "vph": 600}]})
    assert plan[("1W", "1E")] == 600 and plan[("1N", "1S")] == 200  # only the pair crossing the count moves
    row = info["counts"][0]
    assert row["geh_after"] < 0.5 < row["geh_before"]
    assert abs(tools.geh(600, 600)) < 1e-9 and round(tools.geh(400, 600), 2) == 8.94
    # Fitted on the routes the simulation drives: the matrix loaded on them meets the counts (on shortest-distance
    # routes, a fit of GEH 0 loaded GEH 17-24 in the simulation)
    from collections import Counter

    from traffic_simulator.simulation import Graph
    grid = generate_network(3, 3)
    ends = sorted(grid.approach_nodes)
    prior = {(o, d): 50.0 for o in ends for d in ends if o != d}
    counted = {"1,2-2,2": 600, "2,2-2,1": 300}
    fitted, _ = tools.calibration_plan(grid, prior, {"counts": [{"road": r, "vph": v} for r, v in counted.items()]})
    graph, load = Graph(grid), Counter()
    for pair, value in fitted.items():
        for share, route in graph.route_shares(*pair):
            for link in route:
                load[link.road] += value * share
    assert all(tools.geh(load[road], vph) < 1 for road, vph in counted.items()), load
    for counts, reason in (([{"road": "9,9-8,8", "vph": 5}], "No road"), ([{"road": road, "vph": -1}], "vph")):
        try:
            tools.calibration_plan(network, flows, {"counts": counts})
        except ValueError as exc:
            assert reason in str(exc)
            continue
        raise AssertionError(counts)

    window = window_with_demand(300)
    steps = len(window.history.undo_stack)
    window._od_command("calibrate_demand", {"counts": [{"road": road, "vph": 750}]})
    cells = window.od_matrix.cells  # both ways along the road count together: 375 each
    assert cells[("1W", "1E")].flow == cells[("1E", "1W")].flow == 375 and cells[("1N", "1S")].flow == 300
    assert len(window.history.undo_stack) == steps + 1
    window.close()


def test_vehicle_types_change_by_name():
    window = window_with_demand()
    steps = len(window.history.undo_stack)
    window._types_command("update_vehicle_type", {"name": "car", "length_m": 4.2, "sigma": 0.3, "new_name": "Cars"})
    car = window.vehicle_modes.modes[1]
    assert (car.name, car.length_m, car.sigma) == ("Cars", 4.2, 0.3) and len(window.history.undo_stack) == steps + 1
    for args, reason in (({"name": "Cars", "sigma": 2}, "sigma must be"), ({"name": "Tram", "sigma": 0.1}, "No vehicle type"),
                         ({"name": "Cars", "new_name": "Bus"}, "already"), ({"name": "Cars", "colour": "red"}, "Give what")):
        try:
            window._types_command("update_vehicle_type", args, check_only=True)
        except ValueError as exc:
            assert reason in str(exc), (reason, exc)
            continue
        raise AssertionError(args)
    assert window.vehicle_modes.modes[1].sigma == 0.3
    window.undo()
    assert window.vehicle_modes.modes[1].name == "Car" and window.vehicle_modes.modes[1].length_m == 5.0
    window.close()


def test_the_assistant_tests_the_network_and_its_planning_is_kept():
    window = window_with_demand(500)
    panel = window.ai_mode
    assert "error" in panel._execute("note_plan", {})
    panel._execute("note_plan", {"goal": "Less delay on row 1", "hypothesis": "One lane on 1,1-2,1 costs delay"})
    assert panel.journal[-1]["kind"] == "plan" and "One lane" in panel.journal[-1]["what"]

    built = deepcopy(window.network)
    args = {"question": "Does one lane on 1,1-2,1 add delay?", "duration_min": 5, "seeds": "1-2", "scenarios": [
        {"name": "As built"},
        {"name": "One lane", "changes": [{"tool": "update_roads", "args": {"roads": ["1,1-2,1"], "lanes_per_direction": 1}}]},
        {"name": "Busier", "demand_scale": 1.5}]}
    assert window._runs_command("run_experiment", args, check_only=True) == {"ok": True}
    for bad, reason in (({**args, "scenarios": [{"name": "x", "changes": [{"tool": "generate_grid"}]}]}, "can't be part"),
                        ({**args, "seeds": "1-20", "scenarios": [{"name": f"S{i}"} for i in range(4)]}, "at most"),
                        ({**args, "duration_min": 0}, "duration_min"),
                        ({**args, "scenarios": [{"name": "A", "extensions": ["Nothing Such"]}]}, "No extension"),
                        ({**args, "scenarios": [{"name": "A"}, {"name": "a"}]}, "name of its own")):
        try:
            window._runs_command("run_experiment", bad, check_only=True)
        except ValueError as exc:
            assert reason in str(exc), (reason, exc)
            continue
        raise AssertionError(reason)

    from traffic_simulator.ai_mode import _ToolRequest  # a refused proposal is in the journal too
    request = _ToolRequest({"id": "x", "name": "run_experiment", "arguments": {**args, "duration_min": 0}})
    panel._tool_requested(panel._session, panel._turn, request)
    assert "duration_min" in request.result["error"] and panel.journal[-1]["outcome"].startswith("refused")

    answer = window._runs_command("run_experiment", args)
    assert answer["experiment"] == "E1" and answer["runs"] == answer["of"] == 6 and "caution" in answer
    assert window.network.road("1,1-2,1").lanes_per_direction == 2  # the open network stays as it is
    assert window.network.nodes.keys() == built.nodes.keys() and window.runner is None
    sheets = read_xlsx(answer["file"])
    assert {"Read me", "Comparison", "Scenarios", "Runs", "Planning journal"} <= set(sheets)
    assert any("One lane" in str(cell) for row in sheets["Planning journal"] for cell in row)
    assert len(sheets["Runs"]) == 7 and all(Path(row[3]).exists() for row in sheets["Runs"][1:])  # each run's workbook
    busier = [row for row in answer["comparison"] if row["scenario"] == "Busier" and row["figure"] == "Throughput"][0]
    assert busier["diff"] > 0 and busier["unit"] == "veh/h"  # half as much demand again: more through
    assert busier["test"].startswith("paired") and busier["interval_level"] == 0.975 and "replications_needed" in busier
    lead = [row["figure"] for row in answer["comparison"]]  # the objective and the lead figures, not ~100 rows
    assert lead[0] == answer["objective"] and set(lead) <= {answer["objective"], *window.LEAD_FIGURES}
    assert "more rows" in answer["more"] and all("confidence" not in row for row in answer["comparison"])
    read = window._runs_command("read_results", {"id": "e1"})
    assert read["question"] == args["question"] and read["seeds"] == [1, 2]

    from traffic_simulator.main import MainWindow

    empty = MainWindow()  # no demand: refused, not run with zeros that look like results
    for tool in ("run_simulation", "parameter_sensitivity"):
        try:
            empty._runs_command(tool, {"duration_min": 1}, check_only=True)
        except ValueError as exc:
            assert "no demand to test with" in str(exc).lower(), exc
        else:
            raise AssertionError(f"{tool} with no demand should be refused")
    empty.close()
    for args, why in (({"parameters": ["speed"]}, "parameters are some of"), ({"step": 0.9}, "step must be")):
        try:
            window._runs_command("parameter_sensitivity", {"duration_min": 1, **args}, check_only=True)
        except ValueError as exc:
            assert why in str(exc), exc
        else:
            raise AssertionError(args)
    swept = window._runs_command("parameter_sensitivity", {"duration_min": 1, "seeds": [4], "parameters": ["tau_s"]})
    assert swept["runs"] == 3 and "elasticity" in swept["parameters"]["tau_s"]["throughput_vph"] and "note" in swept
    first = window._runs_command("run_simulation", {"duration_min": 1, "seed": 9})
    again = window._runs_command("run_simulation", {"duration_min": 1, "seed": 9})
    assert (first["run"], again["run"]) == ("R1", "R2") and Path(first["file"]).exists() and first["file"] != again["file"]
    compared = window._runs_command("compare_runs", {"runs": ["R1", "R2"]})
    assert all(value["change"] in (None, 0) for value in compared["runs"][1]["figures"].values())  # same seed, same run
    listed = window._runs_command("read_results", {})
    assert [test["id"] for test in listed["tests"]] == ["E1", "R1", "R2"]
    try:
        window._runs_command("compare_runs", {"runs": ["R1", "E1"]})
    except ValueError as exc:
        assert "run ids" in str(exc)
    else:
        raise AssertionError("compared an experiment")

    window.run_action.trigger()  # the user's own run: no test meanwhile
    try:
        window._runs_command("run_simulation", {"duration_min": 1}, check_only=True)
    except ValueError as exc:
        assert "A run is going on" in str(exc)
    else:
        raise AssertionError("tested during a run")
    window._stop_run()
    window.close()


def test_run_statistics_calibration_and_mode_choice():
    """analyse_run, check_calibration and choose_modes on a run the assistant made."""
    window = window_with_demand(600)
    for tool, args in (("analyse_run", {}), ("check_calibration", {"counts": [{"road": "1,1-2,1", "vph": 5}]})):
        try:
            window._runs_command(tool, args)
        except ValueError as exc:
            assert "run_simulation first" in str(exc)
        else:
            raise AssertionError(f"{tool} with no run")
    run = window._runs_command("run_simulation", {"duration_min": 10, "seed": 3})
    assert run["tti"] >= 1 and run["pti"] >= run["tti"] * 0.9  # reliability is among the headline figures
    assert run["bi"] >= 0 and 0 <= run["unfinished_percent"] < 100 and run["mean_system_trip_s"] >= run["mean_trip_s"]
    found = window._runs_command("analyse_run", {})
    assert found["run"] == "R1" and found["reliability"]["trips"] > 50 and found["reliability"]["p95_s"] >= found["reliability"]["median_s"]
    assert {"1W", "1E", "1N"} <= set(found["arrivals_per_minute"])
    assert found["arrivals_per_minute"]["1W"]["intervals"] == 10 and 5 <= found["arrivals_per_minute"]["1W"]["mean"] <= 15
    measured = {row["road"]: row["flow_vph"] for row in window.tests["R1"]["kpis"]["roads"]}
    road = max(measured, key=measured.get)
    checked = window._runs_command("check_calibration", {"id": "r1", "counts": [{"road": road, "vph": measured[road]}]})
    assert checked["counts"][0]["geh"] == 0 and checked["meets_geh_target"]
    try:
        window._runs_command("check_calibration", {"counts": [{"road": "9,9-8,8", "vph": 5}]})
    except ValueError as exc:
        assert "no road" in str(exc)
    else:
        raise AssertionError("an unknown road")

    alternatives = [{"mode": "Car", "cost_per_km": 25}, {"mode": "Bike", "constant": -1}]
    steps = len(window.history.undo_stack)
    chosen = window._od_command("choose_modes", {"alternatives": alternatives, "from_run": "R1"})
    split = window.od_matrix.split_of(window.od_matrix.cells[("1W", "1E")])
    names = window.vehicle_modes.names()
    assert split[names.index("Bus")] == 0 and split[names.index("Car")] > split[names.index("Bike")] > 0
    assert chosen["pairs_split"] == 3 and "run" in chosen["pairs"][0]["times_from"]
    assert len(window.history.undo_stack) == steps + 1  # one undo step
    dearer = window._od_command("choose_modes", {"alternatives": [{"mode": "Car", "cost_per_km": 300}, alternatives[1]]})
    assert dearer["pairs"][0]["people_percent"]["Car"] < chosen["pairs"][0]["people_percent"]["Car"]  # cost drives it
    for args, reason in (({"alternatives": [{"mode": "Car"}]}, "2 or more"),
                         ({"alternatives": [{"mode": "Car"}, {"mode": "Tram"}]}, "no tram"),
                         ({"alternatives": alternatives, "coefficients": {"cost": 0.1}}, "0 or below"),
                         ({"alternatives": alternatives, "from_run": "R9"}, "No run called R9"),
                         ({"alternatives": alternatives, "pairs": ["2W->2E"]}, "no demand")):
        try:
            window._od_command("choose_modes", args, check_only=True)
        except ValueError as exc:
            assert reason in str(exc), (reason, exc)
            continue
        raise AssertionError(args)
    try:  # a warm-up as long as the run would leave nothing to measure
        window._runs_command("run_simulation", {"duration_min": 2, "measure_from_min": 2}, check_only=True)
    except ValueError as exc:
        assert "measure_from_min" in str(exc)
    else:
        raise AssertionError("a warm-up as long as the run")
    warmed = window._runs_command("run_simulation", {"duration_min": 2, "seed": 3, "measure_from_min": 1})
    assert window.tests[warmed["run"]]["kpis"]["measure_from_s"] == 60
    window.close()


def test_a_test_with_an_agent_says_its_timing_is_as_at_1x():
    from traffic_simulator.main import AGENT_TIMING

    window = window_with_demand(300)
    plain = window._runs_command("run_simulation", {"duration_min": 1, "seed": 1})
    assert "agent_timing" not in plain  # nothing that asks an agent: nothing to say
    window._runs_command("set_extension", {"name": "TSP", "on": True})
    with_tsp = window._runs_command("run_simulation", {"duration_min": 1, "seed": 1})
    assert with_tsp["agent_timing"] == AGENT_TIMING and "as if the run went at 1x" in AGENT_TIMING
    plain = window._runs_command("run_experiment", {"duration_min": 1, "seeds": [1], "scenarios": [{"name": "As built"}]})
    assert "agent_timing" not in plain  # an experiment ticks only its scenarios' extensions: none here
    found = window._runs_command("run_experiment", {"duration_min": 1, "seeds": [1], "scenarios": [
        {"name": "As built"}, {"name": "With TSP", "extensions": ["TSP"]}]})
    assert found["agent_timing"] == AGENT_TIMING
    read_me = {row[0]: row[1] for row in read_xlsx(found["file"])["Read me"] if len(row) > 1}
    assert read_me["AI agent timing"] == AGENT_TIMING
    window.close()


def test_extensions_are_set_by_name():
    window = window_with_demand()
    panel = window.extensions
    done = window._runs_command("set_extension", {"name": "batch run", "on": True, "settings": {"Seeds": "3-5"}})
    assert done["ticked"] and "3-5" in done["settings"]
    for args, reason in (({"name": "Nothing"}, "No extension"), ({"name": "Batch Run", "settings": {"Colour": 1}}, "no setting"),
                         ({"name": "Batch Run", "settings": {"Duration": "a week"}}, "can be")):
        try:
            window._runs_command("set_extension", args, check_only=True)
        except ValueError as exc:
            assert reason in str(exc), (reason, exc)
            continue
        raise AssertionError(args)
    assert panel.overrides_for([]) is None and panel.overrides_for(["Batch Run"]) is None  # no signal hook
    assert panel.overrides_for(["TSP"]) is not None and panel.find("transit signal").name == "Transit Signal Priority (TSP)"
    window.close()


def test_the_assistant_works_every_extension():
    """read_extensions, schedule_emergency, start_batch and replay_run, through AI Mode's own entry: each checked
    first (a refusal changes nothing), then done in the extension's own panel."""
    window = window_with_demand()
    run = window.ai_mode._execute  # as an approved tool call runs
    found = run("read_extensions", {})
    named = {extension["name"]: extension for extension in found["extensions"]}
    assert {"Batch Run", "Transit Signal Priority (TSP)", "Dynamic Emergency Vehicle Lane (DEVL)",
            "Dispatch Emergency Vehicle", "Research Log"} <= set(named)
    devl = named["Dynamic Emergency Vehicle Lane (DEVL)"]["settings"]
    assert devl["Decided by"]["can_be"] == ["System 1", "System 2", "Multiple"] and "Models" in devl
    assert named["Batch Run"]["settings"]["Seeds"]["value"] and found["emergency_schedules"] == [
        {"from": "", "to": "", "every_min": 0, "after_min": 0}]  # the panel's first, empty row

    module = window._extension("dispatch_emergency").module
    for rows, reason in (([{"from": "1W", "to": "nowhere"}], "No end or junction called nowhere"),
                         ([{"from": "1W", "to": "1W"}], "No route"),
                         ([{"from": "1W", "to": "1E", "every_min": 5, "after_min": 8}], "not both"),
                         ([{"from": "1W", "to": "1E", "every_min": 500}], "0 to 120"), ("[1]", "a list of")):
        assert reason in run("schedule_emergency", {"schedules": rows})["error"], reason
    assert "Start a run first" in run("schedule_emergency", {"schedules": [{"from": "1W", "to": "1E"}],
                                                             "dispatch_now": True})["error"]
    assert module.fleet() == [("", "", 0, 0)]  # refused: untouched
    done = run("schedule_emergency", {"schedules": [{"from": "1W", "to": "1E", "every_min": 5},
                                                    {"from": "1E", "to": "2,1", "after_min": 8}]})
    assert done["schedules"][1] == {"from": "1E", "to": "2,1", "every_min": 0, "after_min": 8}
    assert module.fleet() == [("1W", "1E", 5, 0), ("1E", "2,1", 0, 8)]  # the panel's rows, After shown as After
    assert run("read_extensions", {})["emergency_schedules"][0] == {"from": "1W", "to": "1E", "every_min": 5, "after_min": 0}

    for args, reason in (({"seeds": "4-1", "duration_min": 5}, "low to high"), ({"seeds": "1", "duration_min": 0}, "1 to"),
                         ({"seeds": "one", "duration_min": 5}, "Can't read")):
        try:
            window._runs_command("start_batch", args, check_only=True)
        except ValueError as exc:
            assert reason in str(exc), (reason, exc)
            continue
        raise AssertionError(args)
    batch = window._extension("batch_run").controls
    done = run("start_batch", {"seeds": [1, 2], "duration_min": 1})  # a list, as the schema asks
    assert done["runs"] == done["of"] == 2 and Path(done["file"]).is_file()
    assert batch.findChild(QLineEdit).text() == "1, 2" and "Saved" in " ".join(l.text() for l in batch.findChildren(QLabel))
    assert "No workbook at" in run("replay_run", {"path": "nowhere.xlsx"})["error"]
    window.close()


def test_the_assistant_analyses_runs_and_experiments():
    """junction_los, diagnose_signal, check_run_health, emissions_report, resilience, power_analysis, rank_scenarios
    and preflight_check through AI Mode's own entry, on this session's runs and experiments."""
    window = window_with_demand(500)
    for node in window.network.nodes.values():
        node.control = "signalised"
    run = window.ai_mode._execute
    first = run("run_simulation", {"duration_min": 6, "seed": 1})
    second = run("run_simulation", {"duration_min": 6, "seed": 2})
    assert "co2_kg" in first and first["co2_kg"] > 0  # headless runs report their cars' CO2
    los = run("junction_los", {"id": first["run"]})
    assert los["run"] == first["run"] and los["junctions"] and los["junctions"][0]["los"] in "ABCDEF"
    signal = run("diagnose_signal", {"id": first["run"], "junction": los["junctions"][0]["junction"]})
    assert signal["signals"][0]["causes"] and "critical_v_c" in signal["signals"][0]
    assert "isn't a signalised junction" in run("diagnose_signal", {"junction": "nowhere"})["error"]
    health = run("check_run_health", {})  # the latest run
    assert health["run"] == second["run"] and "healthy" in health and "gridlock" in health["checks"]
    fuel = run("emissions_report", {"id": first["run"]})
    assert abs(fuel["total"]["co2_kg"] - first["co2_kg"]) < 0.05
    found = run("resilience", {"baseline": first["run"], "disrupted": second["run"]})
    assert found["baseline"] == first["run"] and "Different seeds" in found["caution"]
    assert "two different runs" in run("resilience", {"baseline": first["run"], "disrupted": first["run"]})["error"]

    experiment = run("run_experiment", {"duration_min": 5, "seeds": [1, 2, 3], "scenarios": [
        {"name": "As built"}, {"name": "Busier", "demand_scale": 1.5}, {"name": "Quieter", "demand_scale": 0.5}]})
    power = run("power_analysis", {"experiment": experiment["experiment"], "figure": "Delay", "scenario": "Busier"})
    assert power["seeds_now"] == 3 and power["sd_of_differences"] >= 0 and power["curve"]
    assert power["baseline"] == "As built" and "power_with_them" in power
    direct = run("power_analysis", {"sd": 1.0, "difference": 1.0})
    assert direct["seeds_needed"] == 10
    assert "No figure" in run("power_analysis", {"experiment": experiment["experiment"], "figure": "beauty"})["error"]
    ranked = run("rank_scenarios", {"experiment": experiment["experiment"], "figure": "throughput"})
    assert [row["scenario"] for row in ranked["ranked"]][0] == "Busier"  # most throughput first (higher is better)
    assert ranked["friedman"]["seeds"] == 3 and len(ranked["pairs"]) == 3
    assert "No experiment" in run("rank_scenarios", {"experiment": "E99", "figure": "Delay"})["error"]
    check = run("preflight_check", {"duration_min": 5})
    assert "at_10_percent" in check and "healthy" in check["at_50_percent"] and isinstance(check["ok"], bool)
    window.close()


def test_incidents_and_weather_in_runs_and_experiments():
    found = experiments.incidents_from([{"road": "1,1-2,1", "lanes": 1, "at_min": 5, "for_min": 10}])
    assert found[0]["start_s"] == 300 and found[0]["end_s"] == 900 and found[0]["length_m"] == 50
    for bad, reason in (([{"lanes": 1}], "road needed"), ([{"road": "x", "colour": 1}], "unknown: colour"),
                        ([{"road": "x", "for_min": 0}], "above 0"), ("closed", "a list like")):
        try:
            experiments.incidents_from(bad)
        except ValueError as exc:
            assert reason in str(exc), (bad, exc)
        else:
            raise AssertionError(bad)
    network = generate_network(3, 4)
    one, again = (experiments.draw_incidents(network, 7, 3600, {"per_hour": 4}) for _ in range(2))
    assert one == again and one != experiments.draw_incidents(network, 8, 3600, {"per_hour": 4})  # the seed's own
    assert all(spec["road"] in network.roads and spec["end_s"] - spec["start_s"] == 900 for spec in one)

    window = window_with_demand(400)
    run = window.ai_mode._execute
    road = next(r.id for r in window.network.roads.values() if r.length_m > 200)
    assert "No road called" in run("run_simulation", {"duration_min": 2, "incidents": [{"road": "nowhere"}]})["error"]
    assert "weather is one of" in run("run_simulation", {"duration_min": 2, "weather": "hail"})["error"]
    assert "isn't an on-ramp" in run("run_simulation", {"duration_min": 2, "meters": [{"road": road, "rate_vph": 300}]})["error"]
    wet = run("run_simulation", {"duration_min": 4, "seed": 1, "weather": "heavy rain",
                                 "incidents": [{"road": road, "lanes": 1, "at_min": 1, "for_min": 2}]})
    assert wet["run"] and window.tests[wet["run"]]["kpis"]["observed"]["weather"] == "heavy rain"
    assert window.tests[wet["run"]]["kpis"]["observed"]["closures"]  # both directions of the road
    experiment = run("run_experiment", {"duration_min": 3, "seeds": [1, 2], "scenarios": [
        {"name": "Clear"}, {"name": "Snow", "weather": "heavy snow"},
        {"name": "Crash", "incidents": [{"road": road, "lanes": 2, "at_min": 0.5, "for_min": 2}]},
        {"name": "Random", "random_incidents": {"per_hour": 6}}]})
    assert experiment["runs"] == 8
    sheets = read_xlsx(experiment["file"])
    rows = {row[0]: row for row in sheets["Scenarios"][1:]}
    assert rows["Snow"][4] == "heavy snow" and road in rows["Crash"][5] and "per_hour" in rows["Random"][6]
    assert "Scenario 2: weather is one of" in run("run_experiment", {"duration_min": 1, "seeds": [1], "scenarios": [
        {"name": "A"}, {"name": "B", "weather": "hail"}]})["error"]
    window.close()


def test_lateral_position_in_runs_and_experiments():
    # Lateral position and filtering (Simulation lateral) is a run setting: a headless run's, a scenario's (kept under a
    # condition, set by a factor's level) and the assistant's run_simulation's, said on the workbook's Run sheet.
    scenario = experiments.scenario_from({"name": "Dhaka", "lateral": True}, 0)
    assert scenario.lateral and not experiments.scenario_from({"name": "Plain"}, 1).lateral
    assert experiments.under(scenario, experiments.conditions_from([{"name": "peak", "demand_scale": 1.2},
                                                                    {"name": "rain", "weather": "heavy rain"}])[0]).lateral
    specs, _ = experiments.factorial([{"name": "lanes", "levels": [{"label": "kept"}, {"label": "weak", "lateral": True}]}])
    assert [experiments.scenario_from(spec, i).lateral for i, spec in enumerate(specs)] == [False, True]
    try:
        experiments.scenario_from({"name": "Bad", "lateral": "yes"}, 0)
    except ValueError as exc:
        assert "lateral is true or false" in str(exc)
    else:
        raise AssertionError("a lateral that isn't true or false is refused")
    network, modes = generate_network(1, 1), VehicleModes().modes
    simulation, _ = experiments.run_headless(network, [PairDemand("1W", "1E", 600, None, [], "Poisson", (0, 50, 50))],
                                             modes, 3, 120, lateral=True)
    assert simulation.lateral and any(v.lat_m for v in simulation.vehicles)

    window = window_with_demand(400)
    run = window.ai_mode._execute
    assert "lateral must be true or false" in run("run_simulation", {"duration_min": 1, "lateral": "yes"})["error"]
    done = run("run_simulation", {"duration_min": 2, "seed": 1, "lateral": True})
    settings = {row[0]: row[1] for row in read_xlsx(window.tests[done["run"]]["file"])["Run"][1:] if len(row) > 1}
    assert settings["Lateral position (sublane)"] == "on"
    window.close()


def test_conditions_factorials_equilibrium_and_elastic_demand():
    found = experiments.conditions_from([{"name": "peak", "demand_scale": 1.2, "weight": 2}, {"name": "rain", "weather": "heavy rain"}])
    assert found[0]["demand_scale"] == 1.2 and found[1]["weather"] == "heavy rain" and found[1]["weight"] == 1.0
    for bad, reason in (([{"name": "a"}], "a list of 2 to"), ([{"name": "a"}, {"name": "A"}], "its own name"),
                        ([{"name": "a"}, {"name": "b", "weather": "hail"}], "weather one of")):
        try:
            experiments.conditions_from(bad)
        except ValueError as exc:
            assert reason in str(exc), (bad, exc)
        else:
            raise AssertionError(bad)
    runs = [{"scenario": f"{design} | {condition}", "seed": seed, "measures": {"delay_veh_h": value + seed / 10}}
            for design, condition, value in (("A", "calm", 10), ("B", "calm", 12), ("A", "busy", 30), ("B", "busy", 20))
            for seed in (1, 2)]
    summary = experiments.condition_summary(runs, ["A", "B"], [{"name": "calm", "weight": 3.0}, {"name": "busy", "weight": 1.0}],
                                            "delay_veh_h", False)
    assert summary["by_condition"]["calm"]["winner"] == "A" and summary["by_condition"]["busy"]["winner"] == "B"
    assert not summary["same_winner_everywhere"]
    regret = {d["design"]: d["worst_regret"] for d in summary["designs"]}
    assert abs(regret["A"] - 10) < 1e-9 and abs(regret["B"] - 2) < 1e-9 and summary["robust_choice"] == "B"
    weighted = {d["design"]: d["weighted_mean"] for d in summary["designs"]}
    assert abs(weighted["A"] - (3 * 10.15 + 30.15) / 4) < 1e-9

    window = window_with_demand(400)
    run = window.ai_mode._execute
    road = next(iter(window.network.roads))
    # 3 minutes: the grid's trips are about 2 km, so a 2-minute run finishes none and has no trip figures to compare
    conditioned = run("run_experiment", {"duration_min": 3, "seeds": [1, 2], "scenarios": [
        {"name": "As built"}, {"name": "Wider", "changes": [{"tool": "update_roads", "args": {"roads": [road], "lanes_per_direction": 3}}]}],
        "conditions": [{"name": "today"}, {"name": "growth", "demand_scale": 1.4, "weight": 2}], "objective": "Delay"})
    assert conditioned["runs"] == 8 and set(conditioned["conditions"]["by_condition"]) == {"today", "growth"}
    assert conditioned["conditions"]["objective"] == "Delay" and conditioned["conditions"]["robust_choice"] in ("As built", "Wider")
    baselines = {row["scenario"] for row in conditioned["comparison"] if row.get("diff") is None}
    assert baselines == {"As built | today", "As built | growth"}  # each condition against its own baseline
    assert "objective is one of" in run("run_experiment", {"duration_min": 2, "seeds": [1], "objective": "joy", "scenarios": [
        {"name": "A"}]})["error"]
    factorial = run("run_experiment", {"duration_min": 2, "seeds": [1, 2], "factors": [
        {"name": "lanes", "levels": [{"label": "two"}, {"label": "three", "changes": [
            {"tool": "update_roads", "args": {"roads": [road], "lanes_per_direction": 3}}]}]},
        {"name": "demand", "levels": [{"label": "now"}, {"label": "more", "demand_scale": 1.5}]}]})
    anova = factorial["factorial"]["anova"]["all"]
    assert factorial["runs"] == 8 and set(anova) >= {"lanes", "demand", "interaction"} and anova["replicates"] == 2
    assert "not both" in run("run_experiment", {"duration_min": 2, "seeds": [1], "scenarios": [{"name": "A"}],
                                                "factors": [{"name": "x", "levels": [{"label": "a"}, {"label": "b"}]}]})["error"]
    routed = run("run_experiment", {"duration_min": 2, "seeds": [1], "scenarios": [
        {"name": "Fastest"}, {"name": "Equilibrium", "assign_routes": {"iterations": 2, "until_gap": 0.3}}]})
    assert routed["runs"] == 2 and "error" not in routed
    settled = run("assign_routes", {"iterations": 2, "duration_min": 2, "until_gap": 0.5})
    assert "converged" in settled and settled["iterations"]
    assert len({step["seed"] for step in settled["iterations"]}) == 1 and all("excess_cost" in step for step in settled["iterations"])

    before = run("run_simulation", {"duration_min": 6, "seed": 3})
    window.network.route_choice = {}
    for item in window.network.roads.values():
        item.speed_limit_kmh = 70.0  # a faster network than the reference's
    total_before = sum(cell.flow or 0 for cell in window.od_matrix.cells.values())
    assert "reference" in run("equilibrate_demand", {})["error"]
    assert "elasticity must be below 0" in run("equilibrate_demand", {"reference": before["run"], "elasticity": 0.5})["error"]
    grown = run("equilibrate_demand", {"reference": before["run"], "iterations": 2, "duration_min": 6})
    assert grown["total_vph_after"] > grown["total_vph_before"] and grown["changed_percent"] > 0  # induced traffic
    assert all("largest_gap" in step for step in grown["iterations"])  # stopped on the residual, not the step
    assert sum(cell.flow or 0 for cell in window.od_matrix.cells.values()) > total_before  # typed into the grid
    window.close()


def test_an_agents_decisions_are_read_and_probed():
    window = window_with_demand(300)
    for node in window.network.nodes.values():
        node.control = "signalised"
    window.vehicle_modes.modes[0].headway_min = 2  # buses every 2 minutes: TSP requests
    asked = []

    def fake(question, choice=None):  # TRUE exactly when the bus's exit lane has room: the fact it relies on
        asked.append(question)
        return {"answer": bool(question["telemetry"]["request"]["exit_has_room"]), "model": "fake", "via": "tool"}

    window._ask_agent = fake
    run = window.ai_mode._execute
    run("set_extension", {"name": "TSP", "on": True})
    done = run("run_simulation", {"duration_min": 12, "seed": 2})
    found = run("read_decisions", {"id": done["run"]})
    assert found["in_run"] > 0 and found["decisions"][0]["extension"] == "TSP"
    first = found["decisions"][0]
    assert first["Telemetry read"]["schema"] == "acms-tsp-telemetry/1" and "person_delay" in first["Telemetry read"]
    junction = first["Junction"]
    narrowed = run("read_decisions", {"id": done["run"], "junction": junction, "limit": 2})
    assert all(row["Junction"] == junction for row in narrowed["decisions"]) and len(narrowed["decisions"]) <= 2
    assert run("read_decisions", {"id": done["run"], "from_min": 99})["decisions"] == []
    probed = run("decision_sensitivity", {"id": done["run"], "sample": 3})
    assert probed["same_answer_again_percent"] == 100.0
    assert probed["flips_percent"]["exit_full"] == 100.0  # every answer flips when the exit fills (the fake's rule)
    assert probed["flips_percent"]["no_passengers"] == probed["flips_percent"]["no_cross_queues"] == 0.0
    assert "perturbations are some of" in run("decision_sensitivity", {"id": done["run"], "perturbations": ["rain"]})["error"]
    window.close()


def test_the_assistant_audits_designs_access_and_value():
    window = window_with_demand(400)
    run = window.ai_mode._execute
    audit = run("design_audit", {})
    assert set(audit["counts"]) == {"sight_on_curves", "intersection_sight", "turning_paths", "skew", "junction_spacing",
                                    "lane_balance"} and "Green Book" in audit["rules"]
    assert "No buildings" in run("accessibility_report", {})["error"]
    tools.deploy_institutions(window.network, 8, seed=2)
    access = run("accessibility_report", {"budgets_min": [10]})
    assert access["times"].startswith("free flow") and access["most_accessible_junctions"]
    done = run("run_simulation", {"duration_min": 4, "seed": 1})
    assert done["health"] == "fine" or isinstance(done["health"], list)  # a run's health comes with it
    measured = run("accessibility_report", {"id": done["run"], "budgets_min": [10], "close_road": next(iter(window.network.roads))})
    assert measured["times"] == "measured in the run" and "closing" in measured
    links = run("critical_links", {"limit": 3})
    assert "bridge_roads" in links and "cut_junctions" in links
    node = next(iter(window.network.nodes.values()))
    area = run("critical_links", {"area": {"x": node.x, "y": node.y, "radius_m": 60}})
    assert area["roads_closed"] and "extra_veh_h_per_h" in area
    safety = run("analyse_safety", {"id": done["run"], "speed_change_kmh": [50, 40]})
    assert safety["speed_change"]["fatal_percent"] == round(100 * ((40 / 50) ** 4 - 1), 1)
    assert "[before, after]" in run("analyse_safety", {"id": done["run"], "speed_change_kmh": [50]})["error"]
    unknown = run("describe", {"id": "1,1-9,9"})
    assert unknown["error_type"] == "unknown_id" and "suggested_fix" in unknown  # refusals come back as data

    experiment = run("run_experiment", {"duration_min": 3, "seeds": [1, 2, 3], "scenarios": [
        {"name": "As built"}, {"name": "Quieter", "demand_scale": 0.6}]})
    value = run("benefit_cost", {"experiment": experiment["experiment"], "capital_cost": 1_000_000})
    assert [row["rate_percent"] for row in value["by_discount_rate"]] == [3.5, 4.0, 7.0] and value["seeds"] == 3
    low, high = value["by_discount_rate"][0], value["by_discount_rate"][2]
    assert low["pv_benefits"] >= high["pv_benefits"] or value["yearly_benefit"] < 0  # discounting harder: worth less
    factor = (1 - 1.07 ** -30) / 0.07
    assert abs(high["pv_benefits"] - value["yearly_benefit"] * factor) <= abs(value["yearly_benefit"]) * 0.01 + 2
    assert high["pv_costs"] == 1_000_000 and value["bcr_interval_over_seeds"] is not None
    assert "trips against" in value["caution"]  # 60 % of the demand: fewer trips served, said so
    assert "capital_cost" in run("benefit_cost", {"experiment": experiment["experiment"]})["error"]
    assert "scenario is one of" in run("benefit_cost", {"experiment": experiment["experiment"], "capital_cost": 1,
                                                        "scenario": "nothing"})["error"]
    window.close()


def test_an_agent_is_tested_on_a_fixed_battery():
    window = window_with_demand(300)
    run = window.ai_mode._execute
    assert "TSP or DEVL" in run("test_agent_compliance", {"extension": "Batch Run"})["error"]
    assert "No extension called" in run("test_agent_compliance", {"extension": "nothing"})["error"]
    found = run("test_agent_compliance", {"extension": "DEVL", "seeds": [1], "duration_min": 4})
    assert found["extension"] == "Dynamic Emergency Vehicle Lane (DEVL)" and found["runs"] in (3, 4)
    assert 0 <= found["pass_rate"] <= 1 and found["passed"] + len(found["failures"]) == found["runs"]
    assert len(found["invariants"]) == 4
    window.close()


def test_emergency_vehicles_join_a_run_and_a_batch_tests_the_devl():
    import time

    window = window_with_demand()
    for args, reason in ((("1W", "1E"), "Start a run first"),):
        try:
            window.dispatch_emergency(*args)
        except ValueError as exc:
            assert reason in str(exc)
        else:
            raise AssertionError("no run: nothing to dispatch into")
    window._run()
    try:
        for args, reason in ((("1W", "nowhere"), "No end or junction called nowhere"), (("1W", "1W"), "No route")):
            try:
                window.dispatch_emergency(*args)
            except ValueError as exc:
                assert reason in str(exc), exc
            else:
                raise AssertionError(args)
        window.dispatch_emergency("1W", "2,1", 2)  # to a junction, every 2 min
        simulation, waited = window.runner.simulation, time.monotonic()
        while not simulation.emergency_trips and time.monotonic() - waited < 5:
            time.sleep(0.05)
        assert list(simulation.emergency_trips.values())[0]["to"] == "2,1"
    finally:
        window._stop_runner()
    assert window.tsp_batch()["agents"] == []  # TSP decided by System 1, AI Mode's System 1 Off: the baseline alone

    _, _, start, end, _, every = window._extension("dispatch_emergency").module._lines[0]
    try:
        window.run_batch([1], 5, test_tsp=False, test_devl=True)
    except ValueError as exc:
        assert "no end or junction called (blank)" in str(exc)
    else:
        raise AssertionError("an empty line can't be dispatched")
    start.setCurrentText("1W")
    end.setCurrentText("1E")
    every.setValue(1)
    dispatch = window._extension("dispatch_emergency")
    dispatch.check.setChecked(False)
    assert window._tick_extension("dispatch_emergency") and dispatch.check.isChecked()  # Batch Run's Test DEVL does
    seen, watch = [], QTimer()  # the menu bar's clock while the batch runs headless: its run's time, and its name
    drawn = []  # Show runs on map (on at first): the batch's run drawn as it goes
    watch.timeout.connect(lambda: seen.append((window.clock_label.text(), window.clock_label.toolTip(),
                                               window.statusBar().currentMessage()))
                          or drawn.append(window.scene.frame))
    watch.start(100)
    unpaced = experiments.run_headless  # paced at 60x here (5 s), so the clock and map are seen as it goes
    experiments.run_headless = lambda *args, **kwargs: unpaced(*args, **{**kwargs, "speed": kwargs.get("speed") or 60})
    try:
        done = window.run_batch([1], 5, test_tsp=False, test_devl=True)
    finally:
        watch.stop()
        experiments.run_headless = unpaced
    assert any(text not in ("00:00:00", "0:00:00") for text, _, _ in seen), seen[:3]
    assert any("Never preempt (baseline), seed 1:" in tip and "of 00:05:00" in tip for _, tip, _ in seen)  # by name
    assert any(text == tip.split(": ")[1].split(" of")[0] for text, tip, _ in seen if "seed 1:" in tip)  # its time
    assert any("left" in message for _, _, message in seen)  # the batch's time left
    assert window.clock_label.toolTip() == "Simulation time" and not window._headless  # back to the live clock
    shown = [frame for frame in drawn if frame is not None]
    assert window.show_runs and shown and any(frame.vehicles for frame in shown)  # its cars on the map
    assert window.scene.frame is None  # and gone once it ended
    batch_book = read_xlsx(done["file"])  # the batch compares emergency travel time, labelled, lower better
    labelled = {row[0]: row[1] for row in batch_book["Comparison"][1:] if row}
    assert labelled.get("Emergency travel time, mean (dispatch to arrival)") == "s"
    assert "Emergency vehicles arrived" in labelled and "emergency_trip_s" not in labelled
    book = read_xlsx(next(Path(done["folder"]).glob("* seed 1 always.xlsx")))
    sheet = book["Emergency vehicles"]
    trips = [row for row in sheet[1:] if row and str(row[0]).startswith("Emergency")]
    fixed = [trip for trip in trips if trip[1:3] == ["1W", "1E"]]
    assert len(fixed) == 5 and len(trips) == 6  # at 0, 1, 2, 3 and 4 min, and the seed's own random one
    assert isinstance(trips[0][5], (int, float)) and trips[0][5] > 0  # the first arrived
    run = {row[0]: row[1] for row in book["Run"][1:] if len(row) > 1}
    assert "1W to 1E every 60 s" in run["Emergency vehicles"]
    chosen = run["Random emergency vehicle (from the seed)"]  # seed 1's, within the 5 min run
    start_at, _, end_at, _, at = chosen.split(" ")
    assert start_at in window.places() and end_at in window.places() and "00:00:00" <= at <= "00:05:00"
    window._show_runs_on_map(False)  # off: the map shows the network alone
    drawn.clear()
    watch.start(100)
    experiments.run_headless = lambda *args, **kwargs: unpaced(*args, **{**kwargs, "speed": kwargs.get("speed") or 60})
    try:
        again = window.run_batch([1], 5, test_tsp=False, test_devl=True)  # the same seed: the same random vehicle
    finally:
        watch.stop()
        experiments.run_headless = unpaced
    assert drawn and not any(frame is not None for frame in drawn)
    window._show_runs_on_map(True)
    book2 = read_xlsx(next(Path(again["folder"]).glob("*.xlsx")))
    assert {row[0]: row[1] for row in book2["Run"][1:] if len(row) > 1}["Random emergency vehicle (from the seed)"] == chosen
    never = read_xlsx(next(Path(done["folder"]).glob("* seed 1 never.xlsx")))  # general traffic: no lane ever cleared
    assert {row[7] for row in never["DEVL decisions"][1:]} == {"baseline: FALSE"}
    assert {row[0]: row[1] for row in never["DEVL"][1:] if len(row) > 1}["Lanes cleared"] == 0
    decisions = book["DEVL decisions"]  # Always preempt: the rule, every lane cleared
    assert len(decisions) > 1 and {row[7] for row in decisions[1:]} == {"baseline: TRUE"}
    assert decisions[1][1:4] == ["1,1", "1W", 0] and isinstance(decisions[1][10], (int, float))  # its lane went green
    devl = {row[0]: row[1] for row in book["DEVL"][1:] if len(row) > 1}
    assert devl["Lanes cleared"] > 0 and devl["Lane green, in all (s)"] > 0
    try:
        window.run_batch([1], 5, test_tsp=False, test_devl=True, devl_agents=["System 9: nobody"])
    except ValueError as exc:
        assert "Not found on this PC" in str(exc)
    else:
        raise AssertionError("a model that isn't found can't decide DEVL")
    window.close()


def test_the_largest_share_of_a_demand_that_holds():
    tried = []

    def check(share):
        tried.append(share)
        return {"holds": share <= 0.6, "build_up": 0.0}

    share, _ = experiments.largest_holding(check)
    assert tried == [1.0, 0.5, 0.75, 0.625, 0.5625] and share == 0.5625  # the whole, then bisection
    assert experiments.largest_holding(lambda share: {"holds": True})[0] == 1.0  # holds: one run
    assert experiments.largest_holding(lambda share: {"holds": False})[0] == 0.0  # nothing held
    assert experiments.largest_holding(lambda share: {"holds": None})[0] is None  # stopped
    # One seed decides a clear verdict; a borderline one (build-up near HOLD_BUILD_UP) goes to three seeds' majority
    seen = []

    def by_seed(seed, build_ups={1: 0.06, 2: 0.03, 3: 0.04}):
        seen.append(seed)
        return {"holds": build_ups[seed] <= experiments.HOLD_BUILD_UP, "build_up": build_ups[seed]}

    verdict = experiments.holds_by_majority(by_seed, 1)
    assert seen == [1, 2, 3] and verdict["holds"] is True and verdict["build_ups"] == [0.06, 0.03, 0.04]
    seen.clear()
    assert experiments.holds_by_majority(lambda seed: {"holds": False, "build_up": 0.4}, 1)["holds"] is False
    curve = [0.0] * 24
    curve[8] = 1.0
    daily = PairDemand("1W", "1E", None, 2400, curve, "Random", (0, 100, 0))
    peak = experiments.at_busiest_hour([daily, PairDemand("1N", "1S", None, 240, [], "Random", (0, 100, 0))])
    assert peak[0].hourly == 2400 and peak[1].hourly == 10 and peak[0].daily is None  # 08:00, and evenly


def test_a_run_shows_whether_the_network_holds_a_demand():
    modes = VehicleModes().modes
    network = generate_network(1, 1)
    light = experiments.holds(network, [PairDemand("1W", "1E", 300, None, [], "Random", (0, 100, 0))], modes, seed=1)
    assert light["holds"] and abs(light["build_up"]) <= experiments.HOLD_BUILD_UP
    heavy = experiments.holds(network, [PairDemand("1W", "1E", 5000, None, [], "Random", (0, 100, 0))], modes, seed=1)
    assert heavy["holds"] is False and heavy["build_up"] > 0.3  # far over what a green can pass: it piles up


def test_a_demand_that_never_holds_says_what_might_help():
    # qwen3.5, Dhanmondi from OpenStreetMap (243 of 246 junctions give way): the bare "jams even at 6 %" left it guessing
    window = window_with_demand()
    for node in window.network.nodes.values():
        node.control = "uncontrolled"
    window._in_background = lambda work, what: (0, [(1.0, {"holds": False}), (0.06, {"holds": False})])
    try:
        window._check_demand([("1W", "1E")])
    except ValueError as exc:
        assert "jams even at 6%" in str(exc) and "only give way" in str(exc) and "set_junction_control" in str(exc)
    else:
        raise AssertionError("nothing held: refused")
    for node in window.network.nodes.values():
        node.control = "signalised"
    try:
        window._check_demand([("1W", "1E")])
    except ValueError as exc:
        assert "only give way" not in str(exc)  # signalised already: no such advice
    window.close()


def test_the_window_checks_a_sized_demand_in_a_run():
    from traffic_simulator.main import MainWindow

    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    matrix = window.od_matrix
    matrix.set_ends(*window._od_ends())
    pair = (matrix.origins[0], next(d for d in matrix.destinations if d != matrix.origins[0]))
    matrix.cells[pair] = matrix.cell(pair)
    matrix.cells[pair].flow = 200
    share, report = window._check_demand([pair])
    assert share == 1.0 and report["checked"] and len(report["tried"]) == 1  # light: it holds at once
    matrix.cells[pair].flow = 9000  # far more than one approach takes
    share, report = window._check_demand([pair])
    assert 0 < share < 0.5 and report["kept"] == f"{share:.0%}" and len(report["tried"]) == 1 + experiments.HOLD_STEPS
    window.close()


if __name__ == "__main__":
    for name, check in list(globals().items()):
        if name.startswith("test_"):
            check()
    print("ok")

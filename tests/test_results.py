"""A run's results: the Excel writer, the KPIs recorded from real traffic, the temporary folder kept
minute by minute, the workbook when the run ends, recovery after a crash, the Runner's sampling
(never stopping the traffic), and in the window: the Results page, a workbook when a run stops, and
Export. Run with `python tests/test_results.py` (or the test monitor)."""

import os
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from traffic_simulator import results  # noqa: E402
from traffic_simulator.mode_behavior import VehicleModes  # noqa: E402
from traffic_simulator.network_generator import generate_network  # noqa: E402
from traffic_simulator.simulation import DT, PairDemand, Runner, Simulation  # noqa: E402
from traffic_simulator.xlsx import read_xlsx, write_xlsx  # noqa: E402,F401 (read_xlsx: other tests import it here)

APP = QApplication.instance() or QApplication([])
MODES = VehicleModes().modes  # Bus (occupancy 20), Car (1.5), Bike (1)


def test_the_excel_writer_names_every_sheet_apart():
    with tempfile.TemporaryDirectory() as folder:  # these once took the same name over and over, for ever
        names = ["A" * 31, "A" * 28 + " 2", "a" * 31, "A" * 31, "Roads", "roads", "x:y", "x y"]
        path = write_xlsx(Path(folder) / "book.xlsx", [(name, [[1]]) for name in names])
        written = list(read_xlsx(path))
        assert len({name.lower() for name in written}) == len(names) and max(map(len, written)) <= 31


def test_the_excel_writer_keeps_numbers_and_text():
    with tempfile.TemporaryDirectory() as folder:
        path = write_xlsx(Path(folder) / "book.xlsx", [
            ("Figures", [["Name", "Value"], ["flow & <queue>", 12], ["speed", 43.25], ["none", None], ["bad\x01text", True]]),
            ("A/very:long*name[that]is?more than thirty-one", [["x"], [1]])])
        sheets = read_xlsx(path)
        assert list(sheets) == ["Figures", "A very long name that is more t"]  # Excel's rules for names
        assert sheets["Figures"][1:] == [["flow & <queue>", 12.0], ["speed", 43.25], ["none"], ["badtext", "yes"]]
        assert not path.with_suffix(".part").exists()


def demand():
    return [PairDemand(o, d, 500, None, [], "Poisson", (10, 85, 5))
            for o, d in (("1W", "1E"), ("1E", "1W"), ("1S", "2N"), ("2N", "1S"))]


def recorded(seconds: float, folder: Path, finish=True):
    network = generate_network(2, 2)
    pairs = demand()
    simulation = Simulation(network, pairs, MODES, seed=5)
    inputs = results.input_sheets(network, pairs, MODES, dict(simulation.signal_plans))
    recorder = results.RunRecorder(network, MODES, folder=folder, inputs=inputs,
                                   run=[("Random seed", 5), ("Time step (s)", DT)])
    while simulation.time < seconds:
        simulation.step()
        if simulation.time % results.SAMPLE_S < DT / 2:
            recorder.sample(simulation.frame())
    return simulation, recorder


def test_a_short_roads_flow_counts_every_vehicle_that_entered_it():
    # 40 m roads: a vehicle crosses one in about 3 s, so 5-s samples missed some 39 % of them (GEH 11 on a perfect run)
    network, modes = generate_network(2, 2, 40.0), VehicleModes().modes
    simulation = Simulation(network, [PairDemand("1W", "1E", 700.0, None, [], "Random", (0, 100, 0))], modes, seed=3)
    recorder = results.RunRecorder(network, modes)
    while simulation.time < 600:
        simulation.step()
        if abs(simulation.time % results.SAMPLE_S) < 1e-6:
            recorder.sample(simulation.frame())
    entered = {row["road"]: row["entered"] for row in recorder.kpis()["roads"]}
    exact = {}
    for link_id, count in simulation.link_entries.items():
        exact[link_id.rstrip("<>")] = exact.get(link_id.rstrip("<>"), 0) + count
    short = [road for road in exact if road in entered and network.road(road).length_m < 60]
    assert short and sum(exact[road] for road in short) > 50  # they carried traffic
    assert all(entered[road] == exact[road] for road in short), [(r, entered[r], exact[r]) for r in short]


def test_real_traffic_gives_the_networks_kpis_minute_by_minute():
    with tempfile.TemporaryDirectory() as folder:
        work = Path(folder) / "run test"
        simulation, recorder = recorded(15 * 60, work)
        k = recorder.kpis()
        assert k["time_s"] == 900 and k["spawned"] == simulation.frame().spawned and k["arrived"] > 100
        assert k["throughput_vph"] == round(k["arrived"] * 4)  # a quarter of an hour
        assert k["trips"] >= k["arrived"] - 3  # each seen leaving, within a sample
        by = k["by_type"]
        assert abs(k["people"] - sum(value["people"] for value in by.values())) < 1e-6
        assert by["Car"]["people"] == by["Car"]["trips"] * 1.5 and by["Bus"]["people"] == by["Bus"]["trips"] * 20
        assert 10 < k["mean_speed_kmh"] < 50 and k["delay_veh_h"] > 0 and k["mean_trip_s"] > 60
        assert k["queued_max"] > 0 and k["longest_queue"]["queue_max"] > 0  # queues at the signals
        busiest = k["roads"][0]
        assert busiest["flow_vph"] >= 400 and busiest["speed_kmh"] > 0  # 500 veh/h a pair, each way
        rows = results._csv(work / "minutes.csv")
        assert rows[0] == results.MINUTE_COLUMNS and len(rows) == 1 + 15  # a row each simulated minute
        assert rows[-1][4] == k["arrived"] and sum(row[5] for row in rows[1:]) == k["arrived"]
        assert (work / "totals.json").exists()  # an interruption now loses nothing already written
        path = recorder.finish(Path(folder))
        assert path.name == f"ACMS results {recorder.started:%Y-%m-%d %H-%M-%S}.xlsx" and not work.exists()  # when it ran
        sheets = read_xlsx(path)
        assert list(sheets) == ["Read me", "Summary", "Run", "Per minute", "Roads", "Road conditions", "Roads by minute",
                                "By type", "Trips", "City", "Junctions", "Road network", "Boundary ends", "Demand",
                                "Vehicle types", "Signal plans", "Bus stops", "Institutions"]
        assert path.with_suffix(".geojson").exists()  # the network for a GIS, beside it
        # Enough for an independent analyst: what each sheet is, the definitions, and every input.
        read_me = {row[0]: row[1] for row in sheets["Read me"] if len(row) > 1}
        assert read_me["Random seed"] == 5 and "Delay (veh·h)" in read_me and "Trips" in read_me
        run = {row[0]: row[1] for row in sheets["Run"][1:]}
        assert run["Random seed"] == 5 and run["Time step (s)"] == DT and run["Simulated"] == "00:15:00"
        pairs = sheets["Demand"]
        assert pairs[0][:5] == ["Origin", "Destination", "Hourly (veh/h)", "Daily (trips/day)", "Arrival pattern"]
        assert sorted((row[0], row[1], row[2], row[4]) for row in pairs[1:]) == sorted(
            (o, d, 500.0, "Poisson") for o, d in (("1W", "1E"), ("1E", "1W"), ("1S", "2N"), ("2N", "1S")))
        kinds = {row[0]: row for row in sheets["Vehicle types"][1:]}
        assert kinds["Bus"][2] == 20 and kinds["Car"][3] == 5.0 and len(sheets["Junctions"]) == 1 + 4
        plans = {row[0]: row for row in sheets["Signal plans"][1:]}
        assert plans["1,1"][1] == "webster" and plans["1,1"][2] > 0 and len(sheets["Road network"]) == 1 + 12
        summary = {row[0]: row[1] for row in sheets["Summary"][1:]}
        assert summary["Throughput"] == k["throughput_vph"] and summary["Vehicles arrived"] == k["arrived"]
        assert summary["Passenger throughput"] == k["people_per_h"] and "Car 1.5" in summary["Occupancy assumed"]
        assert len(sheets["Per minute"]) == 16 and len(sheets["Trips"]) == k["trips"] + 1
        roads = {row[0]: row for row in sheets["Roads"][1:]}
        assert roads[busiest["road"]][7] == busiest["flow_vph"]


def test_an_interrupted_run_is_recovered_from_its_folder():
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder) / "runs"
        _, recorder = recorded(150, root / "run crashed")  # 2.5 minutes, then no finish(): a crash
        with results.take_lock(root / "run crashed" / "live.lock"):  # its lock held: another ACMS's run, going
            assert results.recover(Path(folder), root) == [] and (root / "run crashed").exists()
            assert results.lock_held(root / "run crashed" / "live.lock")
        assert not results.lock_held(root / "run crashed" / "live.lock")  # let go: the next start recovers it
        made = results.recover(Path(folder), root)
        assert len(made) == 1 and made[0].name == f"ACMS results {recorder.started:%Y-%m-%d %H-%M-%S} (recovered).xlsx"
        assert not (root / "run crashed").exists()
        sheets = read_xlsx(made[0])
        assert len(sheets["Per minute"]) == 1 + 2  # the two whole minutes it had written
        summary = {row[0]: row[1] for row in sheets["Summary"][1:]}
        assert summary["Simulated time"] == "00:02:00"
        _, empty = recorded(0, root / "run empty")  # nothing recorded: no workbook, no folder left
        assert empty.finish(Path(folder)) is None and not (root / "run empty").exists()


def test_the_runner_samples_on_the_physics_thread_and_survives_a_failure():
    network = generate_network(2, 2)
    with tempfile.TemporaryDirectory() as folder:
        recorder = results.RunRecorder(network, MODES, folder=Path(folder) / "run live")
        runner = Runner(Simulation(network, demand(), MODES, seed=1), speed=60, recorder=recorder)
        runner.play()
        time.sleep(1.5)  # about 90 simulated seconds
        live = recorder.kpis()  # read from this thread while the physics records
        runner.stop()
        assert recorder.totals["samples"] >= 10 and live["time_s"] > 0
        assert abs(recorder.totals["time_s"] - runner.simulation.time) <= results.SAMPLE_S

        class Broken(results.RunRecorder):
            def sample(self, frame):
                raise RuntimeError("disk full")

        broken = Broken(network, MODES, folder=Path(folder) / "run broken")
        runner = Runner(Simulation(network, demand(), MODES, seed=1), speed=60, recorder=broken)
        runner.play()
        time.sleep(0.5)
        runner.stop()
        assert runner.recorder is None and "disk full" in broken.error and runner.simulation.time > 10  # traffic went on


def test_in_the_window_a_run_ends_in_a_workbook():
    from PySide6.QtWidgets import QFileDialog

    from traffic_simulator.main import MainWindow

    window = MainWindow()
    with tempfile.TemporaryDirectory() as folder:
        window.results_folder = Path(folder)
        network_file = Path(folder) / "network.xlsx"
        real = QFileDialog.getSaveFileName
        QFileDialog.getSaveFileName = lambda *args: (str(network_file), "")
        try:
            window._export()  # no run yet: the network itself, for analysis
        finally:
            QFileDialog.getSaveFileName = real
        assert "Exported the network" in window.statusBar().currentMessage()
        assert network_file.exists() and network_file.with_suffix(".geojson").exists()
        assert "City" in read_xlsx(network_file)
        window.summary.tabs.setCurrentIndex(2)
        window.tabs.setCurrentWidget(window.summary)
        assert "No run yet" in window.summary.pages["Results"].toPlainText()
        origins, _ = window._od_ends()
        window._od_command("set_od_demand", {"origins": origins, "demand": "hourly", "value": 60})
        window.speed_combo.setCurrentIndex(window.speed_combo.findData(60))
        batch = next(extension for extension in window.extensions.extensions if extension.name == "Batch Run")
        batch.check.setChecked(True)
        assert "Seeds: 1-10" in dict(window.extensions.settings())["Batch Run"]  # its settings, as its panel shows
        batch.check.setChecked(False)
        window.run_action.trigger()
        assert window.recorder is not None
        time.sleep(1.5)
        window.summary.refresh()
        assert "Running" in window.summary.pages["Results"].toPlainText()
        exported = Path(folder) / "so far.xlsx"
        real = QFileDialog.getSaveFileName
        QFileDialog.getSaveFileName = lambda *args: (str(exported), "")
        try:
            window._export()  # the run so far; it carries on
        finally:
            QFileDialog.getSaveFileName = real
        assert exported.exists() and window.runner is not None and window.recorder is not None
        window.stop_action.trigger()
        assert window.recorder is None and window.last_results is not None and window.last_results.exists()
        assert window.last_results.parent == Path(folder) and "saved" in window.statusBar().currentMessage()
        text = window.summary.pages["Results"].toPlainText()
        assert "Last run" in text and str(window.last_results) in text
        sheets = read_xlsx(window.last_results)
        run = {row[0]: row[1] for row in sheets["Run"][1:]}
        assert run["Random seed"] == 42 and run["Speed (simulated s a real s)"] == 60 and run["Demand used"].startswith("hourly")
        assert run["Network file"] == "not saved" and run["Extension: Batch Run"] == "not ticked"
        assert len(sheets["Demand"]) - 1 == len(origins) * (len(origins) - 1)  # every pair entered
        window.close()


def test_every_live_run_ends_in_a_workbook_however_it_ends():
    """Stopped before its first sample, its extras failing, its physics raising, a step still going at Stop:
    each run's workbook is written (said "Incomplete" when something is missing), never before its physics ended."""
    import threading

    from PySide6.QtTest import QTest

    from traffic_simulator.main import MainWindow

    window = MainWindow()
    with tempfile.TemporaryDirectory() as folder:
        window.results_folder = Path(folder)
        origins, _ = window._od_ends()
        window._od_command("set_od_demand", {"origins": origins, "demand": "hourly", "value": 60})

        def run_record() -> dict:
            return {row[0]: row[1] for row in read_xlsx(window.last_results)["Run"][1:]}

        window._run()  # at 1x: stopped well before the first 5 s sample
        time.sleep(0.5)
        window._stop_run()
        assert window.last_results is not None and "Incomplete" not in run_record()
        window.speed_combo.setCurrentIndex(window.speed_combo.findData(60))
        reports = window.extensions.reports
        window.extensions.reports = lambda *args: 1 / 0
        try:
            window._run()
            time.sleep(0.3)
            window._stop_run()
        finally:
            window.extensions.reports = reports
        assert "ZeroDivisionError" in run_record()["Incomplete"] and "incomplete" in window.statusBar().currentMessage()
        window._run()
        time.sleep(0.3)
        window.runner.simulation.step = lambda: 1 / 0  # the physics raises: the next display frame ends the run
        while window.runner._thread.is_alive():
            time.sleep(0.01)
        window._frame(0.0)
        assert window.runner is None and "The physics stopped" in run_record()["Incomplete"]
        window._run()
        time.sleep(0.3)
        simulation, entered, release = window.runner.simulation, threading.Event(), threading.Event()
        step, before = simulation.step, window.last_results
        simulation.step = lambda: (entered.set(), release.wait(5), step())
        assert entered.wait(2)
        window._stop_runner()  # a step still going: the window carries on, nothing written yet
        assert window.runner is None and window.last_results == before and "still going" in window.statusBar().currentMessage()
        release.set()
        QTest.qWait(1000)
        assert window.last_results != before and window.last_results.exists()
        window.close()


def test_wait_time_passenger_delay_queues_and_the_named_workbook():
    """Wait is time at or below 0.1 m/s outside a bus stop (SUMO's waitingTime); passenger delay is each trip's
    delay × its occupancy; the mean queue is over the samples; the workbook is named by start, seed and model."""
    from types import SimpleNamespace

    def vehicle(id_, mode, speed, place="1,1-2,1 lane 0"):
        return SimpleNamespace(id=id_, mode=mode, origin="1W", destination="1E", speed=speed, road="1,1-2,1",
                               length_m=12.0 if mode == "Bus" else 5.0, place=place)

    def frame(t, vehicles, arrived=0):
        return SimpleNamespace(time=t, vehicles=vehicles, spawned=2, arrived=arrived)

    with tempfile.TemporaryDirectory() as folder:
        recorder = results.RunRecorder(generate_network(2, 2), MODES, folder=Path(folder) / "run", seed=7, model="laya")
        bay = "bus stop S1 beside 1,1-2,1 lane 0"
        for t, bus, car in ((5, (0.0, bay), 0.0), (10, (0.0, bay), 0.0), (15, (0.05, "lane"), 10.0), (20, (8.0, "lane"), 10.0)):
            recorder.sample(frame(t, [vehicle("b", "Bus", bus[0], bus[1]), vehicle("c", "Car", car)]))
        recorder.sample(frame(25, [], arrived=2))  # both arrived
        k = recorder.kpis()
        trips = {row[0]: row for row in recorder._trips}
        # The bus stood at its stop for two samples (calling, not waiting), then 5 s at 0.05 m/s: waiting
        assert trips["b"][9] == 5.0 and trips["c"][9] == 10.0 and k["mean_wait_s"] == 7.5
        bus_delay, car_delay = trips["b"][8], trips["c"][8]
        assert abs(k["people_delay_h"] * 3600 - (bus_delay * 20 + car_delay * 1.5)) < 20  # rounded to 0.01 h
        assert k["mean_person_delay_s"] == round((bus_delay * 20 + car_delay * 1.5) / 21.5, 1)
        assert k["by_type"]["Bus"]["mean_wait_s"] == 5.0 and k["queued_mean"] == round(5 / 5, 1)  # 5 queued in 5 samples
        recorder.add_sheets([{"sheets": [("TSP", [["Figure", "Value"], ["Bus requests", 3]], "What TSP did.")],
                              "files": [("telemetry.jsonl", '{"type": "tsp_request"}\n')]}])
        path = recorder.finish(Path(folder))
        assert path.name == f"ACMS results {recorder.started:%Y-%m-%d %H-%M-%S} seed 7 laya.xlsx"
        beside = path.with_name(f"{path.stem} telemetry.jsonl")  # the extension's file, named after the workbook
        assert beside.read_text(encoding="utf-8") == '{"type": "tsp_request"}\n'
        sheets = read_xlsx(path)
        names = list(sheets)
        assert names.index("TSP") == names.index("Trips") + 1 and sheets["TSP"][1] == ["Bus requests", 3.0]
        read_me = {row[0]: row[1] for row in sheets["Read me"] if len(row) > 1}
        assert read_me["TSP"] == "What TSP did." and read_me["AI model"] == "laya" and "Wait (s)" in read_me
        summary = {row[0]: row[1] for row in sheets["Summary"][1:]}
        assert summary["Mean wait per trip"] == 7.5 and "Passenger delay" in summary and "Mean queued at once" in summary


def placed(simulation, mode, s, speed):
    """A vehicle of `mode` from 1W to 1E, put `s` metres along its first lane at `speed` m/s."""
    from traffic_simulator.vehicle import create_vehicle  # noqa: PLC0415

    route = simulation.graph.route("1W", "1E")
    vehicle = create_vehicle(mode, f"{mode.name}1", route, origin="1W", destination="1E")
    vehicle.segment, vehicle.s, vehicle.speed = route[0].lanes[0], s, speed
    vehicle.pose = vehicle.prev_pose = simulation._pose(vehicle)
    simulation.vehicles.append(vehicle)
    route[0].lanes[0].vehicles.append(vehicle)
    return vehicle


def test_a_free_bicycle_has_no_delay_against_its_own_free_speed():
    """A bicycle riding at its own free speed (20 km/h × its speed factor) on a 50 km/h road loses time against the
    road's limit (Delay, kept as it was) but none against its own free speed."""
    network, bike = generate_network(1, 1), MODES[2]
    simulation = Simulation(network, [], [bike])
    vehicle = placed(simulation, bike, 50.0, 0.0)
    vehicle.speed = simulation.frame().vehicles[0].free_speed
    assert abs(vehicle.speed - 20 / 3.6 * vehicle.speed_factor) < 1e-9
    with tempfile.TemporaryDirectory() as folder:
        recorder = results.RunRecorder(network, [bike], folder=Path(folder) / "run")
        recorder.sample(simulation.frame())
        vehicle.s, simulation.time = vehicle.s + vehicle.speed * 10, 10.0
        recorder.sample(simulation.frame())
        assert recorder.totals["delay_s"] > 3 and recorder.totals["free_delay_s"] < 1e-9
        simulation.vehicles.clear()  # it arrives: its reliability is against its own free speed (TTI 1, not 2.5)
        simulation.time = 10.5
        recorder.sample(simulation.frame())
        assert recorder.trip_log[-1][4] < 1e-9 and recorder.kpis()["tti"] == 1.0


def test_vehicles_waiting_to_enter_are_counted_and_their_wait_reported():
    """Demand that can't get onto the network (a 4 m entry road whose arm has no window, so never green: the one car
    it holds stands at the line) still shows: as the entry wait and the vehicles waiting, spawned = waiting + on the
    road + arrived."""
    network = generate_network(1, 1, lanes_per_direction=1)
    network.set_road_length("1W", 4)
    node = network.node("1,1")
    node.signal_method, node.manual_greens_s = "manual", {"1E": [1.0, 30.0], "1N": [31.0, 60.0], "1S": [31.0, 60.0]}
    network.validate()
    simulation = Simulation(network, [PairDemand("1W", "1E", 600, None, [], "Random", (0, 100, 0))], MODES, seed=7)
    with tempfile.TemporaryDirectory() as folder:
        recorder = results.RunRecorder(network, MODES, folder=Path(folder) / "run")
        while simulation.time < 120:
            simulation.step()
            if simulation.time % results.SAMPLE_S < DT / 2:
                recorder.sample(simulation.frame())
        k = recorder.kpis()
        waiting = sum(len(queue) for queue in simulation.pending.values())
        assert waiting > 10 and k["on_road"] == 1  # one at the line, the rest never got in: little delay on the road...
        assert k["waiting_to_enter"] == waiting == k["spawned"] - k["arrived"] - k["on_road"]
        assert k["entry_wait_veh_h"] > 0.2 > k["delay_veh_h"]  # ...but their wait shows, and counts in the delay with it
        assert abs(k["delay_with_entry_wait_veh_h"] - k["entry_wait_veh_h"] - k["delay_veh_h"]) <= 0.011  # (each rounded)


def test_person_delay_counts_the_people_on_board():
    """A bus carrying 70 (its simulated load; its type assumes 20), stood still for 5 s mid-trip: 350 person-seconds,
    and 70 people carried when it arrives."""
    network, bus = generate_network(1, 1), MODES[0]
    simulation = Simulation(network, [], [bus])
    placed(simulation, bus, 50.0, 0.0).load = 70.0
    assert simulation.frame().vehicles[0].people == 70.0
    with tempfile.TemporaryDirectory() as folder:
        recorder = results.RunRecorder(network, [bus], folder=Path(folder) / "run")
        recorder.sample(simulation.frame())
        simulation.time = 5.0
        recorder.sample(simulation.frame())
        assert abs(recorder.kpis()["person_delay_on_board_h"] * 3600 - 350) < 20  # unfinished: counted already
        simulation.vehicles, simulation.time, simulation.arrived = [], 10.0, 1
        recorder.sample(simulation.frame())
        assert recorder.totals["people"] == 70.0 and recorder.totals["person_delay_s"] == 350.0


def test_trip_times_are_exact_and_the_completion_rate_reported():
    """Each trip's times are its exact insert and exit, as seen step by step (not the 5 s samples): road travel time
    = exit − insert, system travel time = exit − requested departure; beside the means, the completion rate and the
    unfinished share of what was spawned."""
    network = generate_network(2, 2)
    simulation = Simulation(network, demand(), MODES, seed=5)
    on, times = {}, {}  # vehicle id -> when first on the road; (on, off) of each finished trip, step by step
    with tempfile.TemporaryDirectory() as folder:
        recorder = results.RunRecorder(network, MODES, folder=Path(folder) / "run")
        while simulation.time < 600:
            simulation.step()
            now = {vehicle.id for vehicle in simulation.vehicles}
            for vehicle_id in now:
                on.setdefault(vehicle_id, simulation.time)
            for vehicle_id in [v for v in on if v not in now and v not in times]:
                times[vehicle_id] = (on[vehicle_id], simulation.time)
            if simulation.time % results.SAMPLE_S < DT / 2:
                recorder.sample(simulation.frame())
        recorder._flush()
        trips = results._csv(Path(folder) / "run" / "trips.csv")
        column = {name: trips[0].index(name) for name in trips[0]}
        assert trips[0] == results.TRIP_COLUMNS and len(trips) > 100
        for row in trips[1:]:
            entered, left = times[row[0]]
            assert (row[column["Departed (s)"]], row[column["Arrived (s)"]]) == (round(entered, 1), round(left, 1))
            assert abs(row[column["Trip time (s)"]] - (left - entered)) < 0.06
            assert row[column["Requested departure (s)"]] <= entered - DT + 1e-6  # generated a step before, at least
            assert row[column["System travel time (s)"]] >= row[column["Trip time (s)"]]
        assert any(row[column["Trip time (s)"]] % results.SAMPLE_S for row in trips[1:])  # not whole samples
        k = recorder.kpis()
        assert k["mean_system_trip_s"] > k["mean_trip_s"] > 0
        assert k["completion_rate"] == round(k["arrived"] / k["spawned"], 3) < 1
        assert k["unfinished_share"] == round((k["on_road"] + k["waiting_to_enter"]) / k["spawned"], 3)
        assert abs(k["completion_rate"] + k["unfinished_share"] - 1) < 0.002


def test_a_warm_up_is_left_out_of_the_figures():
    """measure_from_s: the KPIs count from its end (time, distance, trips that entered after it, throughput over the
    measured hours); the per-minute rows go on for the whole run, and the Run sheet says where measuring began."""
    network = generate_network(2, 2)
    simulation = Simulation(network, demand(), MODES, seed=5)
    with tempfile.TemporaryDirectory() as folder:
        whole = results.RunRecorder(network, MODES, folder=Path(folder) / "whole")
        warm = results.RunRecorder(network, MODES, folder=Path(folder) / "warm", measure_from_s=300)
        while simulation.time < 600:
            simulation.step()
            if simulation.time % results.SAMPLE_S < DT / 2:
                frame = simulation.frame()
                whole.sample(frame)
                warm.sample(frame)
                if abs(simulation.time - 300) < DT / 2:
                    at_300 = dict(whole.totals)
        k = warm.kpis()
        assert k["measure_from_s"] == 300 and k["arrived"] == whole.kpis()["arrived"]  # the counts: the whole run
        assert abs(warm.totals["vehicle_s"] - (whole.totals["vehicle_s"] - at_300["vehicle_s"])) < 1e-6
        assert k["throughput_vph"] == round((k["arrived"] - at_300["arrived"]) * 3600 / 300)
        trips = results._csv(Path(folder) / "warm" / "trips.csv")[1:]
        assert 0 < k["trips"] == len(trips) < whole.kpis()["trips"] and all(row[4] >= 300 for row in trips)
        assert len(warm.minute_rows) == len(whole.minute_rows) == 10
        path = warm.finish(Path(folder))
        run = {row[0]: row[1] for row in read_xlsx(path)["Run"][1:]}
        assert run["Measured from"] == "00:05:00"
        summary = {row[0]: row[1] for row in read_xlsx(path)["Summary"][1:]}
        assert summary["Measured from"] == "00:05:00" and summary["Completion rate"] == k["completion_rate"]


def test_everything_is_exported_for_analysis():
    """Road conditions by direction with their level of service, the city's character, the observed
    statistics, the GeoJSON (WGS84 once the map is placed) and, with no run, the network alone."""
    import json

    from traffic_simulator import experiments

    assert [results.level_of_service(p) for p in (95, 80, 60, 45, 35, 20, None)] == ["A", "B", "C", "D", "E", "F", None]
    network, modes = generate_network(2, 2), VehicleModes().modes
    network.nodes["1,1"].control = "roundabout"
    network.add_bus_stop("1,1-2,1", "both")
    city = {row[0]: row[1] for row in results.city_sheet(network)[1:]}
    assert city["Junctions"] == 4 and city["Junctions roundabout"] == 1 and city["Bus stops"] == 1
    assert city["Cyclomatic number"] == 1 and city["Road length by lanes each way"].startswith("2 ")
    assert city["Route choice"].startswith("fastest")
    demand = [PairDemand("1W", "1E", 600.0, None, [], "Poisson", (0, 100, 0)),
              PairDemand("1E", "1W", 600.0, None, [], "Poisson", (0, 100, 0))]
    with tempfile.TemporaryDirectory() as folder:
        recorder = results.RunRecorder(network, modes, folder=Path(folder) / "run",
                                       inputs=results.input_sheets(network, demand, modes, {}))
        simulation = Simulation(network, demand, modes, seed=2)
        simulation.observe()
        while simulation.time < 600:
            simulation.step()
            if simulation.time % results.SAMPLE_S < DT / 2:
                recorder.sample(simulation.frame())
        kpis = recorder.kpis()
        kpis["observed"] = {**recorder.observed(), **simulation.observations()}
        recorder.add_analysis(experiments.analysis_sheets(kpis))
        path = recorder.finish(Path(folder))
        sheets = read_xlsx(path)
        for name in ("Road conditions", "Roads by minute", "City", "Run statistics", "Detectors", "Link travel times",
                     "Arrivals", "Reliability by OD"):
            assert len(sheets[name]) > 1, name
        conditions = {row[0]: dict(zip(sheets["Road conditions"][0], row)) for row in sheets["Road conditions"][1:]}
        east, west = conditions["1,1-2,1>"], conditions["1,1-2,1<"]
        assert (east["From"], east["To"], west["From"], west["To"]) == ("1,1", "2,1", "2,1", "1,1")  # each direction
        assert east["Level of service"] == results.level_of_service(east["Speed (% of limit)"])
        assert east["Travel time index"] >= 0.9 and east["Flow (veh/h)"] > 0
        roads = {row[0]: dict(zip(sheets["Road network"][0], row)) for row in sheets["Road network"][1:]}
        assert roads["1,1-2,1"]["Bus stops"] == 1 and roads["1,1-2,1"]["Shape (WKT, scene m, y down)"].startswith("LINESTRING")
        geo = json.loads(path.with_suffix(".geojson").read_text(encoding="utf-8"))
        feature = next(f for f in geo["features"] if f["properties"]["id"] == "1,1-2,1")
        assert feature["geometry"]["type"] == "LineString" and feature["properties"]["forward_los"] in "ABCDEF"
        assert "local metres" in geo["acms_coordinates"] and feature["properties"]["run_flow_vph"] > 0

    network.background.kind, network.background.lat, network.background.lon = "map", 23.75, 90.37  # Dhaka
    with tempfile.TemporaryDirectory() as folder:
        path = results.network_workbook(Path(folder) / "net.xlsx", network, demand, modes, [("Seed", 1)])
        assert list(read_xlsx(path)) == ["Read me", "City", "Junctions", "Road network", "Boundary ends", "Demand",
                                         "Vehicle types", "Bus stops", "Institutions", "Settings"]
        geo = json.loads(path.with_suffix(".geojson").read_text(encoding="utf-8"))
        lon, lat = geo["features"][0]["geometry"]["coordinates"][0]
        assert geo["acms_coordinates"].startswith("WGS84") and abs(lat - 23.75) < 0.05 and abs(lon - 90.37) < 0.05


def test_pedestrians_walks_and_crossings_go_into_the_workbook():
    network = generate_network(2, 1)
    for road in network.all_roads():
        road.footpaths = ["left", "right"]
    road = network.roads["1,2-1,1"]
    network.add_crossing(road.id, network.road_geometry_length(road) / 2, "zebra")
    network.walk_demand = {"2W->1E": 300.0}
    simulation = Simulation(network, [PairDemand("1N", "1S", 700, None, [], "Random", (0, 100, 0))], MODES, seed=5)
    with tempfile.TemporaryDirectory() as folder:
        recorder = results.RunRecorder(network, MODES, folder=Path(folder) / "run walking")
        while simulation.time < 1200:
            simulation.step()
            if simulation.time % results.SAMPLE_S < DT / 2:
                recorder.sample(simulation.frame())
        k = recorder.kpis()
        assert k["walk_trips"] == simulation.walk_arrived - sum(  # each walk once: all but any still in a frame
            1 for walk in simulation.frame().walk_exits if walk[0] not in recorder._walks_seen)
        assert 3 < k["mean_walk_speed_kmh"] < 6 and k["mean_walk_s"] > 600  # about 1 km at about 5 km/h
        zebra = next(row for row in k["crossings"] if row["crossing"] == "X1")
        assert zebra["kind"] == "zebra" and zebra["walkers"] and zebra["vehicles_held_s"] > 0
        path = recorder.finish(Path(folder))
        sheets = read_xlsx(path)
        assert sheets["Walks"][0] == results.WALK_COLUMNS and len(sheets["Walks"]) == k["walk_trips"] + 1
        assert sheets["Crossings"][0] == results.CROSSING_COLUMNS and any(row[0] == "X1" for row in sheets["Crossings"])
        summary = {row[0]: row[1] for row in sheets["Summary"][1:]}
        assert summary["Walks completed"] == k["walk_trips"] and summary["Mean walk time"] == k["mean_walk_s"]


def car(id_, link="1,1-2,1>", speed=10.0, lane=0):
    """A car in a hand-made frame, on `link`'s lane `lane`."""
    return SimpleNamespace(id=id_, mode="Car", origin="1W", destination="1E", speed=speed, road=link.rstrip("<>"),
                           length_m=5.0, place=f"{link} lane {lane}")


def test_after_a_warm_up_people_and_road_conditions_count_the_window_throughput_does():
    """Measured from 10 s: a car on the road from 5 s and one from 15 s both arrive at 20 s. Throughput and the people
    carried count both (every arrival after the warm-up), the trip means only the one that entered after it; each
    direction's flow is over the 10 measured seconds, as the road's is."""
    with tempfile.TemporaryDirectory() as folder:
        recorder = results.RunRecorder(generate_network(2, 2), MODES, folder=Path(folder) / "run", measure_from_s=10)
        for t, ids in ((5, "a"), (10, "a"), (15, "ab")):
            recorder.sample(SimpleNamespace(time=float(t), vehicles=[car(i) for i in ids], spawned=2, arrived=0))
        recorder.sample(SimpleNamespace(time=20.0, vehicles=[], spawned=2, arrived=2))
        k = recorder.kpis()
        assert k["throughput_vph"] == 720 and k["trips"] == 1  # 2 arrivals in 10 s; 1 trip entered after the warm-up
        assert k["people"] == 3.0 and k["people_per_h"] == 1080 == k["by_type"]["Car"]["people_per_h"]  # 1.5 a car
        sheets = read_xlsx(recorder.finish(Path(folder)))
        assert sheets["Per minute"][-1][7] == 3.0  # people carried that minute: both cars
        roads = {row[0]: dict(zip(sheets["Roads"][0], row)) for row in sheets["Roads"][1:]}
        east = next(dict(zip(sheets["Road conditions"][0], row)) for row in sheets["Road conditions"][1:]
                    if row[0] == "1,1-2,1>")
        assert east["Flow (veh/h)"] == roads["1,1-2,1"]["Flow (veh/h)"] == 360  # 1 entered in the 10 measured s


def test_a_run_stopped_in_its_warm_up_has_nothing_measured():
    """Stopped at 20 s of a 600 s warm-up: no measured figures (not 10¹² veh/km of density from 20 s ÷ a negative
    window, not the warm-up's speed labelled measured); the counts of the whole run as they are."""
    with tempfile.TemporaryDirectory() as folder:
        recorder = results.RunRecorder(generate_network(2, 2), MODES, folder=Path(folder) / "run", measure_from_s=600)
        for t in (5, 10, 15, 20):
            recorder.sample(SimpleNamespace(time=float(t), vehicles=[car("a")], spawned=1, arrived=0))
        k = recorder.kpis()
        assert k["throughput_vph"] is None and k["people_per_h"] is None and k["mean_speed_kmh"] is None
        assert k["roads"] == [] and k["trips"] == 0 and k["vehicle_km"] == 0 and (k["spawned"], k["on_road"]) == (1, 1)
        sheets = read_xlsx(recorder.finish(Path(folder)))
        assert len(sheets["Road conditions"]) == 1  # its header only


def test_a_queue_a_lane_is_each_directions_over_its_own_lanes():
    """4 cars queued each way on a two-way road of 1 lane each way: 30 m a lane (not both ways added, 60 m); 4 over
    the 2 lanes of a one-way road: 15 m (not over 1, 30 m). The statistics keep each direction (a link) apart."""
    from traffic_simulator import analysis

    network = generate_network(2, 2)
    network.roads["1,1-2,1"].lanes_per_direction = 1
    one_way = network.roads["1,2-2,2"]
    one_way.lanes_per_direction, one_way.direction = 1, "one-way"  # its 2 lanes, one way
    assert results.road_record(network, one_way)["lanes"] == 2
    cars = [car(f"e{i}", "1,1-2,1>", 0.0) for i in range(4)] + [car(f"w{i}", "1,1-2,1<", 0.0) for i in range(4)]
    cars += [car(f"o{i}", "1,2-2,2>", 0.0, i % 2) for i in range(4)]
    with tempfile.TemporaryDirectory() as folder:
        recorder = results.RunRecorder(network, MODES, folder=Path(folder) / "run")
        recorder.sample(SimpleNamespace(time=5.0, vehicles=cars, spawned=12, arrived=0))
        roads = {row["road"]: row for row in recorder.kpis()["roads"]}
        assert roads["1,1-2,1"]["queue_m_max"] == 30.0 and roads["1,1-2,1"]["queue_max"] == 8
        assert roads["1,2-2,2"]["queue_m_max"] == 15.0
        observed = recorder.observed()
        assert analysis.queue_p95(observed, "1,1-2,1>") == analysis.queue_p95(observed, "1,1-2,1<") == 30.0


def test_the_city_sheet_counts_a_one_way_roads_lanes():
    network = generate_network(2, 2)
    before = {row[0]: row[1] for row in results.city_sheet(network)[1:]}
    network.roads["1,1-2,1"].direction = "one-way"  # its 4 lanes (2 a direction), now all one way
    after = {row[0]: row[1] for row in results.city_sheet(network)[1:]}
    assert after["Lane length"] == before["Lane length"] and "4 0.50" in after["Road length by lanes each way"]


def test_a_crashed_runs_files_are_whole_and_recovered_under_names_of_their_own():
    """meta.json and the GeoJSON are written whole or not at all (a write torn half way leaves the old file); two
    crashed runs that started in the same second with the same seed and model are both recovered, numbered."""
    import json
    import shutil

    real = Path.write_text

    def torn(self, text, *args, **kwargs):  # the disk fills half way through
        real(self, text[:len(text) // 2], *args, **kwargs)
        raise OSError("disk full")

    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder) / "runs"
        recorder = results.RunRecorder(generate_network(2, 2), MODES, folder=root / "run a", seed=3)
        recorder.sample(SimpleNamespace(time=60.0, vehicles=[car("a")], spawned=1, arrived=0))  # a whole minute saved
        recorder.error, geojson = "stopped", Path(folder) / "net.geojson"
        geojson.write_text("{}", encoding="utf-8")
        Path.write_text = torn
        try:
            for write in (lambda: recorder.finish(Path(folder)),
                          lambda: results.write_geojson(geojson, "", {}, {}, {}, None)):
                try:
                    write()
                except OSError:
                    continue
                raise AssertionError("the torn write should have raised")
        finally:
            Path.write_text = real
        assert json.loads((root / "run a" / "meta.json").read_text(encoding="utf-8"))["seed"] == 3
        assert geojson.read_text(encoding="utf-8") == "{}"
        shutil.copytree(root / "run a", root / "run b")  # the same start, seed and model
        made = results.recover(Path(folder), root)
        assert len(set(made)) == 2 and all(path.exists() for path in made), made


def test_a_mid_run_export_leaves_the_minute_series_whole():
    """720 veh/h exported at 90 s: the workbook shows the half minute at its rate (720, not 360), the series the
    statistics read keeps whole minutes, and the finished run's rows are 720 each, its last part-minute's too."""
    with tempfile.TemporaryDirectory() as folder:
        recorder = results.RunRecorder(generate_network(2, 2), MODES, folder=Path(folder) / "run")
        for t in range(5, 181, 5):
            recorder.sample(SimpleNamespace(time=float(t), vehicles=[], spawned=t // 5, arrived=t // 5))
            if t == 90:
                rows = read_xlsx(recorder.export(Path(folder) / "now.xlsx"))["Per minute"]
                assert [row[6] for row in rows[1:]] == [720.0, 720.0], rows
                assert [row[0] for row in recorder.minute_rows] == [60.0]  # the half minute isn't in the series
        recorder.sample(SimpleNamespace(time=210.0, vehicles=[], spawned=42, arrived=42))
        rows = read_xlsx(recorder.finish(Path(folder)))["Per minute"]
        assert [row[6] for row in rows[1:]] == [720.0] * 4, rows


def test_a_failed_workbook_leaves_no_empty_file_behind():
    """finish() takes the workbook's name at once; a write that fails gives it up again and leaves no .part."""
    import traffic_simulator.xlsx as xlsx

    with tempfile.TemporaryDirectory() as folder:
        recorder = results.RunRecorder(generate_network(2, 2), MODES, folder=Path(folder) / "run")
        recorder.sample(SimpleNamespace(time=60.0, vehicles=[car("a")], spawned=1, arrived=0))
        real = xlsx._sheet
        xlsx._sheet = lambda rows: 1 / 0  # fails half way through the zip
        try:
            recorder.finish(Path(folder))
            raise AssertionError("the failed write should have raised")
        except ZeroDivisionError:
            pass
        finally:
            xlsx._sheet = real
        left = [path.name for path in Path(folder).iterdir() if path.suffix in (".xlsx", ".part")]
        assert not left, left


def test_the_runs_extra_files_are_written_whole():
    """extra.json, analysis.json and the extensions' files: a torn write leaves nothing half written under the real
    name."""
    real = Path.write_text

    def torn(self, text, *args, **kwargs):
        real(self, text[:len(text) // 2], *args, **kwargs)
        raise OSError("disk full")

    with tempfile.TemporaryDirectory() as folder:
        work = Path(folder) / "run"
        recorder = results.RunRecorder(generate_network(2, 2), MODES, folder=work)
        Path.write_text = torn
        try:
            for write in (lambda: recorder.add_sheets([{"sheets": [["X", [[1]], "x"]], "files": [("t.jsonl", "{}")]}]),
                          lambda: recorder.add_sheets([{"files": [("t.jsonl", "{}")]}]),
                          lambda: recorder.add_analysis([["Y", [[1]], "y"]])):
                try:
                    write()
                except OSError:
                    continue
                raise AssertionError("the torn write should have raised")
        finally:
            Path.write_text = real
        assert not any((work / name).exists() for name in ("extra.json", "analysis.json", "files/t.jsonl"))


def test_the_excel_writer_keeps_to_excels_limits():
    """A sheet name with a control character or edge apostrophes, and a cell over Excel's 32,767 characters, still
    make a workbook Excel opens."""
    with tempfile.TemporaryDirectory() as folder:
        path = write_xlsx(Path(folder) / "book.xlsx", [("bad\x01name", [["x" * 40_000]]), ("'quoted'", [[1]])])
        sheets = read_xlsx(path)
        assert list(sheets) == ["badname", "quoted"]
        assert len(sheets["badname"][0][0]) == 32_767


if __name__ == "__main__":
    for name, check in list(globals().items()):
        if name.startswith("test_"):
            check()
    print("ok")


def test_one_clock_writes_seconds_and_hours_of_the_day():
    """results.clock and clock_h are the app's only clock texts; an hour just short of eight is 08:00, never 07:60."""
    from traffic_simulator.results import clock, clock_h

    assert clock(0) == "00:00:00" and clock(3725.9) == "01:02:05"
    assert clock_h(7.999) == "08:00" and clock_h(8.5) == "08:30" and clock_h(0) == "00:00"

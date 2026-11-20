"""Projects: the seed box used by a Run; a project file saved and opened with its network, demand
(hourly and daily, with curves), vehicle types and run settings; bad files refused with nothing
changed; older network-only files opening; unsaved changes shown in the title and asked about before
closing. Run with `python tests/test_project.py` (or the test monitor)."""

import json
from dataclasses import replace
import os
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox  # noqa: E402

from traffic_simulator.network_generator import generate_network  # noqa: E402
from traffic_simulator.network_io import project_parts, project_problem, project_record, save_network, saved_project  # noqa: E402

APP = QApplication.instance() or QApplication([])


def window():
    from traffic_simulator.main import MainWindow

    made = MainWindow()
    made.results_folder = Path(tempfile.mkdtemp())
    return made


def test_the_seed_box_seeds_the_run():
    shown = window()
    assert shown.seed_box.value() == 42
    shown.seed_box.setValue(7)
    shown.run_action.trigger()
    settings = dict(shown._run_settings(shown.runner.simulation))
    assert settings["Random seed"] == 7
    shown._stop_run()
    shown.close()


def test_a_project_keeps_the_network_demand_types_and_run_settings():
    first = window()
    with tempfile.TemporaryDirectory() as folder:
        path = str(Path(folder) / "scenario A.json")
        origins, _ = first._od_ends()
        first._od_command("set_od_demand", {"pairs": [f"{origins[0]}->{origins[1]}"], "demand": "hourly", "value": 350,
                                            "arrival_pattern": "binomial", "peak_hour_factor": 0.85})
        first._od_command("set_od_demand", {"pairs": [f"{origins[1]}->{origins[2]}"], "demand": "daily", "value": 4800})
        first._od_command("shape_daily_curve", {"pairs": [f"{origins[1]}->{origins[2]}"], "polynomial": [0, 4, -4]})
        pair = (origins[0], origins[1])
        first.od_matrix.cells[pair] = replace(first.od_matrix.cells[pair], drops=((1, 0.5, 20),))  # a fifth of cars stop
        first.vehicle_modes.add()  # a fourth vehicle type...
        first.vehicle_modes.set_headway(0, 10)  # ...and a bus every 10 min
        first.vehicle_modes.set_pcu(0, 2.5)  # its passenger car units
        first.vehicle_modes.update("Bike", {"filters": False})  # no filtering in a lateral run
        first.network.nodes["2,2"].control = "roundabout"
        first.seed_box.setValue(1234)
        first.speed_combo.setCurrentIndex(first.speed_combo.findData(10))
        assert first.unsaved() and first.isWindowModified()
        real = QFileDialog.getSaveFileName
        QFileDialog.getSaveFileName = lambda *args: (path, "")
        try:
            assert first.save_file()  # the first save asks where
        finally:
            QFileDialog.getSaveFileName = real
        assert not first.unsaved() and "scenario A.json" in first.windowTitle()
        document = json.loads(Path(path).read_text(encoding="utf-8"))
        assert document["format"] == {"app": "ACMS project", "version": 2} and document["run"] == {"seed": 1234, "speed": 10.0}
        assert len(document["vehicle_types"]["types"]) == 4 and len(document["demand"]["pairs"]) == 2
        first.seed_box.setValue(99)
        assert first.unsaved()
        assert first.save_file()  # Ctrl+S: to its file, no question
        assert json.loads(Path(path).read_text(encoding="utf-8"))["run"]["seed"] == 99
        cells = {pair: (cell.flow, cell.daily, cell.pattern, cell.split, cell.profile, cell.quarters, cell.drops)
                 for pair, cell in first.od_matrix.cells.items()}
        assert sum(bool(quarters) for *_, quarters, _ in cells.values()) == 1  # the hourly pair's PHF
        types = first.vehicle_modes.state()
        first.close()

        second = window()
        assert second.load_file(path)
        assert {pair: (cell.flow, cell.daily, cell.pattern, cell.split, cell.profile, cell.quarters, cell.drops)
                for pair, cell in second.od_matrix.cells.items()} == cells
        assert second.vehicle_modes.state() == types and second.vehicle_modes.modes[0].headway_min == 10
        assert second.vehicle_modes.modes[0].pcu == 2.5 and second.vehicle_modes.modes[2].filters is False
        assert second.network.nodes["2,2"].control == "roundabout" and second.seed_box.value() == 99
        assert second.speed_combo.currentData() == 10 and not second.unsaved() and second.network_path == path
        assert second.od_matrix.mode == "daily"  # the demand shown when it was saved
        assert not second.history.undo_stack and not second.undo_action.isEnabled()  # its own history from here
        nodes = len(second.network.nodes)
        second.undo()  # nothing to undo: the window's blank start can't come back under this project's name
        assert len(second.network.nodes) == nodes and second.network_path == path
        second.close()


def test_a_project_keeps_the_extensions_and_emergency_schedules():
    first = window()
    devl, batch = first.extensions.find("DEVL"), first.extensions.find("Batch Run")
    batch.check.setChecked(True)  # its settings are set while ticked, then it is unticked: restored all the same
    first.extensions.set_settings(batch, {"Times each": 3, "Pace": "5×", "Duration": "Other…"})
    batch.module._ui["custom"].setValue(90)  # Other…'s minutes, the box beside Duration
    batch.check.setChecked(False)
    devl.check.setChecked(True)
    schedules = [("1W", "3E", 5, 0), ("2N", "2,1", 0, 4)]
    devl.module.dispatch.set_fleet(schedules)
    devl.module.dispatch._ui["seed"].setValue(77)
    assert first.unsaved()  # extensions count as changes
    with tempfile.TemporaryDirectory() as folder:
        path = str(Path(folder) / "emergency.json")
        first.network_path = path
        assert first.save_file() and not first.unsaved()
        document = json.loads(Path(path).read_text(encoding="utf-8"))
        saved = {one["file"]: one for one in document["extensions"]}
        assert saved["dynamic_emergency_lane.py"]["ticked"] and not saved["batch_run.py"]["ticked"]
        assert saved["dynamic_emergency_lane.py"]["state"] == {"schedules": [list(s) for s in schedules], "seed": 77}
        assert saved["batch_run.py"]["settings"]["Times each"] == 3 and saved["batch_run.py"]["state"] == {"other_min": 90}
        first.close()

        second = window()  # a fresh tab: one empty schedule, DEVL unticked
        devl, batch = second.extensions.find("DEVL"), second.extensions.find("Batch Run")
        assert not devl.on and devl.module.dispatch.fleet() == [("", "", 0, 0)]
        assert second.load_file(path)
        assert devl.on and devl.module.dispatch.fleet() == schedules and devl.module.dispatch._ui["seed"].value() == 77
        fields = second.extensions.fields(batch, refresh=False)
        assert fields["Times each"].value() == 3 and fields["Pace"].group.checkedButton().text() == "5×"  # a choice too
        assert fields["Duration"].currentText() == "Other…" and batch.module._ui["custom"].value() == 90
        assert not batch.on and not batch.controls.isEnabled() and not second.unsaved()
        second.close()

        # One that can't be restored is named, and the rest still are; the project opens
        saved["dynamic_emergency_lane.py"]["state"]["seed"] = -1
        saved["batch_run.py"]["settings"]["Nope"] = 1
        saved["batch_run.py"]["settings"]["Runs at once"] = 987654321  # written as 1e400, read as infinity: named
        document["extensions"].append({"file": "elsewhere.py", "ticked": True, "settings": {"Speed": 2}})
        Path(path).write_text(json.dumps(document).replace("987654321", "1e400"), encoding="utf-8")
        third = window()
        devl = third.extensions.find("DEVL")
        assert third.load_file(path) and devl.on
        assert devl.module.dispatch.fleet() == [("", "", 0, 0)]  # its state refused whole: as it was
        message = third.statusBar().currentMessage()
        assert "Not restored" in message and "seed" in message and "Nope" in message, message
        assert "Runs at once" in message and "elsewhere.py: not loaded here" in message, message
        assert third.extensions.fields(third.extensions.find("Batch Run"), refresh=False)["Times each"].value() == 3
        assert third.save_file()  # an extension not loaded here is written back as it was, not lost
        assert {"file": "elsewhere.py", "ticked": True, "settings": {"Speed": 2}} in             json.loads(Path(path).read_text(encoding="utf-8"))["extensions"]
        third.close()
        saved = {one["file"]: one for one in document["extensions"]}  # the bad file again, for the next check

        document["extensions"] = "everything"  # not a list: refused before anything changes
        Path(path).write_text(json.dumps(document), encoding="utf-8")
        real = QMessageBox.critical
        QMessageBox.critical = lambda *args: None
        try:
            assert not window().load_file(path)
        finally:
            QMessageBox.critical = real


def test_a_bad_project_changes_nothing_and_an_old_network_file_opens():
    shown = window()
    with tempfile.TemporaryDirectory() as folder:
        old = Path(folder) / "network only.json"
        save_network(generate_network(2, 2), old)  # no demand, no vehicle types: an older file
        assert shown.load_file(str(old)) and len(shown.network.nodes) == 4 and not shown.od_matrix.cells
        assert shown.vehicle_modes.names() == ["Bus", "Car", "Bike"]
        for part, message in (({"run": {"seed": -1}}, "seed"),
                              ({"demand": {"shown": "hourly", "pairs": [{"origin": "1W", "destination": "1W",
                                                                         "split": [100]}]}}, "two different ends"),
                              ({"vehicle_types": {"types": [{"name": "Car", "colour": "blue"}], "default_split": [100]}},
                               "colour"),
                              ({"demand": {"shown": "hourly", "pairs": [{"origin": "1W", "destination": "1E",
                                                                         "split": [60, 30], "pattern": "Sometimes"}]}},
                               "arrival pattern"),
                              ({"demand": {"shown": "hourly", "pairs": [{"origin": "1W", "destination": "1E",
                                                                         "split": [100], "quarters": [1, -1, 1, 1]}]}},
                               "quarters"),
                              ({"demand": {"shown": "hourly", "pairs": [{"origin": "1W", "destination": "1E",
                                                                         "split": [100], "drops": [[0, 0.5, 60], [0, 0.7, 50]]}]}},
                               "more than 100 %")):
            assert message in project_problem(part), (part, project_problem(part))
        bad = Path(folder) / "bad.json"
        document = json.loads(old.read_text(encoding="utf-8"))
        document["demand"] = {"shown": "hourly", "pairs": [{"origin": "1W", "destination": "1E", "flow": -5,
                                                            "split": [10, 85, 5]}]}
        bad.write_text(json.dumps(document), encoding="utf-8")
        try:
            saved_project(bad)
        except ValueError as exc:
            assert "flow" in str(exc)
        else:
            raise AssertionError("a negative flow should be refused")
        before = (sorted(shown.network.nodes), shown.network_path)
        real = QMessageBox.critical
        QMessageBox.critical = lambda *args: None
        try:
            assert not shown.load_file(str(bad))
        finally:
            QMessageBox.critical = real
        assert (sorted(shown.network.nodes), shown.network_path) == before  # nothing changed
    shown.close()


def test_a_full_disk_leaves_saved_files_whole_and_nothing_beside_them():
    def torn(source, target, *args, **kwargs):
        Path(target).write_bytes(b"half")
        raise OSError(28, "No space left on device")

    shown, real_write = window(), Path.write_text
    real_dialog, real_critical = QFileDialog.getSaveFileName, QMessageBox.critical
    with tempfile.TemporaryDirectory() as folder:
        saved, workbook = Path(folder) / "project.json", Path(folder) / "results.xlsx"
        save_network(generate_network(2, 2), saved)
        before = saved.read_bytes()
        Path.write_text = lambda self, *args, **kwargs: torn(None, self)
        try:
            save_network(generate_network(3, 3), saved)
        except OSError:
            pass
        finally:
            Path.write_text = real_write
        assert saved.read_bytes() == before and sorted(Path(folder).iterdir()) == [saved]  # no project.json.part
        shown.last_results = Path(folder) / "last run.xlsx"
        shown.last_results.write_bytes(b"the run")
        workbook.write_bytes(b"kept")
        QFileDialog.getSaveFileName = lambda *args: (str(workbook), "")
        QMessageBox.critical = lambda *args: None
        real_bytes = Path.write_bytes

        def half(self, data):  # the copy, half written, then the disk is full
            real_bytes(self, data[:len(data) // 2])
            raise OSError(28, "No space left on device")
        Path.write_bytes = half
        try:
            shown._export()  # Export Results: the last run's workbook over the one there
        finally:
            Path.write_bytes, QFileDialog.getSaveFileName, QMessageBox.critical = real_bytes, real_dialog, real_critical
        assert workbook.read_bytes() == b"kept" and sorted(Path(folder).iterdir()) == sorted(
            [saved, shown.last_results, workbook])  # nothing torn left beside it
    shown.close()


def test_a_malformed_conversation_refuses_the_open_before_anything_changes():
    shown = window()
    network, path, steps = shown.network, shown.network_path, len(shown.history.undo_stack)
    nodes, chat = sorted(network.nodes), shown.ai_mode.conversation_state()
    with tempfile.TemporaryDirectory() as folder:
        bad = Path(folder) / "bad chat.json"
        save_network(generate_network(1, 1), bad, conversation={"messages": None})
        real = QMessageBox.critical
        QMessageBox.critical = lambda *args: None
        try:
            assert not shown.load_file(str(bad))  # refused, not a TypeError halfway through
        finally:
            QMessageBox.critical = real
    assert shown.network is network and sorted(network.nodes) == nodes and shown.network_path == path
    assert len(shown.history.undo_stack) == steps and shown.ai_mode.conversation_state() == chat
    shown.close()


def test_unsaved_changes_are_asked_about_before_closing():
    shown = window()
    shown._ask_to_save = True  # as on screen
    shown.network.nodes["1,1"].control = "roundabout"
    shown._commit()
    answers = []
    real = QMessageBox.question
    QMessageBox.question = lambda *args: answers.append(args[2]) or QMessageBox.StandardButton.Cancel
    try:
        assert shown.close() is False  # Cancel: it stays open
        assert answers and "Save the changes" in answers[0]
        QMessageBox.question = lambda *args: QMessageBox.StandardButton.Discard
        assert shown.close()
    finally:
        QMessageBox.question = real


def test_the_hourly_daily_switch_is_an_unsaved_change():
    shown = window()
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "switched.json"
        real = QFileDialog.getSaveFileName
        QFileDialog.getSaveFileName = lambda *args, **kwargs: (str(path), "")
        try:
            assert shown.save_file() and not shown.unsaved()
        finally:
            QFileDialog.getSaveFileName = real
        shown.od_panel._show_mode("daily")  # as the switch does: a Run now uses daily demand
        assert shown.unsaved() and shown.isWindowModified()
        assert shown.save_file() and json.loads(path.read_text(encoding="utf-8"))["demand"]["shown"] == "daily"
        assert not shown.unsaved()
    shown.close()


def test_a_deleted_ends_demand_goes_with_it_and_undo_brings_it_back():
    shown = window()
    shown._od_command("set_od_demand", {"pairs": ["2E->1W"], "demand": "hourly", "value": 500})
    inner = shown.network.approach_roads["2E"].from_node
    shown.network.delete_road("2E")
    shown._commit()
    assert ("2E", "1W") not in shown.od_matrix.cells  # gone with its end, so not saved
    shown.undo()
    assert shown.od_matrix.cells[("2E", "1W")].flow == 500  # the end and its demand come back together
    shown.redo()
    other = shown.network.nodes["2,1" if inner != "2,1" else "2,2"]
    assert shown.network.add_end(other.id, other.x + 300, other.y - 120).id == "2E"  # the name again
    shown._commit()
    assert not [p for p in shown._pair_demand() if p.origin == "2E"]  # it inherits nothing
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "stale.json"
        save_network(generate_network(2, 2), path, project={"demand": {"shown": "hourly", "pairs": [
            {"origin": "9E", "destination": "1W", "flow": 500, "split": [10, 85, 5]}]}})  # no end 9E
        assert shown.load_file(str(path)) and not shown.od_matrix.cells  # an unknown end's pair is dropped
    shown.close()


def test_project_record_and_parts_round_trip_without_a_window():
    from traffic_simulator.daily_profile import DailyProfile
    from traffic_simulator.demand import ODCell
    from traffic_simulator.vehicle import VehicleMode
    modes = [VehicleMode(name="Car", colour="#3366ff"), VehicleMode(name="Bus", colour="#ff9900", headway_min=10, timetabled=True)]
    cells = {("1W", "2E"): ODCell(400, None, DailyProfile(), "Random", (90, 10), (0.2, 0.3, 0.3, 0.2), ((0, 0.5, 20),)),
             ("2E", "1W"): ODCell()}  # no demand: not saved
    record = project_record(cells, "hourly", modes, (90, 10), 7, 2)
    assert record["run"] == {"seed": 7, "speed": 2.0} and len(record["demand"]["pairs"]) == 1
    read, split, back = project_parts(json.loads(json.dumps(record)), lambda: 1 / 0)  # types saved: no default asked
    assert [m.name for m in read] == ["Car", "Bus"] and read[1].timetabled and split == (90, 10)
    assert back == {("1W", "2E"): cells[("1W", "2E")]}
    assert project_parts({}, lambda: (modes, (90, 10))) == (modes, (90, 10), {})
    record["demand"]["pairs"][0]["split"] = [100]
    try:
        project_parts(record, lambda: 1 / 0)
    except ValueError as exc:
        assert "split doesn't match" in str(exc)
    else:
        raise AssertionError("a split of the wrong length opened")


if __name__ == "__main__":
    for name, check in list(globals().items()):
        if name.startswith("test_"):
            check()
    print("ok")

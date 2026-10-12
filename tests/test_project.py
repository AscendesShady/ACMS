"""Projects: the seed box used by a Run; a project file saved and opened with its network, demand
(hourly and daily, with curves), vehicle types and run settings; bad files refused with nothing
changed; older network-only files opening; unsaved changes shown in the title and asked about before
closing. Run with `python tests/test_project.py` (or the test monitor)."""

import json
import os
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox  # noqa: E402

from traffic_simulator.network_generator import generate_network  # noqa: E402
from traffic_simulator.network_io import project_problem, save_network, saved_project  # noqa: E402

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
        cells = {pair: (cell.flow, cell.daily, cell.pattern, cell.split, cell.profile, cell.quarters)
                 for pair, cell in first.od_matrix.cells.items()}
        assert sum(bool(quarters) for *_, quarters in cells.values()) == 1  # the hourly pair's PHF
        types = first.vehicle_modes.state()
        first.close()

        second = window()
        assert second.load_file(path)
        assert {pair: (cell.flow, cell.daily, cell.pattern, cell.split, cell.profile, cell.quarters)
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
                               "quarters")):
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


if __name__ == "__main__":
    for name, check in list(globals().items()):
        if name.startswith("test_"):
            check()
    print("ok")

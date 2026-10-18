"""Metro rail (Edit Network → Metro): stops and lines in the model, saved with the project, deployed from the map
(each click a stop, joined to the last; a right-click finishes), one undo step a line."""

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PySide6.QtCore import QPoint, Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from traffic_simulator.models import METRO_MIN_SPACING_M, MetroLine  # noqa: E402
from traffic_simulator.network_generator import generate_network  # noqa: E402
from traffic_simulator.network_io import load_network, save_network, saved_project  # noqa: E402

APP = QApplication.instance() or QApplication([])


def test_a_line_is_its_stops_in_order_and_refusals_change_nothing():
    network = generate_network(2, 2)
    line = network.add_metro_line((0.0, 0.0), (400.0, 0.0))
    network.add_metro_stop(line.id, 400.0, 300.0)
    assert line.id == "ML1" and line.stops == ["M1", "M2", "M3"] and network.metro_length_m("ML1") == 700.0
    for attempt in (lambda: network.add_metro_stop("ML1", 450.0, 300.0),  # under 150 m from M3
                    lambda: network.add_metro_stop("ML1", float("nan"), 0.0),
                    lambda: network.add_metro_line((0.0, 900.0), (0.0, 900.0 + METRO_MIN_SPACING_M - 1))):
        try:
            attempt()
        except ValueError:
            pass
        else:
            raise AssertionError("refused")
    assert list(network.metro_stops) == ["M1", "M2", "M3"] and list(network.metro_lines) == ["ML1"]
    assert network.add_metro_line((0.0, 900.0), (0.0, 1200.0)).stops == ["M4", "M5"]  # a second line
    network.metro_demand = {"M1->M3": 1200.0, "M4->M5": 300.0}
    network.validate()
    for bad in ({"M1->M1": 10.0}, {"M1->M9": 10.0}, {"M1-M2": 10.0}, {"M1->M2": -1.0}):
        network.metro_demand = bad
        try:
            network.validate()
        except ValueError as exc:
            assert "Metro demand" in str(exc)
        else:
            raise AssertionError(bad)
    network.metro_demand = {"M1->M3": 1200.0, "M4->M5": 300.0}
    network.delete_metro_line("ML2")  # its stations and their demand go with it
    assert network.metro_demand == {"M1->M3": 1200.0} and "M4" not in network.metro_stops
    network.metro_lines["ML3"] = MetroLine("ML3", ["M1"])  # one stop, shared with ML1: refused
    try:
        network.validate()
    except ValueError as exc:
        assert "two stops" in str(exc)
    else:
        raise AssertionError("a one-stop line was accepted")


def test_metro_is_saved_and_loaded_with_the_network():
    import json

    network = generate_network(2, 2)
    network.add_metro_line((0.0, 0.0), (500.0, 200.0)).cars_per_train = 8
    network.metro_demand = {"M1->M2": 750.0}
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "metro.json"
        save_network(network, path)
        loaded = load_network(path)
        document = json.loads(path.read_text(encoding="utf-8"))  # a line saved before carriages: carriages that hold it
        record = document["metro_lines"][0]
        record["train_capacity"] = 900
        del record["cars_per_train"], record["car_capacity"]
        path.write_text(json.dumps(document), encoding="utf-8")
        older = load_network(path).metro_lines["ML1"]
    assert loaded.metro_lines == network.metro_lines and loaded.metro_stops == network.metro_stops
    assert loaded.metro_demand == {"M1->M2": 750.0}
    assert (older.cars_per_train, older.car_capacity, older.train_capacity) == (3, 300, 900)  # 400 a carriage at most


def test_a_lines_service_is_checked_and_rides_go_station_to_station_on_one_line():
    from traffic_simulator import metro

    network = generate_network(2, 2)
    line = network.add_metro_line((0.0, 0.0), (1000.0, 0.0))
    network.add_metro_stop(line.id, 2000.0, 0.0)
    other = network.add_metro_line((0.0, 3000.0), (1000.0, 3000.0))
    assert (line.headway_min, line.speed_kmh, line.fare_cents) == (5.0, 35.0, 100.0)
    assert (line.cars_per_train, line.car_capacity, line.train_capacity) == (5, 200, 1000)  # carriages x passengers
    line.headway_min = 0.0
    try:
        network.validate()
    except ValueError as exc:
        assert "headway" in str(exc)
    else:
        raise AssertionError("a headway of 0 was accepted")
    line.headway_min = 5.0
    trip = metro.ride(network, "M3", "M1")  # either way along the line
    assert trip["line"] == "ML1" and trip["km"] == 2.0 and abs(trip["ivt_min"] - 2.0 / 35 * 60) < 1e-9
    assert trip["wait_min"] == 2.5 and trip["cost"] == 100.0
    assert metro.ride(network, "M1", "M1") is None and metro.ride(network, "M1", other.stops[0]) is None  # no transfers
    assert metro.door(network, 0.0, 700.0) == ("M1", 700.0) and metro.door(network, 0.0, 1500.0) is None


def test_only_institutions_draw_on_the_metro_from_the_stations_in_their_circle():
    from traffic_simulator import network_tools as tools
    from traffic_simulator.institutions import place, vehicle_split
    from traffic_simulator.mode_behavior import VehicleModes

    network, modes = generate_network(3, 3), VehicleModes().modes
    line = network.add_metro_line((-200.0, 1100.0), (600.0, 1100.0))
    network.add_metro_stop(line.id, 1400.0, 1100.0)
    pairs = [("1W", "1E")]
    try:  # road pairs never take the metro: it has its own OD matrix
        tools.mode_plan(network, pairs, {"alternatives": [{"mode": "Car"}, {"mode": "Metro"}]}, modes, {pairs[0]: 600})
    except ValueError as exc:
        assert "metro" in str(exc).lower()
    else:
        raise AssertionError("a road pair took the metro")
    near = place(network, "Office", 650.0, 1180.0, 2000, 1000, people=200, employees=300)  # by M2; M1, M3 in its circle
    percent, info = tools.institution_split(network, near, modes, {})
    assert info["metro"]["door"] == "M2" and sorted(info["metro"]["stations"]) == ["M1", "M3"]
    assert 0 < percent["Metro"] < 100 and info["metro"]["places"] == info["origins"] + 2  # its junctions, and M1, M3
    narrow = place(network, "Shop", 750.0, 1700.0, 1000, 700)  # M2 within the walk (618 m), no other station in its circle
    percent, info = tools.institution_split(network, narrow, modes, {})
    assert percent["Metro"] == 0 and info["metro"]["stations"] == []
    near.mode_shares = {"Walk": 20.0, "Car": 50.0, "Metro": 30.0}
    per_person, _, unknown = vehicle_split(near, modes)
    assert not unknown and abs(per_person - 0.5 / 1.5) < 1e-9  # walkers and metro riders: no vehicles


def test_passengers_load_the_lines_against_the_hcms_capacity():
    from traffic_simulator import metro

    network = generate_network(2, 2)
    line = network.add_metro_line((0.0, 0.0), (1000.0, 0.0))
    network.add_metro_stop(line.id, 2000.0, 0.0)
    other = network.add_metro_line((0.0, 3000.0), (1000.0, 3000.0))
    assert metro.capacity_per_h(line) == 60 / 5 * 1000 * 0.75  # HCM 2000 Eq 27-28: 9,000 an hour each way
    found = metro.loads(network, [("M1", "M3", 3000.0), ("M2", "M3", 1000.0), ("M3", "M1", 500.0),
                                  ("M1", other.stops[0], 200.0)])  # another line: no transfers, not served
    row = next(row for row in found["lines"] if row["line"] == "ML1")
    assert (row["riders_per_h"], row["peak_load_per_h"], row["peak_segment"]) == (4500, 4000, "M2 to M3")
    assert row["passenger_km_per_h"] == 3000 * 2 + 1000 + 500 * 2 and row["v_c"] == 0.44 and not row["over_capacity"]
    assert row["stops"]["M3"] == {"alighting": 4000, "boarding": 500} and found["unserved_per_h"] == 200
    busy = next(row for row in metro.loads(network, [("M1", "M3", 12000.0)])["lines"] if row["line"] == "ML1")
    assert busy["over_capacity"] and busy["v_c"] == 1.33


def test_the_metro_has_its_own_od_matrix_and_the_report_loads_it_with_the_buildings():
    from traffic_simulator.institutions import place

    window = _window()
    network = window.network
    line = network.add_metro_line((-200.0, 1100.0), (600.0, 1100.0))
    network.add_metro_stop(line.id, 1400.0, 1100.0)
    network.add_metro_line((-200.0, 2500.0), (600.0, 2500.0))
    window._commit()
    window.tabs.setCurrentWidget(window.demand_tab)
    window.od_panel.demand.setCurrentIndex(window.od_panel.demand.findData("metro"))
    assert window.workspace.currentWidget() is window.metro_matrix and window.metro_matrix.stations[:3] == ["M1", "M2", "M3"]
    table = window.metro_matrix.table
    steps = len(window.history.undo_stack)
    table.item(0, 2).setText("2500")  # M1 to M3
    table.item(2, 0).setText("abc")  # refused and put back
    table.item(0, 3).setText("100")  # M1 to M4, another line: no ride, refused
    assert network.metro_demand == {"M1->M3": 2500.0} and table.item(2, 0).text() == ""
    assert not table.item(0, 3).flags() & Qt.ItemFlag.ItemIsEditable and len(window.history.undo_stack) == steps + 1
    report = window.ai_mode._execute("metro_report", {})
    row = next(row for row in report["lines"] if row["line"] == "ML1")
    assert row["peak_load_per_h"] == 2500 and "at its busiest" in window.metro_summary.text()
    building = place(network, "Office", 650.0, 1180.0, 2000, 1000, people=200, employees=300)
    building.mode_shares = {"Car": 60.0, "Metro": 40.0}  # its metro riders between M2 and M1, M3, in equal shares
    report = window._metro_report()
    row = next(row for row in report["lines"] if row["line"] == "ML1")
    assert row["riders_per_h"] > 2500 and row["stops"]["M2"]["alighting"] > 0
    window.undo()
    assert window.network.metro_demand == {}
    window.close()


def _window():
    from traffic_simulator.main import MainWindow

    window = MainWindow()
    window._ask_to_save = False
    window.tabs.setCurrentWidget(window.edit_tab)
    window.edit_tabs.setCurrentIndex(1)  # Edit Network → Metro
    return window


def test_deploying_places_a_stop_a_click_joined_to_the_last_and_a_right_click_finishes():
    window = _window()
    answers = []
    window._accept_metro_line = lambda summary: (answers.append(summary), answers_given.pop(0))[1]
    answers_given = [{"headway_min": 4.0, "speed_kmh": 40.0, "cars_per_train": 6, "car_capacity": 180,
                      "fare_cents": 150.0}, None]
    # the first line accepted with its service, the second deleted
    assert window.metro_summary.text() == "No metro lines yet" and not window.scene.metro_deploying
    steps = len(window.history.undo_stack)
    window.metro_button.setChecked(True)
    assert window.scene.metro_deploying and "first stop" in window.mode_hint.text()
    window.metro_clicked(0.0, 100.0)  # the first stop waits for the second: no line without track
    assert not window.network.metro_lines and window.scene.metro_pending == (0.0, 100.0)
    window.metro_clicked(60.0, 100.0)  # too near: refused, said why
    assert not window.network.metro_lines and "Too near" in window.mode_hint.text()
    for x in (600.0, 1200.0):
        window.metro_clicked(x, 100.0)
    assert window.network.metro_lines["ML1"].stops == ["M1", "M2", "M3"]
    assert window.metro_summary.text() == "1 line, 3 stops, 1.20 km of track" and "M3" in window.mode_hint.text()
    assert len(window.history.undo_stack) == steps  # no undo step until it is accepted
    window.view.right_clicked.emit()  # finishes: accepted
    assert answers == ["ML1: 3 stops, 1.20 km of track"] and not window.metro_button.isChecked()
    assert "accepted" in window.statusBar().currentMessage() and len(window.history.undo_stack) == steps + 1
    line = window.network.metro_lines["ML1"]
    assert (line.headway_min, line.speed_kmh, line.cars_per_train, line.car_capacity, line.fare_cents) == \
        (4.0, 40.0, 6, 180, 150.0) and line.train_capacity == 1080
    window.metro_button.setChecked(True)  # a new deployment is a new line: the accepted one is never extended
    for x in (0.0, 600.0):
        window.metro_clicked(x, 900.0)
    window.cancel()  # Esc finishes too: deleted, nothing left of it and no undo step
    assert list(window.network.metro_lines) == ["ML1"] and len(window.network.metro_stops) == 3
    assert "deleted" in window.statusBar().currentMessage() and len(window.history.undo_stack) == steps + 1
    window.metro_button.setChecked(True)
    window.metro_clicked(0.0, 900.0)
    window.metro_clicked(0.0, 1500.0)
    window.undo()  # while deploying, Ctrl+Z takes back the unfinished line only
    assert list(window.network.metro_lines) == ["ML1"] and not window.metro_button.isChecked()
    window.undo()  # then the accepted line goes, as one step
    assert not window.network.metro_lines and window.metro_summary.text() == "No metro lines yet"
    window.redo()
    assert window.network.metro_lines["ML1"].stops == ["M1", "M2", "M3"]
    window.metro_button.setChecked(True)
    window.metro_clicked(0.0, 900.0)
    window.edit_tabs.setCurrentIndex(0)  # another tab ends it; a lone first stop is dropped without asking
    assert not window.metro_button.isChecked() and len(window.network.metro_stops) == 3 and len(answers) == 2
    window.close()


def test_a_right_click_is_told_from_a_right_drag_pan():
    window = _window()
    window.resize(1200, 800)
    window.show()
    clicks = []
    window.view.right_clicked.connect(lambda: clicks.append(1))
    viewport = window.view.viewport()
    QTest.mouseClick(viewport, Qt.MouseButton.RightButton, pos=QPoint(400, 300))
    assert clicks == [1]
    QTest.mousePress(viewport, Qt.MouseButton.RightButton, pos=QPoint(400, 300))
    QTest.mouseMove(viewport, QPoint(460, 340))
    QTest.mouseRelease(viewport, Qt.MouseButton.RightButton, pos=QPoint(460, 340))
    assert clicks == [1]  # a pan, not a click
    window.close()


if __name__ == "__main__":
    for name, check in list(globals().items()):
        if name.startswith("test_"):
            check()
    print("ok")

"""Vehicle types (Mode Behavior) and how the Demand Generator follows them, offscreen.
Run with `python tests/test_mode_behavior.py` (or the test monitor)."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"))  # real fonts

from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from traffic_simulator.mode_behavior import (MAX_MODES, ModeBehaviorPanel, VehicleModes, fixed_split,  # noqa: E402
                                             redistribute)
from traffic_simulator.od_matrix import ODCell, ODCellPanel, ODMatrix  # noqa: E402

APP = QApplication.instance() or QApplication([])


def test_deleted_shares_go_to_the_others_in_proportion():
    assert redistribute((10, 85, 5), 2) == (11, 89)  # 10:85 kept, in whole percent adding to 100
    assert redistribute((0, 0, 100), 2) == (100, 0)  # nobody else had a share: the first takes it
    assert redistribute((25, 25, 25, 25), 0) == (34, 33, 33) and sum(redistribute((1, 2, 97), 1)) == 100


def test_the_slider_splits_between_any_number_of_types():
    modes = VehicleModes()
    modes.add("Tram")
    matrix = ODMatrix(modes)
    panel = ODCellPanel(matrix)
    slider = panel.modes
    slider.set_split((10, 60, 20, 10))
    assert slider.cuts == [10, 70, 90] and slider.split() == (10, 60, 20, 10)
    slider.set_share(1, 30)  # Car: the handle after it moves, Bike absorbs
    assert slider.split() == (10, 30, 50, 10)
    slider.set_share(3, 70)  # Tram (last): the handle before it moves, pushing the ones it passes
    assert slider.split() == (10, 20, 0, 70)
    slider.set_share(0, 95)  # Bus: pushes every handle to its right
    assert slider.split() == (95, 0, 0, 5)
    for box, share in zip(panel.mode_shares, (25, 25, 25)):  # typed in turn: the last type takes the rest
        box.setValue(share)
    assert slider.split() == (25, 25, 25, 25) and len(panel.mode_shares) == 4


def test_types_are_renamed_added_and_deleted_everywhere():
    modes = VehicleModes()
    matrix = ODMatrix(modes)
    panel = ODCellPanel(matrix)
    tab = ModeBehaviorPanel(modes)
    matrix.set_ends(["1W", "2W"], ["1E", "2E"])
    matrix.cells[("1W", "1E")] = ODCell(flow=100, split=(10, 85, 5))
    matrix.cells[("2W", "2E")] = ODCell(flow=50, split=(0, 0, 100))

    answer = QMessageBox.question
    QMessageBox.question = lambda *args: QMessageBox.StandardButton.Yes  # keep a headway: a timetabled type
    try:
        tab.add_button.click()  # a new type: 0 % in every pair, a box and a handle in the Demand Generator
    finally:
        QMessageBox.question = answer
    assert modes.modes[3].timetabled and tab.details_title.text() == "Type 4"  # its values open, headway and all
    assert modes.names() == ["Bus", "Car", "Bike", "Type 4"] and matrix.cells[("1W", "1E")].split == (10, 85, 5, 0)
    assert len(panel.mode_shares) == 4 and len(panel.modes.cuts) == 3 and modes.default_split == (10, 85, 5, 0)
    tab.names[3].setText("Tram")
    tab.names[3].editingFinished.emit()  # renamed in its box
    tab.names[2].setText("Cycle")
    tab.names[2].editingFinished.emit()
    assert modes.names() == ["Bus", "Car", "Cycle", "Tram"] and matrix.cells[("2W", "2E")].split == (0, 0, 100, 0)

    matrix.table.setCurrentCell(0, 0)
    panel.command("set_od_demand", {"pairs": ["1W->1E"], "demand": "hourly",
                                    "mode_split": [{"mode": "tram", "percent": 30}, {"mode": "Car", "percent": 70}]})
    assert matrix.cells[("1W", "1E")].split == (0, 70, 0, 30) and panel.modes.split() == (0, 70, 0, 30)
    assert panel.command("read_od_matrix", {})["vehicle_types"] == ["Bus", "Car", "Cycle", "Tram"]
    try:
        panel.command("set_od_demand", {"pairs": ["1W->1E"], "demand": "hourly", "mode_split": {"Bike": 100}})
    except ValueError as exc:
        assert "No vehicle type called bike" in str(exc) and "Cycle" in str(exc)
    else:
        raise AssertionError("a vehicle type that doesn't exist must be refused")

    answer = QMessageBox.question
    QMessageBox.question = lambda *args: QMessageBox.StandardButton.Yes
    try:
        tab._delete(1)  # Car's share goes to the others in proportion
    finally:
        QMessageBox.question = answer
    assert modes.names() == ["Bus", "Cycle", "Tram"] and matrix.cells[("1W", "1E")].split == (0, 0, 100)
    assert len(panel.mode_shares) == 3 and len(tab.names) == 3

    for problem in (lambda: modes.rename(0, "tram"), lambda: modes.rename(0, "  ")):
        try:
            problem()
        except ValueError:
            pass
        else:
            raise AssertionError("duplicate or empty names must be refused")
    while len(modes.modes) < MAX_MODES:
        modes.add()
    assert not tab.add_button.isEnabled() and len({mode.colour for mode in modes.modes}) == MAX_MODES
    while len(modes.modes) > 1:
        modes.delete(0)
    assert not tab.rows.itemAtPosition(0, 2).widget().isEnabled()  # the last type can't be deleted


def test_a_headway_fixes_the_bus_share_and_flow_moves_only_the_others():
    assert fixed_split((10, 85, 5), {0: 6}, 100) == (6, 89, 5)  # 6 buses in 100 trips; Car:Bike kept
    assert fixed_split((10, 85, 5), {0: 6}, 200) == (3, 92, 5)
    assert fixed_split((10, 85, 5), {0: 6}, 4) == (100, 0, 0)  # more buses than trips: all of them
    assert fixed_split((10, 85, 5), {0: 6}, None) == (10, 85, 5)  # no flow yet: nothing to fix
    modes = VehicleModes()
    matrix = ODMatrix(modes)
    panel = ODCellPanel(matrix)
    tab = ModeBehaviorPanel(modes)
    matrix.set_ends(["1W", "2W"], ["1E", "2E"])
    matrix.table.setCurrentCell(0, 0)
    tab._pick(0)  # Bus clicked: its headway shows
    tab.headway.setValue(10)  # Bus every 10 min: 6 an hour in every pair
    assert modes.vehicles_per_hour() == {0: 6} and panel.mode_shares[0].isReadOnly()
    for flow in (2, 20, 200):  # typed digit by digit: the tiny flows on the way lose nothing
        panel.flow.setValue(flow)
    assert panel.modes.split() == (3, 92, 5) == matrix.split_of(matrix.cells[("1W", "1E")])
    panel.modes._move(0, 40)  # the handle beside Bus is locked
    panel.mode_shares[1].setValue(50)  # Car: Bike takes the rest, Bus stays
    assert panel.modes.split() == (3, 50, 47)
    panel.flow.setValue(300)  # more flow: the same 6 buses are a smaller share; Car and Bike keep their ratio
    assert panel.modes.split() == (2, 51, 47) and [box.value() for box in panel.mode_shares] == [2, 51, 47]
    tab.headway.setValue(0)  # no timetable: Bus's share is free again
    assert not panel.mode_shares[0].isReadOnly() and modes.vehicles_per_hour() == {}


def test_the_window_orders_tabs_and_undoes_type_changes():
    from traffic_simulator.main import MainWindow

    window = MainWindow()
    names = [window.tabs.tabToolTip(i) for i in range(window.tabs.count())]  # hovering a tab names it
    assert names == ["Create Network", "Edit Network", "Mode Behavior", "Demand Generator", "AI Mode", "View Summary",
                     "Extensions"]
    if not window.tabs.tabIcon(0).isNull():  # icons need Windows' icon font
        window.tabs.setCurrentIndex(3)  # the tabs are icons; the open one's name heads the sidebar
        assert not any(window.tabs.tabText(i) for i in range(window.tabs.count()))
        assert window.page_title.text() == "Demand Generator"
    window.od_matrix.set_ends(*window._od_ends())
    window.od_matrix.cells[("1W", "1E")] = ODCell(flow=100, split=(10, 85, 5))
    window._commit()
    window.vehicle_modes.delete(2)  # Bike goes: one undo step
    assert window.od_matrix.cells[("1W", "1E")].split == (11, 89)
    window.undo()
    assert window.vehicle_modes.names() == ["Bus", "Car", "Bike"]
    assert window.od_matrix.cells[("1W", "1E")].split == (10, 85, 5) and len(window.od_panel.mode_shares) == 3
    window.redo()
    assert window.vehicle_modes.names() == ["Bus", "Car"] and window.od_matrix.cells[("1W", "1E")].split == (11, 89)
    window.behavior_tab._pick(0)
    window.behavior_tab.headway.setValue(12)  # a bus every 12 min: 5 in the pair's 100 trips; one undo step
    assert window.od_matrix.split_of(window.od_matrix.cells[("1W", "1E")]) == (5, 95)
    window.undo()
    assert window.vehicle_modes.modes[0].headway_min == 0 and window.behavior_tab.headway.value() == 0
    window.close()


def test_the_assistant_creates_vehicle_types_as_sumo_classes():
    from traffic_simulator.main import MainWindow
    from traffic_simulator.vehicle import VEHICLE_CLASSES

    window = MainWindow()
    ai, modes = window.ai_mode, window.vehicle_modes
    listed = ai._execute("list_vehicle_types", {})
    assert [t["name"] for t in listed["types"]] == ["Bus", "Car", "Bike"] and "tram" in listed["kinds_create_vehicle_types_can_make"]
    for bad, message in (([{"name": "Car", "kind": "passenger"}], "already"),
                         ([{"name": "Tram", "kind": "hovercraft"}], "kind must be one of"),
                         ([{"name": "Tram", "kind": "tram", "headway_min": 500}], "headway"),
                         ([{"name": f"T{i}", "kind": "truck"} for i in range(6)], "Up to 8")):
        assert message in ai._check("create_vehicle_types", {"types": bad}), bad  # refused before asking
    assert modes.names() == ["Bus", "Car", "Bike"]  # nothing added
    steps = len(window.history.undo_stack)
    result = ai._execute("create_vehicle_types", {"types": [{"name": "Tram", "kind": "tram", "headway_min": 8},
                                                             {"name": "Truck", "kind": "truck", "occupancy": 1.2}]})
    assert [t["name"] for t in result["added"]] == ["Tram", "Truck"] and modes.names()[-2:] == ["Tram", "Truck"]
    tram, truck = modes.modes[-2:]
    assert (tram.length_m, tram.max_speed_kmh, tram.decel, tram.headway_min, tram.occupancy) == (22.0, 80.0, 3.0, 8, 60.0)
    assert (truck.length_m, truck.accel, truck.speed_dev, truck.occupancy) == (7.1, 1.3, 0.05, 1.2)
    assert len(VEHICLE_CLASSES) == 13  # every SUMO class on its defaults page
    assert len(window.history.undo_stack) == steps + 1  # both types: one undo step
    window.undo()
    assert modes.names() == ["Bus", "Car", "Bike"]
    window.close()


def test_a_headway_is_typed_and_a_clicked_type_shows_its_values():
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    from traffic_simulator.main import MainWindow

    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    modes, panel = window.vehicle_modes, window.behavior_tab
    assert [mode.pcu for mode in modes.modes] == [2.0, 1.0, 0.2]  # bus, car, bike (UK DMRB TA 79/99)
    for bad in (0, 11):
        try:
            modes.set_pcu(1, bad)
        except ValueError as exc:
            assert "a car is 1" in str(exc)
        else:
            raise AssertionError(f"PCU {bad} should be refused")
    window.tabs.setCurrentIndex(window.tab_names.index("Mode Behavior"))
    window.show()
    window.activateWindow()  # other tests' windows may still be open
    app.processEvents()
    assert panel.details.isHidden()  # until a type is clicked
    QTest.mouseClick(panel.names[0], Qt.MouseButton.LeftButton)
    headway = panel.headway
    assert headway.isVisibleTo(panel) and headway.specialValueText() == "" and headway.text() == "0 min"  # no "No timetable" text in the box
    QTest.mouseClick(headway.lineEdit(), Qt.MouseButton.LeftButton)  # clicked in, it is all selected...
    app.processEvents()
    QTest.keyClicks(headway, "12")  # ...so the digits replace the 0
    QTest.keyClick(headway, Qt.Key.Key_Return)
    assert modes.modes[0].headway_min == 12
    QTest.mouseClick(panel.names[2], Qt.MouseButton.LeftButton)
    assert not panel.details.isHidden() and panel.details_title.text() == "Bike" and panel.pcu.value() == 0.2
    assert not headway.isVisibleTo(panel) and panel.occupancy.value() == 1.0  # a bike has no headway
    panel.occupancy.setValue(1.2)
    assert modes.modes[2].occupancy == 1.2
    steps = len(window.history.undo_stack)
    panel.pcu.setValue(0.3)
    panel.pcu.setValue(0.4)  # one undo step for the edit
    assert modes.modes[2].pcu == 0.4 and len(window.history.undo_stack) == steps + 1
    window.undo()
    assert modes.modes[2].pcu == 0.2 and panel.pcu.value() == 0.2  # the details follow
    window.close()


def test_only_a_timetabled_type_has_a_headway():
    modes = VehicleModes()
    assert [mode.timetabled for mode in modes.modes] == [True, False, False]  # Bus; never Car or Bike
    for problem in (lambda: modes.set_headway(1, 10), lambda: modes.update("Bike", {"headway_min": 5}),
                    lambda: modes.set_occupancy(0, 0)):
        try:
            problem()
        except ValueError:
            pass
        else:
            raise AssertionError("a headway for a car or bike, or no one aboard, must be refused")
    assert [mode.headway_min for mode in modes.modes] == [0, 0, 0]
    modes.update("Car", {"timetabled": True, "headway_min": 15})  # made timetabled, it may have one
    assert modes.modes[1].headway_min == 15
    modes.update("Car", {"timetabled": False})  # and loses it with the timetable
    assert modes.modes[1].headway_min == 0
    assert modes.modes[modes.add("Lorry")].timetabled is False


if __name__ == "__main__":
    for name, check in list(globals().items()):
        if name.startswith("test_"):
            check()
    print("ok")

"""Vehicle types (Mode Behavior) and how the Demand Generator follows them, offscreen.
Run with `python tests/test_mode_behavior.py` (or the test monitor)."""

import os
import subprocess
import sys
from pathlib import Path

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


def test_a_click_on_a_types_zone_that_slips_moves_no_handle():
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtGui import QMouseEvent

    slider = ODCellPanel(ODMatrix(VehicleModes())).modes
    slider.resize(220, 30)
    slider.set_split((10, 85, 5))
    clicked = []
    slider.type_clicked.connect(clicked.append)
    y = slider.TRACK_TOP + slider.TRACK_HEIGHT / 2

    def mouse(kind, x, buttons):
        point = QPointF(x, y)
        QApplication.sendEvent(slider, QMouseEvent(kind, point, slider.mapToGlobal(point), Qt.MouseButton.LeftButton,
                                                   buttons, Qt.KeyboardModifier.NoModifier))

    car = slider._x(50)
    mouse(QMouseEvent.Type.MouseButtonPress, car, Qt.MouseButton.LeftButton)
    mouse(QMouseEvent.Type.MouseMove, car - 30, Qt.MouseButton.LeftButton)  # the hand slips mid-click
    mouse(QMouseEvent.Type.MouseButtonRelease, car - 30, Qt.MouseButton.NoButton)
    assert clicked == [1] and slider.split() == (10, 85, 5)  # Car picked; no handle followed the slip
    mouse(QMouseEvent.Type.MouseButtonPress, slider._x(10), Qt.MouseButton.LeftButton)  # on Bus|Car's handle
    mouse(QMouseEvent.Type.MouseMove, slider._x(20), Qt.MouseButton.LeftButton)
    assert slider.split() == (20, 75, 5)  # a drag that started on a handle still moves it


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


def test_a_types_vehicle_class_outlives_its_name():
    """What a type is (its vehicle_class: a bus calls at bus stops) is kept through a rename, set by the kind it is
    made as, changed only when asked, saved and checked; a type saved before it, named Bus, is a bus."""
    from dataclasses import asdict

    from traffic_simulator.mode_behavior import VehicleMode
    from traffic_simulator.network_io import project_problem

    modes = VehicleModes()
    assert [mode.vehicle_class for mode in modes.modes] == ["bus", "passenger", "bicycle"]
    modes.update("Bus", {"new_name": "City Bus"})
    modes.create([{"name": "Express", "kind": "bus", "headway_min": 15}])
    modes.update("Car", {"vehicle_class": "taxi"})
    assert [mode.vehicle_class for mode in modes.modes] == ["bus", "taxi", "bicycle", "bus"]
    try:
        modes.update("Car", {"vehicle_class": "hovercraft"})
    except ValueError as exc:
        assert "vehicle_class must be one of" in str(exc)
    else:
        raise AssertionError("an unknown class is refused")
    types = {"types": [asdict(mode) for mode in modes.modes], "default_split": list(modes.default_split)}
    assert project_problem({"vehicle_types": types}) is None
    types["types"][1]["vehicle_class"] = "hovercraft"
    assert "vehicle_class must be one of" in project_problem({"vehicle_types": types})
    old = {key: value for key, value in asdict(modes.modes[0]).items() if key != "vehicle_class"}
    assert VehicleMode(**{**old, "name": "Bus"}).vehicle_class == "bus"
    assert VehicleMode(**{**old, "name": "Coach"}).vehicle_class == "passenger"


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


def test_a_reaction_time_shorter_than_a_step_is_refused():
    modes = VehicleModes()
    try:
        modes.update("Car", {"tau_s": 0.1})  # Krauss can't keep a gap the driver reacts to faster than a step
    except ValueError as error:
        assert "simulation step" in str(error), error
    else:
        raise AssertionError("a reaction time under the 0.5 s step must be refused: vehicles would collide")
    assert modes.modes[1].tau_s == 1.0 and modes.update("Car", {"tau_s": 0.5}) == 1 and modes.modes[1].tau_s == 0.5


def test_emergency_braking_is_sumos_by_class_and_no_softer_than_comfortable():
    # SUMO's emergencyDecel by vClass (passenger 9, bus and bicycle 7, a made truck 7); comfortable braking above it is
    # refused, by update and in a saved project; a type saved before it gets its class's, or its decel if harder.
    from dataclasses import asdict

    from traffic_simulator.mode_behavior import VehicleMode
    from traffic_simulator.network_io import project_problem

    modes = VehicleModes()
    modes.create([{"name": "Truck", "kind": "truck"}])
    assert [mode.emergency_decel for mode in modes.modes] == [7.0, 9.0, 7.0, 7.0]
    for changes in ({"decel": 9.5}, {"emergency_decel": 4.0}, {"decel": 6.0, "emergency_decel": 5.0}):
        try:
            modes.update("Car", changes)
        except ValueError as error:
            assert "emergency_decel" in str(error), error
        else:
            raise AssertionError(f"{changes} must be refused: comfortable braking harder than the hardest")
    assert (modes.modes[1].decel, modes.modes[1].emergency_decel) == (4.5, 9.0)  # a refusal changes nothing
    modes.update("Car", {"decel": 9.5, "emergency_decel": 12.0})
    types = {"types": [asdict(mode) for mode in modes.modes], "default_split": list(modes.default_split)}
    assert project_problem({"vehicle_types": types}) is None and types["types"][1]["emergency_decel"] == 12.0
    types["types"][1]["emergency_decel"] = 9.0
    assert "no more than emergency_decel" in project_problem({"vehicle_types": types})
    old = {key: value for key, value in asdict(modes.modes[1]).items() if key != "emergency_decel"}
    assert VehicleMode(**old).emergency_decel == 9.5 and VehicleMode("Bike", "#000000", vehicle_class="bicycle").emergency_decel == 7.0


def test_two_wheelers_filter_by_their_class_and_it_is_kept():
    # Lateral runs (Simulation lateral): a motorcycle, moped or bicycle filters past queues within its lane; a car or
    # bus never; the assistant may change it; a project keeps it, and one saved before it gets it by the type's class.
    from dataclasses import asdict

    from traffic_simulator.mode_behavior import VehicleMode
    from traffic_simulator.network_io import project_problem
    from traffic_simulator.vehicle import create_vehicle

    modes = VehicleModes()
    modes.create([{"name": "Moto", "kind": "motorcycle"}, {"name": "Scooter", "kind": "moped"}, {"name": "Van", "kind": "delivery"}])
    assert [mode.filters for mode in modes.modes] == [False, False, True, True, True, False]  # Bus, Car, Bike, ...
    assert create_vehicle(modes.modes[3], "m").filters and not create_vehicle(modes.modes[1], "c").filters
    wide = VehicleMode("Wide", "#000000", vehicle_class="motorcycle", width_m=1.5)  # too wide to share half a lane
    assert wide.filters and not create_vehicle(wide, "w").filters
    modes.update("Moto", {"filters": False})
    for bad in ({"filters": "yes"}, {"filters": 1}):
        try:
            modes.update("Car", bad)
        except ValueError as exc:
            assert "filters must be true or false" in str(exc)
        else:
            raise AssertionError(f"{bad} must be refused")
    records = [asdict(mode) for mode in modes.modes]
    types = {"types": records, "default_split": list(modes.default_split)}
    assert project_problem({"vehicle_types": types}) is None
    assert [VehicleMode(**record).filters for record in records] == [False, False, True, False, True, False]
    old = {key: value for key, value in records[4].items() if key != "filters"}  # saved before it: by its class
    assert VehicleMode(**old).filters is True
    records[1]["filters"] = "yes"
    assert "filters must be true or false" in project_problem({"vehicle_types": types})


def test_undo_leaves_a_half_typed_name_alone():
    from PySide6.QtTest import QTest

    from traffic_simulator.main import MainWindow

    window = MainWindow()
    window._ask_to_save = False
    window.show()
    window.activateWindow()
    QTest.qWaitForWindowActive(window)  # the name box really has the focus, as on screen
    window.tabs.setCurrentWidget(window.behavior_tab)
    window.vehicle_modes.add("Tram")
    window._commit()
    before = [mode.name for mode in window.vehicle_modes.modes]
    box = window.behavior_tab.names[1]
    box.setFocus()
    APP.processEvents()
    box.setText("Auto")  # typed, not Entered, then Edit > Undo from the menu
    window.undo()
    APP.processEvents()
    assert [mode.name for mode in window.vehicle_modes.modes] == before[:-1]  # the add undone, no rename applied
    assert window.history.redo_stack  # so Redo still brings the Tram back
    window.redo()
    assert [mode.name for mode in window.vehicle_modes.modes] == before
    window.close()


def test_vehicle_types_need_no_qt():
    """A vehicle type (VehicleMode) lives in vehicle.py: made and filled in without Qt or this tab, so vehicle.py names
    no Qt module, even for its types, and the physics doesn't loop back through the tab."""
    check = ("import sys; from traffic_simulator.vehicle import VehicleMode; "
             "m = VehicleMode('Moto', '#123456', vehicle_class='motorcycle'); "
             "print(m.filters, m.emergency_decel, sorted(sys.modules))")
    loaded = subprocess.run([sys.executable, "-c", check], capture_output=True, text=True, timeout=60, check=True).stdout
    assert loaded.startswith("True 10.0 ")  # FILTERING_CLASSES and VEHICLE_CLASSES' emergency braking, read directly
    assert "PySide6" not in loaded and "traffic_simulator.mode_behavior" not in loaded
    source = (Path(__file__).resolve().parents[1] / "src" / "traffic_simulator" / "vehicle.py").read_text(encoding="utf-8")
    assert "TYPE_CHECKING" not in source and "mode_behavior import" not in source


if __name__ == "__main__":
    for name, check in list(globals().items()):
        if name.startswith("test_"):
            check()
    print("ok")

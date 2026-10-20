"""Selecting with a box, AutoCAD's way (left to right: a window takes what lies wholly inside;
right to left: a crossing box takes whatever it touches), and deleting what it took.
Run with `python tests/test_selection.py` (or the test monitor)."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPointF, Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from traffic_simulator.geometry import polyline_meets_box  # noqa: E402
from traffic_simulator.network_generator import generate_network  # noqa: E402
from traffic_simulator.network_tools import delete_objects  # noqa: E402

APP = QApplication.instance() or QApplication([])
# generate_network(3, 4): junctions 500 m apart; "2,2" (column 2, row 2 from the bottom) sits at (500, 500).


def test_a_line_meets_a_box_when_it_crosses_or_enters_it():
    assert polyline_meets_box([(-10, 5), (20, 5)], 0, 0, 10, 10)  # straight through
    assert polyline_meets_box([(5, 5), (50, 50)], 0, 0, 10, 10)  # starts inside
    assert polyline_meets_box([(-5, 12), (12, -5)], 0, 0, 10, 10)  # cuts a corner, no point inside
    assert not polyline_meets_box([(-5, 20), (20, 20)], 0, 0, 10, 10)  # passes by


def test_a_window_takes_what_lies_inside_a_crossing_box_what_it_touches():
    network = generate_network(3, 4)
    network.add_bus_stop("1,2-2,2", "both")  # its bay from 230 m to 270 m along the road at y = 500
    around_junction = (400, 400, 600, 600)
    assert network.objects_in_box(*around_junction, crossing=False) == [("node", "2,2")]
    touched = network.objects_in_box(*around_junction, crossing=True)
    assert sorted(touched) == sorted([("node", "2,2"), ("road", "1,2-2,2"), ("road", "2,2-3,2"),
                                      ("road", "2,2-2,1"), ("road", "2,3-2,2")])
    around_stop = (200, 450, 300, 550)
    assert network.objects_in_box(*around_stop, crossing=False) == [("bus_stop", "BS1")]
    assert sorted(network.objects_in_box(*around_stop, crossing=True)) == [("bus_stop", "BS1"), ("road", "1,2-2,2")]
    everything = network.objects_in_box(-1000, -1000, 3000, 3000, crossing=False)
    assert len(everything) == len(network.nodes) + len(network.approach_nodes) + len(list(network.all_roads())) + 1
    delete_objects(network, [identifier for _, identifier in everything])  # the whole network, bus stop too
    assert not (network.nodes or network.roads or network.approach_nodes or network.bus_stops)
    network.validate()


def test_the_map_selects_by_box_and_deletes_what_it_took():
    from traffic_simulator.main import MainWindow

    window = MainWindow()
    window.install_network(generate_network(3, 4))
    window._commit()
    window.resize(1400, 900)
    window.show()
    QTest.qWaitForWindowExposed(window)
    view, scene = window.view, window.scene
    view.resetTransform()
    view.scale(0.5, 0.5)
    view.centerOn(500, 500)

    def drag(start, end, modifier=Qt.KeyboardModifier.NoModifier):
        a, b = view.mapFromScene(QPointF(*start)), view.mapFromScene(QPointF(*end))
        QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, modifier, a)
        QTest.mouseMove(view.viewport(), (a + b) / 2)
        QTest.mouseMove(view.viewport(), b)
        QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, modifier, b)

    drag((380, 380), (620, 620))  # left to right: a window
    assert window.group == [("node", "2,2")] and scene.group == {("node", "2,2")}
    assert "1 junction selected" == window.properties.object_type.text()
    assert window.properties.group_fields.isVisibleTo(window.properties) and scene._band is None  # the band's gone
    drag((620, 620), (380, 380))  # right to left: a crossing box
    assert len(window.group) == 5 and ("road", "1,2-2,2") in window.group
    drag((880, 380), (1120, 620), Qt.KeyboardModifier.ControlModifier)  # Ctrl: adds junction 3,2
    assert ("node", "3,2") in window.group and len(window.group) == 6
    asked = []
    real, QMessageBox.question = QMessageBox.question, lambda *args: asked.append(args[2]) or QMessageBox.StandardButton.Yes
    try:
        view.setFocus()
        QTest.keyClick(view, Qt.Key.Key_Delete)
    finally:
        QMessageBox.question = real
    assert asked and asked[0].startswith("Delete 2 junctions, 4 roads?") and "3 other roads" in asked[0]
    network = window.network
    assert "2,2" not in network.nodes and "3,2" not in network.nodes and "1,2-2,2" not in network.roads
    assert "2,2-3,2" not in network.roads and window.group == [] and scene.group == set()
    window.undo()  # all of it back in one step
    assert "2,2" in window.network.nodes and "3,2" in window.network.nodes and "1,2-2,2" in window.network.roads

    drag((380, 380), (620, 620))
    QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=view.mapFromScene(QPointF(250, 250)))
    assert window.group == [] and scene.selection is None  # a click on empty map clears it

    window.add_node_button.setChecked(True)  # a tool on: no box, and its click still places a node
    nodes = len(window.network.nodes)
    drag((380, 380), (620, 620))
    assert window.group == [] and len(window.network.nodes) == nodes + 1
    window.close()


def test_ctrl_clicking_two_meeting_roads_offers_add_slip_road():
    from traffic_simulator.main import MainWindow

    window = MainWindow()
    window.install_network(generate_network(3, 4))
    window._commit()
    button = window.properties.slip_road_button
    window.canvas_clicked("road", "1W")
    window.canvas_clicked("road", "2,1-3,1", additive=True)  # doesn't meet 1W: no slip road
    assert window.group == [("road", "1W"), ("road", "2,1-3,1")] and button.isHidden()
    window.canvas_clicked("road", "2,1-3,1", additive=True)  # Ctrl+click again takes it out
    assert window.group == [] and window.scene.selection == ("road", "1W")
    window.canvas_clicked("road", "1,2-1,1", additive=True)  # meets 1W at 1,1
    assert not button.isHidden() and button.toolTip() == "Add Slip Road"
    roads = len(window.network.roads)
    assert [action.text() for action in button.menu().actions()] == ["Single lane", "Two lanes"]
    button.menu().actions()[0].trigger()
    kind, slip = window.scene.selection
    assert kind == "road" and window.network.roads[slip].single_lane
    assert len(window.network.roads) == roads + 3
    window.undo()  # one step takes it all back
    assert len(window.network.roads) == roads and slip not in window.network.roads
    button.menu().actions()[1].trigger()  # two lanes, this time
    _, slip = window.scene.selection
    start = window.network.nodes[window.network.roads[slip].from_node]
    before = (start.x, start.y)
    window.select("node", "1,1")
    window.properties.control.set_value("roundabout")
    window.properties.apply_control()  # the slip moves out, clear of the circle
    assert window.network.nodes["1,1"].control == "roundabout" and (start.x, start.y) != before
    window.close()


def test_a_refused_road_edit_leaves_the_network_as_it_was():
    from traffic_simulator.main import MainWindow

    window = MainWindow()
    window.install_network(generate_network(3, 4))
    window._commit()
    network, steps = window.network, len(window.history.undo_stack)
    window.select("road", "1,2-2,2")
    window.properties.lanes.setValue(3)
    window.properties.direction.set_value(2)  # reversed too: its swap is put back as well
    window.properties.speed.setCurrentText("0")  # refused
    warned = []
    real, QMessageBox.warning = QMessageBox.warning, lambda *args: warned.append(args[2])
    try:
        window.properties.apply_button.click()
    finally:
        QMessageBox.warning = real
    assert warned and window.network is network and len(window.history.undo_stack) == steps
    assert network == window.history.current[0]  # lanes, speed and ends: what the history holds
    window.close()


def test_a_turn_pocket_is_set_drawn_undone_and_refused_in_the_panel():
    from PySide6.QtWidgets import QGraphicsPolygonItem

    from traffic_simulator.main import MainWindow
    from traffic_simulator.network_scene import ROAD_COLOR

    window = MainWindow()
    window.install_network(generate_network(3, 4))
    window._commit()

    def surfaces() -> int:  # widened carriageway pieces (the pocket's) and bus bays, in the road's colour
        return sum(isinstance(item, QGraphicsPolygonItem) and item.brush().color() == ROAD_COLOR
                   for item in window.scene.items())

    before, steps = surfaces(), len(window.history.undo_stack)
    window.select("road", "1,2-2,2")
    panel = window.properties
    rows = [(panel.road_form.labelForField(row).text(), kind, length) for row, kind, length in panel.pocket_rows]
    assert [text for text, *_ in rows] == ["Pocket to 2,2", "Pocket to 1,2"] and not panel.apply_button.isEnabled()
    _, kind, length = rows[0]
    kind.setCurrentIndex(kind.findData("offside 2"))
    length.setValue(60)
    assert panel.apply_button.isEnabled()
    panel.apply_button.click()
    assert window.network.roads["1,2-2,2"].pockets == {"2,2": {"lanes": 2, "length_m": 60.0, "side": "offside"}}
    assert surfaces() == before + 1 and len(window.history.undo_stack) == steps + 1
    window.undo()
    assert window.network.roads["1,2-2,2"].pockets == {} and surfaces() == before
    window.select("road", "1,2-2,2")
    panel.pocket_rows[1][1].setCurrentIndex(1)  # a kerb pocket at 1,2, on a road without lane changing: refused
    panel.lane_changing.setChecked(False)
    warned = []
    real, QMessageBox.warning = QMessageBox.warning, lambda *args: warned.append(args[2])
    try:
        panel.apply_button.click()
    finally:
        QMessageBox.warning = real
    assert warned and "lane changing" in warned[0] and window.network.roads["1,2-2,2"].pockets == {}
    assert window.network.roads["1,2-2,2"].lane_changing
    window.close()


def test_a_cycle_lane_is_ticked_drawn_and_undone_in_the_panel():
    from PySide6.QtWidgets import QGraphicsPathItem

    from traffic_simulator.main import MainWindow
    from traffic_simulator.theme import CYCLE_LANE

    window = MainWindow()
    window.install_network(generate_network(3, 4))
    window._commit()

    def strips() -> int:
        return sum(isinstance(item, QGraphicsPathItem) and item.pen().color().name() == CYCLE_LANE
                   for item in window.scene.items())

    steps = len(window.history.undo_stack)
    window.select("road", "1,2-2,2")
    panel = window.properties
    assert [tick.text() for tick in panel.cycle_ticks] == ["To 2,2", "To 1,2"] and strips() == 0
    panel.cycle_ticks[0].setChecked(True)
    assert panel.apply_button.isEnabled()
    panel.apply_button.click()
    assert window.network.roads["1,2-2,2"].cycle_lanes == ["2,2"] and strips() == 1
    assert len(window.history.undo_stack) == steps + 1
    window.undo()
    assert window.network.roads["1,2-2,2"].cycle_lanes == [] and strips() == 0
    window.close()


def test_footpaths_are_a_roads_choice_highlighted_before_apply_and_paths_are_selected_and_deleted():
    from traffic_simulator.main import MainWindow

    window = MainWindow()
    window.install_network(generate_network(3, 4))
    window._commit()
    steps = len(window.history.undo_stack)
    window.select("road", "1,2-2,2")  # runs east: its sides are north and south
    panel = window.properties
    choice = panel.footpath_choice
    assert [choice.itemText(i) for i in range(choice.count())] == ["None", "Both sides", "North side only",
                                                                  "South side only"]
    assert choice.currentText() == "None" and not window.scene._footpath_preview
    choice.setCurrentIndex(choice.findText("Both sides"))  # highlighted on the map where it would go, before Apply
    assert len(window.scene._footpath_preview) == 2 and window.network.roads["1,2-2,2"].footpaths == []
    choice.setCurrentIndex(choice.findText("South side only"))
    assert len(window.scene._footpath_preview) == 1 and panel.apply_button.isEnabled()
    panel.apply_button.click()
    assert window.network.roads["1,2-2,2"].footpaths == ["right"] and len(window.history.undo_stack) == steps + 1
    assert not window.scene._footpath_preview  # applied: the paving itself now
    window.select("road", "1,2-2,2")
    assert choice.currentText() == "South side only"
    choice.setCurrentIndex(choice.findText("None"))  # the one to go: outlined dashed
    assert len(window.scene._footpath_preview) == 1
    assert not hasattr(window, "add_path_button")  # paths aren't drawn by hand; one from the map or the assistant:
    window.network.add_walkway("1,1", "2,2", [[250.0, 250.0]])
    window._commit()
    window.canvas_clicked("walkway", "P1")
    assert window.scene.selection == ("walkway", "P1") and "Delete removes it" in window.statusBar().currentMessage()
    window.delete_selection()
    assert not window.network.walkways
    window.undo()
    assert list(window.network.walkways) == ["P1"]
    window.close()


def test_the_pedestrian_tab_places_crossings_that_are_selected_deleted_and_undone():
    from traffic_simulator.main import MainWindow

    network = generate_network(2, 1)
    for road in network.all_roads():
        road.footpaths = ["left", "right"]
    window = MainWindow()
    window.install_network(network)
    window._commit()
    steps = len(window.history.undo_stack)
    window.tabs.setCurrentWidget(window.edit_tab)
    window.edit_tabs.setCurrentIndex(2)
    assert window.edit_tabs.tabText(2) == "Pedestrian"
    road = window.network.roads["1,2-1,1"]
    (ax, ay), (bx, by) = window.network.road_points(road)[0], window.network.road_points(road)[-1]
    window.zebra_button.setChecked(True)
    assert "zebra crossing" in window.mode_hint.text()
    window.canvas_clicked("node", "1,1")
    assert "Click a road" in window.mode_hint.text() and not window.network.pedestrian_crossings
    window.canvas_clicked("road", road.id, ax + (bx - ax) * 0.01, ay + (by - ay) * 0.01)  # on the junction: refused
    assert "at least 20 m" in window.mode_hint.text() and not window.network.pedestrian_crossings
    window.canvas_clicked("road", road.id, (ax + bx) / 2, (ay + by) / 2)
    assert [(c.kind, c.road) for c in window.network.pedestrian_crossings.values()] == [("zebra", road.id)]
    assert window.scene.selection == ("crossing", "X1") and len(window.history.undo_stack) == steps + 1
    window.overbridge_button.setChecked(True)  # one tool at a time
    assert not window.zebra_button.isChecked()
    window.canvas_clicked("road", road.id, ax + (bx - ax) * 0.8, ay + (by - ay) * 0.8)
    assert window.network.pedestrian_crossings["X2"].kind == "overbridge"
    window.overbridge_button.setChecked(False)
    window.canvas_clicked("crossing", "X1")
    assert "Zebra X1" in window.statusBar().currentMessage()
    window.delete_selection()
    assert list(window.network.pedestrian_crossings) == ["X2"]
    window.undo()
    assert sorted(window.network.pedestrian_crossings) == ["X1", "X2"]
    window.close()


def test_spawn_points_are_placed_on_the_map_then_their_grid_shows():
    from traffic_simulator.main import MainWindow

    network = generate_network(3, 3)
    for road in network.all_roads():
        road.footpaths = ["left", "right"]
    window = MainWindow()
    window.install_network(network)
    window._commit()
    steps = len(window.history.undo_stack)
    window.tabs.setCurrentWidget(window.demand_tab)
    window.od_panel.demand.setCurrentIndex(window.od_panel.demand.findData("spawn"))  # none yet: placing at once
    assert window.od_panel.place_spawn_button.isChecked() and window.scene.spawn_placing
    assert window.workspace.currentWidget() is window.view and "S1" in window.mode_hint.text()
    a = window.network.node("1,1")
    for dx, dy in ((140, 60), (900, -800), (500, -300)):
        window.scene.spawn_clicked.emit(a.x + dx, a.y + dy)
    assert list(window.network.spawn_points) == ["S1", "S2", "S3"] and len(window.history.undo_stack) == steps + 3
    assert "nearest footpath is" in window.mode_hint.text()
    assert sum(1 for item in window.scene.items() if item.data(0) and tuple(item.data(0))[0] == "spawn") == 3
    window.view.right_clicked.emit()  # finishes: their grid
    assert not window.scene.spawn_placing and window.workspace.currentWidget() is window.spawn_matrix
    window.spawn_matrix.table.item(0, 1).setText("300")
    assert window.network.spawn_demand == {"S1->S2": 300.0} and len(window.history.undo_stack) == steps + 4
    window.show_grid(False)
    window.canvas_clicked("spawn", "S2")
    assert window.scene.selection == ("spawn", "S2") and window.tabs.currentWidget() is window.demand_tab
    window.delete_selection()  # it goes, and the walking to it
    assert "S2" not in window.network.spawn_points and not window.network.spawn_demand
    window.undo()
    assert "S2" in window.network.spawn_points and window.network.spawn_demand == {"S1->S2": 300.0}
    window.od_panel.place_spawn_button.setChecked(True)
    window.cancel()  # Esc finishes placing too
    assert not window.scene.spawn_placing
    window.close()


if __name__ == "__main__":
    for name, check in list(globals().items()):
        if name.startswith("test_"):
            check()
    print("ok")

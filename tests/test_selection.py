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


if __name__ == "__main__":
    for name, check in list(globals().items()):
        if name.startswith("test_"):
            check()
    print("ok")

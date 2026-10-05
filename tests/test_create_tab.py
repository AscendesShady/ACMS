"""The Create Network tab: its three sections (Roads, Generate Network, Own Network) opening and
closing, the driving side flipping the whole network at once, the Roads settings kept for new roads,
the map's corner controls, and the sidebar scrolling only when the open tab needs it.
Run with `python tests/test_create_tab.py` (or the test monitor)."""

import os
from copy import deepcopy

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QScrollArea  # noqa: E402

from traffic_simulator.network_generator import generate_network  # noqa: E402

APP = QApplication.instance() or QApplication([])


def window_with(network=None):
    from traffic_simulator.main import MainWindow

    window = MainWindow()
    window.install_network(network or generate_network(3, 4))
    window._commit()
    window.resize(1500, 950)
    window.show()
    QTest.qWaitForWindowExposed(window)
    return window


def settle():
    for _ in range(3):
        APP.processEvents()


def test_the_sections_open_and_close():
    window = window_with()
    sections = (window.generate_section, window.own_section)
    assert [section.body.isVisibleTo(window) for section in sections] == [True, False]
    # Generate Network: the road settings, the signals' timing, the horizontal and vertical roads, then Generate
    assert all(box.isVisibleTo(window) for box in (window.driving_side, window.signal_method_combo, window.horizontal_spin,
                                                   window.vertical_spin, window.generate_button))
    window.own_section.header.click()  # Own Network: the background
    assert window.background_panel.kind.isVisibleTo(window)
    window.generate_section.header.click()
    assert not window.driving_side.isVisibleTo(window) and not window.generate_section.header.isChecked()
    window.close()


def test_generate_network_times_its_signals_by_the_chosen_method():
    window = window_with()
    combo = window.signal_method_combo
    assert combo.currentData() == "webster"  # the default
    combo.setCurrentIndex(combo.findData("scoot"))
    assert window.network.default_signal_method == "scoot"  # kept on the network: new junctions get it
    assert window.network.add_node(0, 0).signal_method == "scoot"
    window.horizontal_spin.setValue(2)
    window.vertical_spin.setValue(3)
    window.generate()
    assert {node.signal_method for node in window.network.nodes.values()} == {"scoot"}
    window.undo()
    window.undo()
    assert combo.currentData() == "webster"  # the setting follows undo
    window.close()


def test_the_driving_side_flips_the_whole_network_at_once():
    from traffic_simulator.interchanges import build_trumpet

    network = generate_network(3, 4)
    build_trumpet(network, ("1,1", "3,1"), "2,2")  # on the bottom row, clear of junction 2,2's west approach
    window = window_with(network)
    scene = window.scene
    lamp = lambda: scene.signal_lamps[("2,2", "1,2-2,2")][0].sceneBoundingRect().center()  # arriving from the west
    road_y = window.network.nodes["2,2"].y
    assert lamp().y() < road_y  # driving on the left: eastbound traffic's signal head on the north kerb
    window.run_action.trigger()
    assert window.runner is not None
    steps = len(window.history.undo_stack)
    window.driving_side.group.button(1).click()  # Right-hand
    assert window.network.driving_side == "right" and lamp().y() > road_y  # now on the south kerb
    assert window.runner is None  # the run drove the network as it was
    assert len(window.history.undo_stack) == steps + 1
    assert "Interchange TR1 mirrored for right-hand traffic" in window.statusBar().currentMessage()
    window.undo()  # one step back, and the choice with it
    assert window.network.driving_side == "left" and window.driving_side.value() == "left" and lamp().y() < road_y
    window.redo()
    assert window.network.driving_side == "right" and window.driving_side.value() == "right"
    window.close()


def test_the_roads_settings_are_kept_for_new_roads():
    window = window_with()
    steps = len(window.history.undo_stack)
    window.lanes_spin.setValue(3)
    window.lane_changing_check.setChecked(False)
    window.speed_limit_combo.setCurrentIndex(window.speed_limit_combo.findData(30))
    network = window.network
    assert (network.default_lanes_per_direction, network.default_lane_changing, network.default_speed_limit_kmh) == \
        (3, False, 30.0)
    assert len(window.history.undo_stack) == steps + 3  # a step each
    assert all(road.lanes_per_direction == 2 for road in network.all_roads())  # the roads there are untouched
    trial = deepcopy(network)
    road = trial.add_road(trial.add_node(2250.0, 250.0).id, "4,1")  # a road added now gets them
    assert (road.lanes_per_direction, road.lane_changing, road.speed_limit_kmh) == (3, False, 30.0)
    for _ in range(3):
        window.undo()
    network = window.network
    assert (network.default_lanes_per_direction, network.default_lane_changing, network.default_speed_limit_kmh) == \
        (2, True, 50.0) and window.lanes_spin.value() == 2
    assert len(window.history.undo_stack) == steps  # restoring the fields made no steps of its own
    window.close()


def test_the_map_controls_sit_in_its_corner():
    window = window_with()
    view = window.view
    corner = window.fit_button.parentWidget()
    assert corner.parentWidget() is view and corner.isVisible()
    area = view.viewport().geometry()
    assert area.right() - 20 < corner.geometry().right() <= area.right() and corner.geometry().top() < area.top() + 20
    view.resetTransform()
    window.fit_button.click()
    assert view.transform().m11() != 1.0  # fitted
    window.reset_button.click()
    assert view.transform().m11() == 1.0
    assert "Drag over empty map" in window.map_controls and window.help_button.toolTip() == "Map Controls"
    window.close()


def test_no_tab_makes_the_sidebar_scroll_in_the_default_window():
    """Qt sizes tabs for their tallest page, hidden ones too (in its height for a width as well): the
    sidebar would scroll, and a short page stretch past the window. Every page fits 1280 x 800."""
    window = window_with()
    window.resize(1280, 800)
    scroll = window.findChild(QScrollArea)
    for index in range(window.tabs.count()):
        window.tabs.setCurrentIndex(index)
        pages = range(window.ai_mode.systems.count()) if window.tabs.currentWidget() is window.ai_mode else [None]
        for page in pages:
            if page is not None:
                window.ai_mode.systems.setCurrentIndex(page)
            settle()
            assert not scroll.verticalScrollBar().isVisible(), (window.tab_names[index], page)
            assert scroll.widget().height() <= scroll.viewport().height() + 1
    window.close()


def test_the_od_pair_editor_shows_only_while_a_cell_is_picked():
    from PySide6.QtCore import Qt

    window = window_with()
    panel, table = window.od_panel, window.od_matrix.table

    def click(row, column):
        QTest.mouseClick(table.viewport(), Qt.MouseButton.LeftButton, pos=table.visualRect(table.model().index(row, column)).center())
        settle()

    def editor():
        return panel.pair_group.isVisible(), panel.empty_hint.isVisible()

    window.tabs.setCurrentWidget(window.demand_tab)
    settle()
    assert editor() == (False, True)  # nothing picked: one line saying how
    click(0, 1)
    assert editor() == (True, False) and window.od_matrix.current_pair() is not None
    QTest.keyClick(window, Qt.Key.Key_Escape)
    settle()
    assert editor() == (False, True) and not window.od_matrix.selected_pairs()
    click(0, 1)
    click(1, 1)  # the diagonal: no pair there
    assert editor() == (False, True)
    click(0, 2)
    window.tabs.setCurrentWidget(window.edit_tab)
    window.tabs.setCurrentWidget(window.demand_tab)
    settle()
    assert editor() == (False, True)  # leaving the tab put it away
    window.close()


def test_the_input_grid_toggle_swaps_the_network_and_the_grid():
    window = window_with()
    on_map, on_grid = window.grid_toggles
    assert on_map.parentWidget() is window.fit_button.parentWidget()  # first in the map's corner...
    for tab in (window.create_tab, window.edit_tab, window.summary):
        window.tabs.setCurrentWidget(tab)
        assert not on_map.isVisible()  # ...but only while Demand Generator is open
    window.tabs.setCurrentWidget(window.demand_tab)
    assert on_grid.isVisible() and window.workspace.currentWidget() is window.od_matrix
    on_grid.click()  # the network...
    assert on_map.isVisible() and window.workspace.currentWidget() is window.view and not on_map.isChecked()
    on_map.click()  # ...and the grid again, its rows the network's ends
    assert window.workspace.currentWidget() is window.od_matrix and on_grid.isVisible() and on_grid.isChecked()
    assert window.od_matrix.origins == window._od_ends()[0]
    window.network.delete_node(window.od_matrix.origins[0])  # an end goes while the grid shows...
    window._commit()
    assert window.od_matrix.origins == window._od_ends()[0]  # ...and its row with it
    on_grid.click()  # the network while Demand Generator stays open
    assert window.workspace.currentWidget() is window.view and window.tabs.currentWidget() is window.demand_tab
    window.tabs.setCurrentWidget(window.edit_tab)  # another tab: the network, no toggle
    assert window.workspace.currentWidget() is window.view and not on_map.isVisible()
    window.tabs.setCurrentWidget(window.demand_tab)  # back: it opens on the grid again
    assert window.workspace.currentWidget() is window.od_matrix
    window.close()


def test_the_sidebar_scrolls_only_when_the_open_tab_needs_it():
    window = window_with()
    scroll = window.findChildren(QScrollArea)[0]
    settle()
    assert not scroll.verticalScrollBar().isVisible()
    window.resize(1500, 500)
    window.own_section.header.click()
    settle()
    assert scroll.verticalScrollBar().isVisible()  # both open: taller than the window
    for section in (window.generate_section, window.own_section):
        section.header.click()
    settle()
    assert not scroll.verticalScrollBar().isVisible()  # all closed: it fits, whatever the other tabs need
    window.close()


if __name__ == "__main__":
    for name, check in list(globals().items()):
        if name.startswith("test_"):
            check()
    print("ok")

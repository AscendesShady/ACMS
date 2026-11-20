"""MainWindow's Create Network tab: generate a grid, the road defaults and driving side, putting a network into the
window, and the background (image or map) glue: work zone tool, bounds, zoom range, scroll extent and pointer
position. Cut out of main.py's MainWindow as a plain mixin; __init__ and _build_ui wire scene.extent, view.zoom_range
and the background panel to these through self, and it calls the rest of the window through self (_commit,
tab_changed, _end_modes_except, _metro_ghost)."""

from __future__ import annotations

import math

from PySide6.QtCore import QRectF, Qt, QTimer
from PySide6.QtWidgets import QMessageBox

from traffic_simulator import network_tools
from traffic_simulator.background import map_frame
from traffic_simulator.models import Network
from traffic_simulator.network_generator import generate_network
from traffic_simulator.properties_panel import TIMING_METHODS


class CreateMixin:
    """The Create Network tab and the background glue on the window."""

    # What it uses of the window and doesn't define: MainWindow's state and the other mixins' methods
    # (tests/test_docs.py keeps this exact, so a new dependency shows in the diff)
    NEEDS = ('_commit', '_end_modes_except', '_metro_ghost', '_network_summary', 'add_node_button',
             'background_painter', 'background_panel', 'driving_side', 'fit_button', 'generate_section',
             'grid_toggles', 'horizontal_spin', 'lane_changing_check', 'lanes_spin', 'network', 'node_chain',
             'pointer', 'properties', 'runner', 'scene', 'signal_method_combo', 'speed_limit_combo', 'statusBar',
             'tab_changed', 'tabs', 'vertical_spin', 'view')

    def install_network(self, network: Network, fit: bool = True) -> None:
        self.background_panel.end_tool()  # on the old network: an unconfirmed work zone never reaches the new one
        self._end_modes_except(None)
        self.network = network
        self.scene.set_network(network)
        self.properties.set_network(network)
        self.horizontal_spin.setValue(network.horizontal_corridors)
        self.vertical_spin.setValue(network.vertical_corridors)
        self.driving_side.set_value(network.driving_side)
        roads = (self.speed_limit_combo, self.lanes_spin, self.lane_changing_check, self.signal_method_combo)
        for box in roads:  # the Roads settings shown as the network has them, never written back (no edit, no step)
            box.blockSignals(True)
        speed_index = self.speed_limit_combo.findData(network.default_speed_limit_kmh)
        if speed_index < 0:
            value = network.default_speed_limit_kmh
            self.speed_limit_combo.addItem(f"{value:g} km/h", value)
            speed_index = self.speed_limit_combo.count() - 1
        self.speed_limit_combo.setCurrentIndex(speed_index)
        self.lanes_spin.setValue(network.default_lanes_per_direction)
        self.lane_changing_check.setChecked(network.default_lane_changing)
        if self.signal_method_combo.findData(network.default_signal_method) < 0:  # Manual, from a file or the assistant
            self.signal_method_combo.addItem(dict((method, label) for label, method in TIMING_METHODS).get(
                network.default_signal_method, network.default_signal_method), network.default_signal_method)
        self.signal_method_combo.setCurrentIndex(self.signal_method_combo.findData(network.default_signal_method))
        for box in roads:
            box.blockSignals(False)
        self.background_panel.show_background(network.background)
        self.statusBar().showMessage(self._network_summary())
        self.tab_changed()  # refreshes the OD matrix if it is open
        if fit:
            QTimer.singleShot(0, self.view.fit_network)

    def set_driving_side(self) -> None:
        """Left- or right-hand traffic for the whole network at once (`Network.set_driving_side`): lanes,
        signal heads, stop lines, bus bays, turns and roundabouts flip, and interchanges are mirrored.
        One undo step."""
        side = str(self.driving_side.value())
        if side == self.network.driving_side:
            return
        mirrored = self.network.set_driving_side(side)
        self.scene.rebuild()
        running = self.runner is not None
        self._commit()
        note = f"Driving on the {side}."
        if mirrored:
            note += f" Interchange{'s' if len(mirrored) > 1 else ''} {', '.join(mirrored)} mirrored for {side}-hand traffic."
        if running:
            note += " The run stopped; press Run to start again."
        self.statusBar().showMessage(f"{self._network_summary()} • {note}")

    def _set_default(self, name: str, value) -> None:
        """A Generate Network setting for new roads and signals (a generated grid, roads and junctions added):
        kept on the network, one undo step; the roads and junctions already there, and a run, are untouched."""
        if getattr(self.network, name) != value:
            setattr(self.network, name, value)
            self._commit(("road default", name), stop_run=False)

    def generate(self) -> None:
        try:
            network = generate_network(
                self.horizontal_spin.value(),
                self.vertical_spin.value(),
                driving_side=str(self.driving_side.value()),
                speed_limit_kmh=float(self.speed_limit_combo.currentData()),
                lanes_per_direction=self.lanes_spin.value(),
                lane_changing=self.lane_changing_check.isChecked(),
                signal_method=str(self.signal_method_combo.currentData()),
            )
        except ValueError as exc:
            QMessageBox.warning(self, "Cannot generate network", str(exc))
            return
        network_tools.keep_background(self.network, network)  # the map's place and work zone under its centre
        self.install_network(network)
        self._commit()

    def _junction_bounds(self, network: Network | None = None) -> QRectF:
        """Where the network's (or another's) junctions and ends lie, metres."""
        nodes = list((network or self.network).all_nodes())
        if not nodes:
            return QRectF(0, 0, 1000, 1000)
        xs, ys = [node.x for node in nodes], [node.y for node in nodes]
        return QRectF(min(xs), min(ys), max(max(xs) - min(xs), 1.0), max(max(ys) - min(ys), 1.0))

    def _show_background(self) -> None:
        self.background_panel.show_background(self.network.background)
        self.scene.rebuild()  # what the view may scroll over follows the image or map
        self.view.clamp_zoom()  # a work zone just chosen or undone: never zoomed out past it
        self.view.redraw()
        self.pointer.setVisible(self.network.background.kind == "map")

    def _background_tool(self, tool: object) -> None:
        """A background tool started or ended: the map's cursor says which; the Edit tab's tools end.
        The map's work zone and Set Scale (a new image) come first: the network is hidden and the tabs and
        the other Create sections wait until they end. While the work zone is chosen the view is seen from
        above, its corner controls give way to the zone's bar, and the scene is pinned to the network's
        centre (BackgroundPainter.extent), under the box; after it, the view fits the zone."""
        if tool is not None:
            self._end_modes_except(None)
        setup, zone = tool in ("zone", "scale"), tool == "zone"
        corner = self.fit_button.parentWidget()
        was_zone = corner.isHidden()
        if zone:
            self.view.set_isometric(False)  # the box frames the ground
        if self.scene.hidden == setup:
            self.scene.rebuild()  # already hidden (Set Scale, then the map): what it scrolls over still follows the tool
        else:
            self.scene.set_hidden(setup)
        self.tabs.tabBar().setEnabled(not setup)
        for section in (self.generate_section, self.grid_toggles[0]):
            section.setEnabled(not setup)
        corner.setVisible(not zone)
        if was_zone and not zone:  # the work zone chosen (or back to what it was): it, or the network, in view
            background = self.network.background
            if background.kind == "map" and background.zone:
                QTimer.singleShot(0, lambda: self.view.fit_rect(QRectF(*background.zone)))
            else:
                QTimer.singleShot(0, self.view.fit_network)
        self.pointer.setVisible(self.network.background.kind == "map")
        self.view.set_tool_cursor(Qt.CursorShape.SizeAllCursor if tool == "move" else
                                  Qt.CursorShape.CrossCursor if tool in ("align", "measure", "scale")
                                  else Qt.CursorShape.ArrowCursor)

    def _extent(self, rect: QRectF) -> QRectF:
        """What the view may scroll over (BackgroundPainter.extent). Over a colour or an image, what it shows now as
        well, so a redraw never moves it: a small network's area, smaller than the view, was centred in it, and the
        first node placed on an empty map jumped to the middle (a map keeps its work zone's limits)."""
        background = self.network.background
        area = self.background_painter.extent(background, rect, self._junction_bounds(), self.scene.network_rect)
        if background.kind != "map" and self.background_painter.framing is None:
            shown = self.view.mapToScene(self.view.viewport().rect()).boundingRect()
            # half a view round it: clear of the scroll range's ends, where rounding nudged the view a pixel a redraw
            area = area.united(shown.adjusted(-shown.width() / 2, -shown.height() / 2, shown.width() / 2,
                                              shown.height() / 2))
        return area

    def _zoom_range(self) -> tuple[float, float] | None:
        """How far the wheel zooms (NetworkView.zoom_range): while the work zone is chosen, as its bar allows;
        over a map with one, never out past the zone (with any junction beyond it) filling the view."""
        if self.background_panel.tool == "zone":
            return self.background_panel.zoom_range()
        background = self.network.background
        if background.kind == "map" and background.zone:
            return min(self.view.fit_scale(self.scene.sceneRect()), 100.0), 100.0
        return 0.0005, 100.0

    def _show_pointer(self, x: float, y: float) -> None:
        """The mouse's latitude and longitude, while the map shows; Measure's line follows it, as does a metro
        deployment's next leg."""
        self.background_panel.hover(x, y)
        if self.scene.metro_deploying and self.scene._metro_press is None:  # (an aim drag draws its own)
            self._metro_ghost(x, y)
        if self.add_node_button.isChecked() and self.node_chain and self.node_chain[-1] in self.network.nodes:
            last = self.network.nodes[self.node_chain[-1]]  # placing nodes: the distance from the last to the next
            self.background_painter.guide = (last.x, last.y, x, y, f"{math.hypot(x - last.x, y - last.y):.1f} m")
            self.view.redraw()
        background = self.network.background
        if background.kind == "map":
            lat, lon = map_frame(background).to_wgs84(x, y)
            self.pointer.setText(f"{lat:.6f}°, {lon:.6f}°")

    def _background_changed(self, key: object) -> None:
        """The background changed (the Create tab's section): redrawn, one undo step; a run carries
        on, as it drives the roads, not their backdrop."""
        self._show_background()
        self._commit(key, stop_run=False)

"""MainWindow's map editing (the Edit Network tab): tool modes with their hint and cursor, selection (click,
Ctrl+click, box), Add Node, Add Edge with its crossing dialog, interchanges and slip roads, zebra and overbridge
crossings, drag, delete, Esc, and the bus stop and signal edit follow-ups. Cut out of main.py's MainWindow as a plain
mixin; the widgets it reads are made in LayoutMixin._build_ui, and it calls _commit, _refresh_grid, _refresh_summary
and _delete_institution through self."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QFormLayout, QMessageBox, QPushButton, QSpinBox, QWidget

from traffic_simulator.geometry import nearest_along
from traffic_simulator.interchanges import (
    build_diamond,
    build_slip_road,
    build_trumpet,
    guess_stem,
    pair_legs,
    roads_left_at_hub,
)
from traffic_simulator.models import Network
from traffic_simulator.network_tools import build_edge, delete_objects, edge_crossings
from traffic_simulator.properties_panel import describe_group
from traffic_simulator.widgets import ChoiceButtons, hint_label

def ask_crossing(parent: QWidget, crossings: list) -> str | None:
    """Ask how a new road meets the roads it crosses: "roundabout", "overpass" or None (cancel)."""
    roads = sorted({road_id for *_, road_id in crossings})
    count = len(crossings)
    box = QMessageBox(parent)
    box.setWindowTitle("New road crosses existing roads")
    box.setIcon(QMessageBox.Icon.Question)
    box.setText(f"The new road crosses {count} road{'s' if count > 1 else ''}: {', '.join(roads)}. "
                "How should they meet?")
    box.setInformativeText("Roundabout: a roundabout junction at each crossing, sized from its "
                           "roads; the crossed roads are split there.\n"
                           "Overpass: the new road bridges over them, with no connection.")
    roundabout = box.addButton(f"Merge into Roundabout{'s' if count > 1 else ''}",
                               QMessageBox.ButtonRole.AcceptRole)
    roundabout.setProperty("role", "primary")
    overpass = box.addButton("Overpass", QMessageBox.ButtonRole.AcceptRole)
    box.addButton(QMessageBox.StandardButton.Cancel)
    box.setDefaultButton(roundabout)
    box.exec()
    clicked = box.clickedButton()
    return "roundabout" if clicked is roundabout else "overpass" if clicked is overpass else None


INTERCHANGE_NAMES = {"trumpet": "Trumpet Interchange", "diamond": "Diamond Interchange",
                     "spui": "Single-Point Urban Interchange"}


def ask_interchange(parent: QWidget, kind: str, network: Network, nodes: list[str]) -> dict | None:
    """Dialog for the legs, existing roads and freeway lanes; None when cancelled."""
    dialog = QDialog(parent)
    dialog.setWindowTitle(f"Prepare {INTERCHANGE_NAMES[kind]}")
    form = QFormLayout(dialog)
    if kind == "trumpet":
        legs = ChoiceButtons([(node, node) for node in nodes])
        legs.set_value(guess_stem(network, nodes))
        form.addRow("Terminating road from", legs)
        form.addRow(hint_label("The freeway runs through the other two junctions."))
    else:
        first, second = pair_legs(network, nodes)
        legs = ChoiceButtons([(f"{first[0]} ↔ {first[1]}", (first, second)),
                              (f"{second[0]} ↔ {second[1]}", (second, first))], vertical=True)
        form.addRow("Freeway runs", legs)
        form.addRow(hint_label("The other pair becomes the crossroad, passing over the freeway."))
    existing = ChoiceButtons([("Replace", True), ("Keep", False)])
    existing.setToolTip("Remove or keep old roads")
    form.addRow("Existing roads", existing)
    hub, others = roads_left_at_hub(network, nodes)
    if others:
        form.addRow(hint_label(f"With Replace, {hub} stays for its road(s) to {', '.join(others)}, "
                               "which aren't selected. Delete them afterwards if not needed."))
    lanes = QSpinBox()
    lanes.setRange(1, 6)
    lanes.setValue(max(2, network.default_lanes_per_direction))
    form.addRow("Freeway lanes / direction", lanes)
    form.addRow(hint_label("Ramps are one-way. Roads that cross without a shared node are "
                           "grade-separated: one passes over the other."))
    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
    build_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
    build_button.setText("Build Interchange")
    build_button.setProperty("role", "primary")
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    form.addRow(buttons)
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return None
    return {"legs": legs.value(), "replace": existing.value(), "freeway_lanes": lanes.value()}


class EditMixin:
    """Editing the network on the map."""

    def _network_summary(self) -> str:
        ends = self.network.approach_nodes.values()
        supply = sum(end.supply_vph for end in ends)
        demand = sum(end.demand_vph for end in ends)
        return (
            f"{len(self.network.nodes)} intersections • {len(self.network.roads)} connecting roads"
            f" • {len(self.network.approach_roads)} boundary ends"
            f" • supply {supply:g} / demand {demand:g} veh/h"
        )

    def _show_hint(self, text: str | None) -> None:
        """Highlighted next-step banner while a tool is active; hidden otherwise."""
        self.mode_hint.setText(text or "")
        self.mode_hint.setVisible(bool(text))

    def _refresh_mode(self) -> None:
        """Cursor and hint banner follow whichever canvas tool is active."""
        cross = Qt.CursorShape.CrossCursor
        if self.add_node_button.isChecked():
            cursor = cross
            hint = ("Click empty space to place a node, then keep clicking for more; a dashed line gives the distance "
                    "from the last. Right-click keeps them; Esc asks to keep or discard."
                    if not self.node_chain else
                    f"{len(self.node_chain)} placed, {self.node_chain[-1]} last: click for the next. Right-click keeps "
                    "them; Esc asks to keep or discard; Ctrl+Z takes the last back.")
        elif self.add_edge_button.isChecked():
            cursor = cross
            if self.edge_start is None:
                hint = "Click the junction the new road starts from. Esc cancels."
            elif self.edge_kind.value() == "end":
                hint = f"From {self.edge_start}: click empty space where the boundary end goes."
            else:
                hint = f"From {self.edge_start}: click the junction to connect to."
        elif self.zebra_button.isChecked() or self.overbridge_button.isChecked():
            cursor = cross
            what = "zebra crossing" if self.zebra_button.isChecked() else "overbridge"
            hint = f"Click a road with footpaths on both sides for a {what} there. Esc finishes."
        elif self.metro_button.isChecked():
            cursor = cross
            if self.metro_line in self.network.metro_lines:
                stops = self.network.metro_lines[self.metro_line].stops
                hint = (f"{self.metro_line}: {len(stops)} stops. Click the map for the next stop, joined to "
                        f"{stops[-1]}. Right-click finishes.")
            elif self.scene.metro_pending is not None:
                hint = "First stop placed: click the next stop. Right-click finishes."
            else:
                hint = "Click the map for the line's first stop. Right-click finishes."
        elif self.properties.drag_button.isChecked():
            cursor = Qt.CursorShape.SizeAllCursor
            _, identifier = self.scene.selection or ("", "")
            hint = f"Drag {identifier} on the map; only its connecting roads stretch. Esc finishes."
        else:
            cursor, hint = Qt.CursorShape.ArrowCursor, None
        self.view.set_tool_cursor(cursor)
        self.scene.box_select = hint is None  # with no tool on, a drag over empty map draws a selection box
        self.edge_type_row.setVisible(self.add_edge_button.isChecked())
        self._show_hint(hint)

    def select(self, kind: str, identifier: str) -> None:
        try:
            selected = self.network.road(identifier) if kind == "road" else                 self.network.bus_stops[identifier] if kind == "bus_stop" else                 self.network.institutions[identifier] if kind == "institution" else                 self.network.walkways[identifier] if kind == "walkway" else                 self.network.pedestrian_crossings[identifier] if kind == "crossing" else                 self.network.spawn_points[identifier] if kind == "spawn" else self.network.node(identifier)
        except KeyError:
            kind, selected = "", None
        self.multi_nodes, self.group = [], []
        if kind and self.activity.on:
            self.activity.log("select", kind.replace("_", " "), identifier)
        self.scene.set_selection(kind, identifier)
        building = kind == "institution"  # shown in Demand Generator → Institutions, not the Edit tab
        if kind == "walkway":  # a path has nothing to edit: it is redrawn, or deleted
            self.properties.show_object("", None)
            self.statusBar().showMessage(f"Path {identifier} ({selected.start} to {selected.end}) selected: Delete "
                                         "removes it.")
        elif kind == "spawn":  # placed again, or deleted
            self.properties.show_object("", None)
            self.statusBar().showMessage(f"Deploy point {identifier} selected: Delete removes it and the walking to and "
                                         "from it.")
        elif kind == "crossing":  # as a path: placed again, or deleted
            self.properties.show_object("", None)
            self.statusBar().showMessage(f"{selected.kind.capitalize()} {identifier} on {selected.road}, "
                                         f"{selected.distance_m:.0f} m along it, selected: Delete removes it.")
        else:
            self.properties.show_object("" if building else kind, None if building else selected)
        self.institution_panel.show_selected(self.network, selected if building else None)

    def _select_nodes(self, group: list[str]) -> None:
        """Select several junctions at once; one or none falls back to a normal selection."""
        if len(group) < 2:
            self.select("node", group[0]) if group else self.select("", "")
            return
        self.multi_nodes, self.group = group, []
        self.scene.set_multi_selection(group)
        self.properties.show_multi(group)
        self.tabs.setCurrentWidget(self.edit_tab)

    def _toggle_in_group(self, node_id: str) -> None:
        group = list(self.multi_nodes)
        if not group and self.scene.selection and self.scene.selection[0] == "node":
            group = [self.scene.selection[1]]  # Ctrl+click extends a single junction selection
        if node_id in group:
            group.remove(node_id)
        else:
            group.append(node_id)
        self._select_nodes(group)

    def _toggle_road(self, road_id: str) -> None:
        """Ctrl+click a road: add it to the selection, or take it out (two meeting roads offer Add Slip Road)."""
        group = list(self.group)
        if not group and self.scene.selection and self.scene.selection[0] == "road":
            group = [self.scene.selection]  # Ctrl+click extends a single road selection
        item = ("road", road_id)
        group = [other for other in group if other != item] if item in group else group + [item]
        if len(group) == 1:
            self.select(*group[0])
            return
        self.multi_nodes, self.group = [], group
        self.scene.set_group(group)
        self.properties.show_group(group)
        self.tabs.setCurrentWidget(self.edit_tab)

    def add_slip_road(self, first: str, second: str, single_lane: bool = False) -> None:
        try:
            slip = build_slip_road(self.network, first, second, single_lane)
        except ValueError as exc:
            QMessageBox.warning(self, "Cannot add slip road", str(exc))
            return
        self.select("road", slip)
        self.statusBar().showMessage(self._network_summary() + f" • Added slip road {slip}")
        self._commit()

    def build_interchange(self, kind: str) -> None:
        nodes = list(self.multi_nodes)
        choice = ask_interchange(self, kind, self.network, nodes)
        if choice is None:
            return
        try:
            if kind == "trumpet":
                stem = choice["legs"]
                freeway = tuple(node for node in nodes if node != stem)
                name = build_trumpet(self.network, freeway, stem, choice["replace"], choice["freeway_lanes"])
            else:
                freeway, crossroad = choice["legs"]
                name = build_diamond(self.network, freeway, crossroad, choice["replace"],
                                     choice["freeway_lanes"], single_point=kind == "spui")
        except ValueError as exc:
            QMessageBox.warning(self, f"Cannot build the {INTERCHANGE_NAMES[kind].lower()}", str(exc))
            return
        self.select("", "")
        added = sum(node_id.startswith(name + ".") for node_id in self.network.nodes)
        self.statusBar().showMessage(
            self._network_summary() + f" • Built {INTERCHANGE_NAMES[kind].lower()} {name} ({added} nodes)"
        )
        self._commit()

    def canvas_clicked(self, kind: str, identifier: str, x: float = 0.0, y: float = 0.0,
                       additive: bool = False) -> None:
        if self.zebra_button.isChecked() or self.overbridge_button.isChecked():
            self._place_crossing(kind, identifier, x, y)
            return
        if self.add_node_button.isChecked():
            if kind == "node":
                self._show_hint(f"{identifier} is already there. Click empty space for the new node.")
                return
            node = self.network.add_node(x, y)  # one of a chain: kept (one undo step) or discarded when it ends
            self.node_chain.append(node.id)
            self.background_painter.guide = None  # until the mouse moves on towards the next
            self.select("node", node.id)
            self._refresh_mode()
            self.statusBar().showMessage(self._network_summary() + f" • Placed {node.id}")
            return
        if not self.add_edge_button.isChecked():
            if additive and kind == "node":
                self._toggle_in_group(identifier)
                return
            if additive and kind == "road":
                self._toggle_road(identifier)
                return
            self.select(kind, identifier)
            if kind == "institution":  # its circle of influence and loads, in Demand Generator → Institutions
                self.od_panel.demand.setCurrentIndex(self.od_panel.demand.findData("institutions"))
                self.tabs.setCurrentWidget(self.demand_tab)
            elif kind and kind != "spawn":  # (a spawn point: the Demand Generator's, wherever it is clicked)
                self.tabs.setCurrentWidget(self.edit_tab)  # its editor shows the selection
            return
        to_end = self.edge_kind.value() == "end"
        if self.edge_start is None:
            if kind == "node":
                self.edge_start = identifier
                self.select(kind, identifier)
                self._refresh_mode()
            return
        if to_end and kind in ("node", "entry", "exit"):
            self._show_hint(f"From {self.edge_start}: click empty space for the end, not an existing object.")
            return
        if not to_end and kind != "node":
            return
        start = self.edge_start
        self.add_edge_button.setChecked(False)
        road, note = self._build_edge(start, None if to_end else identifier, x, y)
        if road is None:
            if note:
                QMessageBox.warning(self, "Cannot add edge", note)
            return
        self.select("road", road.id)
        self.statusBar().showMessage(self._network_summary() + f" • Added {road.id}{note}")
        self._commit()

    def _build_edge(self, start: str, target: str | None, x: float, y: float):
        """Add the planned road (to junction `target`, or a boundary end at x, y).

        If it would cross existing roads, the user chooses first: a roundabout at each
        crossing (both roads split there) or an overpass (see network_tools.build_edge).
        Returns (last road or None, note for the status bar / warning).
        """
        try:
            crossings = edge_crossings(self.network, start, target, x, y)
        except ValueError as exc:
            return None, str(exc)
        choice = ask_crossing(self, crossings) if crossings else None
        if crossings and choice is None:
            return None, ""
        try:
            road, junctions = build_edge(self.network, start, target, x, y, choice)
        except ValueError as exc:  # refused before any road changed (a roundabout too wide for its design)
            return None, str(exc)
        if junctions:
            return road, f" with roundabout{'s' if len(junctions) > 1 else ''} {', '.join(junctions)}"
        return road, " as an overpass" if road.overpass else ""

    def cancel(self) -> None:
        """Esc: leave the active tool, or clear the selection when no tool is active."""
        if self.background_panel.tool is not None:
            self.background_panel.end_tool()
            return
        if self.scene.placing is not None:
            self.institution_panel.end_placing()
            return
        if self.od_panel.place_spawn_button.isChecked():
            self.od_panel.place_spawn_button.setChecked(False)
            return
        if self.tabs.currentWidget() is self.demand_tab and self.od_matrix.selected_pairs():
            self.od_matrix.clear_selection()  # the OD Pair editor goes with it
            return
        if self.add_node_button.isChecked() and self.node_chain:  # nodes placed: keep them, or discard them?
            count = len(self.node_chain)
            answer = QMessageBox.question(
                self, "Placed nodes", f"Keep the {count} node{'s' if count > 1 else ''} placed "
                f"({', '.join(self.node_chain)})? Discard takes {'them all' if count > 1 else 'it'} away.",
                QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Save)
            if answer == QMessageBox.StandardButton.Cancel:
                return  # placing goes on
            self._finish_nodes(keep=answer == QMessageBox.StandardButton.Save)
            self.add_node_button.setChecked(False)
            return
        tools = (self.add_node_button, self.add_edge_button, self.properties.drag_button, self.metro_button)
        if any(button.isChecked() for button in tools):
            self._end_modes_except(None)
        else:
            self.select("", "")

    def box_selected(self, left: float, top: float, right: float, bottom: float, crossing: bool, additive: bool) -> None:
        """A selection box: a crossing box (dragged right to left) takes what it touches, a window
        what lies wholly inside; Ctrl adds to what was selected."""
        objects = self.network.objects_in_box(left, top, right, bottom, crossing)
        if additive:
            objects = self.group + [item for item in objects if item not in self.group]
        if not objects:
            self.select("", "")
            return
        self.multi_nodes, self.group = [], objects
        self.scene.set_group(objects)
        self.properties.show_group(objects)
        self.tabs.setCurrentWidget(self.edit_tab)

    def delete_group(self) -> None:
        """Delete the box's selection, after saying what goes (the roads meeting its junctions too),
        as one undo step."""
        if not self.group:
            return
        network, objects = self.network, list(self.group)
        chosen = [identifier for _, identifier in objects]
        junctions = {identifier for kind, identifier in objects if kind == "node"}
        also = [road.id for road in network.all_roads() if road.id not in chosen
                and {road.from_node, road.to_node} & junctions]
        what = describe_group(objects)
        message = f"Delete {what}?"
        if also:
            message += f" The {len(also)} other road{'s' if len(also) > 1 else ''} meeting those junctions go too."
        if QMessageBox.question(self, "Delete selection", message) != QMessageBox.StandardButton.Yes:
            return
        delete_objects(network, chosen)
        self.select("", "")
        self.statusBar().showMessage(f"{self._network_summary()} • Deleted {what}. Ctrl+Z brings it back.")
        self._commit()

    def delete_selection(self) -> None:
        if self.group:
            self.delete_group()
            return
        kind, identifier = self.scene.selection or ("", "")
        if kind == "road":
            self.properties.delete_road()
        elif kind == "bus_stop":
            self.properties.delete_bus_stop()
        elif kind == "institution":
            self._delete_institution(identifier)
        elif kind == "spawn":
            self.network.delete_spawn_point(identifier)
            self.select("", "")
            self.statusBar().showMessage(f"{self._network_summary()} • Deleted deploy point {identifier}. Ctrl+Z brings it "
                                         "back.")
            self._commit()
        elif kind == "crossing":
            del self.network.pedestrian_crossings[identifier]
            self.select("", "")
            self.statusBar().showMessage(f"{self._network_summary()} • Deleted crossing {identifier}. Ctrl+Z brings it "
                                         "back.")
            self._commit()
        elif kind == "walkway":
            del self.network.walkways[identifier]
            self.select("", "")
            self.statusBar().showMessage(f"{self._network_summary()} • Deleted path {identifier}. Ctrl+Z brings it back.")
            self._commit()
        elif kind:
            self.properties.delete_node()

    def _end_modes_except(self, keep: QPushButton | None) -> None:
        """Add Node, Add Edge, Drag to Adjust and metro deployment all capture canvas clicks: one at a time."""
        for button in (self.add_node_button, self.add_edge_button, self.properties.drag_button, self.metro_button,
                       self.zebra_button, self.overbridge_button):
            if button is not keep:
                button.setChecked(False)

    def tab_changed(self) -> None:
        if self.tabs.currentWidget() is not self.edit_tab:
            self._end_modes_except(None)  # also cancels a half-picked edge
        if self.tabs.currentWidget() is not self.create_tab:
            self.background_panel.end_tool()
        if self.tabs.currentWidget() is not self.demand_tab:
            self.institution_panel.end_placing()
            self.od_panel.place_spawn_button.setChecked(False)
            self.od_matrix.clear_selection()  # the OD Pair editor shows again only when a cell is clicked
        self.grid_toggles[0].setVisible(self.tabs.currentWidget() is self.demand_tab)  # the grid is the Demand tab's
        self.view.set_corner(self.fit_button.parentWidget())  # the map's corner controls, re-placed
        self._refresh_grid()
        self._refresh_summary()

    def toggle_add_node(self, checked: bool) -> None:
        """Add Node: each click on empty map places a node, selected, and a dashed line from it to the mouse gives the
        distance to the next. Ended (the button, a right-click, another tool or tab) the nodes are kept, one undo
        step; Esc asks first (`cancel`), and discarding takes them all away."""
        if checked:
            self._end_modes_except(self.add_node_button)
        else:
            self._finish_nodes(keep=True)
        self._refresh_mode()

    def _finish_nodes(self, keep: bool) -> None:
        chain, self.node_chain = self.node_chain, []
        self.background_painter.guide = None
        self.view.redraw()
        if not chain:
            return
        if keep:
            self.statusBar().showMessage(f"{self._network_summary()} • Added {len(chain)} node"
                                         f"{'s' if len(chain) > 1 else ''}: {', '.join(chain)}")
            self._commit()
            return
        for node_id in chain:
            self.network.delete_node(node_id)
        self.select("", "")
        self.statusBar().showMessage(f"{self._network_summary()} • Discarded {len(chain)} placed node"
                                     f"{'s' if len(chain) > 1 else ''}.")

    def _crossing_tool(self, button, checked: bool) -> None:
        """Add Zebra or Add Overbridge on (each road clicked gets one) or off."""
        if checked:
            self._end_modes_except(button)
        self._refresh_mode()

    def _place_crossing(self, kind: str, identifier: str, x: float, y: float) -> None:
        """A crossing where a road was clicked (Network.add_crossing: refused with its reason, changing nothing):
        one undo step, the tool still on for the next."""
        if kind != "road":
            self._show_hint("Click a road: a crossing goes across one, between its footpaths.")
            return
        road = self.network.road(identifier)
        at = nearest_along(self.network.road_points(road), (x, y))
        try:
            crossing = self.network.add_crossing(road.id, at, "zebra" if self.zebra_button.isChecked() else "overbridge")
        except ValueError as exc:
            self._show_hint(str(exc))
            return
        self.statusBar().showMessage(f"{self._network_summary()} • Added {crossing.kind} {crossing.id} on {road.id}")
        self._commit()
        self.select("crossing", crossing.id)

    def toggle_drag(self, checked: bool) -> None:
        self.scene.drag_nodes = []
        if checked:
            self._end_modes_except(self.properties.drag_button)
            kind, identifier = self.scene.selection or ("", "")
            if kind == "road":
                road = self.network.road(identifier)
                self.scene.drag_nodes = [road.from_node, road.to_node]
            elif kind:
                self.scene.drag_nodes = [identifier]
        self._refresh_mode()

    def toggle_add_edge(self, checked: bool) -> None:
        if checked:
            self._end_modes_except(self.add_edge_button)
        self.edge_start = None
        self._refresh_mode()

    def _bus_stop_changed(self, stop_id: str) -> None:
        """A bus stop was added, moved (panel or arrow keys) or re-pointed: show it selected and
        redrawn, as one undo step: adding it is a step of its own, and a burst of nudges or edits
        to it after that merges into one."""
        self.select("bus_stop", stop_id)
        stop = self.network.bus_stops[stop_id]
        self.statusBar().showMessage(f"{self._network_summary()} • Bus stop {stop_id} on {stop.road}")
        added = stop_id not in self.history.current[0].bus_stops
        self._commit(None if added else ("bus_stop", stop_id))

    def _signal_edited(self, key: object) -> None:
        """A junction's phases, timing method or manual windows changed (the properties panel): its heads drawn again,
        their phase letters and idle lamps as the arms now group, and an adaptive method's detectors."""
        if isinstance(key, tuple) and key[0] in ("phases", "method", "manual"):
            self.scene._redraw([("node", key[1])])

    def model_updated(self, note: str = "Model updated") -> None:
        if self.multi_nodes:
            self._select_nodes([node for node in self.multi_nodes if node in self.network.nodes])
        else:
            self.select(*(self.scene.selection or ("", "")))
        self.statusBar().showMessage(f"{self._network_summary()} • {note}")
        self._commit()

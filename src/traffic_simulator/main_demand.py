"""MainWindow's Demand Generator glue: which grid shows, deploy points, placing and editing institutions and their
mode split, demand edits reaching a live run, a run's pair demand and routes for drop-offs. Cut out of main.py's
MainWindow as a plain mixin; the panels it reads are made in LayoutMixin._build_ui, and it calls _commit, select,
_show_hint, _show_metro_summary and _end_modes_except through self."""

from __future__ import annotations

import math
from dataclasses import replace
from itertools import accumulate

from PySide6.QtCore import Qt

from traffic_simulator import network_tools
from traffic_simulator.demand import od_ends, pair_demand
from traffic_simulator.institutions import place, run_demand
from traffic_simulator.lane_graph import Graph
from traffic_simulator.metro import METRO
from traffic_simulator.models import institution_problem
from traffic_simulator.od_matrix import ModeSplitDialog
from traffic_simulator.simulation import PairDemand
from traffic_simulator.walking import footpath_join


class DemandMixin:
    """The Demand Generator's side of the window."""

    def _pair_demand(self) -> list[PairDemand]:
        """Every OD pair with demand of the kind the Demand Generator shows, for a run, and the institutions'
        trips (institutions.run_demand: catchment to door and back; its notes in `_institution_notes`)."""
        matrix = self.od_matrix
        matrix.set_ends(*self._od_ends())
        institutions, self._institution_notes = run_demand(self.network, self.vehicle_modes.modes, matrix.mode == "daily")
        return pair_demand(matrix.cells, matrix.origins, matrix.destinations, matrix.mode,
                           matrix.vehicle_modes.vehicles_per_hour()) + institutions

    def show_grid(self, on: bool) -> None:
        """The main area shows the input grid (the OD matrix; the metro's, with Demand on MRT) or the network; both
        toggles follow."""
        for toggle in self.grid_toggles:
            toggle.setChecked(on)
        kind = self.od_panel.demand.currentData() if on else ""
        if kind == "spawn" and self.od_panel.place_spawn_button.isChecked():
            kind = ""  # placing spawn points: the map
        own = {"metro": self.metro_matrix, "walk": self.walk_matrix,
               "spawn": self.spawn_matrix}.get(kind)  # the metro's, the walkers' or the spawn points' grid
        if own is not None:
            own.daily = self.od_matrix.mode == "daily"  # the Demand switch, as for every tab
            own.show_network(self.network)
        self.workspace.setCurrentWidget(own or (self.od_matrix if on else self.view))
        self._refresh_grid()

    def _refresh_grid(self) -> None:
        """While the grid shows, its rows and columns follow the network's boundary ends."""
        if self.workspace.currentWidget() is self.od_matrix:
            self.od_matrix.set_ends(*self._od_ends())

    def _od_ends(self) -> tuple[list[str], list[str]]:
        """Boundary ends traffic can enter from (origins) and leave by (destinations): demand.od_ends.
        A one-way boundary road only works one way round."""
        return od_ends(self.network)

    def _demand_chosen(self, kind: str) -> None:
        """A Demand tab (or the Hourly/Daily switch): Institutions shows the map to place buildings on; the others,
        their grid."""
        self._show_metro_summary()  # its riders follow the switch
        self._show_saved()  # and the switch is saved with the project
        if kind != "institutions":
            self.institution_panel.end_placing()
        elif self._demand_kind != "institutions":  # opened: the buildings being placed show (hidden by default); the
            self.scene.set_buildings_visible(True)  # Hourly/Daily switch re-sends it and leaves Show Buildings alone
        self._demand_kind = kind
        if self.tabs.currentWidget() is self.demand_tab:
            self.show_grid(kind != "institutions" and not self.od_panel.place_spawn_button.isChecked())

    def _placing_spawn_points(self, on: bool) -> None:
        """Place Deploy Points: on, every left click on the map is a spawn point (the map shows); off (a right-click,
        Esc, another tab or demand), their grid shows."""
        self.scene.spawn_placing = on
        self.view.set_tool_cursor(Qt.CursorShape.CrossCursor if on else Qt.CursorShape.ArrowCursor)
        if self.tabs.currentWidget() is self.demand_tab and self.od_panel.demand.currentData() == "spawn":
            self.show_grid(not on)
        self._show_hint(f"Click the map for deploy point S{len(self.network.spawn_points) + 1}; right-click finishes."
                        if on else None)

    def spawn_clicked(self, x: float, y: float) -> None:
        """A spawn point where the map was clicked (one undo step; placing goes on). The hint says how far its nearest
        footpath is, where its walkers will join the network."""
        try:
            point = self.network.add_spawn_point(x, y)
        except ValueError as exc:
            self._show_hint(str(exc))
            return
        self.statusBar().showMessage(f"{self._network_summary()} • Added deploy point {point.id}")
        self._commit()
        self.scene.rebuild()  # it shows at once, joined to its footpath
        joins = footpath_join(self.network, x, y)
        far = f" Its nearest footpath is {math.hypot(joins[0] - x, joins[1] - y):.0f} m away." if joins else \
            " No footpath yet: give roads footpaths (Edit Network) so its walkers have somewhere to walk."
        self._show_hint(f"{point.id} placed.{far} Click for the next, right-click to finish.")

    def _cell_clicked(self, *_) -> None:
        """A grid cell clicked from another tab (the Input Grid toggle): its editor is the Demand
        Generator's, so that tab opens, on the grid's hourly or daily demand."""
        if self.od_panel.demand.currentData() != "vehicles":
            self.od_panel.demand.setCurrentIndex(self.od_panel.demand.findData("vehicles"))
        if self.tabs.currentWidget() is not self.demand_tab:
            selection = [(index.row(), index.column()) for index in self.od_matrix.table.selectedIndexes()]
            self.tabs.setCurrentWidget(self.demand_tab)  # leaving the other tab cleared the selection: put it back
            for row, column in selection:
                self.od_matrix.table.setCurrentCell(row, column)

    def _placing(self, area_m2: object) -> None:
        """A building picked (its footprint, m²) or dropped (None): the map's placing follows."""
        if area_m2 is not None and self.scene.placing is None:
            self._end_modes_except(None)
            self.background_panel.end_tool()
            self.show_grid(False)
        self.scene.set_placing(area_m2)

    def _place_institution(self) -> None:
        """The placing's second click: the building goes where it was set down, one undo step; a run
        carries on (it doesn't use them yet)."""
        placing, panel = self.scene.placing, self.institution_panel
        try:
            item = place(self.network, panel.kind, placing.x, placing.y, placing.area_m2, placing.radius_m,
                         **panel.figures())
        except ValueError as exc:
            self.statusBar().showMessage(str(exc), 8000)
            return
        panel.end_placing()
        self.select("institution", item.id)
        self.statusBar().showMessage(f"{self._network_summary()} • Placed {item.id} {item.kind}. Click it to see "
                                     "its circle of influence.")
        self._commit(stop_run=False)

    def _set_institution_height(self, identifier: str, height: float) -> None:
        """An appearance edit: validate first, save/undo normally and keep a live run going."""
        item = self.network.institutions.get(identifier)
        if item is None:
            return
        candidate = replace(item, height_m=height)
        if problem := institution_problem(candidate):
            self.statusBar().showMessage(problem, 8000)
            return
        if item.height_m == height:
            return
        item.height_m = height
        self.scene.rebuild()
        self.institution_panel.show_selected(self.network, item)
        self._commit(("institution_height", identifier), stop_run=False)

    def _set_institution_demand(self, identifier: str, field: str, value: dict, part: str | None) -> None:
        """Its people an hour or its day's curves (Demand Generator → Institutions): validated first; a burst of edits
        to one is one undo step."""
        item = self.network.institutions.get(identifier)
        if item is None:
            return
        if problem := institution_problem(replace(item, **{field: value})):
            self.statusBar().showMessage(problem, 8000)
            return
        setattr(item, field, value)
        if field == "per_hour" or not value:  # typed, or back to its shift: its figures again (a curve shows its own)
            self.institution_panel.show_selected(self.network, item)
        self._commit(("institution_demand", identifier, field, part) if part else None)

    def _institution_split(self, args: dict, check_only: bool, who: str = "the assistant") -> dict:
        """institution_mode_split (and the Mode split window's Apply): each institution's logit split
        (network_tools.institution_split), all checked before any is set; one undo step."""
        wanted = network_tools._ids(args.get("institutions") or []) or list(self.network.institutions)
        unknown = [name for name in wanted if name not in self.network.institutions]
        if unknown or not wanted:
            raise ValueError(f"No institution called {', '.join(unknown)}." if unknown else
                             "There are no institutions: place one first (place_institution).")
        modes = self.vehicle_modes.modes
        found = {name: network_tools.institution_split(self.network, self.network.institutions[name], modes, args)
                 for name in wanted}
        if check_only:
            return {"ok": True, "preview": "\n".join(
                f"{name}: " + ", ".join(f"{mode} {value:.1f} %" for mode, value in percent.items())
                for name, (percent, _) in found.items())}
        for name, (percent, _) in found.items():
            item = self.network.institutions[name]
            item.mode_shares, item.car_share = dict(percent), float(round(percent.get("Car", 0.0), 2))
        self._commit(stop_run=False)
        self.institution_panel.show_selected(self.network, self.network.institutions.get(
            getattr(self.institution_panel.selected, "id", ""), None))
        self.statusBar().showMessage(f"Mode split set by {who} for {', '.join(wanted)} (Ctrl+Z undoes it). "
                                     "A Run spawns their trips.", 10000)
        return {"institutions": [info for _, info in found.values()]}

    def _open_mode_split(self, identifier: str) -> None:
        """The selected building's Mode split window: the same statistics as institution_mode_split, by hand."""
        item = self.network.institutions.get(identifier)
        if item is None:
            return
        modes = self.vehicle_modes.modes
        alternatives = [{"mode": "Walk"}, *(network_tools.default_alternative(mode) for mode in modes),
                        *([{"mode": METRO}] if self.network.metro_lines else [])]  # its cost: added to the line's fare
        dialog = ModeSplitDialog(self, f"{item.id} {item.kind}",
                                 lambda args: network_tools.institution_split(self.network, item, modes, args),
                                 alternatives, network_tools.LOGIT_DEFAULTS, network_tools.WALK_KMH)

        def apply(args: dict) -> None:
            try:
                self._institution_split({**args, "institutions": [identifier]}, False, who="you")
            except ValueError as exc:
                dialog.result.setText(str(exc))

        dialog.applied.connect(apply)
        dialog.show()
        self._mode_split_dialog = dialog  # kept while open

    def _delete_institution(self, identifier: str) -> None:
        self.network.institutions.pop(identifier, None)
        self.select("", "")
        self.statusBar().showMessage(f"{self._network_summary()} • Deleted {identifier}. Ctrl+Z brings it back.")
        self._commit(stop_run=False)

    def _demand_changed(self, merge_key: object = None) -> None:
        """The OD matrix changed, by the user or the assistant: a run carries on with the new demand
        from its next minute (Simulation.set_demand) rather than stopping."""
        self._hand_demand_to_run()
        self._commit(merge_key, stop_run=False)

    def _hand_demand_to_run(self) -> None:
        if self.runner is not None:
            self.runner.simulation.set_demand(self._pair_demand())
            self.statusBar().showMessage("Demand changed: the run takes it from its next minute. Signal "
                                         "timings stay as worked out at Run.", 8000)

    def _route_along(self, origin: str, destination: str) -> tuple[float, list] | None:
        """The fastest route's metres from `origin` to `destination` and the junctions on it, each at its share of
        that length (ODCellPanel.router); None without a route."""
        # ponytail: a new Graph each call (a pair picked, an edit); cache it per network change if it lags
        route = Graph(self.network).route(origin, destination)
        if not route:
            return None
        ends = list(accumulate(link.length for link in route))
        seen, junctions = {origin, destination}, []
        for end, link in zip(ends[:-1], route):  # a two-phase signal's detour passes its junction twice: once
            if link.end not in seen:
                seen.add(link.end)
                junctions.append((end / ends[-1], link.end))
        return ends[-1], junctions

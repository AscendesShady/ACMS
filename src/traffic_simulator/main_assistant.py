"""MainWindow's AI Mode bridge: what the assistant sees and does in the window, the callbacks AIModePanel.attach is
given (selection, show on map, status, demand and vehicle-type tools, the network it edits, redraw after its edits).
Cut out of main.py's MainWindow as a plain mixin; _runs_command stays with the runs side (main_experiments.py). It
calls the rest of the window through self (install_network, _commit, _background_changed, model_updated, select)."""

from __future__ import annotations

import time
from dataclasses import asdict

from PySide6.QtCore import QRectF, QThread

from traffic_simulator import tool_args
from traffic_simulator.geometry import point_along
from traffic_simulator.models import Network


class AssistantMixin:
    """AI Mode's view of the window and its hooks into it."""

    # What it uses of the window and doesn't define: MainWindow's state and the other mixins' methods
    # (tests/test_docs.py keeps this exact, so a new dependency shows in the diff)
    NEEDS = ('_background_changed', '_commit', '_network_summary', '_od_ends', '_types_key', 'ai_mode', 'ai_status',
             'background_panel', 'group', 'install_network', 'model_updated', 'multi_nodes', 'network', 'od_matrix',
             'od_panel', 'runner', 'scene', 'select', 'statusBar', 'tabs', 'thread', 'vehicle_modes', 'view')

    # --- AI Mode's view of the window (see AIModePanel.attach) -------------------------

    def _selection_info(self) -> dict:
        if self.multi_nodes:
            return {"selected": "junctions", "ids": list(self.multi_nodes)}
        if self.group:
            return {"selected": "objects in a selection box", "ids": [identifier for _, identifier in self.group]}
        kind, identifier = self.scene.selection or ("", "")
        names = {"road": "road", "node": "junction", "entry": "boundary end (entry marker)",
                 "exit": "boundary end (exit marker)", "bus_stop": "bus stop", "institution": "institution"}
        return {"selected": names.get(kind, "nothing"), "ids": [identifier] if kind else []}

    def _show_on_map(self, object_id: str) -> dict:
        """Select a road, junction or end and centre the map on it."""
        network = self.network
        if object_id in network.roads or object_id in network.approach_roads:
            kind, points = "road", network.road_points(network.road(object_id))
            x, y, _, _ = point_along(points, network.road_geometry_length(network.road(object_id)) / 2)
        elif object_id in network.nodes or object_id in network.approach_nodes:
            kind = "node" if object_id in network.nodes else "entry"
            x, y = network.node(object_id).x, network.node(object_id).y
        else:
            return {"error": f"Nothing called {object_id!r} on the map."}
        self.select(kind, object_id)
        self.view.centerOn(x, y)
        return {"shown": object_id}

    def _show_ai_status(self, text: str, tooltip: str) -> None:
        self.ai_status.setText(text)
        self.ai_status.setToolTip(tooltip)
        self.ai_status.setVisible(bool(text))

    def _open_system_2(self) -> None:
        self.tabs.setCurrentWidget(self.ai_mode)
        self.ai_mode.show_system_2()

    def _od_command(self, tool: str, args: dict, check_only: bool = False) -> dict:
        """AI Mode's demand tools, worked through the Demand Generator's own controls: what
        they change shows there, and each approved change is one undo step."""
        self.od_matrix.set_ends(*self._od_ends())  # the grid may not have been opened yet
        return self.od_panel.command(tool, args, check_only)

    def _types_command(self, tool: str, args: dict, check_only: bool = False) -> dict:
        """AI Mode's vehicle-type tools, on Mode Behavior's types (the tab and the Demand Generator follow)."""
        modes = self.vehicle_modes
        if tool == "list_vehicle_types":
            from traffic_simulator.vehicle import VEHICLE_CLASSES  # noqa: PLC0415
            return {"types": [{**asdict(mode), "desired_max_kmh": None if mode.desired_max_kmh >= 1e4 else mode.desired_max_kmh}
                              for mode in modes.modes], "default_split": list(modes.default_split),
                    "kinds_create_vehicle_types_can_make": list(VEHICLE_CLASSES)}
        stopped = "A run going on stopped: it used the old figures." if self.runner is not None else None
        if tool == "update_vehicle_type":
            changes = {key: value for key, value in args.items() if key != "name" and value is not None}
            modes.update(args.get("name", ""), changes, check_only=True)  # ValueError, before anything changes
            if check_only:
                return {"ok": True}
            self._types_key = ("assistant types", time.monotonic())
            try:
                mode = modes.modes[modes.update(args["name"], changes)]
            finally:
                self._types_key = None
            self.statusBar().showMessage(f"The assistant changed the vehicle type {mode.name} (Ctrl+Z undoes it)", 8000)
            return {"updated": {**asdict(mode), "desired_max_kmh": None if mode.desired_max_kmh >= 1e4 else mode.desired_max_kmh},
                    **({"note": stopped} if stopped else {})}
        specs = args.get("types")
        if isinstance(specs, str):
            specs = tool_args._parsed_json(specs)
        modes.create(specs, check_only=True)  # ValueError, before anything changes
        if check_only:
            return {"ok": True}
        self._types_key = ("assistant types", time.monotonic())
        try:
            made = modes.create(specs)
        finally:
            self._types_key = None
        self.statusBar().showMessage(f"The assistant added vehicle types: {', '.join(modes.modes[i].name for i in made)} "
                                     "(Ctrl+Z undoes it)", 8000)
        return {"added": [{"name": modes.modes[i].name, "headway_min": modes.modes[i].headway_min,
                           "occupancy": modes.modes[i].occupancy, "length_m": modes.modes[i].length_m,
                           "max_speed_kmh": modes.modes[i].max_speed_kmh} for i in made],
                "note": "Each starts with 0 % of every OD pair's trips (a timetabled one has its headway's share); "
                        "set shares with set_od_demand's mode_split." + (" " + stopped if stopped else "")}

    def _assistant_network(self) -> Network:
        """The network as the assistant reads and edits it. A work zone being chosen when an answer arrives is
        cancelled first: the assistant never works on a half-made background, nor is its change put back by the
        setup's own undo of itself (only from the GUI thread, where the assistant's tools run)."""
        if self.background_panel.tool == "zone" and QThread.currentThread() is self.thread():
            self.background_panel.end_tool()
        return self.network

    def _ai_edited(self, tool: str) -> None:
        """The assistant changed the network: redraw, and make it one undo step."""
        if tool in ("generate_grid", "network_from_background"):  # a whole new network: the Create tab too, in view
            self.install_network(self.network)
            self._commit()
        elif tool == "place_map":  # as the user's Confirm: the map and its work zone shown, the view on the zone
            self._background_changed(("background", "zone"))
            self.view.fit_rect(QRectF(*self.network.background.zone))
        elif tool in ("deploy_institutions", "place_institution"):  # buildings only: a run carries on
            self.scene.rebuild()
            self.statusBar().showMessage(f"{self._network_summary()} • Institutions placed by the assistant "
                                         "(Ctrl+Z undoes it)")
            self._commit(stop_run=False)
        else:
            self.model_updated("Changed by the assistant (Ctrl+Z undoes it)")

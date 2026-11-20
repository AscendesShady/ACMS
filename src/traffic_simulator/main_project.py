"""MainWindow's state as a whole: snapshots and undo/redo, the commit that makes an undo step, and Open/Save of the
project file with its unsaved-changes marker (network_io reads and writes it). Cut out of main.py's MainWindow as a
plain mixin; its history, _saved and _building are made in MainWindow.__init__, and it calls the rest of the window
through self (_stop_run, _log_edits, install_network, select, _refresh_grid, _show_metro_summary)."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

from PySide6.QtWidgets import QFileDialog, QMessageBox

from traffic_simulator.history import History
from traffic_simulator.mode_behavior import VehicleModes
from traffic_simulator.network_io import (RUN_SEED, load_network, project_parts, project_record, save_network,
                                         saved_conversation, saved_project)

TITLE = "ACMS — Agentic Context-Aware Mobility System"


class ProjectMixin:
    """Undo/redo and the project file on the window."""

    # What it uses of the window and doesn't define: MainWindow's state and the other mixins' methods
    # (tests/test_docs.py keeps this exact, so a new dependency shows in the diff)
    NEEDS = ('_ask_to_save', '_building', '_end_modes_except', '_hand_demand_to_run', '_log_edits',
             '_network_summary', '_refresh_grid', '_refresh_mode', '_refresh_summary', '_saved', '_select_nodes',
             '_show_background', '_show_metro_summary', '_stop_run', 'add_node_button', 'ai_mode',
             'background_painter', 'background_panel', 'extensions', 'history', 'install_network', 'metro_button', 'metro_line',
             'multi_nodes', 'network', 'network_path', 'node_chain', 'od_matrix', 'od_panel', 'redo_action',
             'runner', 'scene', 'seed_box', 'select', 'setWindowModified', 'setWindowTitle', 'speed_combo',
             'statusBar', 'undo_action', 'vehicle_modes')

    # --- Undo / redo: snapshots of the network and the OD matrix entries -------------

    def _snapshot(self) -> tuple:
        # ponytail: whole-state copies; fine for editor-sized networks, switch to
        # per-action change records if 20x20 grids make typing feel slow.
        return deepcopy(self.network), deepcopy(self.od_matrix.cells), self.vehicle_modes.state()

    def _commit(self, merge_key: object = None, stop_run: bool = True) -> None:
        """Call when an action finishes; the state before it becomes an undo step. Nothing changed
        (a re-click, a drag that didn't move): nothing happens, and a run carries on. Nodes being placed go into
        whatever step this is (an edit of the one selected, Delete, the assistant): kept now, they are no longer the
        chain's to discard or take back, and Add Node, still on, starts a new chain."""
        if getattr(self, "node_chain", None):
            self.node_chain = []
            self.background_painter.guide = None
        self.od_matrix.prune(self.network.approach_nodes)  # a deleted end's demand goes with it (or a file's unknown end's)
        snapshot = None if self._building else self._snapshot()
        if snapshot is not None and snapshot == self.history.current:
            return
        if self.runner is not None and stop_run:  # a run drives the network and vehicle types as they were
            self._stop_run("The network or vehicle types changed, so the run stopped. Press Run to start again.")
        if self._building:  # the build commits once when it ends
            self._refresh_grid()
            return
        if self.history.commit(snapshot, merge_key):
            self._show_metro_summary()  # riders come and go with demand and buildings' splits as well as lines
            self._refresh_grid()  # the assistant may add or remove an end while the grid shows
            self._refresh_undo_actions()
            self._show_saved()
            self._refresh_summary()
            self._log_edits()

    def _refresh_undo_actions(self) -> None:
        self.undo_action.setEnabled(bool(self.history.undo_stack))
        self.redo_action.setEnabled(bool(self.history.redo_stack))

    def _wait_for_build(self) -> bool:
        """True (saying so) while a Scenario Builder build goes on: it is one undo step, made when it ends."""
        if self._building:
            self.statusBar().showMessage("Building the scenario: undo or redo once it has finished.", 5000)
        return self._building

    def undo(self) -> None:
        if self._wait_for_build():
            return
        if self.metro_button.isChecked():  # deploying: the unfinished line isn't an undo step yet; it goes alone
            if self.metro_line in self.network.metro_lines:
                self.network.delete_metro_line(self.metro_line)
            self.metro_line = None
            self.metro_button.setChecked(False)
            self.statusBar().showMessage("The unfinished MRT line was taken back.")
            return
        if self.add_node_button.isChecked() and self.node_chain:  # placing nodes: not a step yet; the last goes back
            last = self.node_chain.pop()
            self.network.delete_node(last)
            self.background_painter.guide = None
            self.select("node", self.node_chain[-1]) if self.node_chain else self.select("", "")
            self._refresh_mode()
            self.statusBar().showMessage(f"{last} taken back.")
            return
        self._restore(self.history.undo(), "Undone. Ctrl+Y redoes it.", "Nothing to undo.")

    def redo(self) -> None:
        if self._wait_for_build():
            return
        self._end_modes_except(None)  # a metro line being deployed ends first: accepted, it is a new step (no redo)
        self._restore(self.history.redo(), "Redone.", "Nothing to redo.")

    def _restore(self, state: tuple | None, done: str, nothing: str) -> None:
        if state is None:
            self.statusBar().showMessage(nothing, 3000)
            return
        self.background_panel.end_tool()  # an unconfirmed work zone is put back first, on the network it was made on
        network, cells, (modes, default_split) = deepcopy(state)  # the history keeps its own copy untouched
        # Only the demand differs: a run carries on with it, as with any demand edit.
        # Only the demand, or the background, differs: a run carries on with it.
        same = replace(network, background=self.network.background, institutions=self.network.institutions) == self.network
        live = self.runner is not None and same and (modes, default_split) == self.vehicle_modes.state()
        if live and network.background != self.network.background:
            self.network.background = network.background
            self._show_background()
        if live and network.institutions != self.network.institutions:
            self.network.institutions = network.institutions
            self.scene.rebuild()
        if self.runner is not None and not live:  # the run drives the network as it was
            self._stop_run()
        selection, group = self.scene.selection, list(self.multi_nodes)
        if not live:
            self.install_network(network, fit=False)
        self.od_matrix.cells = cells  # cells first: restoring the types then matches them (no new step)
        if not live:
            self.vehicle_modes.restore(modes, default_split)
        self.od_matrix.refresh()
        self.od_panel.show_current()
        if live:
            self._hand_demand_to_run()
        if group:
            self._select_nodes([node for node in group if node in network.nodes])
        elif selection:
            self.select(*selection)  # cleared if the object no longer exists
        self._refresh_undo_actions()
        self._show_saved()
        self._log_edits()
        self._refresh_summary()
        self.statusBar().showMessage(f"{self._network_summary()} • {done}")

    # --- Projects: the network, demand, vehicle types and run settings in one file (network_io) -------

    def _project(self) -> dict:
        """The project's parts beyond the network, as plain data for network_io (network_io.project_record): with
        the extensions' ticks, settings and own state (ExtensionsPanel.project_state)."""
        return project_record(self.od_matrix.cells, self.od_matrix.mode, self.vehicle_modes.modes,
                              self.vehicle_modes.default_split, self.seed_box.value(), self.speed_combo.currentData(),
                              self.extensions.project_state())

    def _save_marker(self) -> tuple:
        return (self.history.current, self.seed_box.value(), self.speed_combo.currentIndex(), self.od_matrix.mode,
                json.dumps(self.extensions.project_state(), sort_keys=True, default=str))

    def unsaved(self) -> bool:
        """Changed since last saved or opened: another undo state (edits, demand, types), seed, speed, the
        Hourly/Daily switch (a Run uses the demand it shows) or the extensions (ticks, settings, emergency schedules)."""
        now = self._save_marker()
        return now[0] is not self._saved[0] or now[1:] != self._saved[1:]

    def _show_saved(self) -> None:
        """The title bar: the project's file, * while it has unsaved changes."""
        if not hasattr(self, "_saved"):  # still being built
            return
        name = Path(self.network_path).name if self.network_path else "Untitled"
        self.setWindowTitle(f"{name}[*] — {TITLE}")
        self.setWindowModified(self.unsaved())

    def save_file(self, as_new: bool = False) -> bool:
        """Save the project (to its file; asking where the first time or for Save As). True once saved. A work zone
        being chosen is cancelled first: only a confirmed one is part of the project; nodes being placed are kept first
        (one step), so what is saved is in the history and the title's * is right."""
        self.background_panel.end_tool()
        self.add_node_button.setChecked(False)
        path = self.network_path if self.network_path and not as_new else None
        if path is None:
            start = self.network_path or str(Path.home() / "Documents" / "project.json")
            path, _ = QFileDialog.getSaveFileName(self, "Save Project", start, "ACMS project (*.json)")
            if not path:
                return False
        try:
            save_network(self.network, path, self.ai_mode.conversation_state(), self._project())
        except (OSError, ValueError) as exc:
            QMessageBox.critical(self, "Save failed", str(exc))
            return False
        self.network_path, self._saved = path, self._save_marker()
        self._show_saved()
        self.statusBar().showMessage(f"Saved {path}", 5000)
        return True

    def _may_discard(self) -> bool:
        """Before closing or opening another project: save, discard or stay (unsaved changes only). Nodes being
        placed are kept first (one step), so they count as changes."""
        self.add_node_button.setChecked(False)
        if not self._ask_to_save or not self.unsaved():
            return True
        answer = QMessageBox.question(
            self, "Unsaved changes", "Save the changes to this project first?",
            QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Save)
        if answer == QMessageBox.StandardButton.Save:
            return self.save_file()
        return answer == QMessageBox.StandardButton.Discard

    def load_file(self, path: str | None = None) -> bool:
        """Open a project: everything is read and checked first, so a bad file changes nothing."""
        if not self._may_discard():
            return False
        if path is None:
            path, _ = QFileDialog.getOpenFileName(self, "Open Project", self.network_path or "", "ACMS project (*.json)")
            if not path:
                return False
        try:
            network = load_network(path)
            project = saved_project(path)
            modes, default_split, cells = project_parts(project, lambda: VehicleModes().state())
            conversation = saved_conversation(path)  # read and checked here too, not after the network is in
            if conversation and not all(isinstance(conversation.get(key, []), list) for key in ("messages", "transcript")):
                raise ValueError("Conversation: its messages and transcript must be lists.")
        except (ValueError, TypeError, KeyError) as exc:  # TypeError: a vehicle type with a setting this app lacks
            QMessageBox.critical(self, "Open failed", f"{path} can't be opened: {exc}")
            return False
        self._stop_run()
        self.network_path = path
        self.install_network(network)
        self.od_matrix.cells = cells  # cells first: restoring the types then matches them
        self.vehicle_modes.restore(modes, default_split)
        self.od_panel._show_mode(project.get("demand", {}).get("shown", "hourly"))
        self.od_matrix.refresh()
        run = project.get("run", {})
        self.seed_box.setValue(int(run.get("seed", RUN_SEED)))
        speed = run.get("speed")  # an older file has none: the speed stays
        index = -1 if speed is None else self.speed_combo.findData(int(speed) if float(speed).is_integer() else speed)
        if index >= 0:
            self.speed_combo.setCurrentIndex(index)
        self.ai_mode.restore_conversation(conversation)
        # After the network: an extension's own state may name its places. A file without them leaves the tab as it is
        not_restored = self.extensions.restore_project_state(project["extensions"]) if "extensions" in project else []
        self._commit()
        # Its own history: Undo can't bring the last project back under this one's name (Save would overwrite it)
        self.history = History(self.history.current)
        self._refresh_undo_actions()
        self._saved = self._save_marker()
        self._show_saved()
        self.statusBar().showMessage(f"Opened {path}: {len(network.nodes)} junctions, {len(cells)} OD pairs with "
                                     f"demand, {len(modes)} vehicle types, seed {self.seed_box.value()}" +
                                     (f". Not restored: {'; '.join(not_restored)}" if not_restored else ""),
                                     20000 if not_restored else 8000)
        return True

"""Application entry point and UI assembly for the network editor."""

from __future__ import annotations

import sys
import tempfile
import threading
from pathlib import Path
from dataclasses import replace

from PySide6.QtCore import QBuffer, QEvent, QIODevice, QStandardPaths, Qt, QTimer
from PySide6.QtGui import QAction, QGuiApplication, QImage, QPainter
from PySide6.QtWidgets import QApplication, QMainWindow

# Absolute imports so this file also runs directly as a script (IDE "Run").
from traffic_simulator.background import BackgroundPainter
from traffic_simulator.activity import ActivityLog
from traffic_simulator.clock import SimulationClock
from traffic_simulator import network_tools, results
from traffic_simulator.main_agents import AgentsMixin
from traffic_simulator.main_assistant import AssistantMixin
from traffic_simulator.main_create import CreateMixin
from traffic_simulator.main_demand import DemandMixin
from traffic_simulator.main_edit import EditMixin
from traffic_simulator.main_experiments import ExperimentsMixin
from traffic_simulator.main_layout import (DEFAULT_DRIVING_SIDE, DEFAULT_HORIZONTAL_CORRIDORS,
                                           DEFAULT_LANE_CHANGING, DEFAULT_LANES_PER_DIRECTION,
                                           DEFAULT_SPEED_LIMIT_KMH, DEFAULT_VERTICAL_CORRIDORS, LayoutMixin)
from traffic_simulator.main_metro import MetroMixin
from traffic_simulator.main_project import TITLE, ProjectMixin
from traffic_simulator.main_runs import RunsMixin
from traffic_simulator.main_scenario import ScenarioMixin
from traffic_simulator.simulation import Runner
from traffic_simulator.history import History
from traffic_simulator.models import Network
from traffic_simulator.network_generator import generate_network
from traffic_simulator.network_scene import NetworkGraphicsScene, NetworkView
from traffic_simulator.properties_panel import PropertiesPanel
from traffic_simulator.theme import apply_theme


class MainWindow(RunsMixin, ScenarioMixin, ExperimentsMixin, AgentsMixin, AssistantMixin, ProjectMixin,
                 CreateMixin, MetroMixin, EditMixin, DemandMixin, LayoutMixin, QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"Untitled[*] — {TITLE}")
        self.resize(1280, 800)
        # One desktop workspace palette, whatever the Windows light/dark setting.
        apply_theme(QApplication.instance())
        self.network: Network = generate_network(
            DEFAULT_HORIZONTAL_CORRIDORS,
            DEFAULT_VERTICAL_CORRIDORS,
            driving_side=DEFAULT_DRIVING_SIDE,
            speed_limit_kmh=DEFAULT_SPEED_LIMIT_KMH,
            lanes_per_direction=DEFAULT_LANES_PER_DIRECTION,
            lane_changing=DEFAULT_LANE_CHANGING,
        )
        self.scene = NetworkGraphicsScene(self.network)
        self.clock = SimulationClock()  # Run / Pause / Play / Stop; the traffic and extensions follow it
        self.runner: Runner | None = None  # the current run: its physics on its own thread
        self.recorder: results.RunRecorder | None = None  # its results, kept in a temporary folder as it goes
        self._replaced: list = []  # the run's earlier signal overrides (ticking swaps them): their reports are its too
        self.network_path: str | None = None  # the network's file, once saved or loaded (for a run's record)
        self.last_results: Path | None = None  # the last run's workbook, and its figures for View Summary
        self.last_kpis: dict | None = None
        self.tests: dict[str, dict] = {}  # this session's headless runs (R1...) and experiments (E1...)
        self._building = False  # a Scenario Builder build: its steps commit once, at the end (one undo step)
        self._testing, self._cancel = False, False  # a test going on (off the UI thread); Stop asks it to end
        self._busy_what = ""  # the test going on, as the status bar names it ("Batch run")
        self._close_after_test = False  # the window was closed mid-test: it closes once the test has stopped
        self.activity = ActivityLog(where=lambda: self.page_title.text(), parent=self)  # Watch my edits' log
        self._activity_seen = None
        # Each headless run going (batch, experiment, run_simulation): its label -> (simulated s, of s), set by its
        # worker thread as it steps; the menu bar's clock shows them while the test goes (_in_background)
        self._headless: dict = {}
        self._headless_lock = threading.Lock()
        # ...and, while the map shows them (Batch Run's Show runs on map; on at first), each run's latest frame,
        # taken on its own thread at most 10 times a real second
        self._headless_frames: dict = {}
        self.show_runs = True
        self._institution_notes: list[str] = []  # what the institutions' trips couldn't do (_pair_demand)
        # Where each run's workbook goes when it ends: Documents\ACMS results (tests: a temporary folder).
        offscreen = QGuiApplication.platformName() in ("offscreen", "minimal")
        self.results_folder = Path(tempfile.gettempdir()) / "ACMS test results" if offscreen else \
            Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DocumentsLocation)) / "ACMS results"
        self._drawn_at = 0.0  # when the map last drew a frame of the run
        self.view = NetworkView(self.scene)
        # The map's background (a colour, an image or OpenStreetMap), drawn behind the scene.
        self.background_painter = BackgroundPainter()
        self.view.background = self.background_painter
        self.background_painter.tile_ready.connect(self.view.redraw)
        self.scene.extent = self._extent
        network_tools.image_mask = self.background_painter.road_mask  # network_from_background reads images with it
        self.properties = PropertiesPanel()
        self.properties.set_network(self.network)
        self.edge_start: str | None = None
        self.multi_nodes: list[str] = []  # junctions picked with Ctrl+click, in click order
        self.group: list[tuple[str, str]] = []  # a selection box's objects, (kind, id)
        self._build_ui()
        self._build_menu()
        self._build_sim_bar()
        self.history = History(self._snapshot())
        self._saved = self._save_marker()  # what was last saved or opened: unsaved changes are anything else
        self._ask_to_save = not offscreen  # tests (offscreen) never stop at the prompt
        self._refresh_undo_actions()
        self._connect_signals()
        self.scene.rebuild()
        QTimer.singleShot(0, self.view.fit_network)
        if not offscreen:  # runs a crash cut short: their workbooks from what they had kept
            self.results_folder.mkdir(parents=True, exist_ok=True)
            recovered = results.recover(self.results_folder)
            if recovered:
                self.last_results = recovered[-1]
                self.statusBar().showMessage(f"Recovered the results of {len(recovered)} interrupted run"
                                             f"{'s' if len(recovered) > 1 else ''}: {recovered[-1]}", 20000)
        if not self.view.on_gpu and QApplication.platformName() not in ("offscreen", "minimal"):
            self.statusBar().showMessage(
                f"No GPU drawing ({self.view.renderer or 'no OpenGL driver'}): the map draws on the CPU, so busy "
                "runs may lag. Update the graphics driver, or run ACMS on this PC rather than over Remote Desktop.",
                20000)

    def _watch(self, on: bool) -> None:
        """Watch my edits on or off: the activity log (activity.py) in the results folder."""
        if on:
            path = self.activity.start(self.results_folder, [action for action in self.findChildren(QAction)
                                                             if action is not self.watch_action])  # it logs itself
            self._activity_seen = self._snapshot()
            self.statusBar().showMessage(f"Watching: every step goes to {path}", 8000)
        else:
            path = self.activity.stop()
            if path is not None:
                self.statusBar().showMessage(f"Saved what you did to {path}", 8000)

    def _watch_failed(self, why: str) -> None:
        """The activity log couldn't write (a full disk): it went off; the eye goes off and says why, once."""
        self.watch_action.setChecked(False)
        self.statusBar().showMessage(why, 15000)

    def _log_edits(self) -> None:
        """While watching: each undo step's changes, a line each (network_tools.state_changes)."""
        if self.activity.on:
            now = self._snapshot()
            for line in network_tools.state_changes(self._activity_seen, now):
                self.activity.log("edit", line)
            self._activity_seen = now

    def closeEvent(self, event) -> None:  # type intentionally inherited from Qt
        if not self._close_after_test and not self._may_discard():
            event.ignore()
            return
        if self._testing:  # closing now would end the test's event loop mid-run: stop it, then close
            self._cancel, self._close_after_test = True, True
            self.statusBar().showMessage(f"Stopping: {self._busy_what}. The window closes when it has stopped.")
            event.ignore()
            return
        self._cancel = True  # and a test the assistant is running
        self._stop_runner()  # the physics thread ends with the window
        self.clock.pause()  # and the clock: no frames, draw() or algorithm() after it
        self.background_painter.close()  # and the map's tile downloads
        super().closeEvent(event)

    # --- The Research Log: each AI Mode step, for an extension's log() --------------------------

    def _research_step(self, event: dict) -> None:
        """An AI Mode answer ended: its record (AIModePanel._end_step) with the step's changes to the network,
        demand and vehicle types and a picture of what changed (PNG): the map if the network did, the OD
        matrix if the demand did, none if nothing did; to the ticked extensions' log(); nothing is built
        while none is ticked."""
        before = event.pop("before", None)
        if not self.extensions.wants("log"):
            return
        now = self._snapshot()
        event["changes"] = network_tools.state_changes(before, now) if before is not None else []
        if before is not None and network_tools.describe_changes(before[0], now[0], limit=1):
            event["map_png"] = self._map_png()
        if before is not None and before[1] != now[1]:
            event["demand_png"] = self._demand_png()
        event["results_folder"] = str(self.results_folder)
        event["project_file"] = str(self.network_path or "")
        event["network"] = self._network_summary()
        event["activity"] = self.activity.take() if self.activity.on else []  # what the user did since the last step
        self.extensions.log(event)

    def _map_png(self) -> bytes:
        """The map as the user sees it now (the view's area: the network, its background, the selection), as PNG."""
        size = self.view.viewport().size()
        image = QImage(max(size.width(), 1), max(size.height(), 1), QImage.Format.Format_ARGB32)
        image.fill(0)
        painter = QPainter(image)
        self.view.render(painter)
        painter.end()
        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        image.save(buffer, "PNG")
        return bytes(buffer.data())

    def _demand_png(self) -> bytes:
        """The OD matrix as it now is, as PNG; drawn at the map's size when it isn't the page showing."""
        matrix = self.od_matrix
        if not matrix.isVisible():
            matrix.set_ends(*self._od_ends())
            matrix.resize(self.workspace.size())
        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        matrix.grab().save(buffer, "PNG")
        return bytes(buffer.data())

    def eventFilter(self, watched, event) -> bool:  # type intentionally inherited from Qt
        """The icon tab row resized (the sidebar's scroll bar came or went): its icons centred again. The
        sidebar's contents re-laid out (a row shown, label columns aligned, another screen's fonts): fitted again."""
        if event.type() == QEvent.Type.Resize and watched is self.tabs.tabBar():
            self._centre_tab_icons()
        elif event.type() == QEvent.Type.LayoutRequest and watched is self.side_scroll.widget():
            self._fit_sidebar()
        return super().eventFilter(watched, event)

    def _summary_inputs(self) -> dict:
        """Everything View Summary reports on (see summary.SummaryPanel)."""
        origins, destinations = self._od_ends()
        ai = self.ai_mode
        cells = {pair: replace(cell, split=self.od_matrix.split_of(cell))  # timetabled shares fixed
                 for pair, cell in self.od_matrix.cells.items()}
        return {"network": self.network, "cells": cells, "origins": origins, "results": self._results(),
                "destinations": destinations, "modes": self.vehicle_modes.names(), "ai": [
                    f"System 2: {ai.status_line}",
                    f"Conversation: {len(ai.history)} message{'' if len(ai.history) == 1 else 's'}",
                    "System 1: " + (ai.system_1.currentText() if ai.system_1.currentData() else "Off")
                    + (f" · {ai.system_1_state.text()}" if ai.system_1_state.text() else ""),
                ]}

    def _refresh_summary(self, *_) -> None:
        if self.tabs.currentWidget() is self.summary:
            self.summary.refresh()




def main() -> int:
    # The graphics driver's OpenGL only: Qt would otherwise fall back, unasked, to the software OpenGL it
    # ships (opengl32sw.dll, drawing on the CPU). Without a usable driver the map draws with Qt's raster
    # engine instead, and says so (NetworkView.on_gpu).
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_UseDesktopOpenGL)
    app = QApplication(sys.argv)
    app.setApplicationName("ACMS")
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())

"""Application entry point and UI assembly for the network editor."""

from __future__ import annotations

import math
from collections import defaultdict
import json
import os
import re
import shutil
import statistics
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait as wait_for
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from copy import deepcopy
from dataclasses import asdict, replace
from types import SimpleNamespace

from PySide6.QtCore import QBuffer, QEventLoop, QIODevice, QPoint, QRectF, QSize, QStandardPaths, Qt, QTimer
from PySide6.QtGui import QAction, QFontDatabase, QGuiApplication, QImage, QKeySequence, QPainter, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGraphicsView,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QStackedWidget,
    QToolBar,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

# Absolute imports so this file also runs directly as a script (IDE "Run").
from traffic_simulator.config import (
    DEFAULT_DRIVING_SIDE,
    DEFAULT_HORIZONTAL_CORRIDORS,
    DEFAULT_LANE_CHANGING,
    DEFAULT_LANES_PER_DIRECTION,
    DEFAULT_SPEED_LIMIT_KMH,
    DEFAULT_VERTICAL_CORRIDORS,
)
from traffic_simulator.ai_mode import AIModePanel
from traffic_simulator.background import BackgroundPainter, BackgroundPanel, map_frame
from traffic_simulator.extensions import ExtensionsPanel
from traffic_simulator.activity import ActivityLog
from traffic_simulator.clock import STEP_MS, SimulationClock, clock_text
from traffic_simulator import (accessibility, analysis, assistant, design_checks, experiments, network_tools, results,
                               scenario, stats, weaving)
from traffic_simulator.signal_agent import INSTRUCTIONS, SignalAgent
from traffic_simulator.simulation import DT, Graph, PairDemand, Runner, Simulation, path_size_logit
from traffic_simulator.metro import METRO, loads as metro_loads, places as metro_places
from traffic_simulator.geometry import point_along
from traffic_simulator.history import History
from traffic_simulator.institutions import hourly_people, place, run_demand
from traffic_simulator.institutions import shares as institution_shares
from traffic_simulator.interchanges import (
    build_diamond,
    build_slip_road,
    build_trumpet,
    guess_stem,
    pair_legs,
    roads_left_at_hub,
)
from traffic_simulator.mode_behavior import ModeBehaviorPanel, VehicleMode, VehicleModes
from traffic_simulator.models import METRO_SERVICE, Network, institution_problem, route_choice_problem
from traffic_simulator.network_generator import generate_network
from traffic_simulator.network_io import MAX_SEED, load_network, save_network, saved_conversation, saved_project
from traffic_simulator.network_scene import NetworkGraphicsScene, NetworkView
from traffic_simulator.network_tools import build_edge, delete_objects, edge_crossings
from traffic_simulator.daily_profile import DailyProfile, quarter_shares
from traffic_simulator.od_matrix import MetroMatrix, ModeSplitDialog, ODCell, ODCellPanel, ODMatrix
from traffic_simulator.properties_panel import (ICON_FONTS, ChoiceButtons, Collapsible, OpenPageTabs, PropertiesPanel,
                                                align_form_labels, describe_group, glyph_icon, hint_label,
                                                icon_button, styled_button, TIMING_METHODS)
from traffic_simulator.summary import SummaryPanel
from traffic_simulator.theme import STATUS, apply_theme, float_glass
from traffic_simulator.xlsx import read_xlsx


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


# Sidebar tab icons: glyphs from Windows' own icon font (see properties_panel.ICON_FONTS).
# Without either font, tabs show their names.
TAB_ICONS = {"Create Network": "\ue800", "Edit Network": "\ue70f", "Mode Behavior": "\ue804",
             "Demand Generator": "\ue80a", "AI Mode": "\ue99a", "View Summary": "\ue9d2",
             "Extensions": "\uea86"}  # map, pencil, car, table, robot, chart, puzzle
TAB_ICON_PX = 20
RUN_SEED = 42  # a Run's seed unless the Seed box says otherwise: the same inputs and seed repeat exactly
MAX_AT_ONCE = 8  # a batch's runs going at once, at most (TSP keeps the state of twice as many)
HEADLESS_WAIT_S = 30.0  # a headless test's deadline for each agent answer (Batch Run sets its own): missed, the plan
TITLE = "ACMS — Agentic Context-Aware Mobility System"



# What a headless test with an extension that may ask an AI agent (TSP, DEVL) says about the agent's timing
AGENT_TIMING = ("Extensions that may ask an AI agent were ticked in this test. A headless test pauses for each answer, "
                "and the decision takes effect after the model's real latency in simulated time, as if the run went at "
                "1x; no new questions arise while the run is paused. To test the agent at another pace, with traffic "
                "moving on while it thinks, use a paced Batch Run.")
LIVE_OBSERVED = 400_000  # a live run keeps its latest passages, conflicts and stop-line crossings for the analysis



def emergency_report(simulation) -> dict:
    """A run's Emergency vehicles sheet (Test DEVL): each emergency vehicle's trip (from, to, dispatched, arrived,
    travel time); measures emergency_trips, emergency_arrived and emergency_trip_s (mean). The lanes DEVL cleared
    for them are in its own sheets."""
    trips = list(simulation.emergency_trips.values())
    done = [trip["arrived_s"] - trip["dispatched_s"] for trip in trips if trip["arrived_s"] is not None]
    rows = [["Vehicle", "From", "To", "Dispatched", "Arrived", "Travel time (s)"]]
    rows += [[trip["id"], trip["from"], trip["to"], results.clock(trip["dispatched_s"]),
              results.clock(trip["arrived_s"]) if trip["arrived_s"] is not None else "on the road",
              round(trip["arrived_s"] - trip["dispatched_s"], 1) if trip["arrived_s"] is not None else ""] for trip in trips]
    return {"sheets": [("Emergency vehicles", rows, "Each emergency vehicle dispatched: its trip and travel time.")],
            "measures": {"emergency_trips": len(trips), "emergency_arrived": len(done),
                         "emergency_trip_s": round(sum(done) / len(done), 1) if done else None}}


def _rounded(value, digits: int = 3):
    """Floats rounded throughout a result (dicts, lists), for the model to read."""
    if isinstance(value, float):
        return round(value, digits)
    if isinstance(value, dict):
        return {key: _rounded(item, digits) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_rounded(item, digits) for item in value]
    return value


class MainWindow(QMainWindow):
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
        self.scene.extent = lambda rect: self.background_painter.extent(self.network.background, rect)
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

    def _build_ui(self) -> None:
        root = QWidget()
        root.setObjectName("appShell")
        layout = QHBoxLayout(root)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(12)
        sidebar = QFrame()
        side_layout = QVBoxLayout(sidebar)
        side_layout.setContentsMargins(12, 12, 12, 12)
        side_layout.setSpacing(8)
        # Scrolls instead of forcing the window taller when the road editor is open.
        side_scroll = QScrollArea()
        side_scroll.setWidget(sidebar)
        side_scroll.setWidgetResizable(True)
        side_scroll.setFrameShape(QFrame.Shape.NoFrame)
        side_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        side_scroll.setObjectName("sidebar")  # theme.py: the opaque control panel

        title = QLabel("ACMS")
        title.setToolTip("Agentic Context-Aware Mobility System")
        title.setProperty("role", "brand")
        self.page_title = QLabel()  # the open tab's name: the tabs themselves are icons
        self.page_title.setProperty("role", "pageTitle")
        title_row = QHBoxLayout()
        title_row.addWidget(title)
        title_row.addSpacing(8)
        title_row.addWidget(self.page_title)
        title_row.addStretch(1)
        side_layout.addLayout(title_row)
        side_layout.addWidget(hint_label("Build and refine networks. Distances in metres."))

        # Two sections that open and close: Generate Network (driving side for the whole network, what new
        # roads and signals get, and a grid of them) and a network of your own over a colour, image or map.
        create_tab = QWidget()
        create_layout = QVBoxLayout(create_tab)
        create_layout.setSpacing(6)
        roads = QWidget()
        form = QFormLayout(roads)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.FieldsStayAtSizeHint)
        self.driving_side = ChoiceButtons([("Left-hand", "left"), ("Right-hand", "right")])
        self.driving_side.set_value(DEFAULT_DRIVING_SIDE)
        self.driving_side.setToolTip("Side traffic keeps to")

        self.speed_limit_combo = QComboBox()
        for speed in (20, 30, 40, 50, 60, 70, 80, 100, 120):
            self.speed_limit_combo.addItem(f"{speed} km/h", speed)
        self.speed_limit_combo.setCurrentText(f"{DEFAULT_SPEED_LIMIT_KMH:g} km/h")
        self.speed_limit_combo.setToolTip("Speed limit of new roads")
        self.lanes_spin = QSpinBox()
        self.lanes_spin.setRange(1, 6)
        self.lanes_spin.setValue(DEFAULT_LANES_PER_DIRECTION)
        self.lanes_spin.setToolTip("Lanes of new roads")
        self.lane_changing_check = QCheckBox("Allowed")
        self.lane_changing_check.setChecked(DEFAULT_LANE_CHANGING)
        self.lane_changing_check.setToolTip("Lane changes on new roads")
        form.addRow("Driving side", self.driving_side)
        form.addRow("Speed limit", self.speed_limit_combo)
        form.addRow("Lanes per direction", self.lanes_spin)
        form.addRow("Lane changing", self.lane_changing_check)
        self.signal_method_combo = QComboBox()  # the network's default; demand retimes them at Run
        for label, method in TIMING_METHODS:
            self.signal_method_combo.addItem(label, method)
        self.signal_method_combo.setToolTip("Timing of new signals")
        form.addRow("Signal timing", self.signal_method_combo)
        self.horizontal_spin = QSpinBox()
        self.horizontal_spin.setRange(1, 20)
        self.horizontal_spin.setValue(DEFAULT_HORIZONTAL_CORRIDORS)
        self.horizontal_spin.setToolTip("East–west roads (1–20)")
        self.vertical_spin = QSpinBox()
        self.vertical_spin.setRange(1, 20)
        self.vertical_spin.setValue(DEFAULT_VERTICAL_CORRIDORS)
        self.vertical_spin.setToolTip("North–south roads (1–20)")
        form.addRow("Horizontal roads", self.horizontal_spin)
        form.addRow("Vertical roads", self.vertical_spin)
        self.generate_button = styled_button("Generate Network", "primary", "Replace network with grid")
        form.addRow(self.generate_button)
        self.generate_section = Collapsible("Generate Network", roads, expanded=True, tooltip="Grid of crossing roads")
        create_layout.addWidget(self.generate_section)

        self.background_panel = BackgroundPanel(self.background_painter)
        self.background_panel.attach(network=lambda: self.network, bounds=self._junction_bounds)
        self.background_panel.redraw = self.view.redraw
        self.scene.background_tool = self.background_panel  # its Move and Two Points take the map's clicks
        self.create_tab = create_tab
        self.background_panel.show_background(self.network.background)
        self.own_section = Collapsible("Own Network", self.background_panel, tooltip="Draw over a background")
        create_layout.addWidget(self.own_section)
        create_layout.addStretch(1)

        # The map's own controls, over its top-right corner: fit, reset, and the mouse and keys.
        corner = float_glass(QWidget())  # a floating glass bar: the map shows through it
        row = QHBoxLayout(corner)
        row.setContentsMargins(6, 4, 6, 4)
        row.setSpacing(4)
        self.fit_button = icon_button(0xE9A6, "Fit Network")
        self.reset_button = icon_button(0xE80F, "Reset View")
        self.help_button = icon_button(0xE9CE, "Map Controls")  # hover: its name; click: the list below
        drawing = (f"Drawing on the GPU: {self.view.renderer}" if self.view.on_gpu else
                   f"Drawing on the CPU ({self.view.renderer or 'no OpenGL'}): busy runs may lag")
        self.map_controls = (
            "Wheel: zoom at the cursor · Right or middle drag: pan the view (network and background together)\n"
            "Drag over empty map: select by box (left to right: what lies inside; right to left: what it touches)\n"
            "Ctrl+click: pick several junctions (interchanges) or two roads (slip road)\n"
            "Esc: cancel a tool · Del: delete the selection · F: fit · R: reset\n"
            "End arrows: yellow = entry (supply), white = exit (demand)\n"
            "Power Saver (leaf): runs drawn 30 times a second, without multisampling\n" + drawing)
        self.help_button.clicked.connect(lambda: QToolTip.showText(
            self.help_button.mapToGlobal(QPoint(0, self.help_button.height())), self.map_controls, self.help_button))
        # Input Grid: one toggle, first in the map's corner and in the grid's, so it stays under the mouse.
        self.grid_toggles = []
        for _ in range(2):
            toggle = icon_button(0xE80A, "Input Grid")
            toggle.setCheckable(True)
            toggle.toggled.connect(self.show_grid)
            self.grid_toggles.append(toggle)
        self.power_button = icon_button(0xE8BE, "Power Saver")  # a leaf
        self.power_button.setCheckable(True)
        self.power_button.toggled.connect(self._power_saver)
        self.isometric_button = icon_button(0xE809, "3D View")  # native TiltUp glyph, including Windows 10
        self.isometric_button.setCheckable(True)
        self.isometric_button.toggled.connect(self.view.set_isometric)
        self.view.projection_changed.connect(self.isometric_button.setChecked)
        self.buildings_button = icon_button(0xEC06, "Show Buildings")  # native CityNext glyph
        self.buildings_button.setCheckable(True)
        self.buildings_button.setChecked(self.scene.buildings_visible)
        self.buildings_button.toggled.connect(self.scene.set_buildings_visible)
        self.scene.buildings_changed.connect(self.buildings_button.setChecked)
        self.map_controls += ("\n3D View: orthographic isometric camera, 45 degree yaw, 35.3 degree elevation, zero roll"
                              "\nIn 3D View, selection boxes follow the world grid. Zoom never changes simulation speed.")
        self.map_controls += "\nShow Buildings: display institution footprints and labels; demand is unchanged."
        for button in (self.grid_toggles[0], self.isometric_button, self.buildings_button, self.power_button,
                       self.fit_button, self.reset_button, self.help_button):
            row.addWidget(button)
        self.view.set_corner(corner)

        edit_tab = QWidget()
        edit_layout = QVBoxLayout(edit_tab)
        roads_page = QWidget()  # Edit Network's own tabs: Roads (junctions and roads) and Metro (rail lines)
        roads_layout = QVBoxLayout(roads_page)
        roads_layout.setContentsMargins(0, 0, 0, 0)
        tools = QGroupBox("Add to Network")
        tools_layout = QVBoxLayout(tools)
        self.add_node_button = styled_button("Add Node", tooltip="Place a new junction")
        self.add_node_button.setCheckable(True)
        self.add_edge_button = styled_button("Add Edge", tooltip="Draw a new road")
        self.add_edge_button.setCheckable(True)
        add_buttons = QHBoxLayout()
        add_buttons.addWidget(self.add_node_button)
        add_buttons.addWidget(self.add_edge_button)
        tools_layout.addLayout(add_buttons)
        self.edge_kind = ChoiceButtons([("Connection", "connection"), ("End", "end")])
        self.edge_kind.setToolTip(
            "To a junction or boundary"
        )
        self.edge_type_row = QWidget()  # only while Add Edge is active
        edge_form = QFormLayout(self.edge_type_row)
        edge_form.setContentsMargins(0, 0, 0, 0)
        edge_form.addRow("Edge type", self.edge_kind)
        tools_layout.addWidget(self.edge_type_row)
        roads_layout.addWidget(tools)
        metro_page = QWidget()
        metro_layout = QVBoxLayout(metro_page)
        metro_layout.setContentsMargins(0, 0, 0, 0)
        metro_box = QGroupBox("Metro Rail")
        metro_row = QHBoxLayout(metro_box)
        self.metro_button = icon_button(0xE7C0, "Start deployment")  # a train: each click a stop, right-click ends
        self.metro_button.setCheckable(True)
        metro_row.addWidget(self.metro_button)
        self.metro_summary = hint_label()  # its lines, stops and track
        metro_row.addWidget(self.metro_summary, 1)
        metro_layout.addWidget(metro_box)
        self.edit_tabs = OpenPageTabs()
        self.edit_tabs.addTab(roads_page, "Roads")
        self.edit_tabs.addTab(metro_page, "Metro")
        edit_layout.addWidget(self.edit_tabs)
        self.metro_line: str | None = None  # the line a deployment is extending
        self.mode_hint = hint_label()  # next-step banner, shown only while a tool is active
        self.mode_hint.setProperty("role", "banner")
        edit_layout.addWidget(self.mode_hint)
        edit_layout.addWidget(self.properties)
        edit_layout.addStretch(1)

        demand_tab = QWidget()
        demand_layout = QVBoxLayout(demand_tab)
        # The matrix shows in the main area; this panel edits its selected cell.
        self.vehicle_modes = VehicleModes()  # edited in Mode Behavior, split in the Demand Generator
        self.od_matrix = ODMatrix(self.vehicle_modes)
        self.od_panel = ODCellPanel(self.od_matrix)
        self.institution_panel = self.od_panel.institutions  # Demand → Institutions: buildings placed on the map
        # generate_demand's gravity model measures routes on the open network
        self.od_panel.planner = lambda pairs, args, values=None, shares=None: network_tools.demand_plan(
            self.network, pairs, args, values, shares)
        self.od_panel.scaler = lambda values, pairs, args, shares=None: network_tools.scale_plan(
            self.network, values, pairs, args, shares)
        self.od_panel.checker = self._check_demand  # a degree of saturation, checked in a run
        self.od_panel.calibrator = lambda flows, args: network_tools.calibration_plan(self.network, flows, args)
        self.od_panel.equilibrator = self._equilibrate_demand
        self.od_panel.chooser = lambda pairs, args, values, current=None: network_tools.mode_plan(  # choose_modes
            self.network, pairs, args, self.vehicle_modes.modes, values,
            self._run_test(str(args["from_run"]))["kpis"]["observed"]["trips"] if args.get("from_run") else None,
            current)
        self.od_matrix.corner.insertWidget(0, self.grid_toggles[1])
        demand_layout.addWidget(self.od_panel)
        demand_layout.addStretch(1)

        self.tabs = OpenPageTabs()  # as tall as the open page: the sidebar scrolls only when it needs to
        self.tabs.setDocumentMode(True)  # the tab row spans the sidebar...
        bar = self.tabs.tabBar()
        bar.setExpanding(True)  # ...and the tabs share it (document mode had turned this off)
        self.tabs.addTab(create_tab, "Create Network")
        self.edit_tab = edit_tab
        self.tabs.addTab(edit_tab, "Edit Network")
        self.behavior_tab = ModeBehaviorPanel(self.vehicle_modes)  # before Demand: it feeds the split
        self.tabs.addTab(self.behavior_tab, "Mode Behavior")
        self.demand_tab = demand_tab
        self.tabs.addTab(demand_tab, "Demand Generator")
        self.ai_mode = AIModePanel()
        self.ai_mode.export_folder = self.results_folder  # Export History saves there by default
        self.tabs.addTab(self.ai_mode, "AI Mode")
        self.summary = SummaryPanel(self._summary_inputs, self._show_on_map)
        self.tabs.addTab(self.summary, "View Summary")
        self.extensions = ExtensionsPanel(self.scene, self.clock)  # drop-in features from the extensions folder
        # The agent signal overrides ask (TSP): System 1 or System 2, in the background (a live run never waits).
        self.signal_agent = SignalAgent(self._ask_agent)
        self.extensions.agent = self.signal_agent
        # What extensions may use of the window (Batch Run: its runs, and who can decide TSP in them)
        self.extensions.app = SimpleNamespace(run_batch=self.run_batch, replay_run=self.replay_run,
                                              agent_options=self.ai_mode.agent_options, tsp_batch=self.tsp_batch,
                                              devl_batch=self.devl_batch,
                                              dispatch_emergency=self.dispatch_emergency, places=self.places,
                                              tick_extension=self._tick_extension, show_runs=self._show_runs_on_map,
                                              signal_agent=self.signal_agent,  # the live run's, for TSP's map rings
                                              busy=self.busy)  # what runs now: Start waits for it
        self.extensions.latest_frame = lambda: self.runner.frame if self.runner is not None else None
        self.extensions.hooks_changed.connect(self._apply_hooks)
        self.tabs.addTab(self.extensions, "Extensions")
        for index in range(self.tabs.count()):  # every page lines up with the tab row's edges
            self.tabs.widget(index).layout().setContentsMargins(0, 8, 0, 0)
        # Tabs are icons: hovering one names it, and the open tab's name heads the sidebar.
        self.tab_names = [self.tabs.tabText(index) for index in range(self.tabs.count())]
        self.icon_font = icon_font = next((family for family in ICON_FONTS if family in QFontDatabase.families()), None)
        bar.setIconSize(QSize(TAB_ICON_PX, TAB_ICON_PX))
        for index, name in enumerate(self.tab_names):
            self.tabs.setTabToolTip(index, name)
            if hasattr(bar, "setAccessibleTabName"):  # screen readers hear the name of an icon-only tab
                bar.setAccessibleTabName(index, name)
            if icon_font:
                self.tabs.setTabIcon(index, glyph_icon(TAB_ICONS[name], icon_font))
                self.tabs.setTabText(index, "")
        if icon_font:
            bar.setObjectName("iconTabs")  # theme.py: slim icon-only tabs
        self.tabs.currentChanged.connect(self._name_open_tab)
        self._name_open_tab(self.tabs.currentIndex())
        side_layout.addWidget(self.tabs)

        side_layout.addStretch(1)

        layout.addWidget(side_scroll)
        # The main area shows the map or the OD matrix (show_grid, the Input Grid toggle).
        self.workspace = QStackedWidget()
        self.workspace.addWidget(self.view)
        self.workspace.addWidget(self.od_matrix)
        self.metro_matrix = MetroMatrix()  # Demand Generator → Metro: the metro's own OD matrix
        self.workspace.addWidget(self.metro_matrix)
        layout.addWidget(self.workspace, 1)
        self.setCentralWidget(root)
        # Wide enough for the tab row and the widest page, plus the sidebar's margins and scrollbar. Measured
        # only now that the tabs sit inside the window and use the stylesheet's font.
        bar.ensurePolished()
        margins = side_layout.contentsMargins()
        pages = max(self.tabs.widget(index).minimumSizeHint().width() for index in range(self.tabs.count()))
        side_width = max(bar.sizeHint().width(), pages) + margins.left() + margins.right() +             side_scroll.verticalScrollBar().sizeHint().width() + 4
        side_scroll.setMinimumWidth(side_width)
        side_scroll.setMaximumWidth(side_width)
        # One label column per tab, so every section's fields start on the same line.
        for page in (create_tab, edit_tab, self.od_panel, *(self.ai_mode.systems.widget(i)
                                                            for i in range(self.ai_mode.systems.count()))):
            align_form_labels(page)
        self.statusBar().showMessage(self._network_summary())
        # Always in view, whatever the tab: which model System 2 is using and what it's doing.
        self.ai_status = QLabel()
        self.ai_status.setTextFormat(Qt.TextFormat.RichText)
        self.ai_status.setContentsMargins(0, 0, 8, 0)
        self.ai_status.hide()
        self.statusBar().addPermanentWidget(self.ai_status)
        # While the map is the background: the mouse's WGS84 position.
        self.pointer = QLabel()
        self.pointer.setToolTip("Mouse latitude and longitude")
        self.pointer.setContentsMargins(0, 0, 8, 0)
        self.pointer.hide()
        self.statusBar().addPermanentWidget(self.pointer)
        self._refresh_mode()

    def _build_sim_bar(self) -> None:
        """Run, Play-Pause, Stop, the simulation clock, its speed and Export: small icons in the
        menu bar's top-right corner. Play and Pause share one spot, swapping as the clock starts
        and stops."""
        clock = self.clock
        bar = QToolBar("Simulation", self)  # a parent, or Python frees it when this method returns
        bar.setIconSize(QSize(14, 14))
        bar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)  # names in the tooltips
        bar.setStyleSheet("QToolBar { border: none; spacing: 1px; } QToolButton { padding: 1px; }"
                          "QComboBox { padding: 0 4px; font-size: 9pt; } QLabel { font-size: 9pt; }")
        self.run_action = QAction("Run", self)
        self.run_action.triggered.connect(self._run)
        self.play_action = QAction("Play", self)
        self.play_action.triggered.connect(self._play)
        self.pause_action = QAction("Pause", self)
        self.pause_action.triggered.connect(self._pause)
        self.stop_action = QAction("Stop", self)
        self.stop_action.triggered.connect(self._stop_pressed)
        self.export_action = QAction("Export", self)
        self.export_action.triggered.connect(self._export)
        for action, glyph, tip in ((self.run_action, 0xE72C, "Run from 00:00:00"),  # restart arrow
                                   (self.play_action, 0xE768, "Play"),  # triangle
                                   (self.pause_action, 0xE769, "Pause"),  # bars
                                   (self.stop_action, 0xE71A, "Stop and reset"),  # square
                                   (self.export_action, 0xEDE1, "Export results workbook")):  # arrow out
            action.setToolTip(tip)
            if self.icon_font:
                action.setIcon(glyph_icon(chr(glyph), self.icon_font, 14))
        bar.addActions([self.run_action, self.play_action, self.pause_action, self.stop_action])
        self.clock_label = hint_label(clock_text(0))
        self.clock_label.setProperty("role", "clock")
        self.clock_label.setProperty("activity_skip", True)  # it ticks all the time: Watch my edits leaves it out
        self.clock_label.setWordWrap(False)
        self.clock_label.setToolTip("Simulation time")
        self.clock_label.setContentsMargins(6, 0, 4, 0)
        bar.addWidget(self.clock_label)
        self.speed_combo = QComboBox()
        for factor in (1, 2, 5, 10, 60):
            self.speed_combo.addItem(f"{factor}×", factor)
        self.speed_combo.setToolTip("Simulated seconds per second")
        self.speed_combo.currentIndexChanged.connect(self._set_speed)
        bar.addWidget(self.speed_combo)
        seed_label = hint_label("Seed")  # beside the box, not in it
        seed_label.setWordWrap(False)
        seed_label.setContentsMargins(6, 0, 2, 0)
        bar.addWidget(seed_label)
        self.seed_box = QSpinBox()
        self.seed_box.setRange(0, MAX_SEED)
        self.seed_box.setValue(RUN_SEED)
        self.seed_box.setKeyboardTracking(False)
        self.seed_box.setToolTip("Random seed for runs")
        self.seed_box.valueChanged.connect(lambda: self._show_saved())
        self._seed_box_action = bar.addWidget(self.seed_box)
        # While Batch Run is ticked its own seeds are the ones that count: the box gives way to "Overridden"
        self.seed_overridden = hint_label("Overridden")
        self.seed_overridden.setWordWrap(False)
        self.seed_overridden.setToolTip("Batch Run's seeds")
        self.seed_overridden.setMinimumWidth(self.seed_box.sizeHint().width())
        self._seed_overridden_action = bar.addWidget(self.seed_overridden)
        batch = self._extension("batch_run")
        if batch is not None:
            batch.check.toggled.connect(self._override_seed)
        self._override_seed(batch is not None and batch.check.isChecked())
        bar.addAction(self.export_action)  # after the clock: it saves what a run produced
        # Watch my edits (the eye): every click, entry, choice and edit logged as it happens (activity.py); a logger
        # only: the assistant never looks by itself
        self.watch_action = QAction("Watch my edits", self)
        self.watch_action.setCheckable(True)
        self.watch_action.setToolTip("Watch my edits")
        if self.icon_font:
            self.watch_action.setIcon(glyph_icon(chr(0xE7B3), self.icon_font, 14))  # an eye
        self.watch_action.toggled.connect(self._watch)
        bar.addAction(self.watch_action)
        edge = QWidget()
        edge.setFixedWidth(8)  # keeps the last icon clear of the window edge
        bar.addWidget(edge)
        self.menuBar().setCornerWidget(float_glass(bar), Qt.Corner.TopRightCorner)  # the run controls: a glass bar
        clock.ticked.connect(self._frame)
        clock.state_changed.connect(self._show_clock_state)
        self._show_clock_state(False)

    def _run(self) -> None:
        """Run: a fresh simulation of the network and demand as they are now, from 00:00:00, its
        physics on its own thread in step with the wall clock (simulation.Runner)."""
        if self._testing:  # Run and Play are off meanwhile; this catches any other way in
            self.statusBar().showMessage(f"{self.busy()} is going on: Stop ends it, then Run.", 8000)
            return
        self._stop_runner()
        demand = self._pair_demand()
        # Its own copy: an edit stops the run, but only after it has changed the network, and the run's workbook
        # describes the network and types it drove (the physics thread never reads one half-edited)
        network, modes = deepcopy(self.network), deepcopy(self.vehicle_modes.modes)
        simulation = Simulation(network, demand, modes, seed=self.seed_box.value())
        notes = []
        webster = [plan["cycle_s"] for plan in simulation.signal_plans.values() if plan["method"] == "webster"]
        if webster:
            notes.append(f"Webster timing from the demand at {len(webster)} junction{'s' if len(webster) > 1 else ''}: "
                         f"cycles {min(webster):g}–{max(webster):g} s.")
        groups = {plan["method"]: plan for plan in simulation.signal_plans.values() if "group" in plan}
        for method, plan in groups.items():
            name = {"transyt": "TRANSYT", "synchro": "Synchro", "scats": "SCATS", "scoot": "SCOOT"}[method]
            adapts = " to start, adapting from its detectors" if method in ("scats", "scoot") else ""
            notes.append(f"{name}: {plan['group']} junctions on a {plan['cycle_s']:g} s cycle{adapts}.")
        self.properties.show_plans(dict(simulation.signal_plans))
        if not simulation.demand:
            notes.append("No demand yet: enter flows in the Demand Generator. The signals run meanwhile.")
        notes += self._institution_notes
        if simulation.unreachable:
            notes.append(f"No route for {', '.join(simulation.unreachable[:5])}: those pairs spawn nothing.")
        model, warning = self._deciding()
        if warning:
            notes.append(warning)
        if notes:
            self.statusBar().showMessage(" ".join(notes), 8000)
        self.signal_agent.reset()
        simulation.observe(keep=LIVE_OBSERVED)  # detectors, link times, conflicts…: for the workbook's analysis sheets
        simulation.signal_override = self.extensions.signal_override()  # None unless TSP is ticked
        self.recorder = results.RunRecorder(network, modes,
                                            name=f"{len(simulation.demand)} OD pairs with demand",
                                            inputs=self._run_inputs(simulation, demand), seed=self.seed_box.value(),
                                            model=model, run=self._run_settings(simulation, extra=[
                                                ("AI model deciding TSP", model)]))
        self.runner = Runner(simulation, float(self.speed_combo.currentData()), recorder=self.recorder)
        self.runner.play()
        self.clock.run()

    def _stop_pressed(self) -> None:
        self._cancel = True  # a headless test stops at its next step
        self._stop_run()

    def _in_background(self, work, what: str):
        """Run work(progress, cancelled) on its own thread, the window kept alive (a local event loop) and
        the status bar saying how far it is; Stop cancels it. Returns its result, or raises its error."""
        if self.runner is not None or self._testing:  # never two at once, whichever way it was asked for
            raise ValueError(f"{self.busy()} is going on: stop it first (the menu bar's Stop).")
        state = {"done": False, "result": None, "error": None, "progress": ""}
        self._cancel, self._testing, self._busy_what = False, True, what
        self._show_busy()

        def target():
            try:
                state["result"] = work(lambda text: state.__setitem__("progress", text), lambda: self._cancel)
            except Exception as exc:  # noqa: BLE001 - handed back to the caller
                state["error"] = exc
            finally:
                state["done"] = True

        threading.Thread(target=target, name="network test", daemon=True).start()
        loop, timer = QEventLoop(), QTimer()

        def poll():
            self.statusBar().showMessage(f"{what}: {state['progress']} (Stop ends it)")
            self._show_headless()
            self.extensions.report_failures()  # a run's override raised: switched off, saying why
            if state["done"]:
                loop.quit()

        timer.timeout.connect(poll)
        timer.start(100)
        loop.exec()
        timer.stop()
        self._testing, self._busy_what = False, ""
        self._show_busy()
        self.statusBar().clearMessage()
        failed = self.extensions.report_failures()
        if failed:  # the runs went on without it from then: their workbooks' Extensions dropped sheet says so
            self.statusBar().showMessage(f"{', '.join(failed)} raised during {what.lower()} and was switched off: "
                                         "see the Extensions tab.", 15000)
        with self._headless_lock:
            self._headless.clear()
            self._headless_frames.clear()
        if self.runner is None:
            self.scene.show_frame(1.0, None)  # the test's vehicles leave the map
        self.clock_label.setToolTip("Simulation time")
        self.clock_label.setText(clock_text(self.clock.time_s))
        if self._close_after_test:
            QTimer.singleShot(0, self.close)  # after the caller has had its result
        if state["error"] is not None:
            raise state["error"]
        return state["result"]

    def busy(self) -> str:
        """What is using the simulator ("Batch run", "A live run"), or "" when nothing: for a control that
        would start another (Batch Run's Start), so it can say why it waits."""
        if self._testing:
            return self._busy_what
        return "A live run" if self.runner is not None else ""

    def _show_busy(self) -> None:
        """While a test runs, Run and Play wait, their tooltips naming it (the status bar says how far it is)."""
        busy = self._busy_what if self._testing else ""
        for action, tip in ((self.run_action, "Run from 00:00:00"), (self.play_action, "Play")):
            action.setEnabled(not busy)
            action.setToolTip(" ".join(f"Busy: {busy}".split()[:5]) if busy else tip)

    def _show_headless(self) -> None:
        """While a test runs headless: the menu bar's clock shows its run's simulated time (the least advanced of
        those going side by side), its tooltip each run by name ("Never preempt (baseline), seed 1: 00:12:34 of 00:30:00")."""
        with self._headless_lock:
            going = list(self._headless.items())
        if not going:
            return
        label, (seconds, _) = min(going, key=lambda item: item[1][0])
        self.clock_label.setText(clock_text(seconds))
        with self._headless_lock:
            frame = self._headless_frames.get(label) if self.show_runs else None
        if frame is not self.scene.frame:  # the same run the clock shows, drawn as a live run is (or none)
            self.scene.show_frame(1.0, frame)
        self.clock_label.setToolTip("\n".join(f"{label}: {results.clock(t)} of {results.clock(of)}"
                                              for label, (t, of) in going))

    def _headless_seconds(self) -> float:
        """The simulated seconds the headless runs going have done so far, together."""
        with self._headless_lock:
            return sum(t for t, _ in self._headless.values())

    def _one_run(self, network, demand, modes, seed, duration_s, override, settings, extra_inputs, label, progress, cancelled,
                 folder: Path | None = None, model: str = "", speed: float | None = None, dispatches=(),
                 incidents=(), weather: str = "clear", meters=(), measure_from_s: float = 0.0, lateral: bool = False):
        """One headless run on the worker thread: its recorder, its workbook (named by its seed and `model`, the AI
        deciding TSP; its extensions' reports in it); (kpis, path, simulated seconds). kpis["reported"]: their figures.
        `speed`: in step with the wall clock at this pace (a batch's), else as fast as the CPU allows. `dispatches`:
        emergency vehicles sent at the start (their trips: the Emergency vehicles sheet)."""
        made = {}
        for agent in ((override.agent.values() if isinstance(override.agent, dict) else [override.agent])
                      if override is not None else []):
            if hasattr(agent, "reset"):
                agent.reset()  # its own run's answers only

        def recorder_for(simulation):
            made["simulation"] = simulation  # so a run that raises still ends in a workbook
            made["recorder"] = results.RunRecorder(
                network, modes, name=label, run=settings, seed=seed, model=model, measure_from_s=measure_from_s,
                inputs=results.input_sheets(network, demand, modes, dict(simulation.signal_plans)) + extra_inputs)
            return made["recorder"]

        def stepped_to(t: float) -> None:  # the menu bar's clock follows it (_show_headless)
            with self._headless_lock:
                self._headless[label] = (t, duration_s)
            progress(f"{label}, {results.clock(t)} of {results.clock(duration_s)}")

        drawn = [0.0]

        def framed(simulation) -> None:  # on the run's thread: a frame for the map, while it shows runs
            now = time.monotonic()
            if not self.show_runs or now - drawn[0] < 0.1:
                return
            drawn[0] = now
            frame = simulation.frame()
            with self._headless_lock:
                self._headless_frames[label] = frame

        began = time.monotonic()
        try:
            simulation, recorder = experiments.run_headless(
                network, demand, modes, seed, duration_s, override, recorder_for, progress=stepped_to,
                cancelled=cancelled, observe=True, speed=speed, dispatches=dispatches, incidents=incidents,
                weather=weather, meters=meters, on_frame=framed, lateral=lateral)
        except BaseException as exc:  # it raised: what it recorded is still its workbook; the error goes on up
            if "recorder" in made:
                self._end_recording(made["recorder"], made["simulation"], folder or self.results_folder, lambda: None,
                                    f"The run stopped: {exc!r}")
            raise
        finally:
            with self._headless_lock:
                self._headless.pop(label, None)
                self._headless_frames.pop(label, None)
        stepped = time.monotonic()
        kpis = {}

        def extras() -> None:
            kpis.update(self._observed_kpis(simulation, recorder))
            recorder.add_analysis(experiments.analysis_sheets(kpis))
            reports = self.extensions.reports(override)  # TSP's and DEVL's: their sheets, every decision, the agents' figures
            if dispatches:
                reports.append(emergency_report(simulation))
            recorder.add_sheets(reports)
            kpis["reported"] = {key: value for report in reports for key, value in report.get("measures", {}).items()}
            kpis["decision_sheets"] = {name: rows for report in reports for name, rows, *_ in report.get("sheets", [])
                                       if "decisions" in name.lower()}  # each agent decision (read_decisions)

        path, failed = self._end_recording(recorder, simulation, folder or self.results_folder, extras)
        if failed is not None:
            raise failed  # its workbook is written, without what failed
        if speed:  # under the pace asked: the CPU fell behind the clock (an answer's latency then cost less)
            kpis["reported"]["pace_achieved"] = round(simulation.time / max(stepped - began, 1e-9), 2)
        return kpis, path, simulation.time

    @staticmethod
    def _observed_kpis(simulation, recorder) -> dict:
        """A headless run's KPIs with what it observed (the analysis tools read it) and its conflict count."""
        kpis = recorder.kpis()
        kpis["observed"] = {**recorder.observed(), **simulation.observations()}
        kpis["conflicts"] = sum(event["ttc_min"] < experiments.CONFLICT_TTC_S for event in kpis["observed"].get("conflicts", []))
        kpis["co2_kg"] = analysis.co2_kg(kpis)
        kpis["bus_headway_cv"] = experiments.headway_cv(kpis["observed"])
        return kpis

    @staticmethod
    def _end_recording(recorder, simulation, folder: Path, extras, why: str = ""):
        """A run's workbook in `folder`, however the run ended: the part-sample since its last one and `extras()`
        (its reports and analysis) as far as they go, what went wrong (`why`, their error) on its Run sheet, and
        the recorder always finished. (Its path, None if it simulated nothing; the error extras raised, or None.)"""
        failed = None
        try:
            if simulation is not None and simulation.time > recorder.totals["time_s"]:
                recorder.sample(simulation.frame())
            extras()
        except Exception as exc:  # noqa: BLE001 - whatever else failed, what the run recorded is written
            failed = exc
        recorder.error = "; ".join(filter(None, [recorder.error, why, failed and f"Its extras failed: {failed!r}"]))
        folder.mkdir(parents=True, exist_ok=True)
        return recorder.finish(folder), failed

    def _quick_run(self, network, demand, modes, seed, duration_s, override, progress, cancelled, label,
                   **conditions) -> dict:
        """A headless run kept to its KPIs (no workbook), for searches with many runs. `conditions`: run_headless's
        dispatches, incidents, weather, meters."""
        folder = Path(tempfile.mkdtemp(prefix="acms-search-"))
        try:
            recorder = results.RunRecorder(network, modes, folder=folder / "run")
            simulation, _ = experiments.run_headless(
                network, demand, modes, seed, duration_s, override, recorder, observe=True, cancelled=cancelled,
                progress=lambda t: progress(f"{label}, {results.clock(t)} of {results.clock(duration_s)}"), **conditions)
            if simulation.time > recorder.totals["time_s"]:
                recorder.sample(simulation.frame())
            return self._observed_kpis(simulation, recorder)
        finally:
            shutil.rmtree(folder, ignore_errors=True)

    def _check_demand(self, pairs: list) -> tuple[float | None, dict]:
        """The Demand Generator's demand, just sized to a degree of saturation, run headless (its busiest hour,
        the Seed box's seed, the ticked extensions): the share of `pairs`' demand that holds, the whole of it
        or found by bisection (experiments.largest_holding), and a report. None: not checked. Raises
        ValueError if none tried holds."""
        minutes = experiments.HOLD_MINUTES
        if self.runner is not None or self._testing:
            return None, {"checked": False, "note": "Not checked in a run: one is going. Stop it, then check with "
                                                    "run_simulation."}
        scope, seed = set(pairs), self.seed_box.value()
        demand = experiments.at_busiest_hour(self._pair_demand())
        network, modes = deepcopy(self.network), deepcopy(self.vehicle_modes.modes)
        override = self.extensions.signal_override(agent=self._waiting_agent())

        def check(share, progress, cancelled):
            sized = [replace(pair, hourly=pair.hourly * share) if (pair.origin, pair.destination) in scope else pair
                     for pair in demand]
            return experiments.holds_by_majority(  # a borderline verdict confirmed on two more seeds
                lambda s: experiments.holds(network, sized, modes, s, override, cancelled=cancelled, progress=lambda t: progress(
                    f"{share:.0%} of the demand, seed {s}, {results.clock(t)} of {results.clock(minutes * 60)}")), seed)

        share, tried = self._in_background(lambda progress, cancelled: experiments.largest_holding(
            lambda s: check(s, progress, cancelled)), "Checking the demand in a run")
        report = {"simulated_min": minutes, "seed": seed, "tried": [
            {"share": f"{s:.0%}", "held": r["holds"], "build_up": r.get("build_up")} for s, r in tried]}
        if share is None:
            return None, {**report, "checked": False, "note": "Stopped by the user: not checked."}
        if share == 0:
            controls = [node.control for node in self.network.nodes.values()]
            giving_way = controls.count("uncontrolled")
            advice = (f" {giving_way} of its {len(controls)} junctions only give way (an OpenStreetMap network marks few "
                      "signals): signalising the busiest (set_junction_control, control 'signalised'), or main roads "
                      "only (network_from_background, road_classes 'main'), may let demand hold."
                      if giving_way > len(controls) / 2 else "")
            raise ValueError(f"The simulation jams even at {tried[-1][0]:.0%} of that demand: vehicles pile up."
                             f"{advice} Look at the network (run_simulation, read_traffic) before sizing demand.")
        note = ("It held in the run." if share == 1 else
                f"At that size the simulation jammed (vehicles piled up), so {share:.0%} of it is kept: the largest "
                "share that held. The capacity model's saturation figures are for the size before this check.")
        return share, {**report, "checked": True, "kept": f"{share:.0%}", "note": note}

    def _journal_sheet(self) -> list:
        rows = [["Time", "Kind", "Tool", "What", "Arguments", "Outcome"]]
        rows += [[e["time"], e["kind"], e["tool"], e["what"], e["arguments"], e["outcome"]] for e in self.ai_mode.journal]
        return [("Planning journal", rows)]

    def _runs_command(self, tool: str, args: dict, check_only: bool = False) -> dict:
        """AI Mode's network-testing tools: headless runs and experiments, their results, the extensions."""
        if tool == "institution_mode_split":
            return self._institution_split(args, check_only)
        if tool == "read_results":
            return self._read_results(str(args.get("id") or ""))
        if tool == "compare_runs":
            return self._compare_runs(network_tools._ids(args.get("runs") or []))
        if tool in ("analyse_run", "read_detectors", "analyse_safety", "forecast", "check_calibration"):
            test = self._run_test(str(args.get("id") or ""))
            kpis, head = test["kpis"], {"run": test["id"], "seed": test["seed"], "simulated": test["simulated"]}
            if tool == "analyse_run":
                return {**head, **experiments.analyse_run(kpis)}
            if tool == "read_detectors":
                return {**head, **experiments.detectors(kpis, args.get("road") or None)}
            if tool == "analyse_safety":
                found = {**head, **experiments.safety(kpis, float(args.get("ttc_s") or experiments.CONFLICT_TTC_S))}
                change = args.get("speed_change_kmh")
                if change:
                    if not isinstance(change, list) or len(change) != 2 or any(not isinstance(v, (int, float)) or v <= 0
                                                                               for v in change):
                        raise ValueError("speed_change_kmh is [before, after] mean speeds in km/h, both above 0.")
                    found["speed_change"] = {"before_kmh": change[0], "after_kmh": change[1],
                                             **experiments.speed_risk(float(change[0]), float(change[1])),
                                             "model": "Nilsson's power model: crashes x (v1/v0), injury crashes ^2, "
                                                      "fatal crashes ^4 (Handbook of Transportation Science p92-93)"}
                return found
            if tool == "forecast":
                interval, horizon = int(args.get("interval_min") or 1), int(args.get("horizon") or 10)
                if not 1 <= horizon <= 60:
                    raise ValueError("horizon must be 1 to 60 intervals.")
                values = experiments.series(kpis, str(args.get("what")), args.get("road") or None, interval)
                return {**head, "what": args.get("what"), "road": args.get("road"), "interval_min": interval,
                        "recent": [round(v, 1) for v in values[-12:]], **stats.holt(values, horizon)}
            counts, profiles = (network_tools._parsed_json(v) if isinstance(v, str) else v
                                for v in (args.get("counts"), args.get("profiles")))
            return {**head, **experiments.check_calibration(kpis, counts, profiles)}
        if tool in ("junction_los", "diagnose_signal", "check_run_health", "emissions_report"):
            test = self._run_test(str(args.get("id") or ""))
            kpis, head = test["kpis"], {"run": test["id"], "seed": test["seed"], "simulated": test["simulated"]}
            if tool == "junction_los":
                return {**head, **analysis.junction_los(kpis)}
            if tool == "diagnose_signal":
                return {**head, **analysis.diagnose_signal(kpis, str(args["junction"]) if args.get("junction") else None)}
            if tool == "check_run_health":
                return {**head, **analysis.run_health(kpis)}
            return {**head, **analysis.emissions_report(kpis)}
        if tool == "resilience":
            if not args.get("baseline") or not args.get("disrupted"):
                raise ValueError("Give the baseline run and the disrupted run (ids from read_results).")
            base, hit = (self._run_test(str(args[key])) for key in ("baseline", "disrupted"))
            if base is hit:
                raise ValueError("Give two different runs: the undisrupted one and the disrupted one.")
            found = {"baseline": base["id"], "disrupted": hit["id"],
                     **analysis.resilience(base["kpis"], hit["kpis"], str(args.get("measure") or "throughput"))}
            if base["seed"] != hit["seed"]:
                found["caution"] = (f"Different seeds ({base['seed']} and {hit['seed']}): part of the difference is chance. "
                                    "Run the disrupted case on the baseline's seed.")
            return found
        if tool == "power_analysis":
            return self._power_analysis(args)
        if tool == "rank_scenarios":
            return self._rank_scenarios(args)
        if tool == "preflight_check":
            return self._preflight(args, check_only)
        if tool == "test_agent_compliance":
            return self._compliance(args, check_only)
        if tool == "read_decisions":
            return self._read_decisions(args)
        if tool == "decision_sensitivity":
            return self._decision_sensitivity(args, check_only)
        if tool == "critical_links":
            demand = experiments.at_busiest_hour(self._pair_demand())
            flows = {(pair.origin, pair.destination): pair.hourly for pair in demand}
            area = network_tools._parsed_json(args["area"]) if isinstance(args.get("area"), str) else args.get("area")
            return network_tools.critical_links_plan(self.network, flows, args.get("limit") or 10, area)
        if tool == "benefit_cost":
            return self._benefit_cost(args)
        if tool == "design_audit":
            return design_checks.audit(self.network, self.vehicle_modes.modes)
        if tool == "weaving_los":
            return self._weaving_los(args)
        if tool == "metro_report":
            if not self.network.metro_lines:
                raise ValueError("No metro lines: deploy one first (Edit Network > Metro).")
            return self._metro_report()
        if tool == "accessibility_report":
            measured = None
            if args.get("id"):
                observed = self._run_test(str(args["id"]))["kpis"]["observed"]
                measured = {link: mean for link, (count, mean) in observed.get("traversals", {}).items() if count >= 3}
            budgets = tuple(float(b) for b in (args.get("budgets_min") or (15, 30)))
            return accessibility.report(self.network, measured, budgets, float(args.get("beta_per_min") or 0.1),
                                        str(args["close_road"]) if args.get("close_road") else None)
        if tool == "regress":
            return self._regress(args)
        if tool == "set_extension":
            extension = self.extensions.find(str(args.get("name", "")))
            settings = args.get("settings") or {}
            if isinstance(settings, str):
                settings = network_tools._parsed_json(settings)
            self.extensions.set_settings(extension, settings if isinstance(settings, dict) else {}, check_only=True)
            if check_only:
                return {"ok": True}
            extension.check.setChecked(network_tools._bool(args.get("on")))
            self.extensions.set_settings(extension, settings if isinstance(settings, dict) else {})
            return {"extension": extension.name, "ticked": extension.on, "settings": dict(self.extensions.settings())[extension.name],
                    "description": getattr(extension.module, "DESCRIPTION", "") or (extension.module.__doc__ or "").split("\n\n")[0]}
        if tool == "read_extensions":
            dispatch = self._extension("dispatch_emergency")
            return {"extensions": self.extensions.describe(),
                    "emergency_schedules": [{"from": origin, "to": destination, "every_min": every, "after_min": after}
                                            for origin, destination, every, after in (dispatch.module.fleet() if dispatch else [])]}
        if tool == "schedule_emergency":
            return self._schedule_emergency(args, check_only)
        if tool == "replay_run":
            path = Path(str(args.get("path") or ""))
            if not path.is_file():
                raise ValueError(f"No workbook at {path}: give the path of a batch run's workbook (.xlsx).")
            return {"ok": True} if check_only else self.replay_run(path)
        if tool == "start_batch":
            batch = self._extension("batch_run")
            if batch is None:
                raise ValueError("The Batch Run extension didn't load: see the Extensions tab.")
            test_tsp, test_devl = (network_tools._bool(args.get(key)) for key in ("test_tsp", "test_devl"))
            if check_only:  # what run_batch checks, before the approval card
                if self.runner is not None or self._testing:
                    raise ValueError("A run or test is going on: stop it first (the menu bar's Stop), then start the batch.")
                if not self._pair_demand():
                    raise ValueError("There's no demand to run: generate or enter some first.")
                if test_devl:
                    self._emergency_fleet()
            try:
                minutes = int(float(args.get("duration_min") or 0))
            except (TypeError, ValueError):
                raise ValueError("duration_min is a whole number of minutes.") from None
            seeds = args.get("seeds") or ""  # a list as the schema asks, or text like "1-4" as the panel takes
            seeds = ", ".join(map(str, experiments.seeds_from(seeds))) if isinstance(seeds, list) else str(seeds)
            return batch.module.start(seeds, minutes, test_tsp, test_devl, check_only)
        if self.runner is not None or self._testing:
            raise ValueError("A run is going on: stop it first (the menu bar's Stop), then test.")
        duration = float(args.get("duration_min") or (30 if tool in ("assign_routes", "build_scenario") else
                                                      20 if tool == "parameter_sensitivity" else 0))
        if not 1 <= duration <= experiments.MAX_MINUTES:
            raise ValueError(f"duration_min must be 1 to {experiments.MAX_MINUTES} simulated minutes.")
        if not 0 <= float(args.get("measure_from_min") or 0) < duration:
            raise ValueError("measure_from_min must be 0 or more and less than duration_min.")
        modes = deepcopy(self.vehicle_modes.modes)
        # A test (at most MAX_MINUTES) of daily demand runs its busiest hour, as the demand check sizes it: from
        # midnight it would see the night's few trips and report no congestion
        raw = self._pair_demand()
        peak_hour, demand = experiments.busiest_hour(raw), experiments.at_busiest_hour(raw)
        if tool == "assign_routes":
            return self._assign_routes(args, demand, modes, duration, check_only)
        if tool == "build_scenario":
            return self._build_scenario(args, check_only)
        if tool == "optimise_signals":
            return self._optimise_signals(args, demand, modes, duration, check_only)
        if tool == "parameter_sensitivity":
            return self._parameter_sensitivity(args, demand, modes, duration, check_only)
        if tool == "run_simulation":
            if not demand:
                raise ValueError(f"There's no demand to test with: the OD matrix has no {self.od_matrix.mode} demand "
                                 "(a Run uses the kind it shows). Enter or generate some first.")
            seed = int(args["seed"]) if args.get("seed") is not None else self.seed_box.value()
            if not 0 <= seed <= MAX_SEED:
                raise ValueError(f"seed must be 0 to {MAX_SEED:,}.")
            incidents = experiments.incidents_from(network_tools._parsed_json(args["incidents"])
                                                   if isinstance(args.get("incidents"), str) else args.get("incidents"))
            weather = str(args.get("weather") or "clear").lower()
            meters = experiments.meters_from(network_tools._parsed_json(args["meters"])
                                             if isinstance(args.get("meters"), str) else args.get("meters"))
            lateral = args.get("lateral") or False
            if not isinstance(lateral, bool):
                raise ValueError("lateral must be true or false.")
            experiments.apply_conditions(Simulation(self.network, [], modes), incidents, weather, meters)  # checked
            if check_only:
                return {"ok": True}
            network, override = deepcopy(self.network), self.extensions.signal_override(agent=self._waiting_agent())
            model = self._deciding()[0]
            settings = self._run_settings(None, seed=seed, extra=[("Kind", "headless test (run_simulation, by the assistant)"),
                                                                    ("Duration (simulated)", results.clock(duration * 60)),
                                                                    ("AI model deciding TSP", model),
                                                                    ("Weather", weather),
                                                                    ("Lateral position (sublane)", "on" if lateral else "off"),
                                                                    ("Incidents", json.dumps(incidents) if incidents else "none"),
                                                                    ("Ramp meters", json.dumps(meters) if meters else "none")])
            kpis, path, simulated = self._in_background(
                lambda progress, cancelled: self._one_run(network, demand, modes, seed, duration * 60, override, settings,
                                                          self._journal_sheet(), "Testing the network", progress, cancelled,
                                                          model=model, incidents=incidents, weather=weather,
                                                          meters=meters, lateral=lateral,
                                                          measure_from_s=float(args.get("measure_from_min") or 0) * 60),
                "Testing the network")
            run_id = f"R{sum(key.startswith('R') for key in self.tests) + 1}"
            self.tests[run_id] = {"id": run_id, "kind": "run", "what": "the network as built", "seed": seed,
                                  "simulated": results.clock(simulated), "kpis": kpis, "file": str(path or "")}
            if path:
                self.last_results, self.last_kpis = path, kpis
            self._refresh_summary()
            answer = {"run": run_id, "seed": seed, "simulated": results.clock(simulated), "file": str(path or ""),
                      **experiments.headline(kpis), **({"agent_timing": AGENT_TIMING} if override is not None else {})}
            health = analysis.run_health(kpis)  # a broken run said at once, with what to do (ChatSUMO's recovery loop)
            answer["health"] = "fine" if health["healthy"] else health["problems"]
            if peak_hour is not None:
                answer["demand_simulated"] = (f"daily demand at its busiest hour, {peak_hour:02d}:00-{peak_hour + 1:02d}:00, "
                                              "held for the whole run")
            if simulated < duration * 60 - 1:
                answer["note"] = "Stopped by the user before the end: the figures are of the part simulated."
            return answer
        return self._experiment(args, demand, modes, duration, check_only)

    # --- The Scenario Builder: a described scenario, built and tested in one go -----------------------

    def _scenario_state(self) -> dict:
        names = self.vehicle_modes.names()
        return {"has_network": bool(self.network.nodes and self.network.approach_nodes),
                "has_demand": bool(self._pair_demand()), "bus_type": next((n for n in names if "bus" in n.lower()), None)}

    def _build_scenario(self, args: dict, check_only: bool) -> dict:
        """build_scenario (scenario.py): the form completed with stated defaults, planned, and (applied) every
        step run through the ordinary tools, in order, as one undo step. A step that fails is recovered where a
        known fix exists (demand that can't be sized by saturation: sized by flow; a what-if that can't be
        built: left out), else reported; nothing after a failed network step runs."""
        spec, assumptions, problems = scenario.prepare(args, self._scenario_state())
        if spec.get("intent") == "question":
            raise ValueError("That is a question, not something to build: answer it instead.")
        if problems:
            raise ValueError(" ".join(problems))
        steps = scenario.plan(spec)
        if check_only:
            return {"ok": True, "preview": scenario.describe(spec, steps, assumptions)}
        done: list[dict] = []
        self._building = True
        try:
            for step in steps:
                try:
                    resolved = self._resolve(step, done)
                    result = self._scenario_step(step["tool"], resolved)
                    done.append({**step, "args": resolved, "ok": True, "result": result})
                except (ValueError, network_tools.ToolError, KeyError) as exc:
                    fixed = self._recover(step, exc, done, spec)
                    done.append(fixed or {**step, "ok": False, "error": str(exc)})
                    if not fixed and step["tool"] in ("place_map", "network_from_background", "generate_grid",
                                                      "generate_demand", "run_simulation"):
                        break  # nothing after it can work
        finally:
            self._building = False
            self._ai_edited("generate_grid" if any(s["tool"] in ("generate_grid", "network_from_background") and s.get("ok")
                                                   for s in done) else "build_scenario")
        files = [s["result"]["file"] for s in done if s.get("ok") and isinstance(s.get("result"), dict)
                 and s["result"].get("file")]
        return {"report": scenario.report(spec, done), "assumptions": assumptions, "files": files,
                "steps": [{"tool": s["tool"], "why": s["why"], "ok": s["ok"], **({"error": s["error"]} if not s["ok"] else {})}
                          for s in done],
                "note": "Built as one undo step (Ctrl+Z undoes all of it). The numbers are the tools' own; read the "
                        "workbooks for the detail."}

    def _scenario_step(self, tool: str, args: dict) -> dict:
        """One planned step through the tool's own path (the Demand Generator, Mode Behavior, the run tools,
        or a checked network edit)."""
        args = network_tools.normalise(tool, args)
        if tool in network_tools.OD_TOOLS:
            return self._od_command(tool, args)
        if tool in network_tools.TYPE_TOOLS:
            return self._types_command(tool, args)
        if tool in network_tools.RUN_TOOLS:
            return self._runs_command(tool, args)
        network_tools.prepare(self.network, tool, args)
        result = network_tools.run_tool(self.network, tool, args)
        self._ai_edited(tool)
        return result

    def _busiest(self, done: list[dict]) -> tuple[list[str], list[str]]:
        """(roads, junctions) by delay in the build's baseline run, busiest first; before it, the junctions
        the demand loads most."""
        run = next((s["result"] for s in reversed(done) if s["tool"] == "run_simulation" and s.get("ok")), None)
        if run:
            rows = self.tests[run["run"]]["kpis"]["roads"]
            roads = [r["road"] for r in sorted(rows, key=lambda r: -r["delay_veh_h"]) if r["road"] in self.network.roads]
            at: dict = defaultdict(float)
            for r in rows:
                road = self.network.roads.get(r["road"]) or self.network.approach_roads.get(r["road"])
                for node in (road.from_node, road.to_node) if road else ():
                    if node in self.network.nodes:
                        at[node] += r["delay_veh_h"]
            return roads, sorted(at, key=lambda n: -at[n])
        demand = next((s["result"] for s in done if s["tool"] == "generate_demand" and s.get("ok")), {})
        return list(self.network.roads)[:1], list((demand.get("degree_of_saturation") or {}) or self.network.nodes)

    def _resolve(self, step: dict, done: list[dict]) -> dict:
        """The step's arguments with its @ targets filled in from the network and the build so far."""
        nodes = self.network.nodes

        def token(value: str):
            roads, junctions = self._busiest(done)
            if value == "@junctions":
                return list(nodes)
            if value == "@signalised":
                return [n for n, node in nodes.items() if node.control == "signalised"]
            if value == "@busiest_road":
                return roads[:1] or list(self.network.roads)[:1]
            if value == "@busiest_junction":
                return junctions[:1] or list(nodes)[:1]
            if value == "@busiest_signalised":
                return [n for n in junctions if nodes[n].control == "signalised"][:3] or \
                    [n for n, node in nodes.items() if node.control == "signalised"][:1]
            if value == "@bus_type":
                return self._scenario_state()["bus_type"]
            if value.startswith("@lanes:"):
                return self.network.road(value[7:]).lanes_per_direction
            if value == "@optimised":
                found = next((s["result"] for s in done if s["tool"] == "optimise_signals" and s.get("ok")), None)
                if not found:
                    raise ValueError("The signal search didn't run, so there's no optimised plan to test.")
                return [{"tool": c["tool"], "args": c["args"]} for c in found["apply_with"]]
            raise ValueError(f"Unknown target {value}.")

        def fill(value):
            if isinstance(value, str) and value.startswith("@"):
                return token(value)
            if isinstance(value, dict):
                return {k: fill(v) for k, v in value.items()}
            if isinstance(value, list):
                return [fill(v) for v in value]
            return value

        if step["tool"] == "run_experiment":  # the what-ifs as scenarios against the network as built
            args = step["args"]
            variants = [scenario.variant(item, token) for item in args["what_if"]]
            return {"scenarios": [{"name": "As built"}, *variants], "seeds": list(range(1, args["seeds"] + 1)),
                    "duration_min": args["duration_min"], "question": "What-ifs from the Scenario Builder"}
        return fill(step["args"])

    def _recover(self, step: dict, exc: Exception, done: list[dict], spec: dict) -> dict | None:
        """A known fix for a failed step, run now; None if there is none (the step is reported failed)."""
        tool, error = step["tool"], str(exc)
        if error.startswith("Nothing would change"):  # already as asked (the grid on screen is that grid): done
            return {**step, "ok": True, "result": {"unchanged": error}}
        if tool == "generate_demand" and "degree_of_saturation" in step["args"]:
            # Nothing to size by (no junction where traffic waits): a flow per boundary end by the level instead
            per_end = {"light": 150, "medium": 300, "heavy": 450, "near capacity": 550, "over capacity": 700}[spec["traffic"]]
            args = {k: v for k, v in step["args"].items() if k != "degree_of_saturation"}
            args["total"] = per_end * len(self.network.approach_nodes) * (24 if args.get("demand") == "daily" else 1)
            try:
                result = self._scenario_step(tool, args)
            except (ValueError, network_tools.ToolError):
                return None
            result["recovered"] = f"Sized by flow ({per_end} veh/h an end) because {error}"
            return {**step, "args": args, "ok": True, "result": result}
        if tool == "run_experiment":  # leave out the what-ifs that can't be built, and test the rest
            args = self._resolve(step, done)
            kept = [sc for sc in args["scenarios"][1:] if self._buildable(sc)]
            if kept and len(kept) < len(args["scenarios"]) - 1:
                try:
                    result = self._scenario_step(tool, {**args, "scenarios": [args["scenarios"][0], *kept]})
                except (ValueError, network_tools.ToolError):
                    return None
                result["recovered"] = f"Left out what-ifs that couldn't be built ({error})"
                return {**step, "args": args, "ok": True, "result": result}
        return None

    def _buildable(self, spec: dict) -> bool:
        try:
            experiments.scenario_network(self.network, experiments.scenario_from(spec, 0), network_tools.SCENARIO_TOOLS)
            self.extensions.overrides_for(spec.get("extensions") or [])
        except ValueError:
            return False
        return True

    def _regress(self, args: dict) -> dict:
        """regress: OLS of y on x over rows given, or over an experiment's runs (their figures and demand_scale)."""
        rows = network_tools._parsed_json(args["data"]) if isinstance(args.get("data"), str) else args.get("data")
        if args.get("experiment"):
            test = self.tests.get(str(args["experiment"]).upper())
            if test is None or test["kind"] != "experiment":
                raise ValueError(f"No experiment called {args['experiment']}.")
            scale = {sc["name"]: sc["demand_scale"] for sc in test["experiment"]["scenarios"]}
            rows = [{**run["measures"], "demand_scale": scale[run["scenario"]], "seed": run["seed"]}
                    for run in test["experiment"]["runs"]]
        if not isinstance(rows, list) or not rows or not all(isinstance(row, dict) for row in rows):
            raise ValueError("Give data (a list of objects) or an experiment id.")
        def keyed(name: str) -> str:  # a comparison's label ("Delay") is its runs' key (delay_veh_h)
            if args.get("experiment") and not any(name in row for row in rows):
                try:
                    return self._figure_key(name)
                except ValueError:
                    return name
            return name

        y, xs = keyed(str(args.get("y"))), [keyed(str(x)) for x in network_tools._ids(args.get("x") or [])]
        logs = {keyed(name) for name in network_tools._ids(args.get("log") or [])}
        names = [y, *xs]
        missing = [name for name in names if not any(name in row for row in rows)]
        if missing:
            raise ValueError(f"No {', '.join(missing)} in the data; it has {', '.join(sorted({k for r in rows for k in r}))}.")
        table = []
        for row in rows:
            values = [row.get(name) for name in names]
            if any(not isinstance(v, (int, float)) or (name in logs and v <= 0) for v, name in zip(values, names)):
                continue
            table.append([math.log(v) if name in logs else float(v) for v, name in zip(values, names)])
        fit = stats.ols([row[1:] for row in table], [row[0] for row in table],
                        [f"ln {x}" if x in logs else x for x in xs])
        fit["y"] = f"ln {y}" if y in logs else y
        fit["rows_used"], fit["rows_skipped"] = len(table), len(rows) - len(table)
        return fit

    def _assign_routes(self, args, demand, modes, duration, check_only) -> dict:
        """assign_routes: route choice on the network, its link times by the method of successive averages over
        headless runs (Handbook of Transportation Science p391–392): each iteration runs with the times so far,
        measures each link's traversal time (and each move's: Graph.move_time), and moves the times 1/n of the way
        to it."""
        if network_tools._bool(args.get("off", False)):
            if check_only:
                if not self.network.route_choice:
                    raise ValueError("Nothing would change: route choice is off already.")
                return {"ok": True}
            self.network.route_choice = {}
            self._ai_edited("assign_routes")
            return {"route_choice": "off", "note": "Every vehicle takes the fastest route again."}
        iterations = int(args.get("iterations", 4) if args.get("iterations") is not None else 4)
        choice = {"theta_per_min": float(args.get("theta_per_min") or 0.5), "routes": int(args.get("routes") or 3),
                  "link_times_s": {}}
        problem = None if 0 <= iterations <= 10 else "iterations must be 0 to 10."
        problem = problem or route_choice_problem(choice)
        if problem:
            raise ValueError(problem)
        if not 1 <= duration <= experiments.MAX_MINUTES:
            raise ValueError(f"duration_min must be 1 to {experiments.MAX_MINUTES} simulated minutes.")
        if iterations and not demand:
            raise ValueError("There's no demand to assign: enter or generate some first (or give iterations 0).")
        if check_only:
            return {"ok": True}
        seed = int(args["seed"]) if args.get("seed") is not None else self.seed_box.value()
        until_gap = float(args["until_gap"]) if args.get("until_gap") is not None else None
        if until_gap is not None and not 0 < until_gap <= 0.5:
            raise ValueError("until_gap must be above 0 and at most 0.5.")
        override = self.extensions.signal_override(agent=self._waiting_agent())
        network, before = deepcopy(self.network), self._snapshot()
        choice, history = self._in_background(
            lambda progress, cancelled: self._equilibrium(network, demand, modes, seed, duration, iterations, choice,
                                                          override, progress, cancelled, until_gap), "Assigning routes")
        if self._snapshot() != before:  # edited, undone or another project opened while it ran
            raise ValueError("The network or demand changed while routes were assigned, so nothing was applied: "
                             "assign them again.")
        rates = {(pair.origin, pair.destination): pair.hourly for pair in experiments.at_busiest_hour(demand)}
        self.network.route_choice = choice
        self._ai_edited("assign_routes")
        graph = Graph(self.network)
        busiest = sorted(rates, key=lambda pair: -rates[pair])[:5]
        shares = {f"{o}->{d}": [{"share": round(share, 3), "via": " ".join([o] + [link.end for link in path])}
                                for share, path in graph.route_shares(o, d)] for o, d in busiest}
        gaps = [step["relative_gap"] for step in history if step["relative_gap"] is not None]
        return {"route_choice": {k: v for k, v in choice.items() if k not in ("link_times_s", "turn_times_s")},
                "link_times_measured": len(choice["link_times_s"]),
                "turn_times_measured": sum(map(len, choice.get("turn_times_s", {}).values())), "iterations": history, "busiest_pairs": shares,
                **({"converged": bool(gaps) and gaps[-1] <= until_gap} if until_gap is not None else {}),
                "note": "Every run now draws each vehicle's route by these shares (saved with the network; Ctrl+Z "
                        "undoes it). The relative gap is the share of the flow off its logit share: towards 0, the routes are settling "
                        "(stochastic user equilibrium)."}

    def _equilibrium(self, network, demand, modes, seed, duration, iterations, choice, override, progress, cancelled,
                     until_gap=None, label="Route choice") -> tuple[dict, list]:
        """Route choice towards equilibrium by the method of successive averages over headless runs (Handbook of
        Transportation Science p391-392): each iteration runs with the link times so far, measures each link's
        traversal time and moves the times 1/n of the way to it, every run on the same seed (common random numbers:
        a new seed each time kept the gap at 0.05-0.09 by noise alone). The relative gap is the share of the flow off
        its logit share at the times just measured (½Σ|P − P_logit|, 0 at stochastic user equilibrium); the time spent
        over that on the fastest route (`excess_cost`) has a positive floor under logit choice, so it never falls to 0.
        `until_gap` stops it once the gap is that low. Returns (route choice, each iteration's figures). `network` is
        changed (its route_choice)."""
        choice = deepcopy(choice)
        rates = {(pair.origin, pair.destination): pair.hourly for pair in experiments.at_busiest_hour(demand)}
        history = []
        for n in range(1, iterations + 1):
            if cancelled():
                break
            network.route_choice = deepcopy(choice)
            kpis = self._quick_run(network, demand, modes, seed, duration * 60, override, progress, cancelled,
                                   f"{label}, iteration {n} of {iterations}")
            observed = kpis["observed"]
            measured = {link: mean for link, (count, mean) in observed["traversals"].items() if count >= 3}
            old = {link: choice["link_times_s"].get(link, info["free_s"]) for link, info in observed["links"].items()}
            new = dict(choice["link_times_s"])
            for link, mean in measured.items():
                new[link] = round(old[link] + (mean - old[link]) / n, 2)
            graph = Graph(network)  # the routes drivers took, by the times so far
            # Each move's own time (the link and that move's junction delay, counted once: Graph.move_time)
            turns = deepcopy(choice.get("turn_times_s", {}))
            for link, moves in observed.get("turn_traversals", {}).items():
                for after, (count, mean) in moves.items():
                    if count >= 3:
                        was = graph.move_time(graph.links[link], graph.links[after])
                        turns.setdefault(link, {})[after] = round(was + (mean - was) / n, 2)
            network.route_choice = {**choice, "link_times_s": new, "turn_times_s": turns}
            priced = Graph(network)
            spent = best = off = total = 0.0
            theta = float(choice.get("theta_per_min", 0.5))
            for pair, q in rates.items():
                options = graph.route_shares(*pair)
                costs = [priced.path_time(path) for _, path in options]
                if costs:
                    spent += q * sum(share * cost for (share, _), cost in zip(options, costs))
                    best += q * min(costs)
                    aims = path_size_logit([path for _, path in options], costs, theta)
                    off += q * sum(abs(share - aim) for (share, _), aim in zip(options, aims)) / 2
                    total += q
            gap = round(off / total, 4) if total else None
            history.append({"iteration": n, "seed": seed, "mean_trip_s": kpis["mean_trip_s"],
                            "delay_veh_h": kpis["delay_veh_h"], "links_measured": len(measured), "relative_gap": gap,
                            "excess_cost": round((spent - best) / best, 4) if best else None})
            choice["link_times_s"], choice["turn_times_s"] = new, turns
            if until_gap is not None and gap is not None and gap <= until_gap:
                break
        network.route_choice = choice
        return choice, history

    OBJECTIVES = {"delay": ("delay_veh_h", False), "trip_delay": ("mean_trip_delay_s", False),
                  "throughput": ("throughput_vph", True), "conflicts": ("conflicts", False)}

    def _optimise_signals(self, args, demand, modes, duration, check_only) -> dict:
        """optimise_signals: a genetic algorithm over fixed-time settings (Explainable AI for ITS p146–158:
        signal settings searched with the simulation as fitness). A genome per junction: its cycle and phase
        shares; greens = minimum green + share × (cycle − phases × (minimum green + amber + all-red)). Tournament
        selection, blend crossover, Gaussian mutation, one elite; every candidate scored on the same seeds."""
        import random  # noqa: PLC0415
        from traffic_simulator.signal_timing import ALL_RED_S, AMBER_S, MIN_GREEN_S  # noqa: PLC0415

        junctions = network_tools._ids(args.get("junctions") or [])
        for node_id in junctions:
            node = self.network.nodes.get(node_id)
            if node is None or node.control != "signalised":
                raise ValueError(f"{node_id} isn't a signalised junction.")
        if not junctions:
            raise ValueError("Give the signalised junctions to optimise.")
        seeds = experiments.seeds_from(args.get("seeds") or "1-3")
        population, generations = int(args.get("population") or 6), int(args.get("generations") or 3)
        objective = str(args.get("objective") or "delay")
        if objective not in self.OBJECTIVES:
            raise ValueError(f"objective must be one of {', '.join(self.OBJECTIVES)}.")
        if not 4 <= population <= 10 or not 1 <= generations <= 5 or not 1 <= len(seeds) <= 5:
            raise ValueError("population 4-10, generations 1-5, seeds 1-5.")
        runs = (population * generations + 1) * len(seeds)
        if runs > 120:
            raise ValueError(f"That is up to {runs} runs; at most 120: fewer seeds, a smaller population or fewer generations.")
        if not demand:
            raise ValueError("There's no demand to test with: enter or generate some first.")
        if check_only:
            return {"ok": True}
        key, higher = self.OBJECTIVES[objective]
        plans = Simulation(deepcopy(self.network), demand, modes).signal_plans
        nodes = {node_id: self.network.nodes[node_id] for node_id in junctions}
        rng, override = random.Random(seeds[0]), self.extensions.signal_override(agent=self._waiting_agent())

        def bounds(node):
            phases = node.signal_phases
            low = max(node.cycle_min_s, phases * (MIN_GREEN_S + AMBER_S + ALL_RED_S) + 5)
            return low, max(low + 1, node.cycle_max_s)

        def greens_of(node, cycle, shares):
            phases, spare = node.signal_phases, cycle - node.signal_phases * (MIN_GREEN_S + AMBER_S + ALL_RED_S)
            total = sum(shares) or 1.0
            return [round(MIN_GREEN_S + spare * share / total, 1) for share in shares[:phases]]

        def start(node_id):
            node, plan = nodes[node_id], plans.get(node_id, {})
            greens = list(plan.get("greens_s") or plan.get("green_times_s") or node.green_times_s)[:node.signal_phases]
            while len(greens) < node.signal_phases:
                greens.append(greens[-1] if greens else 20.0)
            low, high = bounds(node)
            cycle = min(max(sum(greens) + node.signal_phases * (AMBER_S + ALL_RED_S), low), high)
            return [cycle] + [max(g - MIN_GREEN_S, 0.5) for g in greens]

        def changes(genome):
            return [{"tool": "set_signal_timing", "args": {"junctions": [node_id], "method": "fixed",
                                                           "green_times_s": greens_of(nodes[node_id], gene[0], gene[1:])}}
                    for node_id, gene in zip(junctions, genome)]

        cache, values_of = {}, {}

        def score(genome, progress, cancelled, label):
            token = repr([[round(g, 1) for g in gene] for gene in genome])
            if token not in cache:
                network = experiments.scenario_network(self.network, experiments.Scenario("candidate", changes(genome)),
                                                       network_tools.SCENARIO_TOOLS) if genome else deepcopy(self.network)
                per_seed = {}
                for seed in seeds:
                    if cancelled():
                        return None
                    per_seed[seed] = self._quick_run(network, demand, modes, seed, duration * 60, override, progress,
                                                     cancelled, f"{label}, seed {seed}").get(key)
                values = [v for v in per_seed.values() if v is not None]
                mean = sum(values) / len(values) if values else None
                cache[token] = None if mean is None else (-mean if higher else mean)
                values_of[token] = per_seed
            return cache[token]

        def mutate(genome):
            out = []
            for node_id, gene in zip(junctions, genome):
                low, high = bounds(nodes[node_id])
                cycle = gene[0] + (rng.gauss(0, (high - low) * 0.15) if rng.random() < 0.4 else 0)
                shares = [max(0.5, g + (rng.gauss(0, 5) if rng.random() < 0.4 else 0)) for g in gene[1:]]
                out.append([min(max(cycle, low), high)] + shares)
            return out

        def work(progress, cancelled):
            baseline = score([], progress, cancelled, "Current plans")
            first = [start(node_id) for node_id in junctions]
            people = [first] + [mutate(first) for _ in range(population - 1)]
            history = []
            for generation in range(1, generations + 1):
                scored = []
                for index, genome in enumerate(people, 1):
                    value = score(genome, progress, cancelled, f"Generation {generation}, candidate {index}")
                    if value is None:
                        return baseline, scored, history
                    scored.append((value, genome))
                scored.sort(key=lambda item: item[0])
                history.append({"generation": generation, "best": round(abs(scored[0][0]), 3)})
                if generation == generations:
                    return baseline, scored, history

                def pick():
                    a, b = rng.sample(scored, 2)
                    return min(a, b, key=lambda item: item[0])[1]

                children = [scored[0][1]]
                while len(children) < population:
                    mother, father = pick(), pick()
                    weight = rng.random()
                    child = [[weight * m + (1 - weight) * f for m, f in zip(gm, gf)] for gm, gf in zip(mother, father)]
                    children.append(mutate(child))
                people = children
            return baseline, [], history

        baseline, scored, history = self._in_background(work, "Optimising signals")
        if not scored or baseline is None:
            raise ValueError("Stopped before any candidate was scored.")
        best_value, best = scored[0]
        token = repr([[round(g, 1) for g in gene] for gene in best])
        compared = stats.paired(values_of[repr([])], values_of[token])
        apply = [change["args"] for change in changes(best)]
        return {"objective": key, "higher_is_better": higher, "candidates_scored": len(cache) - 1,
                "runs": len(cache) * len(seeds), "seeds": seeds,
                "current": round(abs(baseline), 3), "best": round(abs(best_value), 3),
                "difference": None if compared["diff"] is None else {"diff": round(compared["diff"], 3),
                                                                     "ci95": round(compared["ci"], 3), "p": compared["p"],
                                                                     "clear": compared["clear"]},
                "best_plans": {a["junctions"][0]: {"green_times_s": a["green_times_s"],
                                                   "cycle_s": round(sum(a["green_times_s"]) + len(a["green_times_s"]) * (AMBER_S + ALL_RED_S), 1)}
                               for a in apply},
                "apply_with": [{"tool": "set_signal_timing", "args": a} for a in apply], "history": history,
                "note": "Apply only if the difference is clear and better; check with run_experiment on new seeds "
                        "(the search fits these seeds). The open network is unchanged.",
                **({"agent_timing": AGENT_TIMING} if override is not None else {})}

    def _weaving_los(self, args: dict) -> dict:
        """weaving_los: the HCM 2000 Ch 24 on every weaving segment, its movements from the demand (the busiest hour,
        each pair's vehicles in PCU by its split, at its peak quarter: V / PHF, Eq 24-1) on the routes a Run takes; a
        run's own speed and density beside it when one is given."""
        modes = self.vehicle_modes.modes
        rates: dict = defaultdict(float)
        for pair in experiments.at_busiest_hour(self._pair_demand()):
            pcu = sum(share / 100 * float(getattr(mode, "pcu", 1.0)) for share, mode in zip(pair.split, modes)) or 1.0
            peak = 4 * max(pair.quarters) if getattr(pair, "quarters", ()) else 1.0
            rates[(pair.origin, pair.destination)] += (pair.hourly or 0.0) * pcu * peak
        rows = weaving.report(Graph(self.network), dict(rates))
        found = {"segments": rows, "method": "HCM 2000 Chapter 24 (Eqs 24-1 to 24-6, Exhibits 24-2, 24-5 to 24-8)",
                 "flows": "pc/h at each pair's peak 15 minutes, the busiest hour, on the routes a Run takes"}
        if not rows:
            found["note"] = ("No weaving segments: no uncontrolled merge followed within 750 m by a diverge, with the "
                             "second road joining and leaving on the kerb side.")
        if args.get("id"):
            test = self._run_test(str(args["id"]))
            kpis, observed = test["kpis"], test["kpis"].get("observed", {})
            hours = float(observed.get("observed_s") or kpis.get("time_s") or 0.0) / 3600
            for row in rows:
                row.update(weaving.measured(row, observed, hours))
            found["run"] = test["id"]
        return found

    def _equilibrate_demand(self, flows: dict, args: dict, check_only: bool = False) -> tuple[dict, dict]:
        """equilibrate_demand's numbers: elastic demand (induced or suppressed traffic; Rodrigue, Geography of Transport
        Systems, and the HCM's demand elasticity): each pair's veh/h moved towards q0 × (t / t_ref)^elasticity, t its
        mean trip time in a headless run with the demand so far, t_ref in the reference run (before the change), by
        successive averages (q += (target − q) / n) until every pair is within 2 % of its target (the residual, not the
        step, which shrinks as 1/n by itself). Trips still on the road at the end count with their time so far (only
        finished trips understated a jam's times). Returns (plan, report)."""
        elasticity = float(args.get("elasticity", -0.5))
        iterations = int(args.get("iterations") or 3)
        duration = float(args.get("duration_min") or 30)
        if not -2.0 <= elasticity < 0:
            raise ValueError("elasticity must be below 0 and at least -2 (travel time elasticity of demand: about -0.3 "
                             "to -0.8 is usual; -0.5 by default).")
        if not 1 <= iterations <= 6 or not 5 <= duration <= experiments.MAX_MINUTES:
            raise ValueError(f"iterations 1-6, duration_min 5-{experiments.MAX_MINUTES}.")
        if not args.get("reference"):
            raise ValueError("Give reference: the run (R1...) of the network before the change, whose trip times the "
                             "demand you have was made for.")
        reference = self._run_test(str(args["reference"]))
        if self.runner is not None or self._testing:
            raise ValueError("A run is going on: stop it first.")
        def times(observed) -> dict:  # each pair's trip times, the unfinished ones' so far too
            found: dict = defaultdict(list)
            for origin, destination, _kind, trip, *_ in [*observed["trips"], *observed.get("unfinished", [])]:
                found[(origin, destination)].append(trip)
            return found

        before = times(reference["kpis"]["observed"])
        reference_s = {pair: statistics.fmean(found) for pair, found in before.items() if len(found) >= 2}
        if not any(pair in reference_s for pair in flows):
            raise ValueError(f"{reference['id']} has no trips of these pairs (2 or more each): run the reference again.")
        if check_only:
            return {}, {"ok": True}
        pairs = {(p.origin, p.destination): p for p in self._pair_demand()}
        network, modes = deepcopy(self.network), deepcopy(self.vehicle_modes.modes)
        override = self.extensions.signal_override(agent=self._waiting_agent())
        seed = reference["seed"]

        def work(progress, cancelled):
            q, history = dict(flows), []
            for n in range(1, iterations + 1):
                if cancelled():
                    break
                demand = [replace(pairs[pair], hourly=value) if pair in pairs else
                          PairDemand(pair[0], pair[1], value, None, [], "Random", (100,)) for pair, value in q.items()]
                kpis = self._quick_run(network, demand, modes, seed, duration * 60, override, progress, cancelled,
                                       f"Elastic demand, iteration {n} of {iterations}")
                now, moved, gap = times(kpis["observed"]), 0.0, 0.0
                for pair, base in flows.items():
                    if pair in reference_s and len(now.get(pair, ())) >= 2:
                        target = base * (statistics.fmean(now[pair]) / reference_s[pair]) ** elasticity
                        step = (target - q[pair]) / n
                        gap = max(gap, abs(target - q[pair]) / max(q[pair], 1.0))
                        moved = max(moved, abs(step) / max(q[pair], 1.0))
                        q[pair] += step
                history.append({"iteration": n, "total_vph": round(sum(q.values())), "largest_change": round(moved, 3), "largest_gap": round(gap, 3),
                                "mean_trip_s": kpis["mean_trip_s"]})
                if gap <= 0.02:  # at equilibrium: its runs' times give back the demand they were run with
                    break
            return q, history

        q, history = self._in_background(work, "Elastic demand")
        plan = {pair: round(value) for pair, value in q.items()}
        total, start = sum(plan.values()), round(sum(flows.values()))
        return plan, {"elasticity": elasticity, "reference": reference["id"], "iterations": history,
                      "total_vph_before": start, "total_vph_after": total,
                      "changed_percent": round(100 * (total - start) / start, 1) if start else None,
                      "settled": bool(history) and history[-1]["largest_change"] <= 0.02,
                      "note": "Pairs whose trips got faster than in the reference gain demand (induced traffic); slower "
                              "ones lose it. Test the design again with this demand; Ctrl+Z puts the demand back."}

    def _experiment_groups(self, identifier, figure) -> tuple[dict, str, bool | None]:
        """One figure of an experiment of this session: ({scenario: {seed: value}}, its label, higher is better)."""
        test = self.tests.get(str(identifier or "").upper())
        if test is None or test["kind"] != "experiment":
            raise ValueError(f"No experiment called {identifier or '(none given)'}; this session's: "
                             f"{', '.join(k for k, t in self.tests.items() if t['kind'] == 'experiment') or 'none yet'}.")
        experiment = test["experiment"]
        labels = {key: (label, up) for key, label, _unit, up in experiments.MEASURES + experiments.AGENT_MEASURES}
        keys = list(dict.fromkeys(key for run in experiment["runs"] for key in run["measures"]))
        wanted = str(figure or "").strip().lower()
        key = next((key for key in keys if wanted in (key.lower(), labels.get(key, (key,))[0].lower())), None)
        if key is None:
            raise ValueError(f"No figure {figure!r} in {test['id']}; it has: "
                             + ", ".join(labels.get(key, (key,))[0] for key in keys) + ".")
        groups = {spec["name"]: {} for spec in experiment["scenarios"]}
        for run in experiment["runs"]:
            groups[run["scenario"]][run["seed"]] = run["measures"].get(key)
        label, up = labels.get(key, (key, None))
        return groups, label, up

    @staticmethod
    def _baseline_of(names: list[str], scenario: str) -> str:
        """The baseline a scenario is compared with: the first design under the same condition ("B | rain" against
        "A | rain", not the first condition's "A | peak"), or the first scenario when there are no conditions."""
        if " | " in scenario and " | " in names[0]:
            return f"{names[0].split(' | ')[0]} | {scenario.split(' | ', 1)[1]}"
        return names[0]

    def _power_analysis(self, args: dict) -> dict:
        """power_analysis: seeds needed for a paired comparison, from an experiment's per-seed differences or from
        an sd and a difference given (stats.paired_power, seeds_for_power, detectable)."""
        power = float(args.get("power") or 0.8)
        if not 0.5 <= power <= 0.99:
            raise ValueError("power must be 0.5 to 0.99 (0.8 is usual).")
        found: dict = {}
        if args.get("experiment"):
            groups, label, _ = self._experiment_groups(args["experiment"], args.get("figure") or "")
            names = list(groups)
            if len(names) < 2:
                raise ValueError("That experiment has one scenario: nothing to compare.")
            other = str(args.get("scenario") or names[1])
            baseline = self._baseline_of(names, other)
            if other not in groups or other == baseline or baseline not in groups:
                raise ValueError(f"scenario is one of {', '.join(names[1:])} (each is compared with {names[0]}"
                                 f"{', under its own condition' if ' | ' in names[0] else ''}).")
            base = groups[baseline]
            shared = [s for s in base if s in groups[other] and None not in (base[s], groups[other][s])]
            if len(shared) < 2:
                raise ValueError("Fewer than 2 seeds ran both: run the experiment on 2 or more shared seeds first.")
            differences = [groups[other][s] - base[s] for s in shared]
            sd, observed = statistics.stdev(differences), statistics.fmean(differences)
            found.update(figure=label, baseline=baseline, scenario=other, seeds_now=len(shared),
                         observed_difference=round(observed, 3), sd_of_differences=round(sd, 3))
            difference = float(args["difference"]) if args.get("difference") is not None else observed
        else:
            if args.get("sd") is None or args.get("difference") is None:
                raise ValueError("Give an experiment (and figure), or sd and difference.")
            sd, difference = float(args["sd"]), float(args["difference"])
            if sd < 0:
                raise ValueError("sd can't be below zero.")
            found.update(sd_of_differences=sd)
        seeds = int(args.get("seeds") or found.get("seeds_now") or 0)
        found.update(target_difference=round(difference, 3), power=power,
                     seeds_needed=stats.seeds_for_power(sd, difference, power),
                     seeds_for_90_percent=stats.seeds_for_power(sd, difference, 0.9),
                     curve=[{"seeds": n, "power": round(stats.paired_power(sd, difference, n), 2)}
                            for n in (3, 5, 8, 10, 15, 20, 30) if difference])
        if seeds >= 2:
            found.update(seeds=seeds, power_with_them=round(stats.paired_power(sd, difference, seeds), 2),
                         smallest_detectable=round(stats.detectable(sd, seeds, power), 3))
        found["note"] = ("Paired t-test at 5 %, two-sided, on the per-seed differences (common random numbers). "
                         "Sized from the observed difference it is a pilot estimate: say the difference that matters.")
        if found["seeds_needed"] is None and difference:
            found["note"] += " Over 1,000 seeds: the difference is too small to find against this noise."
        return found

    def _benefit_cost(self, args: dict) -> dict:
        """benefit_cost: a scenario of an experiment against its baseline in money: per seed, the person-hours of delay,
        vehicle-km and CO2 saved each simulated hour, valued, over a year's hours and the appraisal years; net present
        value and benefit-cost ratio at each discount rate (7 % as NSW Treasury requires, 3.5 % the UK's, 4 % elsewhere:
        Hensher, Bus Transport, p358), with the 95 % interval of the yearly benefit over the seeds."""
        test = self.tests.get(str(args.get("experiment") or "").upper())
        if test is None or test["kind"] != "experiment":
            raise ValueError("Give the experiment (E1...) whose scenario to appraise.")
        experiment = test["experiment"]
        names = [spec["name"] for spec in experiment["scenarios"]]
        scenario = str(args.get("scenario") or (names[1] if len(names) > 1 else ""))
        baseline = self._baseline_of(names, scenario)
        if scenario not in names[1:] or scenario == baseline or baseline not in names:
            raise ValueError(f"scenario is one of {', '.join(names[1:]) or '(none: one scenario)'} (each against {names[0]}"
                             f"{', under its own condition' if ' | ' in names[0] else ''}).")
        try:
            capital = float(args["capital_cost"])
        except (KeyError, TypeError, ValueError):
            raise ValueError("Give capital_cost: what the change costs to build, in your currency.") from None
        values = {key: float(args.get(key) if args.get(key) is not None else default) for key, default in (
            ("annual_cost", 0.0), ("value_of_time_per_h", 15.0), ("cost_per_veh_km", 0.15), ("carbon_per_tonne", 100.0),
            ("hours_per_year", 500.0), ("years", 30.0))}
        rates = [float(r) for r in (args.get("discount_rates_percent") or (3.5, 4.0, 7.0))]
        if capital < 0 or values["hours_per_year"] <= 0 or not 1 <= values["years"] <= 100 or not rates \
                or any(not 0 <= r <= 20 for r in rates):
            raise ValueError("capital_cost 0 or more, hours_per_year above 0, years 1-100, discount rates 0-20 %.")
        hours = (experiment["duration_s"] - experiment.get("measure_from_s", 0.0)) / 3600  # the figures' window
        runs = {(run["scenario"], run["seed"]): run["measures"] for run in experiment["runs"]}
        yearly = []
        for seed in experiment["seeds"]:
            base, other = runs.get((baseline, seed)), runs.get((scenario, seed))
            if base is None or other is None:
                continue

            def saved(key, base=base, other=other):
                return ((base.get(key) or 0.0) - (other.get(key) or 0.0)) / hours  # a measured hour's worth

            # Everyone's delay, not only finished trips': on board (people_delay_h counts only trips that
            # ended, so a scenario letting more finish showed more delay) and waiting to get on the road
            people = "person_delay_on_board_h" if "person_delay_on_board_h" in base else "people_delay_h"
            occupancy = (base.get("people_per_h") or 0.0) / max(base.get("throughput_vph") or 0.0, 1.0) or 1.0
            hourly = ((saved(people) + saved("entry_wait_veh_h") * occupancy) * values["value_of_time_per_h"]
                      + saved("vehicle_km") * values["cost_per_veh_km"]
                      + saved("co2_kg") / 1000 * values["carbon_per_tonne"])
            yearly.append(hourly * values["hours_per_year"])
        if not yearly:
            raise ValueError("No seed ran both the baseline and that scenario.")
        mean = statistics.fmean(yearly)
        half = experiments.summary(yearly)["ci95"]

        def appraise(benefit, rate):
            factor = values["years"] if rate == 0 else (1 - (1 + rate / 100) ** -values["years"]) / (rate / 100)
            costs = capital + values["annual_cost"] * factor
            return {"rate_percent": rate, "pv_benefits": round(benefit * factor), "pv_costs": round(costs),
                    "npv": round(benefit * factor - costs), "bcr": round(benefit * factor / costs, 2) if costs else None}

        by_rate = [appraise(mean, rate) for rate in rates]
        ranges = None
        if half is not None:
            ranges = [{"rate_percent": rate, "bcr_low": appraise(mean - half, rate)["bcr"],
                       "bcr_high": appraise(mean + half, rate)["bcr"]} for rate in rates]
        flips = len({row["npv"] > 0 for row in by_rate}) > 1
        served = [statistics.fmean(runs[(name, seed)].get("arrived") or 0.0 for seed in experiment["seeds"]
                                   if (name, seed) in runs) for name in (baseline, scenario)]
        caution = None
        if served[0] and abs(served[1] - served[0]) > 0.05 * served[0]:  # more or fewer trips, not the same trips faster
            caution = (f"{scenario} completes {(served[1] - served[0]) / served[0]:+.0%} trips against {baseline}: its "
                       "vehicle-km and delay differ partly because it serves a different number of trips, not only "
                       "because each is faster or slower.")
        return {"experiment": test["id"], "scenario": scenario, "baseline": baseline, "seeds": len(yearly),
                **({"caution": caution} if caution else {}),
                "yearly_benefit": round(mean), "yearly_benefit_ci95": None if half is None else round(half),
                "by_discount_rate": by_rate, "bcr_interval_over_seeds": ranges, "decision_flips_with_rate": flips,
                "assumptions": {**values, "discount_rates_percent": rates},
                "note": "Benefits are person-hours of delay (everyone on board, finished or not, and waiting to enter), "
                        "vehicle-km and car CO2 saved over the measured hours, after any warm-up (conflicts aren't "
                        "crashes, so "
                        "they aren't valued). value_of_time_per_h, cost_per_veh_km, carbon_per_tonne and "
                        "hours_per_year (default 500: two peak hours, 250 days) are assumptions: give your own."}

    def _rank_scenarios(self, args: dict) -> dict:
        """rank_scenarios: all of an experiment's scenarios on one figure (stats.anova, friedman, all_pairs)."""
        groups, label, up = self._experiment_groups(args.get("experiment"), args.get("figure"))
        if len(groups) < 2:
            raise ValueError("That experiment has one scenario: nothing to rank.")
        overall = stats.anova(groups)
        ranked = stats.all_pairs(groups, higher_is_better=up)
        friedman = stats.friedman(groups)
        r = lambda value: round(value, 4) if isinstance(value, float) else value  # noqa: E731
        answer = {"figure": label, "higher_is_better": up,
                  "anova": None if overall is None else {"design": overall["design"], "f": r(overall["f"]),
                                                         "p": r(overall["p"])},
                  "friedman": None if friedman is None else {k: r(v) if not isinstance(v, dict) else v
                                                             for k, v in friedman.items()}}
        if friedman is None:
            answer["kruskal_wallis"] = stats.kruskal_wallis({name: list(values.values()) for name, values in groups.items()})
        if ranked:
            answer["ranked"] = [{**row, "mean": r(row["mean"])} for row in ranked["ranked"]]
            answer["pairs"] = [{k: r(v) for k, v in pair.items()} for pair in ranked["pairs"]]
            answer["confidence_each"] = ranked["confidence_each"]
        answer["note"] = ("Scenarios sharing a letter can't be told apart. Friedman ranks within each seed, so it holds "
                          "when the figure is skewed or the seeds few; trust it over ANOVA when they disagree.")
        return answer

    def _preflight(self, args: dict, check_only: bool) -> dict:
        """preflight_check: a run at a tenth of the demand for links slower than free flow with no signal to blame
        (analysis.slow_links), and one at half the demand checked for gridlock and blocked entries (run_health)."""
        duration = float(args.get("duration_min") or 15)
        if not 5 <= duration <= 60:
            raise ValueError("duration_min must be 5 to 60.")
        demand = experiments.at_busiest_hour(self._pair_demand())  # daily: its busiest hour, as run_simulation
        if not demand:
            raise ValueError("There's no demand to check with: enter or generate some first.")
        if self.runner is not None or self._testing:
            raise ValueError("A run is going on: stop it first, then check.")
        if check_only:
            return {"ok": True}
        seed = int(args["seed"]) if args.get("seed") is not None else self.seed_box.value()
        network, modes = deepcopy(self.network), deepcopy(self.vehicle_modes.modes)
        override = self.extensions.signal_override(agent=self._waiting_agent())

        def work(progress, cancelled):
            return [self._quick_run(network, experiments.scaled(demand, scale), modes, seed, duration * 60, override,
                                    progress, cancelled, f"Pre-flight at {scale:.0%} of the demand") for scale in (0.1, 0.5)]

        low, half = self._in_background(work, "Pre-flight check")
        slow = analysis.slow_links(low)
        health = analysis.run_health(half)
        return {"ok": not slow and health["healthy"], "seed": seed,
                "at_10_percent": {"slow_links": slow[:15], "verdict": "no link is slow without a signal to explain it"
                                  if not slow else f"{len(slow)} link(s) slower than free flow with almost no traffic: "
                                  "look at them (describe, read_traffic) for a fault: a tight curve, a short link, a "
                                  "junction that makes vehicles wait for nothing."},
                "at_50_percent": health}

    def _compliance(self, args: dict, check_only: bool) -> dict:
        """test_agent_compliance: the extension (TSP or DEVL), decided as its tab says (its model), run headless on a
        fixed battery of conditions (the network as it is, 30 % more demand, heavy rain, a lane closed on the busiest
        road) and seeds, each run checked against the invariants (analysis.invariants); with DEVL, emergency vehicles
        sent across the network every 4 minutes. The pass rate, and each failure with its evidence."""
        name = str(args.get("extension") or "")
        extension = self.extensions.find(name)  # ValueError: names the extensions
        if extension.path.stem not in ("transit_signal_priority", "dynamic_emergency_lane"):
            raise ValueError("extension is TSP or DEVL (the ones whose agent decides the signals).")
        seeds = experiments.seeds_from(args.get("seeds") or [1, 2])
        duration = float(args.get("duration_min") or 10)
        if not 1 <= len(seeds) <= 5 or not 3 <= duration <= 60:
            raise ValueError("seeds: 1 to 5; duration_min: 3 to 60.")
        demand = experiments.at_busiest_hour(self._pair_demand())  # daily: its busiest hour, as run_simulation
        if not demand:
            raise ValueError("There's no demand to test with: enter or generate some first.")
        if self.runner is not None or self._testing:
            raise ValueError("A run is going on: stop it first.")
        if check_only:
            return {"ok": True}
        devl = extension.path.stem == "dynamic_emergency_lane"
        dispatches = []
        if devl:
            ends = sorted(self.network.approach_nodes)
            graph = Simulation(self.network, [], []).graph
            pairs = [(a, b) for a in ends for b in ends if a != b and graph.route(a, b)]
            if not pairs:
                raise ValueError("No two boundary ends are joined: emergency vehicles have nowhere to go.")
            origin, destination = max(pairs, key=lambda pair: len(graph.route(*pair)))
            dispatches = [(origin, destination, 240.0, 30.0), (destination, origin, 240.0, 150.0)]
        busiest = max(self.network.roads.values(), key=lambda road: road.length_m, default=None)
        battery = [("as it is", 1.0, "clear", []), ("30 % more demand", 1.3, "clear", []),
                   ("heavy rain", 1.0, "heavy rain", [])]
        if busiest is not None and busiest.lanes_per_direction >= 2 and busiest.length_m > 120:
            battery.append(("a lane closed", 1.0, "clear", experiments.incidents_from(
                [{"road": busiest.id, "lanes": 1, "at_min": 1, "length_m": 50}])))
        network, modes = deepcopy(self.network), deepcopy(self.vehicle_modes.modes)
        agent = self._waiting_agent()
        model = self._deciding()[0]

        def work(progress, cancelled):
            found = []
            for label, scale, weather, incidents in battery:
                for seed in seeds:
                    if cancelled():
                        return found
                    override = self.extensions.overrides_for([extension.name], agent=agent)
                    kpis = self._quick_run(network, experiments.scaled(demand, scale), modes, seed, duration * 60, override,
                                           progress, cancelled, f"Compliance: {label}, seed {seed}",
                                           dispatches=dispatches, incidents=incidents, weather=weather)
                    reported = {k: v for r in self.extensions.reports(override) for k, v in r.get("measures", {}).items()}
                    found.append({"condition": label, "seed": seed, "broken": analysis.invariants(kpis, reported),
                                  "decisions": reported.get("tsp_requests") or reported.get("devl_requests")})
            return found

        runs = self._in_background(work, f"Testing {extension.name}")
        passed = [run for run in runs if not run["broken"]]
        return {"extension": extension.name, "decided_by": model or "its tab's choice", "runs": len(runs),
                "passed": len(passed), "pass_rate": round(len(passed) / len(runs), 3) if runs else None,
                "conditions": [condition[0] for condition in battery],  # a lane closed is left out on a short road
                "failures": [run for run in runs if run["broken"]][:12],
                "invariants": [f"no signal arm with traffic left without a green over {analysis.STARVE_S:g} s",
                               f"no emergency vehicle standing still over {analysis.EMERGENCY_STANDSTILL_S:g} s at once",
                               "no gridlock", "no agent errors"],
                "note": "A fixed battery, the same for every model: compare pass rates before a long batch. With nobody "
                        "deciding (a baseline's rule) the extension's own rule runs."}

    @staticmethod
    def _seconds(value) -> float | None:
        """A sheet's time: seconds, or a clock "HH:MM:SS"."""
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str) and value.count(":") == 2:
            hours, minutes, seconds = value.split(":")
            return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
        return None

    def _decision_rows(self, test: dict) -> list[dict]:
        """A run's agent decisions (TSP's and DEVL's sheets) as dicts, each with its extension and when it was asked."""
        found = []
        for name, rows in (test["kpis"].get("decision_sheets") or {}).items():
            for row in rows[1:]:
                item = dict(zip(rows[0], row))
                asked = item.get("Asked at", item.get("Asked"))
                found.append({"extension": "DEVL" if name.startswith("DEVL") else "TSP",
                              "asked_s": self._seconds(asked), **item})
        return found

    def _read_decisions(self, args: dict) -> dict:
        """read_decisions: the agent decisions of a run of this session, filtered by junction and time."""
        test = self._run_test(str(args.get("id") or ""))
        rows = self._decision_rows(test)
        if not rows:
            return {"run": test["id"], "decisions": [], "note": "No agent decisions in that run: tick TSP or DEVL "
                                                                 "(set_extension) before run_simulation."}
        junction = str(args.get("junction") or "")
        start = float(args["from_min"]) * 60 if args.get("from_min") is not None else None
        end = float(args["to_min"]) * 60 if args.get("to_min") is not None else None
        picked = [row for row in rows if (not junction or row.get("Junction") == junction)
                  and (start is None or (row["asked_s"] or 0) >= start) and (end is None or (row["asked_s"] or 0) <= end)]
        limit = max(1, min(int(args.get("limit") or 20), 100))
        shown = []
        for row in picked[:limit]:
            item = {key: value for key, value in row.items() if value not in (None, "")}
            if isinstance(item.get("Telemetry read"), str):
                item["Telemetry read"] = json.loads(item["Telemetry read"])
            shown.append(item)
        return {"run": test["id"], "decisions": shown, "matching": len(picked), "in_run": len(rows),
                "note": "Outcome is what came of each request (served, not needed, blocked...): a hindsight check of "
                        "the answer. TSP's rows carry the telemetry the agent read and the person-delay estimate."}

    SENSITIVITY = ("no_passengers", "exit_full", "no_cross_queues", "arrives_on_green", "no_estimate")

    def _decision_sensitivity(self, args: dict, check_only: bool) -> dict:
        """decision_sensitivity: TSP decisions of a run re-asked with one fact of their telemetry changed at a time
        (tsp_agent.perturbed), to the model deciding now; how often each change flips the answer, and how often the
        same question asked again keeps it (Explainable AI for ITS Ch 7: occlusion; ZORRO-style perturbation)."""
        from traffic_simulator import agent_transit_signal_priority as tsp_agent  # noqa: PLC0415

        test = self._run_test(str(args.get("id") or ""))
        rows = [row for row in self._decision_rows(test) if row["extension"] == "TSP"
                and isinstance(row.get("Telemetry read"), str) and row.get("System")]
        if not rows:
            raise ValueError("That run has no TSP decisions with their telemetry: tick TSP, run_simulation, then try.")
        changes = network_tools._ids(args.get("perturbations") or list(self.SENSITIVITY))
        unknown = [change for change in changes if change not in self.SENSITIVITY]
        if unknown:
            raise ValueError(f"perturbations are some of {', '.join(self.SENSITIVITY)}.")
        sample = max(1, min(int(args.get("sample") or 8), 30))
        if check_only:
            return {"ok": True}
        picked = rows[:: max(1, len(rows) // sample)][:sample]

        def work(progress, cancelled):
            found = []
            for index, row in enumerate(picked, 1):
                if cancelled():
                    break
                message = json.loads(row["Telemetry read"])
                system = "1" if row["System"].endswith("1") else "2"

                def ask(telemetry):
                    reply = self._ask_agent(tsp_agent.question_for(system, telemetry))
                    return bool(reply.get("answer")) if isinstance(reply, dict) else str(reply).strip().upper() == "TRUE"

                progress(f"Re-asking decision {index} of {len(picked)}")
                base, again = ask(message), ask(message)
                found.append({"base": base, "same_again": base == again,
                              **{change: ask(tsp_agent.perturbed(message, change)) != base for change in changes}})
            return found

        try:
            answers = self._in_background(work, "Decision sensitivity")
        except RuntimeError as exc:
            raise ValueError(f"The model couldn't be asked: {exc}") from None
        if not answers:
            raise ValueError("Stopped before any decision was re-asked.")
        return {"run": test["id"], "decisions_re_asked": len(answers),
                "same_answer_again_percent": round(100 * sum(a["same_again"] for a in answers) / len(answers), 1),
                "flips_percent": {change: round(100 * sum(a[change] for a in answers) / len(answers), 1) for change in changes},
                "note": "A change that flips many answers is a fact the model relies on: passengers and a full exit lane "
                        "should; one that flips none is ignored. Read the flips against the same-again rate: a model that "
                        "changes its answer to the same question isn't relying on anything."}

    def _parameter_sensitivity(self, args: dict, demand, modes, duration: float, check_only: bool) -> dict:
        """parameter_sensitivity: the network and demand as they are, run with each calibration parameter 10 %
        either side (experiments.parameter_sensitivity), in the background."""
        if not demand:
            raise ValueError(f"There's no demand to test with: the OD matrix has no {self.od_matrix.mode} demand. "
                             "Enter or generate some first.")
        names = network_tools._ids(args.get("parameters") or list(experiments.SENSITIVITY_PARAMETERS))
        unknown = [name for name in names if name not in experiments.SENSITIVITY_PARAMETERS]
        if unknown:
            raise ValueError(f"parameters are some of {', '.join(experiments.SENSITIVITY_PARAMETERS)}.")
        seeds = experiments.seeds_from(args.get("seeds")) or [self.seed_box.value(), self.seed_box.value() + 1]
        if len(seeds) > 5 or not all(0 <= seed <= MAX_SEED for seed in seeds):
            raise ValueError(f"seeds: one to five, each 0 to {MAX_SEED:,}.")
        step = float(args.get("step") or experiments.SENSITIVITY_STEP)
        if not 0.05 <= step <= 0.3:
            raise ValueError("step must be 0.05 to 0.3 (0.1 = 10 % either side).")
        if check_only:
            return {"ok": True, "runs": (1 + 2 * len(names)) * len(seeds)}
        network, measure_from_s = deepcopy(self.network), float(args.get("measure_from_min") or 0) * 60
        found = self._in_background(
            lambda progress, cancelled: experiments.parameter_sensitivity(
                network, demand, modes, seeds, duration * 60, names, step, measure_from_s, progress, cancelled),
            "Parameter sensitivity")
        if found is None:
            raise ValueError("Stopped before every run was done: no elasticities.")
        return {**found, "duration_min": duration,
                "note": "Elasticity: the percent change in a figure for a 1 % change in the parameter. Beyond about 0.5 "
                        "(either sign) the results lean on that parameter, so calibrate it to local counts first; near "
                        "0 it hardly matters here. An interval wider than its value means more seeds are needed."}

    def _run_test(self, identifier: str) -> dict:
        """A headless run of this session by id (R1...), or the latest when none is given."""
        runs = [test for test in self.tests.values() if test["kind"] == "run"]
        latest = list(self.tests.values())[-1] if self.tests else None
        if not identifier and latest is not None and latest["kind"] != "run":  # "the latest" would be an older run
            raise ValueError(f"The latest test is {latest['id']}, an experiment: its runs aren't read one by one. Give "
                             f"a run id ({', '.join(t['id'] for t in runs) or 'none yet'}), or run_simulation the "
                             "design to analyse (read_results gives the experiment's comparison).")
        test = self.tests.get(identifier.upper()) if identifier else (runs[-1] if runs else None)
        if test is None or test["kind"] != "run":
            raise ValueError(f"No run called {identifier or '(the latest)'}: run_simulation first. This session's runs: "
                             f"{', '.join(t['id'] for t in runs) or 'none yet'}.")
        return test

    def _experiment(self, args, demand, modes, duration, check_only):
        parsed = {key: network_tools._parsed_json(args[key]) if isinstance(args.get(key), str) else args.get(key)
                  for key in ("scenarios", "factors", "conditions")}
        specs, factor_names = parsed["scenarios"], []
        if parsed["factors"]:
            if specs:
                raise ValueError("Give scenarios or factors, not both (factors make the scenarios).")
            specs, factor_names = experiments.factorial(parsed["factors"])
        if not isinstance(specs, list) or not 1 <= len(specs) <= (12 if factor_names else experiments.MAX_SCENARIOS):
            raise ValueError(f"Give 1 to {experiments.MAX_SCENARIOS} scenarios (the first is the baseline), or factors "
                             "making 12 or fewer.")
        designs = [experiments.scenario_from(spec, i) for i, spec in enumerate(specs)]
        if len({s.name.lower() for s in designs}) != len(designs):
            raise ValueError("Each scenario needs a name of its own.")
        conditions = experiments.conditions_from(parsed["conditions"])
        scenarios = [experiments.under(s, c) for c in conditions for s in designs] if conditions else designs
        objective = self._figure_key(args.get("objective") or "delay_veh_h")
        seeds = experiments.seeds_from(args.get("seeds"))
        if not 1 <= len(seeds) <= experiments.MAX_SEEDS or any(not 0 <= seed <= MAX_SEED for seed in seeds):
            raise ValueError(f"Give 1 to {experiments.MAX_SEEDS} seeds, each 0 to {MAX_SEED:,}.")
        precision = float(args.get("precision_percent") or 5)
        if not 0.5 <= precision <= 50:
            raise ValueError("precision_percent must be 0.5 to 50.")
        total = len(scenarios) * len(seeds)
        if total > experiments.MAX_RUNS:
            raise ValueError(f"That is {total} runs; at most {experiments.MAX_RUNS}: fewer scenarios or seeds.")
        networks = [experiments.scenario_network(self.network, s, network_tools.SCENARIO_TOOLS) for s in scenarios]
        overrides = [self.extensions.overrides_for(s.extensions, agent=self._waiting_agent())  # unknown names refused here
                     for s in scenarios]
        incidents = {}  # (scenario, seed) -> its incidents: given, and those drawn for the seed
        for scenario, network in zip(scenarios, networks):
            for seed in seeds:
                drawn = experiments.draw_incidents(network, seed, duration * 60, scenario.random_incidents) \
                    if scenario.random_incidents else []
                incidents[(scenario.name, seed)] = scenario.incidents + drawn
            try:  # every road and lane checked before anything runs
                experiments.apply_conditions(Simulation(network, [], modes), scenario.incidents, scenario.weather,
                                             scenario.meters)
            except ValueError as exc:
                raise ValueError(f"{scenario.name}: {exc}") from None
        if not demand:
            raise ValueError("There's no demand to test with: enter or generate some first.")
        if check_only:
            return {"ok": True}
        started = datetime.now()
        experiment_id = f"E{sum(key.startswith('E') for key in self.tests) + 1}"
        runs_folder = self.results_folder / f"{experiments.experiment_name(started)[:-5]} runs"

        planned = []  # every run's settings read here, on the UI thread (they read the window's widgets)
        for scenario, network, override in zip(scenarios, networks, overrides):
            for seed in seeds:
                planned.append((scenario, network, override, seed, self._run_settings(None, seed=seed, network=network, extra=[
                    ("Kind", f"experiment {experiment_id} (run_experiment, by the assistant)"),
                    ("Scenario", scenario.name), ("Demand scale", scenario.demand_scale),
                    ("Extensions in it", ", ".join(scenario.extensions) or "none"),
                    ("Weather", scenario.weather), ("Lateral position (sublane)", "on" if scenario.lateral else "off"),
                    ("Incidents", json.dumps(incidents[(scenario.name, seed)]) if incidents[(scenario.name, seed)] else "none"),
                    ("Duration (simulated)", results.clock(duration * 60))])))

        def work(progress, cancelled):
            runs, assigned = [], {}
            for count, (scenario, network, override, seed, settings) in enumerate(planned, 1):
                if cancelled():
                    break
                if scenario.assign_routes:  # its route choice found first (once a scenario), then every seed uses it
                    if scenario.name not in assigned:
                        found = deepcopy(network)
                        self._equilibrium(found, experiments.scaled(demand, scenario.demand_scale), modes, seeds[0],
                                          min(duration, 30), scenario.assign_routes["iterations"],
                                          {"theta_per_min": 0.5, "routes": 3, "link_times_s": {}}, override, progress,
                                          cancelled, scenario.assign_routes["until_gap"], f"{scenario.name}: routes")
                        assigned[scenario.name] = found
                    network = assigned[scenario.name]
                kpis, path, simulated = self._one_run(
                    network, experiments.scaled(demand, scenario.demand_scale), modes, seed, duration * 60, override,
                    settings, [], f"{scenario.name}, seed {seed} ({count}/{total})", progress, cancelled, runs_folder,
                    model=self._deciding(on=override is not None and any(
                        extension.path.stem == "transit_signal_priority" for extension in override.extensions))[0],
                    incidents=incidents[(scenario.name, seed)], weather=scenario.weather, meters=scenario.meters,
                    measure_from_s=float(args.get("measure_from_min") or 0) * 60, lateral=scenario.lateral)
                if simulated >= duration * 60 - 1:
                    runs.append({"id": f"{experiment_id}.{count}", "scenario": scenario.name, "seed": seed,
                                 "measures": experiments.measures_of(kpis), "file": str(path or "")})
            return runs

        runs = self._in_background(work, f"Experiment {experiment_id}")
        names = [s.name for s in scenarios]
        experiment = {"id": experiment_id, "started": started.isoformat(timespec="seconds"), "seeds": seeds,
                      "duration_s": duration * 60, "question": str(args.get("question") or ""),
                      "measure_from_s": float(args.get("measure_from_min") or 0) * 60,  # its runs' warm-up
                      "scenarios": [{"name": s.name, "demand_scale": s.demand_scale, "extensions": s.extensions,
                                     "changes": s.changes, "weather": s.weather, "incidents": s.incidents,
                                     "random_incidents": s.random_incidents, "meters": s.meters,
                                     "assign_routes": s.assign_routes, "lateral": s.lateral} for s in scenarios],
                      "runs": runs, "comparison": []}
        groups = [[f"{s.name} | {c['name']}" for s in designs] for c in conditions] if conditions else [names]
        for group in groups:  # each condition's designs against its own baseline (the first design under it)
            experiment["comparison"] += experiments.compare([r for r in runs if r["scenario"] in group], group,
                                                            precision / 100) if runs else []
        label, higher = self._figure_label(objective)
        if conditions and runs:
            experiment["conditions"] = {"objective": label, **experiments.condition_summary(
                runs, [s.name for s in designs], conditions, objective, higher)}
        if len(factor_names) == 2 and runs:
            experiment["factorial"] = self._factorial(runs, designs, factor_names, conditions, objective, label)
        inputs = results.input_sheets(networks[0], demand, modes, {})
        self.results_folder.mkdir(parents=True, exist_ok=True)
        if any(override is not None for override in overrides):
            experiment["agent_timing"] = AGENT_TIMING  # in its Read me, its answer and the Scenario Builder's report
        path = experiments.experiment_workbook(self.results_folder / experiments.experiment_name(started), experiment,
                                               inputs, self.ai_mode.journal)
        self.tests[experiment_id] = {"id": experiment_id, "kind": "experiment", "what": experiment["question"] or
                                     " vs ".join(names), "seeds": seeds, "experiment": experiment, "file": str(path)}
        experiment["objective"] = label
        shown = self._compact(experiment["comparison"], label)
        answer = {"experiment": experiment_id, "file": str(path), "runs": len(runs), "of": total, "objective": label,
                  **({"agent_timing": AGENT_TIMING} if experiment.get("agent_timing") else {}),
                  "comparison": shown}
        if len(shown) < len(experiment["comparison"]):
            answer["more"] = (f"{len(experiment['comparison']) - len(shown)} more rows (every figure, each vehicle "
                              "type's) are in the workbook; rank_scenarios ranks any figure.")
        if len(designs) == 1 and not conditions:  # one design and nothing to set it against
            answer["note"] = ("One scenario: nothing was compared. Put the network as it is first "
                              "({\"name\": \"baseline\"}), then the change.")
        for key in ("conditions", "factorial"):
            if key in experiment:
                answer[key] = _rounded(experiment[key])
        if len(runs) < total:
            answer["note"] = "Stopped by the user: only the finished runs are compared." + (
                " " + answer["note"] if answer.get("note") else "")
        if len(seeds) < 3:
            answer["caution"] = "Fewer than 3 seeds: the intervals are wide or open; don't call a difference from this."
        unstable = sorted({f"{row['figure']} ({row['scenario']})" for row in experiment["comparison"]
                           if (row.get("replications_needed") or 0) > experiments.UNSTABLE_RUNS})
        if unstable:
            answer["unstable"] = (f"Over {experiments.UNSTABLE_RUNS} runs needed for ±{precision:g} %: "
                                  + ", ".join(unstable[:8]) + ". The model may be unstable or gridlocked there.")
        return answer

    @staticmethod
    def _figure_key(figure: str) -> str:
        """A compared figure's key from its key or label (experiments.MEASURES)."""
        wanted = str(figure).strip().lower()
        for key, label, *_ in experiments.MEASURES:
            if wanted in (key.lower(), label.lower()):
                return key
        raise ValueError(f"objective is one of: {', '.join(label for _, label, *_ in experiments.MEASURES)}.")

    @staticmethod
    def _figure_label(key: str) -> tuple[str, bool | None]:
        return next(((label, up) for k, label, _unit, up in experiments.MEASURES if k == key), (key, None))

    @staticmethod
    def _factorial(runs, designs, factor_names, conditions, key, label) -> dict:
        """Two factors' main effects and interaction on the objective (stats.two_way_anova), each seed a replicate;
        per condition when there are conditions."""
        def level(name: str, factor: str) -> str:
            return dict(part.split("=", 1) for part in name.split(", "))[factor]

        found = {}
        for condition in conditions or [None]:
            cells: dict = defaultdict(list)
            for design in designs:
                scenario = design.name if condition is None else f"{design.name} | {condition['name']}"
                cells[(level(design.name, factor_names[0]), level(design.name, factor_names[1]))] += [
                    run["measures"].get(key) for run in runs if run["scenario"] == scenario]
            result = stats.two_way_anova(dict(cells))
            if result is not None:
                result = {factor_names[0] if k == "A" else factor_names[1] if k == "B" else k: v for k, v in result.items()}
            found[condition["name"] if condition else "all"] = result
        return {"objective": label, "factors": factor_names, "anova": found,
                "note": "An interaction p under 0.05: one factor's effect depends on the other's level, so compare the "
                        "cell means, not the factors one at a time."}

    LEAD_FIGURES = ("Delay", "Mean trip time", "Throughput", "Unfinished trips")  # an experiment's answer leads with

    @staticmethod
    def _compact(comparison: list[dict], objective: str | None = None) -> list[dict]:
        """An experiment's comparison, rounded, for the model: the objective's figure and LEAD_FIGURES, each with its
        unit (some 100 rows of every figure buried the one asked about for a small model; the workbook has them all).
        A difference is called better or worse only when clear; else "not told apart"."""
        def r(value):
            return round(value, 2) if isinstance(value, float) else value
        lead = [objective, *MainWindow.LEAD_FIGURES] if objective else list(MainWindow.LEAD_FIGURES)
        kept = [row for row in comparison if row["figure"] in lead]
        kept.sort(key=lambda row: lead.index(row["figure"]))
        rows = []
        for row in kept or comparison:
            item = {"figure": row["figure"], "unit": row.get("unit"), "scenario": row["scenario"], "mean": r(row["mean"]),
                    "ci95": r(row["ci95"]), "n": row["n"], "replications_needed": row.get("replications_needed")}
            if "vs_baseline_diff" in row:
                clear = row["vs_baseline_clear"]
                item.update(diff=r(row["vs_baseline_diff"]), diff_ci=r(row["vs_baseline_ci"]),
                            p=None if row["vs_baseline_p"] is None else round(row["vs_baseline_p"], 4),
                            interval_level=round(row["confidence"], 4), test=row["vs_baseline_test"],
                            change_percent=r(row["vs_baseline_percent"]), clear=clear,
                            **({"better": row.get("better")} if clear and row.get("better") is not None
                               else {"verdict": "not told apart"} if clear is False else {}))
                if row.get("vs_baseline_wilcoxon_p") is not None:
                    item["wilcoxon_p"] = round(row["vs_baseline_wilcoxon_p"], 4)
                if row.get("vs_baseline_p_holm") is not None:  # adjusted across the ~20 figures tested at once
                    item["p_adjusted"] = round(row["vs_baseline_p_holm"], 4)
                if row.get("speed_risk"):
                    item["speed_risk"] = row["speed_risk"]
            if row.get("anova"):
                item["anova_p"] = row["anova"]["p"]
            rows.append(item)
        return rows

    def _read_results(self, identifier: str) -> dict:
        if not identifier:
            listed = []
            for test in self.tests.values():
                entry = {"id": test["id"], "kind": test["kind"], "what": test["what"], "file": test["file"]}
                if test["kind"] == "run":
                    entry.update(seed=test["seed"], simulated=test["simulated"],
                                 throughput_vph=test["kpis"]["throughput_vph"], delay_veh_h=test["kpis"]["delay_veh_h"])
                else:
                    entry.update(seeds=test["seeds"], runs=len(test["experiment"]["runs"]))
                listed.append(entry)
            latest = self._results()
            return {"tests": listed, "latest_run": None if latest is None else {
                "running": latest["running"], "file": latest["file"], **experiments.headline(latest["kpis"])}}
        test = self.tests.get(identifier.upper())
        if test is None:
            raise ValueError(f"No test called {identifier}; this session's: {', '.join(self.tests) or 'none yet'}.")
        if test["kind"] == "run":
            kpis = test["kpis"]
            return {"id": test["id"], "seed": test["seed"], "simulated": test["simulated"], "file": test["file"],
                    **experiments.headline(kpis), "busiest_roads": kpis["roads"][:15],
                    "longest_queues": sorted(kpis["roads"], key=lambda row: -row["queue_max"])[:10]}
        experiment = test["experiment"]
        return {"id": test["id"], "question": experiment["question"], "seeds": experiment["seeds"],
                "scenarios": experiment["scenarios"], "file": test["file"],
                "comparison": self._compact(experiment["comparison"], experiment.get("objective"))}

    def _compare_runs(self, ids: list[str]) -> dict:
        tests = [self.tests.get(i.upper()) for i in ids]
        if len(ids) < 2 or any(t is None or t["kind"] != "run" for t in tests):
            raise ValueError(f"Give two or more run ids (R1, R2...); this session's runs: "
                             f"{', '.join(k for k, t in self.tests.items() if t['kind'] == 'run') or 'none yet'}.")
        first = experiments.measures_of(tests[0]["kpis"])
        rows = []
        for test in tests:
            values = experiments.measures_of(test["kpis"])
            rows.append({"run": test["id"], "seed": test["seed"], "figures": {
                key: {"value": value, "change": None if test is tests[0] or value is None or first.get(key) is None
                      else round(value - first[key], 2)} for key, value in values.items()}})
        return {"runs": rows, "note": "Each run is one sample (one seed): use run_experiment to tell designs apart."}

    def _run_settings(self, simulation: Simulation | None, seed: int | None = None, network=None,
                      extra: list | None = None) -> list[tuple[str, object]]:
        """A run's settings, for its workbook: enough for someone else to repeat and check it."""
        network = network or self.network
        seed = self.seed_box.value() if seed is None else seed
        background = network.background
        where = {"colour": f"colour {background.colour}", "map": f"OpenStreetMap at {background.lat:.6f}, "
                 f"{background.lon:.6f} (under ({background.anchor_x:.1f}, {background.anchor_y:.1f}) m), turned "
                 f"{background.map_rotation_deg:g}°", "image": f"image {background.image}, {background.width_m:g} m wide"}
        rows = [("Application", "ACMS (Agentic Context-Aware Mobility System) 0.1.0"),
                ("Run started", time.strftime("%Y-%m-%d %H:%M:%S")),
                ("Random seed", seed), ("Time step (s)", simulation.dt if simulation else DT),
                ("Step method", simulation.step_method if simulation else "euler"),
                *((("Lateral position (sublane)", "on" if simulation.lateral else "off"),) if simulation else ()),
                ("Sampled every (s)", results.SAMPLE_S),
                ("Speed (simulated s a real s)", float(self.speed_combo.currentData())),
                ("Demand used", "daily (trips/day, by each pair's curve)" if self.od_matrix.mode == "daily" else "hourly (veh/h)"),
                *((("OD pairs with demand", len(simulation.demand)),
                   ("Pairs with no route", ", ".join(simulation.unreachable) or "none")) if simulation else ()),
                ("Network file", str(self.network_path) if self.network_path else "not saved"),
                ("Network", f"{len(network.nodes)} junctions, {len(network.roads)} roads, "
                            f"{len(network.approach_nodes)} boundary ends"),
                ("Driving side", network.driving_side), ("Background", where.get(background.kind, background.kind)),
                ("Queued below (km/h)", results.QUEUE_KMH)]
        rows += [(f"Extension: {name}", state) for name, state in self.extensions.settings()]
        return rows + list(extra or [])

    def _run_inputs(self, simulation: Simulation, demand) -> list:
        return results.input_sheets(self.network, demand, self.vehicle_modes.modes, dict(simulation.signal_plans))

    def _apply_hooks(self) -> None:
        """An extension that overrides signals was ticked or unticked: the run follows at once."""
        if self.runner is not None:
            self.runner.simulation.signal_override = self.extensions.signal_override()

    def _ask_agent(self, question, choice: dict | None = None):
        """The signal agent's question (runs in its thread). TSP's ({"system": "1" | "2", ...}: System 1's state and
        typed question, or System 2's messages) goes to that system: `choice` names the model (a batch run's agent),
        else AI Mode's own (System 1's Use; System 2's started model); the answer comes back with what the model
        reported (model, tokens, P(yes)). Plain text (another extension's yes/no) goes to System 1 when one is
        chosen (one yes/no in an encoder pass), else to System 2's started model."""
        if not isinstance(question, dict):
            fast = self.ai_mode.system_one_for_agents()
            if fast is not None:
                provider, key = fast
                return "TRUE" if assistant.system_one_true(provider, question, key) else "FALSE"
            connection = self.ai_mode.model_for_agents()
            if connection is None:
                raise RuntimeError("Nothing is chosen in AI Mode (System 1 or a started System 2 model), "
                                   "so every answer is FALSE.")
            provider, model, key = connection
            return assistant.chat(provider, model, [{"role": "system", "content": INSTRUCTIONS},
                                                    {"role": "user", "content": question}], key)
        choice = choice or question.get("choice")  # a batch's agent, else the model picked in the extension
        system = choice["system"] if choice else question.get("system")  # TSP's question has both forms
        if system == "1":
            fast = (choice["provider"], self.ai_mode.key_for(choice["provider"])) if choice else \
                self.ai_mode.system_one_for_agents()
            if fast is None:
                raise RuntimeError("AI Mode's System 1 is Off, so every answer is FALSE (the plan).")
            provider, key = fast
            reply = assistant.system_one(provider, question["state"], question["questions"], key)
            usage = reply.get("usage") or {}
            found = {"provider": provider, "system": "1", "model": reply.get("model") or provider,
                     "input_tokens": usage.get("input_tokens"), "output_tokens": usage.get("output_tokens")}
            each = {name: (reply["answers"].get(name) or {}).get("noul") for name in question["questions"]}
            for name, p_yes in each.items():  # the guard: a probability for every question, or the plan
                if not isinstance(p_yes, (int, float)) or isinstance(p_yes, bool) or not 0 <= p_yes <= 1:
                    return {**found, "answer": False, "guard": f"rejected: P(yes) for {name} was {p_yes!r}"}
            lowest = min(each.values())  # TRUE when every one is yes: the lowest decides
            return {**found, "answer": lowest >= 0.5, "p_yes": round(float(lowest), 4), "guard": "accepted",
                    "reason": ", ".join(f"{name} {p_yes:.2f}" for name, p_yes in each.items())}
        connection = (choice["provider"], choice["model"], self.ai_mode.key_for(choice["provider"])) if choice else \
            self.ai_mode.model_for_agents()
        if connection is None:
            raise RuntimeError("No model is started in AI Mode's System 2, so every answer is FALSE (the plan).")
        provider, model, key = connection
        tools = question.get("tools")
        try:  # one request: the decision comes as a tool call (data), or failing that in words
            reply = assistant.stream_chat(provider, model, question["messages"], key, tools)
        except assistant.ToolsUnsupported:  # a model that can't call tools (phi3...): TRUE or FALSE in words
            reply = assistant.stream_chat(provider, model, question["messages"], key)
        usage = assistant.last_usage()
        found = {"provider": provider, "model": model, "system": "2", "input_tokens": usage.get("input_tokens"),
                 "output_tokens": usage.get("output_tokens"), "reply": reply.get("content", "").strip()}
        name, argument = question.get("answer_from") or (None, None)
        call = next((call for call in reply.get("tool_calls", []) if call["name"] == name), None)
        # The guard: only a decision is one. Anything else is the plan, counted as rejected, never read into.
        if call is None:  # no tool call: a bare TRUE or FALSE, and nothing else, will do
            word = re.fullmatch(r"\W*(true|false)\W*", found["reply"], re.IGNORECASE)
            if word is None:
                return {**found, "answer": False, "via": "text",
                        "guard": f"rejected: no {name} call, and the reply isn't a bare TRUE or FALSE"}
            return {**found, "answer": word.group(1).lower() == "true", "via": "text", "guard": "accepted"}
        value = call["arguments"].get(argument)  # a boolean, or as small models send it, the word
        if isinstance(value, str) and value.strip().lower() in ("true", "false"):
            value = value.strip().lower() == "true"
        decided = {**found, "via": "tool", "reason": str(call["arguments"].get("reason") or "")[:300] or None,
                   "reply": found["reply"] or json.dumps(call["arguments"])}
        if not isinstance(value, bool):
            return {**decided, "answer": False, "guard": f"rejected: {argument} was {call['arguments'].get(argument)!r}"}
        return {**decided, "answer": value, "guard": "accepted"}

    def _warm_up(self, choice: dict) -> None:
        """One throwaway request, so a model's loading isn't charged to its first run's answers; a failure is
        left for the runs to report."""
        provider, key = choice["provider"], self.ai_mode.key_for(choice["provider"])
        try:
            if choice["system"] == "1":
                assistant.system_one(provider, "warm-up", {"ready": {"type": "noul", "instructions": "Is this a test?"}},
                                     key)
            else:
                assistant.chat(provider, choice["model"], [{"role": "user", "content": "Reply with one word: OK."}], key)
        except assistant.AssistantError:
            pass

    def _waiting_agent(self, choice: dict | None = None, wait_s: float = HEADLESS_WAIT_S) -> SignalAgent:
        """A headless run's signal agent: it waits for each answer, up to `wait_s` (missed: the plan). `choice`: a
        batch run's agent (a baseline: nobody asked, its rule answers); None: AI Mode's own choices, as a live run's."""
        if choice is not None and not choice.get("system"):
            return SignalAgent(None, wait_s=wait_s, rule=choice.get("rule"))
        return SignalAgent(lambda question: self._ask_agent(question, choice), wait_s=wait_s)

    def _tsp(self):
        """The TSP extension, if it loaded."""
        return self._extension("transit_signal_priority")

    def _extension(self, stem: str):
        """The extension from extensions/<stem>.py, if it loaded."""
        return next((extension for extension in self.extensions.extensions
                     if extension.path.stem == stem and extension.module is not None), None)

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

    def _log_edits(self) -> None:
        """While watching: each undo step's changes, a line each (_state_changes)."""
        if self.activity.on:
            now = self._snapshot()
            for line in self._state_changes(self._activity_seen, now):
                self.activity.log("edit", line)
            self._activity_seen = now

    def _override_seed(self, overridden: bool) -> None:
        """The menu bar's Seed box, or "Overridden" in its place while Batch Run (whose seeds a batch uses) is ticked."""
        self._seed_box_action.setVisible(not overridden)
        self._seed_overridden_action.setVisible(overridden)

    def _show_runs_on_map(self, on: bool) -> None:
        """Batch Run's Show runs on map: a headless test's run drawn on the map as it goes (the one the clock
        shows), or not (the map shows the network alone and the runs go a little faster)."""
        self.show_runs = bool(on)
        if not on and self.runner is None:
            self.scene.show_frame(1.0, None)

    def _tick_extension(self, stem: str) -> bool:
        """Tick the extension from extensions/<stem>.py in the Extensions tab (Batch Run's Test DEVL ticks Dispatch
        Emergency Vehicle, so its fleet is there to set); False if it didn't load."""
        extension = self._extension(stem)
        if extension is not None:
            extension.check.setChecked(True)
        return extension is not None

    def places(self) -> list[str]:
        """Where an emergency vehicle can be sent from or to: the boundary ends, then the junctions."""
        return sorted(self.network.approach_nodes) + sorted(self.network.nodes)

    def dispatch_emergency(self, origin: str, destination: str, every_min: float = 0, check_only: bool = False,
                           after_s: float = 0.0) -> None:
        """Dispatch Emergency Vehicle: an emergency vehicle into the live run from `origin` to `destination` (ends or
        junctions), `after_s` seconds from now (0: now) and every `every_min` minutes after (0: once;
        Simulation.dispatch). ValueError says why not."""
        if self.runner is None:
            raise ValueError("Start a run first (Run in the menu bar): emergency vehicles join a run.")
        for place in (origin, destination):
            if place not in self.places():
                raise ValueError(f"No end or junction called {place or '(blank)'}.")
        if origin == destination or not self.runner.simulation.graph.route(origin, destination):
            raise ValueError(f"No route from {origin} to {destination}.")
        if not check_only:
            self.runner.simulation.dispatch(origin, destination, float(every_min) * 60, float(after_s))

    def tsp_batch(self) -> dict:
        """The TSP tab's settings for Batch Run (_agent_plan)."""
        return self._agent_plan(self._tsp(), "TSP", "Transit Signal Priority")

    def devl_batch(self) -> dict:
        """The DEVL tab's settings for Batch Run (_agent_plan)."""
        return self._agent_plan(self._extension("dynamic_emergency_lane"), "DEVL", "Dynamic Emergency Vehicle Lane")

    def _agent_plan(self, extension, short: str, name: str) -> dict:
        """An extension's who-decides settings for Batch Run (its batch_settings(), agent_choice.py): the models
        picked (Multiple's, or System 1's or 2's), else the model on in AI Mode for its System, as agents (labels of
        agent_options) with their servers; its deadline, pace, runs at once and times each. None of them: no agents,
        and the batch runs the two baselines alone (Never and Always preempt)."""
        if extension is None:
            raise ValueError(f"The {name} extension didn't load: see the Extensions tab.")
        settings = dict(extension.module.batch_settings())
        system = settings.pop("system", None)
        if system:
            fast, connection = self.ai_mode.system_one_for_agents(), self.ai_mode.model_for_agents()
            option = next((option for option in self.ai_mode.agent_options() if option["system"] == system and (
                (system == "1" and fast and option["provider"] == fast[0]) or
                (system == "2" and connection and option["model"] == connection[1]))), None)
            settings.update(agents=[option["label"]] if option else [],
                            servers={option["label"]: option["provider"]} if option else {})
        return settings

    def _deciding(self, on: bool | None = None) -> tuple[str, str]:
        """Who decides TSP, by the TSP extension's System 1 / System 2 and AI Mode: (the model's name, for the
        workbook's; a warning when nobody can answer). `on`: TSP in the run (default: ticked). No TSP: "no-ai"."""
        tsp = self._tsp()
        if tsp is None or not (tsp.on if on is None else on):
            return "no-ai", ""
        picked = getattr(tsp.module.tsp_agent, "model", None)
        if picked and picked.get("system") == tsp.module.tsp_agent.system:
            return picked["name"], ""
        if tsp.module.tsp_agent.system == "1":
            fast = self.ai_mode.system_one_for_agents()
            return (fast[0], "") if fast else (
                "no-llm", "TSP asks System 1, but AI Mode's System 1 is Off: every bus request falls back to the plan.")
        connection = self.ai_mode.model_for_agents()
        return (connection[1], "") if connection else (
            "no-llm", "TSP asks System 2, but no model is started in AI Mode's System 2: every bus request falls back "
                      "to the plan.")

    def run_batch(self, seeds: list[int], minutes: float, times: int = 1, agents: list[str] | None = None,
                  deadline_s: float = 30.0, speed: float = 1.0, at_once: int = 4, test_tsp: bool | None = None,
                  test_devl: bool = False, devl_agents: list[str] | None = None, devl_deadline_s: float = 30.0) -> dict:
        """Batch Run's Start: every seed x agent x time, headless (Stop ends it), each run saving its workbook (named
        by its seed and model) and its telemetry feed in a folder of the batch's, then the batch's workbook
        comparing each agent with the first (experiments.batch_workbook). `test_tsp` (Batch Run's Test TSP; None:
        whether TSP is ticked) runs TSP, ticked or not: `agents` are labels of AIModePanel.agent_options(), the
        models to test, and the two baselines (Never preempt, the plan, general traffic; Always preempt) always run too, first, so each model is
        compared with it seed by seed. Each run keeps in step with the wall clock at
        `speed`, never pausing: its own agent answers in the background, and a request not answered within
        `deadline_s` simulated seconds falls back to the plan. Up to `at_once` runs go side by side, but never two
        on one model server (a provider: Ollama, Laya, Jev...), so no run's answers wait behind another's; each
        records the pace it achieved. Without TSP the runs are plain, one at a time as fast as the CPU allows.
        `test_devl` (Batch Run's Test DEVL): each run dispatches the Dispatch Emergency Vehicle tab's fleet at its
        start and its workbook gets an Emergency vehicles sheet (each trip, and how long each Dynamic Emergency
        Vehicle Lane signal was on); `devl_agents` decide its lane signals (each against the baselines, Never and Always
        preempt), each answer within `devl_deadline_s`. With both, every TSP agent runs with every DEVL agent. The runs
        are paced only where a model answers (a baseline never: as fast as the CPU allows). Everything is checked first: ValueError says what can't run."""
        if self.runner is not None or self._testing:
            raise ValueError("A run or test is going on: stop it first (the menu bar's Stop), then start the batch.")
        if not seeds or not 1 <= int(times) <= 100 or not 0 < float(minutes) <= 24 * 60 or not 1 <= deadline_s <= 280 \
                or not 1 <= devl_deadline_s <= 280 \
                or not 1 <= speed <= 60 or not 1 <= int(at_once) <= MAX_AT_ONCE:
            raise ValueError(f"A batch needs seeds, 1 to 100 times each, up to 24 h a run, a deadline of 1 to 280 s, "
                             f"a pace of 1x to 60x and 1 to {MAX_AT_ONCE} runs at once.")
        demand = self._pair_demand()
        if not demand:
            raise ValueError(f"There's no demand to run: the OD matrix has no {self.od_matrix.mode} demand (a run uses "
                             "the kind it shows). Enter or generate some first.")
        tsp = self._tsp()
        if test_tsp is None:
            test_tsp = tsp is not None and tsp.on
        if test_tsp and tsp is None:
            raise ValueError("The Transit Signal Priority extension didn't load: see the Extensions tab.")
        options = {option["label"]: option for option in self.ai_mode.agent_options()}
        # Never preempt (general traffic: the one every agent is compared with) and Always preempt: both always run, first
        baselines = [option for option in options.values() if not option["system"]]

        def resolve(labels, what: str) -> list[dict]:
            """The baselines and the models `labels` name, checked (none: the baselines alone)."""
            labels = labels or []  # none picked: the baselines alone
            unknown = [label for label in labels if label not in options]
            if unknown:
                raise ValueError(f"Not found on this PC now: {', '.join(unknown)}. Rescan in AI Mode, then pick again.")
            found = [option for label, option in options.items() if option in baselines or label in labels]
            missing = [option["label"] for option in found
                       if option["provider"] in ("jev", "grok", "gemini") and not self.ai_mode.key_for(option["provider"])]
            if missing:
                raise ValueError(f"No key on this PC for {', '.join(missing)}: save it (see README), then Rescan PC.")
            return found

        tsp_options = resolve(agents, "TSP") if test_tsp else [None]
        devl_options = (resolve(devl_agents, "DEVL") if devl_agents else baselines) if test_devl else [None]
        if not test_tsp:
            tsp = None

        def merged(tsp_option, devl_option) -> dict:
            """One agent of the batch: who decides TSP and who decides DEVL, as one label."""
            parts = [part for part in (tsp_option, devl_option) if part]
            if not parts:
                return {"label": "No TSP", "system": "", "provider": "", "model": "", "name": "no-ai", "tsp": None,
                        "devl": None, "servers": set()}
            asking = [part for part in parts if part["system"]]
            return {"label": " · ".join(f"{kind} {part['label']}" for kind, part in (("TSP", tsp_option), ("DEVL", devl_option)))
                    if tsp_option and devl_option else parts[0]["label"],
                    "system": asking[0]["system"] if asking else "",
                    "provider": "+".join(dict.fromkeys(part["provider"] for part in asking)),
                    "model": " + ".join(dict.fromkeys(part["model"] for part in asking if part["model"])),
                    "name": "-".join(dict.fromkeys(part["name"] for part in parts)),
                    "tsp": tsp_option, "devl": devl_option, "servers": {part["provider"] for part in asking}}

        chosen = [merged(tsp_option, devl_option) for tsp_option in tsp_options for devl_option in devl_options]
        # Only a run a model answers in keeps in step with the wall clock (its latency then costs what it would on the
        # street); the baselines and their emergency fleets depend on nothing but the seed: as fast as the CPU
        paced = any(option["servers"] for option in chosen)
        fleet, devl, random_emergency = self._emergency_fleet() if test_devl else ([], None, None)
        times, duration = int(times), float(minutes) * 60
        # Test DEVL: each seed's own random emergency vehicle, the same for every agent on that seed (within the run)
        randoms = {seed: random_emergency(seed, duration) for seed in seeds} if random_emergency else {}
        started = datetime.now()
        folder = self.results_folder / f"{experiments.batch_name(started)[:-5]} runs"
        planned = []  # every run's settings read here, on the UI thread (they read the window's widgets)
        # Seed by seed (every agent on a seed before the next seed): a batch cut short has every agent on the seeds it
        # finished, so they still compare, pair by pair
        for seed in seeds:
            for option in chosen:
                for time_ in range(1, times + 1):
                    planned.append((option, seed, time_, self._run_settings(None, seed=seed, extra=[
                        ("Kind", "batch run (the Batch Run extension)"), ("Agent", option["label"]),
                        ("AI model deciding TSP", (option["tsp"] or {}).get("model") or "none"),
                        ("Decision deadline (simulated s)", deadline_s if tsp else "none"),
                        ("AI model deciding DEVL", (option["devl"] or {}).get("model") or "none"),
                        ("DEVL decision deadline (simulated s)", devl_deadline_s if devl_agents and test_devl else "none"),
                        ("Pace (simulated s a real s)", speed if option["servers"] else "as fast as the CPU allows"),
                        ("Repeat", f"{time_} of {times}"),
                        ("Emergency vehicles", "; ".join(f"{a} to {b}" + (f" every {every:g} s" if every else "")
                                                         + (f" at {results.clock(after)}" if after else "")
                                                         for a, b, every, after in fleet) or "none"),
                        ("Random emergency vehicle (from the seed)", (lambda a, b, every, after: f"{a} to {b} at "
                                                                      f"{results.clock(after)}")(*randoms[seed])
                         if seed in randoms else "none"),
                        ("Duration (simulated)", results.clock(duration))])))
        network, modes = deepcopy(self.network), deepcopy(self.vehicle_modes.modes)
        at_once = int(at_once) if paced else 1
        path, inputs = self.results_folder / experiments.batch_name(started), results.input_sheets(network, demand, modes, {})
        names = [option["label"] for option in chosen]

        def summary(runs: list) -> dict:
            return {"started": started.isoformat(timespec="seconds"), "seeds": list(seeds), "times": times,
                    "duration_s": duration, "deadline_s": deadline_s, "tsp": tsp is not None, "agents": chosen,
                    "speed": speed if paced else None, "at_once": at_once, "planned": len(planned),
                    "runs": runs, "folder": str(folder),
                    "comparison": experiments.compare(experiments.averaged(runs), names) if runs else []}

        def save(runs: list) -> Path:
            """The batch's workbook as it stands, swapped in whole: a crash mid-write keeps the last one."""
            self.results_folder.mkdir(parents=True, exist_ok=True)
            saving = path.with_name(f"{path.stem} (saving).xlsx")
            experiments.batch_workbook(saving, summary(runs), inputs)
            os.replace(saving, path)
            return path  # plain runs are CPU-bound: side by side they'd only share the CPU
        done: list = []  # the runs finished, for the progress line

        def one(count, option, seed, time_, settings, progress, cancelled):
            """One run of the batch, on a worker thread: its entry for the comparison, or None if cut short."""
            agents = {}  # each extension's own agent and state: it never waits, and never meets another run's vehicles
            for extension, deciding, deadline in ((tsp, option["tsp"], deadline_s), (devl, option["devl"], devl_deadline_s)):
                if extension is not None:
                    agents[extension.name] = SignalAgent((lambda question, deciding=deciding: self._ask_agent(question, deciding))
                                                         if deciding["system"] else None, deadline_s=deadline,
                                                         rule=deciding.get("rule"))
            override = self.extensions.overrides_for(list(agents), agent=agents) if agents else None
            label = f"{option['label']}, seed {seed}" + (f", time {time_}" if times > 1 else "")
            try:
                kpis, path, simulated = self._one_run(network, demand, modes, seed, duration, override, settings, [],
                                                      label, progress, cancelled, folder, model=option["name"],
                                                      speed=speed if option["servers"] else None,
                                                      dispatches=fleet + ([randoms[seed]] if seed in randoms else []))
            finally:
                for asked in agents.values():
                    asked.close()
            if simulated < duration - 1:
                return None
            measures = experiments.measures_of(kpis)  # with the pace it achieved (_one_run)
            return {"id": f"B{count}", "scenario": option["label"], "seed": seed, "repeat": time_, "measures": measures,
                    "file": str(path or "")}

        def work(progress, cancelled):
            if any("laya" in option["servers"] for option in chosen):
                progress("starting Laya")
                self.ai_mode.start_laya()
            models = {part["label"]: part for option in chosen for part in (option["tsp"], option["devl"]) if part}
            for part in models.values():  # loaded before the clock runs: a deployed model would be
                if part["system"] and not cancelled():
                    progress(f"warming up {part['label']}")
                    self._warm_up(part)
            failed = threading.Event()

            def halted():
                return cancelled() or failed.is_set()

            began = time.monotonic()

            def line(text):  # one status line for all the runs going, with the time left at the pace so far
                simulated = len(done) * duration + self._headless_seconds()
                left = ""
                if simulated > 0:
                    rest = (time.monotonic() - began) * (len(planned) * duration - simulated) / simulated
                    left = f" · about {results.clock(rest)} left"
                progress(f"{len(done)} of {len(planned)} done, {len(going)} going · {text}{left}")

            waiting, going = list(enumerate(planned, 1)), {}  # going: future -> its model servers
            with ThreadPoolExecutor(max_workers=at_once, thread_name_prefix="batch run") as pool:
                while waiting or going:
                    busy = set().union(*going.values())
                    for item in list(waiting) if not halted() else []:
                        count, (option, seed, time_, settings) = item
                        if len(going) >= at_once:
                            break
                        if option["servers"] & busy:
                            continue  # one run a model server at a time: none waits behind another's questions
                        waiting.remove(item)
                        busy |= option["servers"]
                        going[pool.submit(one, count, option, seed, time_, settings, line, halted)] = option["servers"]
                    if halted() and not going:
                        break
                    finished, _ = wait_for(list(going), timeout=0.2, return_when=FIRST_COMPLETED)
                    for future in finished:
                        going.pop(future)
                        try:
                            entry = future.result()
                        except Exception:
                            failed.set()  # the others stop at their next step; this run's error is the batch's
                            raise
                        if entry is not None:
                            done.append(entry)
                            try:  # a checkpoint after every run: a crash or power cut keeps what finished
                                save(sorted(done, key=lambda entry: int(entry["id"][1:])))
                            except OSError:
                                pass  # open in Excel, say: the next run's checkpoint tries again
            return sorted(done, key=lambda entry: int(entry["id"][1:]))

        before, began = (self._snapshot() if self.extensions.wants("log") else None), time.time()
        asked = {"seeds": list(seeds), "minutes": float(minutes), "times": times, "test_tsp": bool(tsp),
                 "test_devl": bool(test_devl), "agents": [option["label"] for option in chosen],
                 "deadline_s": deadline_s if tsp else None, "devl_deadline_s": devl_deadline_s if test_devl else None,
                 "speed": speed if paced else None, "at_once": at_once}
        what = (f"Batch run: {len(planned)} runs, {minutes:g} min each, seeds {', '.join(map(str, seeds))}; "
                + "; ".join(option["label"] for option in chosen))
        try:
            runs = self._in_background(work, "Batch run")
        except Exception as exc:
            self._log_step("batch", what, "run_batch", asked, {"error": str(exc)}, began, before, str(exc))
            raise
        save(runs)
        self._refresh_summary()
        answer = {"file": str(path), "runs": len(runs), "of": len(planned), "folder": str(folder)}
        behind = [run["id"] for run in runs if run["measures"].get("pace_achieved", speed) < 0.9 * speed]
        if behind:
            answer["note"] = (f"{len(behind)} run(s) fell behind the clock (under 90 % of {speed:g}x): fewer at once "
                              "or a slower pace keeps answers' latency honest.")
        workbooks = sorted(str(book) for book in folder.glob("*.xlsx")) if folder.is_dir() else []
        self._log_step("batch", what, "run_batch", asked, {**answer, "files": workbooks}, began, before)
        return answer

    def _log_step(self, kind: str, what: str, tool: str, arguments: dict, result: dict, started: float, before,
                  error: str = "") -> None:
        """Something done outside AI Mode (a batch run) as a step of the ticked logging extensions (the Research
        Log: its folder, the files it wrote with their SHA-256 and the activity since the step
        before): the same record as an AI Mode step, with no model."""
        if before is None or not self.extensions.wants("log"):
            return
        ended = time.time()
        reply = error or "; ".join(f"{key}: {value}" for key, value in result.items() if key != "files")
        self._research_step({
            "kind": kind, "prompt": what, "editor_update": "", "provider": None, "model": None, "plan": "",
            "tools_offered": [], "started": started, "ended": ended, "messages": [], "reply": reply, "error": error,
            "usage": [], "before": before,
            "calls": [{"tool": tool, "arguments": arguments, "what": what, "decision": "failed" if error else "applied",
                       "result": result, "at": started, "proposed_at": started, "decided_at": started,
                       "compute_s": round(ended - started, 3)}]})

    def _route_check(self, no: str = "No"):
        """can_go(origin, destination): ValueError ("<no> route from ... to ...") unless a route joins them on the
        network as it is, no run needed."""
        graph = Simulation(self.network, [], []).graph  # ponytail: a lane graph just to check routes

        def can_go(origin: str, destination: str) -> None:
            if origin == destination or not graph.route(origin, destination):
                raise ValueError(f"{no} route from {origin} to {destination}.")

        return can_go

    def _schedule_emergency(self, args: dict, check_only: bool = False) -> dict:
        """The assistant's schedule_emergency: the Dispatch Emergency Vehicle tab's schedules replaced (set_fleet),
        each checked first (an end or junction each, a route between them, every or after 0-120 min, not both);
        with dispatch_now also sent into the live run (dispatch_emergency). ValueError: why not, nothing changed."""
        dispatch = self._extension("dispatch_emergency")
        if dispatch is None:
            raise ValueError("The Dispatch Emergency Vehicle extension didn't load: see the Extensions tab.")
        rows = args.get("schedules")
        if isinstance(rows, str):
            rows = network_tools._parsed_json(rows)
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise ValueError("schedules is a list of {from, to, every_min or after_min}.")
        places, can_go, most = self.places(), self._route_check(), dispatch.module.MAX_EVERY_MIN
        fleet = []
        for row in rows:
            origin, destination = (str(row.get(key) or "").strip() for key in ("from", "to"))
            for place in (origin, destination):
                if place not in places:
                    raise ValueError(f"No end or junction called {place or '(blank)'}.")
            can_go(origin, destination)
            try:
                every, after = (int(float(row.get(key) or 0)) for key in ("every_min", "after_min"))
            except (TypeError, ValueError):
                raise ValueError("every_min and after_min are whole minutes.") from None
            if not (0 <= every <= most and 0 <= after <= most):
                raise ValueError(f"every_min and after_min are 0 to {most}.")
            if every and after:
                raise ValueError(f"{origin} → {destination}: a schedule repeats (every_min) or waits (after_min), not both.")
            fleet.append((origin, destination, every, after))
        now = network_tools._bool(args.get("dispatch_now"))
        for origin, destination, every, _ in fleet if now else []:
            self.dispatch_emergency(origin, destination, every, check_only=True)  # no run: refused here
        if check_only:
            return {"ok": True}
        dispatch.module.set_fleet(fleet)
        for origin, destination, every, after in fleet if now else []:
            self.dispatch_emergency(origin, destination, every, after_s=after * 60)
        return {"schedules": [{"from": origin, "to": destination, "every_min": every, "after_min": after}
                              for origin, destination, every, after in fleet], "dispatched_now": now}

    def _emergency_fleet(self) -> tuple[list, object, object]:
        """Test DEVL: the Dispatch Emergency Vehicle tab's schedules as (from, to, every s, s in), each checked; the
        DEVL extension (its signal override clears their lanes); and `random_for(seed, duration_s)`, a seed's own
        random emergency vehicle as (from, to, 0, seconds in), within the run. ValueError says what's missing."""
        dispatch, devl = self._extension("dispatch_emergency"), self._extension("dynamic_emergency_lane")
        if dispatch is None or devl is None:
            raise ValueError("Test DEVL needs the Dispatch Emergency Vehicle and Dynamic Emergency Vehicle Lane extensions: "
                             "see the Extensions tab.")
        fleet, places, can_go = dispatch.module.fleet(), self.places(), self._route_check("Dispatch Emergency Vehicle: no")

        for origin, destination, *_ in fleet:
            if origin not in places or destination not in places:
                raise ValueError(f"Dispatch Emergency Vehicle: no end or junction called "
                                 f"{(origin if origin not in places else destination) or '(blank)'}.")
            can_go(origin, destination)

        def random_for(seed: int, duration_s: float) -> tuple:
            start, end, delay = dispatch.module.random_emergency(seed, places, can_go,
                                                                 min(dispatch.module.MAX_DELAY_MIN * 60, int(duration_s)))
            return start, end, 0.0, float(delay)

        return [(origin, destination, every * 60.0, after * 60.0) for origin, destination, every, after in fleet], devl,             random_for

    def replay_run(self, path: str | Path) -> dict:
        """Replay a TSP run from its workbook (`path`) and the telemetry feed beside it: the same seed and
        simulated time, headless, each bus request getting the decision the feed recorded at the simulated time
        it reached the controller (tsp_agent.Replay), no model asked. With the same network and demand open it
        runs exactly as it did; its workbook ("<model>-replay") is compared with the original's Summary:
        {"file", "same", "differences": [[figure, original, replay]]}. ValueError says what can't run."""
        path = Path(path)
        feed = path.with_name(f"{path.stem} telemetry.jsonl")
        if self.runner is not None or self._testing:
            raise ValueError("A run or test is going on: stop it first (the menu bar's Stop), then replay.")
        if not path.is_file() or not feed.is_file():
            raise ValueError(f"A replay needs a run's workbook and its telemetry feed beside it ({feed.name}).")
        tsp = self._tsp()
        if tsp is None:
            raise ValueError("The Transit Signal Priority extension didn't load: see the Extensions tab.")
        demand = self._pair_demand()
        if not demand:
            raise ValueError("There's no demand to run: open the network and demand the run had, then replay.")
        sheets = read_xlsx(path)
        settings = {row[0]: row[1] for row in sheets.get("Run", [])[1:] if len(row) > 1}
        original = {row[0]: row[1] for row in sheets.get("Summary", [])[1:] if len(row) > 1}
        read_me = {row[0]: row[1] for row in sheets.get("Read me", [])[1:] if len(row) > 1}
        try:
            seed = int(settings["Random seed"])
            hours, minutes, seconds = (int(part) for part in str(original["Simulated time"]).split(":"))
        except (KeyError, ValueError) as exc:
            raise ValueError(f"{path.name} isn't a run's workbook (no seed or simulated time).") from exc
        duration = hours * 3600 + minutes * 60 + seconds
        agent = tsp.module.tsp_agent.Replay([json.loads(line) for line in feed.read_text(encoding="utf-8").splitlines()
                                             if line.strip()])
        override = self.extensions.overrides_for([tsp.name], agent=agent)
        model = f"{read_me.get('AI model') or 'run'}-replay"
        run_settings = self._run_settings(None, seed=seed, extra=[("Kind", f"replay of {path.name}"),
                                                                  ("Duration (simulated)", results.clock(duration))])
        network, modes = deepcopy(self.network), deepcopy(self.vehicle_modes.modes)
        _, replayed, _ = self._in_background(lambda progress, cancelled: self._one_run(
            network, demand, modes, seed, duration, override, run_settings, [], f"Replay of seed {seed}", progress,
            cancelled, model=model), "Replay")
        self._refresh_summary()
        if replayed is None:
            raise ValueError("The replay recorded nothing.")
        again = {row[0]: row[1] for row in read_xlsx(replayed).get("Summary", [])[1:] if len(row) > 1}
        differences = [[figure, value, again.get(figure)] for figure, value in original.items()
                       if isinstance(value, float) and again.get(figure) != value]
        return {"file": str(replayed), "same": not differences, "differences": differences}

    def _play(self) -> None:
        if self.runner is None:
            self._run()
            return
        self.runner.play()
        self.clock.play()

    def _pause(self) -> None:
        if self.runner is not None:
            self.runner.pause()  # returns with its last step published
        self.clock.pause()
        self._frame(self.clock.time_s)  # clock, map and data all show that last step

    def _set_speed(self) -> None:
        self._show_saved()
        speed = float(self.speed_combo.currentData())
        self.clock.speed = speed
        if self.runner is not None:
            self.runner.set_speed(speed)

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
        self.background_painter.close()  # and the map's tile downloads
        super().closeEvent(event)

    def _stop_runner(self) -> None:
        """End the run, whatever ended it (Stop, a new Run, a change to the network, closing, its physics failing):
        its results become a workbook in the results folder."""
        if self.runner is not None:
            runner, recorder = self.runner, self.recorder
            self.runner, self.recorder = None, None
            self._end_run(runner, recorder)

    def _end_run(self, runner, recorder) -> None:
        """_stop_runner's run, finished once its physics thread has ended: a step still going (a slow hook) is
        asked again shortly, the window carrying on meanwhile (closing then leaves it to results.recover)."""
        if not runner.stop(0.5):
            self.statusBar().showMessage("Stopping the run: its last step is still going.")
            QTimer.singleShot(200, self, lambda: self._end_run(runner, recorder))
            return
        self.extensions.report_failures()  # one that raised in the run's last steps too
        simulation = runner.simulation
        if recorder is not None:
            self.last_kpis = None

            def extras() -> None:
                recorder.add_sheets(self.extensions.reports(simulation.signal_override))  # TSP: its sheet, every decision
                self.last_kpis = self._observed_kpis(simulation, recorder)
                recorder.add_analysis(experiments.analysis_sheets(self.last_kpis))

            try:
                path, _ = self._end_recording(recorder, simulation, self.results_folder, extras, runner.error)
            except Exception as exc:  # noqa: BLE001 - never in the way of Stop or closing; results.recover tries again
                self.statusBar().showMessage(f"Couldn't save the run's results: {exc}", 15000)
                return
            if path is not None:
                self.last_results = path
                incomplete = f" (incomplete: {recorder.error})" if recorder.error else ""
                self.statusBar().showMessage(f"The run's results are saved{incomplete}: {path}", 15000)
        self._refresh_summary()

    def _export(self) -> None:
        """Export: a workbook of the run so far (it carries on), or of the last run."""
        if self.recorder is None and self.last_results is None:
            self._export_network()
            return
        recorder = self.recorder
        name = results.file_name(recorder.started, seed=recorder.seed, model=recorder.model) if recorder is not None \
            else self.last_results.name
        path, _ = QFileDialog.getSaveFileName(self, "Export Results", str(self.results_folder / name),
                                              "Excel workbook (*.xlsx)")
        if not path:
            return
        try:
            if recorder is not None:
                if self.runner is not None:
                    try:  # TSP's figures so far: its sheet, the agent's decisions, latency and tokens
                        recorder.add_sheets(self.extensions.reports(self.runner.simulation.signal_override))
                    except RuntimeError:  # the run changed them while they were read: export without
                        pass
                    for _ in range(3):  # what it has observed so far, analysed (read while the physics carries on)
                        try:
                            recorder.add_analysis(experiments.analysis_sheets(
                                self._observed_kpis(self.runner.simulation, recorder)))
                            break
                        except RuntimeError:
                            continue
                recorder.export(path)
            else:
                shutil.copyfile(self.last_results, path)
        except OSError as exc:
            QMessageBox.critical(self, "Export failed", str(exc))
            return
        self.statusBar().showMessage(f"Exported {path}", 8000)

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
        event["changes"] = self._state_changes(before, now) if before is not None else []
        if before is not None and network_tools.describe_changes(before[0], now[0], limit=1):
            event["map_png"] = self._map_png()
        if before is not None and before[1] != now[1]:
            event["demand_png"] = self._demand_png()
        event["results_folder"] = str(self.results_folder)
        event["project_file"] = str(self.network_path or "")
        event["network"] = self._network_summary()
        event["activity"] = self.activity.take() if self.activity.on else []  # what the user did since the last step
        self.extensions.log(event)

    def _state_changes(self, before: tuple, after: tuple) -> list[str]:
        """What changed between two of the window's states (_snapshot): the network (describe_changes), the
        OD matrix pair by pair, and the vehicle types, one line each."""
        lines = network_tools.describe_changes(before[0], after[0], limit=1000)
        old, new = before[1], after[1]
        for pair in sorted(set(old) | set(new)):
            a, b = old.get(pair), new.get(pair)
            what = []
            for key, label, unit in (("flow", "hourly", "veh/h"), ("daily", "daily", "trips/day")):
                x, y = getattr(a, key, None), getattr(b, key, None)
                if x != y:
                    what.append(f"{label} {x if x is not None else 'none'} → {y if y is not None else 'none'} {unit}")
            if a is not None and b is not None:
                if a.pattern != b.pattern:
                    what.append(f"arrivals {a.pattern} → {b.pattern}")
                if tuple(a.split) != tuple(b.split):
                    what.append(f"split {list(a.split)} → {list(b.split)} %")
                if a.profile != b.profile:
                    what.append("daily curve reshaped")
            if what:
                lines.append(f"Demand {pair[0]}->{pair[1]}: " + ", ".join(what))
        old_types, new_types = {m.name: asdict(m) for m in before[2][0]}, {m.name: asdict(m) for m in after[2][0]}
        lines += [f"Vehicle type {name} added" for name in new_types if name not in old_types]
        lines += [f"Vehicle type {name} removed" for name in old_types if name not in new_types]
        for name in old_types.keys() & new_types.keys():
            what = [f"{key} {old_types[name][key]} → {value}" for key, value in new_types[name].items()
                    if old_types[name].get(key) != value]
            if what:
                lines.append(f"Vehicle type {name}: " + ", ".join(what))
        if tuple(before[2][1]) != tuple(after[2][1]):
            lines.append(f"Default split {list(before[2][1])} → {list(after[2][1])} %")
        return lines

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

    def _export_network(self) -> None:
        """Export with no run yet: the network for analysis (its City sheet, junctions, roads with their geometry
        and curves, ends, demand, vehicle types, bus stops, institutions) and its GeoJSON beside it."""
        name = f"ACMS network {time.strftime('%Y-%m-%d %H-%M-%S')}.xlsx"
        path, _ = QFileDialog.getSaveFileName(self, "Export Network", str(self.results_folder / name),
                                              "Excel workbook (*.xlsx)")
        if not path:
            return
        try:
            results.network_workbook(path, self.network, self._pair_demand(), self.vehicle_modes.modes,
                                     self._run_settings(None))
        except OSError as exc:
            QMessageBox.critical(self, "Export failed", str(exc))
            return
        self.statusBar().showMessage(f"Exported the network (no run yet): {path} and its .geojson", 8000)

    def _results(self) -> dict | None:
        """View Summary's Results page: the run going on, or the last one."""
        if self.recorder is not None:
            return {"kpis": self.recorder.kpis(), "running": True, "file": "", "error": self.recorder.error}
        if self.last_kpis is not None:
            return {"kpis": self.last_kpis, "running": False, "file": str(self.last_results or ""), "error": ""}
        return None

    def _stop_run(self, note: str = "") -> None:
        """Stop: end the run, clear its vehicles and put the clock back to 00:00:00."""
        self._stop_runner()
        self.clock.stop()
        self.scene.show_frame(1.0, None)
        if note:
            self.statusBar().showMessage(note, 8000)

    def _frame(self, seconds: float) -> None:
        """Each display frame: draw the run's latest published frame. The physics never waits for
        this; frames it published in between are skipped. The clock shows the physics' time."""
        runner = self.runner
        if runner is not None:
            self.extensions.report_failures()  # an override raised in the physics thread: switched off, saying why
            if runner.error:  # the physics raised: the run ends there, its workbook saying why (_end_run)
                self._stop_run()
                return
            frame = runner.frame
            seconds = frame.time
            self.clock.hold(seconds)
            alpha = min(max((runner.target() - frame.time) / DT, 0.0), 1.0) if runner.running else 1.0
            behind = runner.running and runner.target() - frame.time > 2 * DT
            now = time.monotonic()
            # Behind: draw at most 10 frames a second, so drawing takes less time from the physics.
            if not behind or now - self._drawn_at >= 0.1:
                self.scene.show_frame(alpha, frame)
                self._drawn_at = now
            self.properties.show_plans(frame.plans)  # SCATS and SCOOT change theirs as they go
            self.clock_label.setToolTip(
                f"Behind: running at {runner.speed_now:.0f}×" if behind else "Simulation time")
            self.clock_label.setStyleSheet(f"color: {STATUS['waiting']};" if behind else "")
        if not self._testing:  # a headless test's runs have the clock meanwhile (_show_headless)
            self.clock_label.setText(clock_text(seconds))

    def _power_saver(self, on: bool) -> None:
        """Power Saver: the map draws a run 30 times a second instead of 60, and on a GPU without multisampling.
        The physics is untouched (it never waits for drawing)."""
        self.clock.set_step_ms(2 * STEP_MS if on else STEP_MS)
        self.view.set_power_saver(on)

    def _traffic(self, args: dict) -> dict:
        """The live run for the assistant (read_traffic): its latest published frame."""
        if self.runner is None:
            return {"error": "No live run is going (the user presses Run, ▷ ↻ in the menu bar corner, to start one). "
                             "run_simulation's runs are headless: read them with read_results or analyse_run."}
        road = str(args.get("road") or "") or None
        known = {*self.network.roads, *self.network.approach_roads, *self.network.nodes, *self.network.approach_nodes}
        if road and road not in known:  # a wrong id once matched nothing and said so only as "matching 0"
            own = network_tools._road_from_other_end(self.network, road)
            if own is None:
                return network_tools.explain_error(f"Unknown id '{road}'.", self.network)
            road = own
        return self.runner.frame.records(road, int(args.get("limit") or 100))

    def _pair_demand(self) -> list[PairDemand]:
        """Every OD pair with demand of the kind the Demand Generator shows, for a run, and the institutions'
        trips (institutions.run_demand: catchment to door and back; its notes in `_institution_notes`)."""
        matrix = self.od_matrix
        matrix.set_ends(*self._od_ends())
        daily = matrix.mode == "daily"
        institutions, self._institution_notes = run_demand(self.network, self.vehicle_modes.modes, daily)
        return [PairDemand(origin, destination, None if daily else cell.flow, cell.daily if daily else None,
                           cell.profile.shares() if daily else [], cell.pattern, matrix.split_of(cell),
                           quarters=() if daily else cell.quarters)
                for (origin, destination), cell in matrix.cells.items()
                if origin in matrix.origins and destination in matrix.destinations
                and (cell.daily if daily else cell.flow)] + institutions

    def _show_clock_state(self, running: bool) -> None:
        self.pause_action.setVisible(running)
        self.play_action.setVisible(not running)

    def _name_open_tab(self, current: int) -> None:
        self.page_title.setText(self.tab_names[current] if current >= 0 else "")
        if self.activity.on:
            self.activity.log("tab", self.page_title.text())

    def _build_menu(self) -> None:
        file_menu = self.menuBar().addMenu("&File")
        load_action = QAction("&Open Project…", self)
        load_action.setShortcut(QKeySequence.StandardKey.Open)
        load_action.triggered.connect(self.load_file)
        save_action = QAction("&Save Project", self)
        save_action.setShortcut(QKeySequence.StandardKey.Save)
        save_action.triggered.connect(lambda: self.save_file())
        save_as_action = QAction("Save Project &As…", self)
        save_as_action.setShortcut(QKeySequence("Ctrl+Shift+S"))
        save_as_action.triggered.connect(lambda: self.save_file(as_new=True))
        for action, tip in ((load_action, "Open a project"), (save_action, "Save the project"),
                            (save_as_action, "Save as a new file")):
            action.setToolTip(tip)
            file_menu.addAction(action)
        file_menu.setToolTipsVisible(True)
        file_menu.addSeparator()
        file_menu.addAction("E&xit", self.close, QKeySequence.StandardKey.Quit)
        edit_menu = self.menuBar().addMenu("&Edit")
        self.undo_action = QAction("&Undo", self)
        self.undo_action.setShortcut(QKeySequence("Ctrl+Z"))
        self.undo_action.triggered.connect(self.undo)
        self.redo_action = QAction("&Redo", self)
        self.redo_action.setShortcuts([QKeySequence("Ctrl+Y"), QKeySequence("Ctrl+Shift+Z")])
        self.redo_action.triggered.connect(self.redo)
        edit_menu.addAction(self.undo_action)
        edit_menu.addAction(self.redo_action)

    def _connect_signals(self) -> None:
        self.generate_button.clicked.connect(self.generate)
        self.driving_side.changed.connect(self.set_driving_side)
        self.speed_limit_combo.currentIndexChanged.connect(
            lambda: self._set_default("default_speed_limit_kmh", float(self.speed_limit_combo.currentData())))
        self.lanes_spin.valueChanged.connect(lambda value: self._set_default("default_lanes_per_direction", value))
        self.lane_changing_check.toggled.connect(lambda on: self._set_default("default_lane_changing", on))
        self.signal_method_combo.currentIndexChanged.connect(
            lambda: self._set_default("default_signal_method", self.signal_method_combo.currentData()))
        self.fit_button.clicked.connect(self.view.fit_network)
        self.reset_button.clicked.connect(self.view.reset_view)
        self.scene.clicked.connect(self.canvas_clicked)
        self.scene.boxed.connect(self.box_selected)
        self.properties.delete_group.connect(self.delete_group)
        self.properties.slip_road_requested.connect(self.add_slip_road)
        self.add_node_button.toggled.connect(self.toggle_add_node)
        self.add_edge_button.toggled.connect(self.toggle_add_edge)
        self.metro_button.toggled.connect(self.toggle_metro)
        self.scene.redrawn.connect(self._show_metro_summary)  # whatever changed the network: an edit, undo, a file
        self.scene.redrawn.connect(lambda: self.workspace.currentWidget() is self.metro_matrix
                                   and self.metro_matrix.show_network(self.network))
        self.metro_matrix.changed.connect(self._commit)
        self.scene.metro_clicked.connect(self.metro_clicked)
        self.view.right_clicked.connect(lambda: self.metro_button.setChecked(False))  # ends a deployment
        self.edit_tabs.currentChanged.connect(lambda: self._end_modes_except(None))
        self.edge_kind.changed.connect(self._refresh_mode)
        self.tabs.currentChanged.connect(self.tab_changed)
        self.tabs.currentChanged.connect(lambda: self.show_grid(self.tabs.currentWidget() is self.demand_tab
                                                                and self.od_panel.demand.currentData() != "institutions"))
        self.od_panel.demand_chosen.connect(self._demand_chosen)
        self.od_matrix.table.pressed.connect(self._cell_clicked)
        self.institution_panel.placing_changed.connect(self._placing)
        self.institution_panel.delete_requested.connect(self._delete_institution)
        self.institution_panel.split_requested.connect(self._open_mode_split)
        self.institution_panel.height_requested.connect(self._set_institution_height)
        self.scene.placement_changed.connect(lambda: self.institution_panel.show_step(self.scene.placing))
        self.scene.placed.connect(self._place_institution)
        self.properties.drag_button.toggled.connect(self.toggle_drag)
        self.scene.drag_finished.connect(self.model_updated)
        self.properties.network_changed.connect(self.model_updated)
        self.properties.interchange_requested.connect(self.build_interchange)
        self.properties.edited.connect(self._commit)
        self.background_panel.changed.connect(self._background_changed)
        self.background_panel.tool_changed.connect(self._background_tool)
        self.view.pointed.connect(self._show_pointer)
        self.properties.bus_stop_changed.connect(self._bus_stop_changed)
        self.scene.bus_stop_moved.connect(self._bus_stop_changed)
        self.od_panel.changed.connect(self._demand_changed)
        # Renamed, added, deleted types; a headway's, PCU's or occupancy's arrow clicks merge into one undo step.
        self._types_key = None  # while the assistant adds types: one undo step for them all
        self.vehicle_modes.changed.connect(lambda action, index: self._commit(
            self._types_key or ((action, index) if action in ("headway", "pcu", "occupancy") else None)))
        self.ai_mode.indicator.connect(self._show_ai_status)
        self.ai_mode.indicator.connect(self._refresh_summary)  # AI Usage follows each request
        self.ai_status.linkActivated.connect(self._open_system_2)
        self.ai_mode.attach(network=lambda: self.network, selection=self._selection_info,
                            show=self._show_on_map, edited=self._ai_edited, od=self._od_command,
                            traffic=self._traffic, types=self._types_command, runs=self._runs_command,
                            state=lambda: self._snapshot() if self.extensions.wants("log") else None,
                            step=self._research_step)
        QShortcut(QKeySequence(Qt.Key.Key_Escape), self).activated.connect(self.cancel)
        # Only while the map has focus, so Delete still edits text in the side panel.
        delete = QShortcut(QKeySequence(QKeySequence.StandardKey.Delete), self.view)
        delete.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        delete.activated.connect(self.delete_selection)

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
            cursor, hint = cross, "Click empty space on the map to place the new node. Esc cancels."
        elif self.add_edge_button.isChecked():
            cursor = cross
            if self.edge_start is None:
                hint = "Click the junction the new road starts from. Esc cancels."
            elif self.edge_kind.value() == "end":
                hint = f"From {self.edge_start}: click empty space where the boundary end goes."
            else:
                hint = f"From {self.edge_start}: click the junction to connect to."
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
            selected = self.network.road(identifier) if kind == "road" else                 self.network.bus_stops[identifier] if kind == "bus_stop" else                 self.network.institutions[identifier] if kind == "institution" else self.network.node(identifier)
        except KeyError:
            kind, selected = "", None
        self.multi_nodes, self.group = [], []
        if kind and self.activity.on:
            self.activity.log("select", kind.replace("_", " "), identifier)
        self.scene.set_selection(kind, identifier)
        building = kind == "institution"  # shown in Demand Generator → Institutions, not the Edit tab
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
        if self.add_node_button.isChecked():
            if kind == "node":
                self._show_hint(f"{identifier} is already there. Click empty space for the new node.")
                return
            self.add_node_button.setChecked(False)
            node = self.network.add_node(x, y)
            self.select("node", node.id)
            self.statusBar().showMessage(self._network_summary() + f" • Added {node.id}")
            self._commit()
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
            elif kind:
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
            self.background_panel.cancel_tool()
            return
        if self.scene.placing is not None:
            self.institution_panel.end_placing()
            return
        if self.tabs.currentWidget() is self.demand_tab and self.od_matrix.selected_pairs():
            self.od_matrix.clear_selection()  # the OD Pair editor goes with it
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
        elif kind:
            self.properties.delete_node()

    def _end_modes_except(self, keep: QPushButton | None) -> None:
        """Add Node, Add Edge, Drag to Adjust and metro deployment all capture canvas clicks: one at a time."""
        for button in (self.add_node_button, self.add_edge_button, self.properties.drag_button, self.metro_button):
            if button is not keep:
                button.setChecked(False)

    def tab_changed(self) -> None:
        if self.tabs.currentWidget() is not self.edit_tab:
            self._end_modes_except(None)  # also cancels a half-picked edge
        if self.tabs.currentWidget() is not self.create_tab:
            self.background_panel.end_tool()
        if self.tabs.currentWidget() is not self.demand_tab:
            self.institution_panel.end_placing()
            self.od_matrix.clear_selection()  # the OD Pair editor shows again only when a cell is clicked
        self.grid_toggles[0].setVisible(self.tabs.currentWidget() is self.demand_tab)  # the grid is the Demand tab's
        self.view.set_corner(self.fit_button.parentWidget())  # the map's corner controls, re-placed
        self._refresh_grid()
        self._refresh_summary()

    def show_grid(self, on: bool) -> None:
        """The main area shows the input grid (the OD matrix; the metro's, with Demand on Metro) or the network; both
        toggles follow."""
        for toggle in self.grid_toggles:
            toggle.setChecked(on)
        metro = on and self.od_panel.demand.currentData() == "metro"
        if metro:
            self.metro_matrix.show_network(self.network)
        self.workspace.setCurrentWidget(self.metro_matrix if metro else self.od_matrix if on else self.view)
        self._refresh_grid()

    def _refresh_grid(self) -> None:
        """While the grid shows, its rows and columns follow the network's boundary ends."""
        if self.workspace.currentWidget() is self.od_matrix:
            self.od_matrix.set_ends(*self._od_ends())

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

    def _od_ends(self) -> tuple[list[str], list[str]]:
        """Boundary ends traffic can enter from (origins) and leave by (destinations).
        A one-way boundary road only works one way round."""
        origins, destinations = [], []
        for road in self.network.approach_roads.values():
            end = road.from_node if road.from_node in self.network.approach_nodes else road.to_node
            inbound = road.from_node == end
            if road.direction == "two-way" or inbound:
                origins.append(end)
            if road.direction == "two-way" or not inbound:
                destinations.append(end)

        def order(end: str) -> tuple:  # compass sides N, E, S, W, then number
            try:
                return ("NESW".index(end[-1]), float(end[:-1]), end)
            except ValueError:
                return (4, 0.0, end)  # older files' names, e.g. "A_W_1"
        return sorted(origins, key=order), sorted(destinations, key=order)

    def toggle_add_node(self, checked: bool) -> None:
        if checked:
            self._end_modes_except(self.add_node_button)
        self._refresh_mode()

    def toggle_metro(self, checked: bool) -> None:
        """Start deployment: every map click a metro stop, the second making a new line and each one after it
        extending it; unchecked (a right-click, Esc, another tool or tab) the deployment ends and the line is
        accepted, final (one undo step; no later edits), or deleted (`_accept_metro_line`). A first stop with no
        second is dropped: a line needs track."""
        if checked:
            self._end_modes_except(self.metro_button)
        elif self.metro_line in self.network.metro_lines:
            line = self.network.metro_lines[self.metro_line]
            summary = f"{line.id}: {len(line.stops)} stops, {self.network.metro_length_m(line.id) / 1000:.2f} km of track"
            service = self._accept_metro_line(summary)
            if service is not None:
                for name, value in service.items():
                    setattr(line, name, value)
                self.statusBar().showMessage(f"{self._network_summary()} • Metro {summary}, a train every "
                                             f"{line.headway_min:g} min, accepted")
                self._commit()
            else:
                self.network.delete_metro_line(line.id)
                self.statusBar().showMessage(f"{self._network_summary()} • Metro {line.id} deleted")
        self.metro_line, self.scene.metro_pending = None, None
        self.scene.metro_deploying = checked
        self.scene.rebuild()
        self._refresh_mode()

    def metro_clicked(self, x: float, y: float) -> None:
        """A stop where the map was clicked while deploying: the first waits for the second, which makes the
        line; then each joins the line's last stop. Refused (too near the last stop) says why and changes nothing.
        Nothing is an undo step until the line is accepted (toggle_metro)."""
        if self.metro_line is not None and self.metro_line not in self.network.metro_lines:
            self.metro_line = None  # undone while deploying: start a new line
        try:
            if self.metro_line is not None:
                self.network.add_metro_stop(self.metro_line, x, y)
            elif self.scene.metro_pending is None:
                self.scene.metro_pending = (x, y)
            else:
                self.metro_line = self.network.add_metro_line(self.scene.metro_pending, (x, y)).id
                self.scene.metro_pending = None
        except ValueError as exc:
            self._show_hint(f"{exc} Click elsewhere, or right-click to finish.")
            return
        self.scene.rebuild()
        self._refresh_mode()

    def _accept_metro_line(self, summary: str) -> dict | None:
        """The finished line: accepted with its service (headway, speed, carriages a train and passengers a carriage,
        fare: assumed values until changed, METRO_SERVICE) as final, never edited after; or deleted (None)."""
        dialog = QDialog(self)
        dialog.setWindowTitle("Finish metro line")
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel(f"Metro {summary}. Accept it as final, or delete it?"))
        note = hint_label("Assumed service: set this line's own.")
        layout.addWidget(note)
        form, boxes = QFormLayout(), {}
        for name, label, suffix in (("headway_min", "A train every", " min"), ("speed_kmh", "Speed, stops included",
                                                                               " km/h"),
                                    ("cars_per_train", "Carriages a train", ""),
                                    ("car_capacity", "Passengers a carriage", ""), ("fare_cents", "Fare", " ¢")):
            default, low, high = METRO_SERVICE[name]
            box = QSpinBox() if isinstance(default, int) else QDoubleSpinBox()
            box.setRange(low, high)
            box.setSuffix(suffix)
            box.setValue(default)
            form.addRow(label, box)
            boxes[name] = box
        layout.addLayout(form)
        buttons = QDialogButtonBox()
        accept = buttons.addButton("Accept", QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton("Delete", QDialogButtonBox.ButtonRole.DestructiveRole).clicked.connect(dialog.reject)
        accept.clicked.connect(dialog.accept)
        accept.setDefault(True)
        layout.addWidget(buttons)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        return {name: box.value() for name, box in boxes.items()}

    def _show_metro_summary(self) -> None:
        """Lines, stops and track; and each line's busiest segment at the design hour against its capacity."""
        network = self.network
        lines = len(network.metro_lines)
        if not lines:
            self.metro_summary.setText("No metro lines yet")
            return
        km = sum(network.metro_length_m(line_id) for line_id in network.metro_lines) / 1000
        text = [f"{lines} line{'s' * (lines != 1)}, {len(network.metro_stops)} stops, {km:.2f} km of track"]
        report = self._metro_report()
        for row in report["lines"]:
            if row["riders_per_h"]:
                text.append(f"{row['line']}: {row['peak_load_per_h']:,} an hour at its busiest ({row['peak_segment']}), "
                            f"{row['v_c']:.0%} of {row['capacity_per_h_each_way']:,}"
                            + (" – over capacity" if row["over_capacity"] else ""))
        self.metro_summary.setText("\n".join(text))

    def _metro_report(self) -> dict:
        """metro_report: the metro's passengers at the design hour (the hour the most ride), on their lines
        (metro.loads): its own OD matrix (Network.metro_demand, the same every hour) and each building's metro riders,
        its metro share of the people arriving and leaving, between its door station and its catchment's stations in
        equal shares (metro.places), as its trips go."""
        network, by_hour = self.network, []  # (board, alight, passengers each hour)
        for pair, people in network.metro_demand.items():
            board, alight = pair.split("->")
            by_hour.append((board, alight, [people] * 24))
        for item in network.institutions.values():
            share = institution_shares(item).get(METRO, 0.0) / 100
            entrance, stations = metro_places(network, item)
            if not share or not stations:
                continue
            arrive, leave = hourly_people(item)
            for station in stations:
                by_hour.append((station, entrance, [people * share / len(stations) for people in arrive]))
                by_hour.append((entrance, station, [people * share / len(stations) for people in leave]))
        hour = max(range(24), key=lambda h: sum(hours[h] for *_, hours in by_hour)) if by_hour else 8
        report = metro_loads(network, [(board, alight, hours[hour]) for board, alight, hours in by_hour])
        return {"design_hour": f"{hour:02d}:00-{hour + 1:02d}:00", **report,
                "method": "passengers on their line (no transfers), each stop-to-stop segment's load each way against "
                          "HCM 2000 Eq 27-28's capacity, (60 / headway) trains x carriages x passengers a carriage x "
                          "PHF 0.75"}

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

    def install_network(self, network: Network, fit: bool = True) -> None:
        self._end_modes_except(None)
        self.network = network
        self.scene.set_network(network)
        self.properties.set_network(network)
        self.horizontal_spin.setValue(network.horizontal_corridors)
        self.vertical_spin.setValue(network.vertical_corridors)
        self.driving_side.set_value(network.driving_side)
        speed_index = self.speed_limit_combo.findData(network.default_speed_limit_kmh)
        if speed_index < 0:
            value = network.default_speed_limit_kmh
            self.speed_limit_combo.addItem(f"{value:g} km/h", value)
            speed_index = self.speed_limit_combo.count() - 1
        self.speed_limit_combo.setCurrentIndex(speed_index)
        self.lanes_spin.setValue(network.default_lanes_per_direction)
        self.lane_changing_check.setChecked(network.default_lane_changing)
        self.signal_method_combo.setCurrentIndex(max(0, self.signal_method_combo.findData(network.default_signal_method)))
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
        background = self.network.background  # a new grid keeps the backdrop it is drawn over
        if background.kind == "map":  # and the place under the old network's centre goes under its centre
            old, new = self._junction_bounds().center(), self._junction_bounds(network).center()
            background.lat, background.lon = map_frame(background).to_wgs84(old.x(), old.y())
            background.anchor_x, background.anchor_y = new.x(), new.y()
        network.background = background
        self.install_network(network)
        self._commit()

    def _bus_stop_changed(self, stop_id: str) -> None:
        """A bus stop was added, moved (panel or arrow keys) or re-pointed: show it selected and
        redrawn, as one undo step: adding it is a step of its own, and a burst of nudges or edits
        to it after that merges into one."""
        self.select("bus_stop", stop_id)
        stop = self.network.bus_stops[stop_id]
        self.statusBar().showMessage(f"{self._network_summary()} • Bus stop {stop_id} on {stop.road}")
        added = stop_id not in self.history.current[0].bus_stops
        self._commit(None if added else ("bus_stop", stop_id))

    def _demand_chosen(self, kind: str) -> None:
        """The Demand drop-down: Institutions shows the map to place buildings on; hourly or daily, the grid."""
        if kind != "institutions":
            self.institution_panel.end_placing()
        if self.tabs.currentWidget() is self.demand_tab:
            self.show_grid(kind != "institutions")

    def _cell_clicked(self, *_) -> None:
        """A grid cell clicked from another tab (the Input Grid toggle): its editor is the Demand
        Generator's, so that tab opens, on the grid's hourly or daily demand."""
        if self.od_panel.demand.currentData() in ("institutions", "metro"):
            self.od_panel.demand.setCurrentIndex(self.od_panel.demand.findData(self.od_matrix.mode))
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
        self.view.redraw()
        self.pointer.setVisible(self.network.background.kind == "map")

    def _background_tool(self, tool: object) -> None:
        """A background tool started or ended: the map's cursor says which; the Edit tab's tools end.
        Choose Location (the map) and Set Scale (a new image) come first: the network is hidden and the
        tabs and the other Create sections wait until they end; the wheel zooms about the view's centre,
        where the network's centre is, while choosing a place."""
        if tool is not None:
            self._end_modes_except(None)
        setup = tool in ("locate", "scale")
        was_locating = self.view.transformationAnchor() == QGraphicsView.ViewportAnchor.AnchorViewCenter
        self.scene.set_hidden(setup)
        self.tabs.tabBar().setEnabled(not setup)
        for section in (self.generate_section, self.grid_toggles[0]):
            section.setEnabled(not setup)
        self.view.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter if tool == "locate"
                                          else QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        if tool == "locate":
            self.view.centerOn(self._junction_bounds().center())
        elif was_locating:
            QTimer.singleShot(0, self.view.fit_network)  # back from looking round the map: the network in view
        self.view.set_tool_cursor(Qt.CursorShape.SizeAllCursor if tool in ("move", "locate") else
                                  Qt.CursorShape.CrossCursor if tool in ("align", "measure", "scale")
                                  else Qt.CursorShape.ArrowCursor)

    def _show_pointer(self, x: float, y: float) -> None:
        """The mouse's latitude and longitude, while the map shows; Measure's line follows it."""
        self.background_panel.hover(x, y)
        background = self.network.background
        if background.kind == "map":
            lat, lon = map_frame(background).to_wgs84(x, y)
            self.pointer.setText(f"{lat:.6f}°, {lon:.6f}°")

    def _background_changed(self, key: object) -> None:
        """The background changed (the Create tab's section): redrawn, one undo step; a run carries
        on, as it drives the roads, not their backdrop. A place just entered is shown."""
        self._show_background()
        if key == "place":
            background = self.network.background
            self.view.centerOn(background.anchor_x, background.anchor_y)
        self._commit(key if key != "place" else ("background", "place"), stop_run=False)

    def model_updated(self, note: str = "Model updated") -> None:
        if self.multi_nodes:
            self._select_nodes([node for node in self.multi_nodes if node in self.network.nodes])
        else:
            self.select(*(self.scene.selection or ("", "")))
        self.statusBar().showMessage(f"{self._network_summary()} • {note}")
        self._commit()

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
            specs = network_tools._parsed_json(specs)
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

    def _ai_edited(self, tool: str) -> None:
        """The assistant changed the network: redraw, and make it one undo step."""
        if tool in ("generate_grid", "network_from_background"):  # a whole new network: the Create tab too, in view
            self.install_network(self.network)
            self._commit()
        elif tool in ("deploy_institutions", "place_institution"):  # buildings only: a run carries on
            self.scene.rebuild()
            self.statusBar().showMessage(f"{self._network_summary()} • Institutions placed by the assistant "
                                         "(Ctrl+Z undoes it)")
            self._commit(stop_run=False)
        else:
            self.model_updated("Changed by the assistant (Ctrl+Z undoes it)")

    # --- Undo / redo: snapshots of the network and the OD matrix entries -------------

    def _snapshot(self) -> tuple:
        # ponytail: whole-state copies; fine for editor-sized networks, switch to
        # per-action change records if 20x20 grids make typing feel slow.
        return deepcopy(self.network), deepcopy(self.od_matrix.cells), self.vehicle_modes.state()

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

    def _commit(self, merge_key: object = None, stop_run: bool = True) -> None:
        """Call when an action finishes; the state before it becomes an undo step."""
        if self.runner is not None and stop_run:  # a run drives the network and vehicle types as they were
            self._stop_run("The network or vehicle types changed, so the run stopped. Press Run to start again.")
        if self._building:  # the build commits once when it ends
            self._refresh_grid()
            return
        if self.history.commit(self._snapshot(), merge_key):
            self._show_metro_summary()  # riders come and go with demand and buildings' splits as well as lines
            self._refresh_grid()  # the assistant may add or remove an end while the grid shows
            self._refresh_undo_actions()
            self._show_saved()
            self._refresh_summary()
            self._log_edits()

    def _refresh_undo_actions(self) -> None:
        self.undo_action.setEnabled(bool(self.history.undo_stack))
        self.redo_action.setEnabled(bool(self.history.redo_stack))

    def undo(self) -> None:
        if self.metro_button.isChecked():  # deploying: the unfinished line isn't an undo step yet; it goes alone
            if self.metro_line in self.network.metro_lines:
                self.network.delete_metro_line(self.metro_line)
            self.metro_line = None
            self.metro_button.setChecked(False)
            self.statusBar().showMessage("The unfinished metro line was taken back.")
            return
        self._restore(self.history.undo(), "Undone. Ctrl+Y redoes it.", "Nothing to undo.")

    def redo(self) -> None:
        self._restore(self.history.redo(), "Redone.", "Nothing to redo.")

    def _restore(self, state: tuple | None, done: str, nothing: str) -> None:
        if state is None:
            self.statusBar().showMessage(nothing, 3000)
            return
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
        """The project's parts beyond the network, as plain data for network_io."""
        pairs = [{"origin": origin, "destination": destination, "flow": cell.flow, "daily": cell.daily,
                  "pattern": cell.pattern, "split": list(cell.split),
                  **({"quarters": list(cell.quarters)} if cell.quarters else {}),
                  "profile": {"kind": cell.profile.kind, "points": [list(point) for point in cell.profile.points],
                              "coefficients": list(cell.profile.coefficients)}}
                 for (origin, destination), cell in self.od_matrix.cells.items()
                 if cell.flow is not None or cell.daily is not None]
        return {"demand": {"shown": self.od_matrix.mode, "pairs": pairs},
                "vehicle_types": {"types": [asdict(mode) for mode in self.vehicle_modes.modes],
                                  "default_split": list(self.vehicle_modes.default_split)},
                "run": {"seed": self.seed_box.value(), "speed": float(self.speed_combo.currentData())}}

    def _save_marker(self) -> tuple:
        return self.history.current, self.seed_box.value(), self.speed_combo.currentIndex()

    def unsaved(self) -> bool:
        """Changed since last saved or opened: another undo state (edits, demand, types), seed or speed."""
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
        """Save the project (to its file; asking where the first time or for Save As). True once saved."""
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
        """Before closing or opening another project: save, discard or stay (unsaved changes only)."""
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
            types = project.get("vehicle_types")
            # A project saved before `timetabled`: a type with a headway, or named like a bus, is timetabled.
            modes, default_split = (([VehicleMode(**{"timetabled": bool(record.get("headway_min")) or "bus" in str(record.get("name")).lower(),
                                                    **record}) for record in types["types"]], tuple(types["default_split"]))
                                    if types else VehicleModes().state())
            cells = {}
            for pair in project.get("demand", {}).get("pairs", []):
                if len(pair["split"]) != len(modes):
                    raise ValueError(f"Demand {pair['origin']}->{pair['destination']}: its split doesn't match the "
                                     f"{len(modes)} vehicle types.")
                shape = pair.get("profile") or {}
                profile = DailyProfile(shape["kind"], [tuple(point) for point in shape["points"]],
                                       list(shape.get("coefficients", []))) if shape else DailyProfile()
                cells[(pair["origin"], pair["destination"])] = ODCell(pair.get("flow"), pair.get("daily"), profile,
                                                                      pair.get("pattern", "Random"), tuple(pair["split"]),
                                                                      quarter_shares(pair.get("quarters") or [1] * 4))
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
        self._commit()
        # Its own history: Undo can't bring the last project back under this one's name (Save would overwrite it)
        self.history = History(self.history.current)
        self._refresh_undo_actions()
        self._saved = self._save_marker()
        self._show_saved()
        self.statusBar().showMessage(f"Opened {path}: {len(network.nodes)} junctions, {len(cells)} OD pairs with "
                                     f"demand, {len(modes)} vehicle types, seed {self.seed_box.value()}", 8000)
        return True


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

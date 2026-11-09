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
from itertools import accumulate
from types import SimpleNamespace

from PySide6.QtCore import (QBuffer, QEvent, QIODevice, QPoint, QRectF, QSize, QStandardPaths, Qt, QThread,
                            QTimer)
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
from traffic_simulator.ai_mode import AIModePanel
from traffic_simulator.background import BackgroundPainter, BackgroundPanel, map_frame
from traffic_simulator.extensions import ExtensionsPanel
from traffic_simulator.activity import ActivityLog
from traffic_simulator.clock import SimulationClock
from traffic_simulator import (accessibility, analysis, assistant, design_checks, experiments, network_tools, results,
                               run_reports, scenario, searches, stats, weaving)
from traffic_simulator.signal_agent import INSTRUCTIONS, SignalAgent
from traffic_simulator.lane_graph import Graph
from traffic_simulator.main_experiments import ExperimentsMixin
from traffic_simulator.main_runs import RunsMixin
from traffic_simulator.main_scenario import ScenarioMixin
from traffic_simulator.simulation import PairDemand, Runner, Simulation
from traffic_simulator.metro import METRO
from traffic_simulator.geometry import nearest_along, point_along
from traffic_simulator.walking import footpath_join
from traffic_simulator.history import History
from traffic_simulator.institutions import metro_report, place, run_demand
from traffic_simulator.interchanges import (
    build_diamond,
    build_slip_road,
    build_trumpet,
    guess_stem,
    pair_legs,
    roads_left_at_hub,
)
from traffic_simulator.mode_behavior import ModeBehaviorPanel, VehicleModes
from traffic_simulator.models import (METRO_MIN_SPACING_M, METRO_SERVICE, MetroCrossing, Network, institution_problem,
                                      route_choice_problem)
from traffic_simulator.network_generator import generate_network
from traffic_simulator.network_io import (MAX_SEED, load_network, project_parts, project_record, save_network,
                                         saved_conversation, saved_project)
from traffic_simulator.network_scene import NetworkGraphicsScene, NetworkView
from traffic_simulator.network_tools import build_edge, delete_objects, edge_crossings
from traffic_simulator.demand import od_ends, pair_demand
from traffic_simulator.od_matrix import (MetroMatrix, ModeSplitDialog, ODCellPanel, ODMatrix, PedestrianMatrix,
                                        SpawnMatrix)
from traffic_simulator.properties_panel import PropertiesPanel, describe_group, TIMING_METHODS
from traffic_simulator.summary import SummaryPanel
from traffic_simulator.theme import apply_theme, float_glass
from traffic_simulator.widgets import (ICON_FONTS, ChoiceButtons, Collapsible, OpenPageTabs, align_form_labels,
                                       glyph_icon, hint_label, icon_button, styled_button)
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


# Sidebar tab icons: glyphs from Windows' own icon font (see widgets.ICON_FONTS).
# Without either font, tabs show their names.
TAB_ICONS = {"Create Network": "\ue800", "Edit Network": "\ue70f", "Mode Behavior": "\ue804",
             "Demand Generator": "\ue80a", "AI Mode": "\ue99a", "View Summary": "\ue9d2",
             "Extensions": "\uea86"}  # map, pencil, car, table, robot, chart, puzzle
TAB_ICON_PX = 20
# The grid a new window starts with, and the Create Network tab's settings for new roads
DEFAULT_HORIZONTAL_CORRIDORS, DEFAULT_VERTICAL_CORRIDORS = 3, 4
DEFAULT_DRIVING_SIDE, DEFAULT_SPEED_LIMIT_KMH, DEFAULT_LANES_PER_DIRECTION, DEFAULT_LANE_CHANGING = "left", 50, 2, True
RUN_SEED = 42  # a Run's seed unless the Seed box says otherwise: the same inputs and seed repeat exactly
MAX_AT_ONCE = 8  # a batch's runs going at once, at most (TSP keeps the state of twice as many)
HEADLESS_WAIT_S = 30.0  # a headless test's deadline for each agent answer (Batch Run sets its own): missed, the plan
TITLE = "ACMS — Agentic Context-Aware Mobility System"


class MainWindow(RunsMixin, ScenarioMixin, ExperimentsMixin, QMainWindow):
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

    def _build_ui(self) -> None:
        root = QWidget()
        root.setObjectName("appShell")
        layout = QHBoxLayout(root)
        layout.setContentsMargins(12, 0, 12, 8)  # the menu bar's own padding is the gap above
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
        title_row.setSpacing(10)
        title_row.addWidget(title, 0, Qt.AlignmentFlag.AlignTop)  # tops aligned: the page title is lowered to
        title_row.addWidget(self.page_title, 0, Qt.AlignmentFlag.AlignTop)  # the brand's baseline once styled
        title_row.addStretch(1)
        side_layout.addLayout(title_row)
        side_layout.addWidget(hint_label("Build and refine networks. Distances in metres."))

        # Three sections that open and close: Generate Network (what new roads and signals get, and a grid of
        # them), a network of your own over a colour, image or map, and Universal Attributes (driving side).
        create_tab = QWidget()
        create_layout = QVBoxLayout(create_tab)
        create_layout.setSpacing(6)
        roads = QWidget()
        form = QFormLayout(roads)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.FieldsStayAtSizeHint)
        self.speed_limit_combo = QComboBox()
        for speed in (20, 30, 40, 50, 60, 70, 80, 100, 120):
            self.speed_limit_combo.addItem(f"{speed} km/h", speed)
        self.speed_limit_combo.setCurrentText(f"{DEFAULT_SPEED_LIMIT_KMH:g} km/h")
        self.speed_limit_combo.setToolTip("Speed limit of new roads")
        self.lanes_spin = QSpinBox()
        self.lanes_spin.setRange(1, 12)  # as a road's own (the Edit tab, set_road)
        self.lanes_spin.setValue(DEFAULT_LANES_PER_DIRECTION)
        self.lanes_spin.setToolTip("Lanes of new roads")
        self.lane_changing_check = QCheckBox("Allowed")
        self.lane_changing_check.setChecked(DEFAULT_LANE_CHANGING)
        self.lane_changing_check.setToolTip("Lane changes on new roads")
        form.addRow("Speed limit", self.speed_limit_combo)
        form.addRow("Lanes per direction", self.lanes_spin)
        form.addRow("Lane changing", self.lane_changing_check)
        self.signal_method_combo = QComboBox()  # the network's default; demand retimes them at Run
        for label, method in TIMING_METHODS[:-1]:  # not Manual: a new junction has no windows yet
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
        self.background_panel.view = self.view  # the work zone is framed on it, its bar over the map
        self.view.set_top(self.background_panel.bar)
        self.view.zoomed.connect(self.background_panel.show_zone)
        self.view.zoom_range = self._zoom_range
        self.scene.background_tool = self.background_panel  # its Move and Two Points take the map's clicks
        self.create_tab = create_tab
        self.background_panel.show_background(self.network.background)
        self.own_section = Collapsible("Own Network", self.background_panel, tooltip="Draw over a background")
        create_layout.addWidget(self.own_section)
        # Universal Attributes: what holds for the whole network, whichever way it was made (generated, drawn,
        # from a map or the assistant), so not under Generate Network.
        universal = QWidget()
        universal_layout = QVBoxLayout(universal)
        universal_form = QFormLayout()
        universal_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.FieldsStayAtSizeHint)
        self.driving_side = ChoiceButtons([("Left-hand", "left"), ("Right-hand", "right")])
        self.driving_side.set_value(DEFAULT_DRIVING_SIDE)
        self.driving_side.setToolTip("Side traffic keeps to")
        universal_form.addRow("Driving side", self.driving_side)
        universal_layout.addLayout(universal_form)
        universal_layout.addWidget(hint_label(  # under the form: a wrapped row of a form is clipped
            "The whole network at once: lanes, turns and signals mirror to the other side, and new roads and grids "
            "follow it."))
        self.universal_section = Collapsible("Universal Attributes", universal, tooltip="Whole-network settings")
        create_layout.addWidget(self.universal_section)
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
            toggle = icon_button(0xE80A, "Input Grid", checkable=True)
            toggle.toggled.connect(self.show_grid)
            self.grid_toggles.append(toggle)
        self.power_button = icon_button(0xE8BE, "Power Saver", checkable=True)  # a leaf
        self.power_button.toggled.connect(self._power_saver)
        self.isometric_button = icon_button(0xE809, "3D View", checkable=True)  # native TiltUp glyph, Windows 10 too
        self.isometric_button.toggled.connect(self.view.set_isometric)
        self.view.projection_changed.connect(self.isometric_button.setChecked)
        self.map_controls += ("\n3D View: orthographic isometric camera, 45 degree yaw, 35.3 degree elevation, zero roll"
                              "\nIn 3D View, selection boxes follow the world grid. Zoom never changes simulation speed.")
        self.map_controls += ("\nHide Background (the eye, over an image or the map): the roads alone; nothing saved "
                              "changes.")
        # Hide Background sits here, on the map it hides, and shows only over an image or the map (the panel says)
        for button in (self.grid_toggles[0], self.isometric_button, self.background_panel.hide_button,
                       self.power_button, self.fit_button, self.reset_button, self.help_button):
            row.addWidget(button)
        self.view.set_corner(corner)

        edit_tab = QWidget()
        edit_layout = QVBoxLayout(edit_tab)
        roads_page = QWidget()  # Edit Network's own tabs: Roads (junctions and roads) and MRT (rail lines)
        roads_layout = QVBoxLayout(roads_page)
        roads_layout.setContentsMargins(0, 0, 0, 0)
        tools = QGroupBox("Add to Network")
        tools_layout = QVBoxLayout(tools)
        self.add_node_button = styled_button("Add Node", tooltip="Place a new junction", checkable=True)
        self.add_edge_button = styled_button("Add Edge", tooltip="Draw a new road", checkable=True)
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
        edge_form.setContentsMargins(0, 6, 0, 2)  # clear of the buttons above: the radio dots stood against them
        edge_form.addRow("Edge type", self.edge_kind)
        tools_layout.addWidget(self.edge_type_row)
        roads_layout.addWidget(tools)
        metro_page = QWidget()
        metro_layout = QVBoxLayout(metro_page)
        metro_layout.setContentsMargins(0, 0, 0, 0)
        metro_box = QGroupBox("MRT Lines")
        metro_row = QHBoxLayout(metro_box)
        # A train: each click a stop, right-click ends
        self.metro_button = icon_button(0xE7C0, "Start deployment", checkable=True)
        metro_row.addWidget(self.metro_button)
        self.metro_summary = hint_label()  # its lines, stops and track
        metro_row.addWidget(self.metro_summary, 1)
        metro_layout.addWidget(metro_box)
        self.edit_tabs = OpenPageTabs()
        walk_page = QWidget()  # Edit Network → Pedestrian: crossings over the roads' footpaths
        walk_layout = QVBoxLayout(walk_page)
        walk_layout.setContentsMargins(0, 0, 0, 0)
        crossings_box = QGroupBox("Crossings")
        crossings_layout = QVBoxLayout(crossings_box)
        crossing_buttons = QHBoxLayout()
        self.zebra_button = styled_button("Add Zebra", tooltip="Click a road: zebra crossing", checkable=True)
        self.overbridge_button = styled_button("Add Overbridge", tooltip="Click a road: footbridge", checkable=True)
        for button in (self.zebra_button, self.overbridge_button):
            crossing_buttons.addWidget(button)
        crossings_layout.addLayout(crossing_buttons)
        crossings_layout.addWidget(hint_label(
            "Click a road with footpaths on both sides to place one there, clear of its junctions. Walkers have "
            "right of way on a zebra, so traffic stops for them; an overbridge takes them over the traffic, stairs "
            "to climb. At a junction they wait for a gap. Click a crossing to select it; Delete removes it."))
        walk_layout.addWidget(crossings_box)
        walk_layout.addStretch(1)
        self.edit_tabs.addTab(roads_page, "Roads")
        self.edit_tabs.addTab(metro_page, "MRT")
        self.edit_tabs.addTab(walk_page, "Pedestrian")
        edit_layout.addWidget(self.edit_tabs)
        self.metro_line: str | None = None  # the line a deployment is extending
        self.node_chain: list[str] = []  # Add Node's nodes placed since it started: not an undo step until it ends
        self._demand_kind = ""  # the Demand tab last chosen (Institutions shows the buildings when it opens)
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
        self.buildings_button = self.institution_panel.buildings_button  # Show Buildings: with the buildings
        self.buildings_button.setChecked(self.scene.buildings_visible)
        self.buildings_button.toggled.connect(self.scene.set_buildings_visible)
        self.scene.buildings_changed.connect(self.buildings_button.setChecked)
        # generate_demand's gravity model measures routes on the open network
        self.od_panel.planner = lambda pairs, args, values=None, shares=None: network_tools.demand_plan(
            self.network, pairs, args, values, shares)
        self.od_panel.scaler = lambda values, pairs, args, shares=None: network_tools.scale_plan(
            self.network, values, pairs, args, shares)
        self.od_panel.checker = self._check_demand  # a degree of saturation, checked in a run
        self.od_panel.calibrator = lambda flows, args: network_tools.calibration_plan(self.network, flows, args)
        self.od_panel.equilibrator = self._equilibrate_demand
        self.od_panel.router = self._route_along  # drop-offs are placed along a pair's fastest route
        self.od_panel.chooser = lambda pairs, args, values, current=None: network_tools.mode_plan(  # choose_modes
            self.network, pairs, args, self.vehicle_modes.modes, values,
            run_reports.run_test(self.tests, str(args["from_run"]))["kpis"]["observed"]["trips"] if args.get("from_run") else None,
            current)
        self.od_matrix.corner.insertWidget(0, self.grid_toggles[1])
        demand_layout.addWidget(self.od_panel)
        demand_layout.addStretch(1)

        self.tabs = OpenPageTabs()  # as tall as the open page: the sidebar scrolls only when it needs to
        self.tabs.setDocumentMode(True)  # the tab row spans the sidebar...
        bar = self.tabs.tabBar()
        bar.setExpanding(True)  # ...and the tabs share it (document mode had turned this off)
        bar.setDrawBase(False)  # no base line: it ran into the tiles and broke at the selected one
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
            bar.setObjectName("iconTabs")  # icon-only tiles, each icon centred (_centre_tab_icons)
        self.tabs.currentChanged.connect(self._name_open_tab)
        self._name_open_tab(self.tabs.currentIndex())
        side_layout.addWidget(self.tabs)

        side_layout.addStretch(1)

        layout.addWidget(side_scroll)
        # The main area shows the map or the OD matrix (show_grid, the Input Grid toggle).
        self.workspace = QStackedWidget()
        self.workspace.addWidget(self.view)
        self.workspace.addWidget(self.od_matrix)
        self.metro_matrix = MetroMatrix()  # Demand Generator → MRT: the metro's own OD matrix
        self.workspace.addWidget(self.metro_matrix)
        self.walk_matrix = PedestrianMatrix()  # Demand Generator → Pedestrian OD: people walking between ends
        self.workspace.addWidget(self.walk_matrix)
        self.spawn_matrix = SpawnMatrix()  # Demand Generator → Deploy Pedestrian: between the points placed
        for kind, matrix in (("metro", self.metro_matrix), ("walk", self.walk_matrix), ("spawn", self.spawn_matrix)):
            self.od_panel.add_side(kind, matrix.side)  # their day curves in the sidebar, as Vehicle OD's editor
        self.workspace.addWidget(self.spawn_matrix)
        layout.addWidget(self.workspace, 1)
        self.setCentralWidget(root)
        # Wide enough for the tab row and the widest page (_fit_sidebar). The row is measured only now that the
        # tabs sit inside the window and use the stylesheet's font, before centring pads its tiles.
        bar.ensurePolished()
        self.side_scroll = side_scroll
        self._tab_row, self._side_cap = bar.sizeHint().width(), None
        self._fit_sidebar()
        sidebar.installEventFilter(self)  # fitted again whenever what it holds needs another width
        self._tab_side = 10  # the tiles' side padding as theme.py sets it (QTabBar::tab), until centred
        bar.installEventFilter(self)  # only now: centring pads the tiles, and the width above reads the row's hint
        # One label column per tab, so every section's fields start on the same line.
        for page in (create_tab, edit_tab, self.od_panel, *(self.ai_mode.systems.widget(i)
                                                            for i in range(self.ai_mode.systems.count()))):
            align_form_labels(page)
        # The page title sits on the brand's baseline: lowered by the difference in their styled fonts' ascents.
        for label in (title, self.page_title):
            label.ensurePolished()
        self.page_title.setContentsMargins(0, max(title.fontMetrics().ascent() - self.page_title.fontMetrics().ascent(), 0), 0, 0)
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
        self.clock_label = hint_label(results.clock(0))
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
        self.activity.failed.connect(self._watch_failed)
        bar.addAction(self.watch_action)
        edge = QWidget()
        edge.setFixedWidth(8)  # keeps the last icon clear of the window edge
        bar.addWidget(edge)
        self.menuBar().setCornerWidget(float_glass(bar), Qt.Corner.TopRightCorner)  # the run controls: a glass bar
        clock.ticked.connect(self._frame)
        clock.state_changed.connect(self._show_clock_state)
        self._show_clock_state(False)

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
            graph = Graph(self.network)
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

    SENSITIVITY = ("no_passengers", "exit_full", "no_cross_queues", "arrives_on_green", "no_estimate")

    def _decision_sensitivity(self, args: dict, check_only: bool) -> dict:
        """decision_sensitivity: TSP decisions of a run re-asked with one fact of their telemetry changed at a time
        (tsp_agent.perturbed), to the model deciding now; how often each change flips the answer, and how often the
        same question asked again keeps it (Explainable AI for ITS Ch 7: occlusion; ZORRO-style perturbation)."""
        from traffic_simulator import agent_transit_signal_priority as tsp_agent  # noqa: PLC0415

        test = run_reports.run_test(self.tests, str(args.get("id") or ""))
        rows = [row for row in run_reports.decision_rows(test) if row["extension"] == "TSP"
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

    def _ask_agent(self, question, choice: dict | None = None):
        """The signal agent's question (runs in its thread). TSP's ({"system": "1" | "2", ...}: System 1's state and
        typed question, or System 2's messages) goes to that system: `choice` names the model (a batch run's agent),
        else AI Mode's own (System 1's Use; System 2's started model); the answer comes back with what the model
        reported (model, tokens, P(yes)). Plain text (another extension's yes/no) goes to System 1 when one is
        chosen (one yes/no in an encoder pass), else to System 2's started model."""
        if not isinstance(question, dict):
            fast = self.ai_mode.system_one_for_agents()
            if fast is not None:
                provider, key, model = fast
                return "TRUE" if assistant.system_one_true(provider, question, key, model) else "FALSE"
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
            fast = (choice["provider"], self.ai_mode.key_for(choice["provider"]), choice["model"]) if choice else \
                self.ai_mode.system_one_for_agents()
            if fast is None:
                raise RuntimeError("AI Mode's System 1 is Off, so every answer is FALSE (the plan).")
            provider, key, model = fast
            reply = assistant.system_one(provider, question["state"], question["questions"], key, model=model)
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
                                     key, model=choice["model"])
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
                (system == "1" and fast and option["model"] == fast[2]) or
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
            return (fast[2] if fast[0] == "local" else fast[0], "") if fast else (
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
        on one model server (a provider: Ollama, Jev...), so no run's answers wait behind another's; each
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
        graph = Graph(self.network)

        def can_go(origin: str, destination: str) -> None:
            if origin == destination or not graph.route(origin, destination):
                raise ValueError(f"{no} route from {origin} to {destination}.")

        return can_go

    def _schedule_emergency(self, args: dict, check_only: bool = False) -> dict:
        """The assistant's schedule_emergency: the Dispatch Emergency Vehicle tab's schedules replaced (set_fleet),
        each checked first (an end or junction each, a route between them, every or after 0-120 min, not both),
        the extension ticked (its tab is used only while ticked, as Test DEVL ticks it); with dispatch_now also sent
        into the live run (dispatch_emergency). ValueError: why not, nothing changed."""
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
        self._tick_extension("dispatch_emergency")
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

    def _pair_demand(self) -> list[PairDemand]:
        """Every OD pair with demand of the kind the Demand Generator shows, for a run, and the institutions'
        trips (institutions.run_demand: catchment to door and back; its notes in `_institution_notes`)."""
        matrix = self.od_matrix
        matrix.set_ends(*self._od_ends())
        institutions, self._institution_notes = run_demand(self.network, self.vehicle_modes.modes, matrix.mode == "daily")
        return pair_demand(matrix.cells, matrix.origins, matrix.destinations, matrix.mode,
                           matrix.vehicle_modes.vehicles_per_hour()) + institutions

    def _name_open_tab(self, current: int) -> None:
        self.page_title.setText(self.tab_names[current] if current >= 0 else "")
        if self.activity.on:
            self.activity.log("tab", self.page_title.text())

    def eventFilter(self, watched, event) -> bool:  # type intentionally inherited from Qt
        """The icon tab row resized (the sidebar's scroll bar came or went): its icons centred again. The
        sidebar's contents re-laid out (a row shown, label columns aligned, another screen's fonts): fitted again."""
        if event.type() == QEvent.Type.Resize and watched is self.tabs.tabBar():
            self._centre_tab_icons()
        elif event.type() == QEvent.Type.LayoutRequest and watched is self.side_scroll.widget():
            self._fit_sidebar()
        return super().eventFilter(watched, event)

    def _fit_sidebar(self) -> None:
        """The sidebar exactly as wide as the tab row or its widest page needs with a scroll bar beside them, so
        nothing in it is cut off at its right edge on any screen, scrolling or not. Measured again whenever its
        contents change, never just once: a row shown later (a map's) or a label column aligned can widen a page."""
        scroll = self.side_scroll
        scroll.ensurePolished()  # the stylesheet's frame and scroll bar, not the style's defaults
        scroll.verticalScrollBar().ensurePolished()
        inner = scroll.widget()
        margins = inner.layout().contentsMargins()
        chrome = 2 * scroll.frameWidth() + scroll.verticalScrollBar().sizeHint().width()
        row = self._tab_row + margins.left() + margins.right()
        need = max(row, inner.minimumSizeHint().width())
        # Never past half as wide again as it first fitted: a very long name in a label that doesn't wrap (a vehicle
        # type's, from the assistant or a project) is clipped rather than taking the map and the window off screen
        if self._side_cap is None:
            self._side_cap = need * 3 // 2
        width = min(need, self._side_cap) + chrome
        if width != scroll.minimumWidth():
            scroll.setMinimumWidth(width)
            scroll.setMaximumWidth(width)

    def _centre_tab_icons(self) -> None:
        """The tiles share the row edge to edge (expanding); a tab's icon sits at the start of its text, so equal
        side padding filling each tile centres it. Each padding pixel widens every tile's hint by 2, so the padding
        that fills the row is read off the row's own hint, never past its width (the sidebar never grows from it).
        Only icon tabs: without an icon font the tabs show names."""
        bar = self.tabs.tabBar()
        if bar.objectName() != "iconTabs" or not bar.count():
            return
        side = max(self._tab_side + (bar.width() - bar.sizeHint().width()) // (2 * bar.count()), 4)
        if side != self._tab_side:
            self._tab_side = side
            bar.setStyleSheet(f"QTabBar::tab {{ padding: 6px {side}px; }}")

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
        self.scene.redrawn.connect(lambda: self.workspace.currentWidget() is self.walk_matrix
                                   and self.walk_matrix.show_network(self.network))
        self.walk_matrix.changed.connect(self._commit)
        self.scene.redrawn.connect(lambda: self.workspace.currentWidget() is self.spawn_matrix
                                   and self.spawn_matrix.show_network(self.network))
        self.spawn_matrix.changed.connect(self._commit)
        self.od_panel.place_spawn_button.toggled.connect(self._placing_spawn_points)
        self.scene.spawn_clicked.connect(self.spawn_clicked)
        self.view.right_clicked.connect(lambda: self.od_panel.place_spawn_button.setChecked(False))  # finishes
        self.scene.metro_clicked.connect(self.metro_clicked)
        self.scene.metro_aiming.connect(self._metro_ghost)
        self.view.right_clicked.connect(lambda: self.metro_button.setChecked(False))  # ends a deployment
        self.view.right_clicked.connect(lambda: self.add_node_button.setChecked(False))  # keeps the nodes placed
        self.properties.footpath_preview.connect(self.scene.preview_footpaths)
        for button in (self.zebra_button, self.overbridge_button):
            button.toggled.connect(lambda checked, button=button: self._crossing_tool(button, checked))
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
        self.institution_panel.demand_requested.connect(self._set_institution_demand)
        self.scene.placement_changed.connect(lambda: self.institution_panel.show_step(self.scene.placing))
        self.scene.placed.connect(self._place_institution)
        self.properties.drag_button.toggled.connect(self.toggle_drag)
        self.scene.drag_finished.connect(self.model_updated)
        self.properties.network_changed.connect(self.model_updated)
        self.properties.interchange_requested.connect(self.build_interchange)
        self.properties.edited.connect(self._commit)
        self.properties.edited.connect(self._signal_edited)
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
        self.ai_mode.attach(network=self._assistant_network, selection=self._selection_info,
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
        """Boundary ends traffic can enter from (origins) and leave by (destinations): demand.od_ends.
        A one-way boundary road only works one way round."""
        return od_ends(self.network)

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
                self.statusBar().showMessage(f"{self._network_summary()} • MRT {summary}, a train every "
                                             f"{line.headway_min:g} min, accepted")
                self._commit()
            else:
                self.network.delete_metro_line(line.id)
                self.statusBar().showMessage(f"{self._network_summary()} • MRT {line.id} deleted")
        self.metro_line, self.scene.metro_pending = None, None
        self.scene.metro_deploying = checked
        self.scene.rebuild()
        self._refresh_mode()

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

    def metro_clicked(self, x: float, y: float, dx: float = 0.0, dy: float = 0.0) -> None:
        """A stop where the map was clicked while deploying: the first waits for the second, which makes the
        line; then each joins the line's last stop, aimed along (dx, dy) when the click was dragged (the track runs
        through the stop that way; (0, 0): a plain click). Refused (too near the last stop, an aim that can't be met)
        says why and changes nothing.
        Nothing is an undo step until the line is accepted (toggle_metro)."""
        if self.metro_line is not None and self.metro_line not in self.network.metro_lines:
            self.metro_line = None  # undone while deploying: start a new line
        merge = False
        while True:
            try:
                if self.metro_line is not None:
                    self.network.add_metro_stop(self.metro_line, x, y, merge, (dx, dy) if dx or dy else None)
                elif self.scene.metro_pending is None:
                    self.scene.metro_pending = (x, y)
                else:
                    self.metro_line = self.network.add_metro_line(self.scene.metro_pending, (x, y), merge).id
                    self.scene.metro_pending = None
                break
            except MetroCrossing as crossing:  # it crosses another line: an interchange there, or not this line
                choice = self._merge_or_discard(crossing.line_id)
                if choice == "merge":
                    merge = True
                    continue
                if choice == "discard":
                    if self.metro_line is not None:
                        self.network.delete_metro_line(self.metro_line)
                    self.metro_line, self.scene.metro_pending = None, None
                    self.scene.rebuild()
                    self._refresh_mode()
                    self._show_hint("Line discarded. Click to start another, or right-click to finish.")
                return
            except ValueError as exc:
                self._show_hint(f"{exc} Click elsewhere, or right-click to finish.")
                return
        self.scene.rebuild()
        self._refresh_mode()

    def _merge_or_discard(self, line_id: str) -> str | None:
        """A leg crossing line `line_id`: "merge" (an interchange station at the crossing, on both lines), "discard"
        (the line being drawn goes) or None (Cancel: not this stop)."""
        box = QMessageBox(QMessageBox.Icon.Question, "Lines Cross",
                          f"This leg crosses line {line_id}. Merge them at an interchange station where they cross "
                          "(riders can change lines there), or discard the line you are drawing?", parent=self)
        merge = box.addButton("Merge", QMessageBox.ButtonRole.AcceptRole)
        discard = box.addButton("Discard Line", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        return {merge: "merge", discard: "discard"}.get(box.clickedButton())

    def _metro_ghost(self, x: float, y: float, dx: float = 0.0, dy: float = 0.0) -> None:
        """While deploying, the leg a click at (x, y) would add (Network.metro_leg: curved, drawn in if too sharp),
        before it is placed: amber if it crosses another line, red if refused."""
        points, state = [], "ok"
        if self.metro_line in self.network.metro_lines:
            start = self.network.metro_track(self.metro_line)[-1]
            try:
                leg, crossing = self.network.metro_leg(self.metro_line, x, y, (dx, dy) if dx or dy else None)
                points, state = [start, *leg], "crossing" if crossing else "ok"
            except ValueError:
                points, state = [start, (x, y)], "refused"
        elif self.scene.metro_pending is not None:
            px, py = self.scene.metro_pending
            points = [(px, py), (x, y)]
            state = "ok" if math.hypot(x - px, y - py) >= METRO_MIN_SPACING_M else "refused"
        self.scene.show_metro_ghost(points, state, (x, y, dx, dy) if dx or dy else None)

    def _accept_metro_line(self, summary: str) -> dict | None:
        """The finished line: accepted with its service (headway, speed, carriages a train and passengers a carriage,
        fare: assumed values until changed, METRO_SERVICE) as final, never edited after; or deleted (None)."""
        dialog = QDialog(self)
        dialog.setWindowTitle("Finish MRT line")
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel(f"MRT {summary}. Accept it as final, or delete it?"))
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
            self.metro_summary.setText("No MRT lines yet")
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
        """metro_report: the metro's design-hour passengers on their lines (institutions.metro_report)."""
        return metro_report(self.network, self.od_matrix.mode == "daily")

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

    def _signal_edited(self, key: object) -> None:
        """A junction's phases, timing method or manual windows changed (the properties panel): its heads drawn again,
        their phase letters and idle lamps as the arms now group, and an adaptive method's detectors."""
        if isinstance(key, tuple) and key[0] in ("phases", "method", "manual"):
            self.scene._redraw([("node", key[1])])

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

    def _project(self) -> dict:
        """The project's parts beyond the network, as plain data for network_io (network_io.project_record)."""
        return project_record(self.od_matrix.cells, self.od_matrix.mode, self.vehicle_modes.modes,
                              self.vehicle_modes.default_split, self.seed_box.value(), self.speed_combo.currentData())

    def _save_marker(self) -> tuple:
        return self.history.current, self.seed_box.value(), self.speed_combo.currentIndex(), self.od_matrix.mode

    def unsaved(self) -> bool:
        """Changed since last saved or opened: another undo state (edits, demand, types), seed, speed or the
        Hourly/Daily switch (a Run uses the demand it shows)."""
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

"""MainWindow's construction: the sidebar and its tabs, the map's corner bar, the menu bar, the run controls bar,
and the wiring of every widget signal to its handler. Cut out of main.py's MainWindow as a plain mixin; __init__ in
main.py calls _build_ui, _build_menu, _build_sim_bar and _connect_signals in that order and imports the DEFAULT_*
settings back for the first grid. eventFilter stays on MainWindow (a Qt override) and calls _centre_tab_icons and
_fit_sidebar through self."""

from __future__ import annotations

from types import SimpleNamespace

from PySide6.QtCore import QPoint, QSize, Qt
from PySide6.QtGui import QAction, QFontDatabase, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSpinBox,
    QStackedWidget,
    QToolBar,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from traffic_simulator.ai_mode import AIModePanel
from traffic_simulator.background import BackgroundPanel
from traffic_simulator.extensions import ExtensionsPanel
from traffic_simulator import demand_plans, results, run_reports
from traffic_simulator.signal_agent import SignalAgent
from traffic_simulator.mode_behavior import ModeBehaviorPanel, VehicleModes
from traffic_simulator.network_io import MAX_SEED, RUN_SEED
from traffic_simulator.od_matrix import ODCellPanel, ODMatrix
from traffic_simulator.metro_matrix import MetroMatrix, PedestrianMatrix, SpawnMatrix
from traffic_simulator.properties_panel import TIMING_METHODS
from traffic_simulator.summary import SummaryPanel
from traffic_simulator.theme import float_glass
from traffic_simulator.widgets import (ICON_FONTS, ChoiceButtons, Collapsible, OpenPageTabs, align_form_labels,
                                       glyph_icon, hint_label, icon_button, styled_button)

# Sidebar tab icons: glyphs from Windows' own icon font (see widgets.ICON_FONTS).
# Without either font, tabs show their names.
TAB_ICONS = {"Create Network": "\ue800", "Edit Network": "\ue70f", "Mode Behavior": "\ue804",
             "Demand Generator": "\ue80a", "AI Mode": "\ue99a", "View Summary": "\ue9d2",
             "Extensions": "\uea86"}  # map, pencil, car, table, robot, chart, puzzle
TAB_ICON_PX = 20
# The grid a new window starts with, and the Create Network tab's settings for new roads
DEFAULT_HORIZONTAL_CORRIDORS, DEFAULT_VERTICAL_CORRIDORS = 3, 4
DEFAULT_DRIVING_SIDE, DEFAULT_SPEED_LIMIT_KMH, DEFAULT_LANES_PER_DIRECTION, DEFAULT_LANE_CHANGING = "left", 50, 2, True


class LayoutMixin:
    """Building the window and wiring its signals."""

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
        self.od_panel.planner = lambda pairs, args, values=None, shares=None: demand_plans.demand_plan(
            self.network, pairs, args, values, shares)
        self.od_panel.scaler = lambda values, pairs, args, shares=None: demand_plans.scale_plan(
            self.network, values, pairs, args, shares)
        self.od_panel.checker = self._check_demand  # a degree of saturation, checked in a run
        self.od_panel.calibrator = lambda flows, args: demand_plans.calibration_plan(self.network, flows, args)
        self.od_panel.equilibrator = self._equilibrate_demand
        self.od_panel.router = self._route_along  # drop-offs are placed along a pair's fastest route
        self.od_panel.chooser = lambda pairs, args, values, current=None: demand_plans.mode_plan(  # choose_modes
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
        # What extensions may use of the window (Batch Run: its runs, and who decides each controller in them)
        self.extensions.app = SimpleNamespace(run_batch=self.run_batch, replay_run=self.replay_run,
                                              agent_options=self.ai_mode.agent_options,
                                              controller_plan=self.controller_plan,
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

    def _name_open_tab(self, current: int) -> None:
        self.page_title.setText(self.tab_names[current] if current >= 0 else "")
        if self.activity.on:
            self.activity.log("tab", self.page_title.text())

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

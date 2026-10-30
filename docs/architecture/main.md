# main.py: the main window (Qt)

- **Sidebar tabs:**
  - **Workspace shell:** 12 px horizontal gutters and panel padding, matte slate sidebar, blue ACMS brand
    and a separate page-title role. The short introduction keeps the metres unit visible. Only presentation
    properties, spacing and typography differ; tab order, controls, actions and signal connections are shared.
  - **Create Network:** two `Collapsible` sections, fields at their natural size:
    - **Generate Network** (open at start): Driving side, applied to the whole network at once
      (`set_driving_side`: interchanges mirrored, the scene redrawn, one undo step, a run stopped,
      the status bar naming the interchanges); Speed limit, Lanes per direction and Lane changing, kept on the
      network as the defaults new roads get (a generated grid, Add Edge, interchanges), one undo step
      each (`_set_default`), the roads already there and a run untouched; Signal timing (Webster, TRANSYT,
      Synchro, SCATS, SCOOT, Fixed time), the network's `default_signal_method`, kept the same way: the method a
      generated grid's junctions and junctions added later start with (a Run retimes them from the demand; each
      junction's own method is changed in Edit Network); then Horizontal and Vertical roads (1–20 corridors) and
      Generate Network. Lanes per direction is 1–12, as a road's own. A network installed (Open, Undo, the
      assistant's generate_grid: `install_network`) shows its own Roads settings, Manual added to Signal timing when
      it is the network's, without writing them back (signals blocked: no edit, no undo step).
    - **Own Network:** the background to draw a network over (see background.py).
  - **Map controls** over the map's top-right corner (`NetworkView.set_corner`): Input Grid (only while
    Demand Generator is open), 3D View (checkable, `isometric_button`: an orthographic isometric view,
    same world geometry, frame, time, centre and zoom; off returns to top-down), Show Buildings (the city
    glyph, `buildings_button`: session visibility of institution footprints/labels/catchments, off until ticked or Demand → Institutions opens, demand unchanged),
    Power Saver (a leaf, checkable, `_power_saver`: the clock steps every 32 ms
    instead of 16, so a run draws 30 frames a second, and on a GPU the map drops multisampling; the physics
    is untouched; not remembered between sessions), Fit
    Network (the network itself), Reset View and Map Controls, whose click shows the mouse and keys and what draws
    the map (`map_controls`); hovering it, like every tooltip, gives only its name (five words at most).
  - **Input Grid** toggle (`show_grid`): the main area (`workspace`) shows the network or the OD
    matrix. One checkable button first in the map's corner and one first in the grid's title row, kept
    in step, so it stays under the mouse. The map's is shown only while Demand Generator is open (the
    grid is that tab's; `tab_changed`), which opens on the grid; every other tab shows the network. While the grid shows, its rows and columns follow the
    boundary ends (`_refresh_grid`, also after every change, undo and redo, so an end the assistant
    adds appears).
  - The sidebar scrolls only when the open tab needs it: its `OpenPageTabs` size to the open page
    rather than the tallest, height for width included, re-measuring when a page's layout changes. Every
    tab, and both AI Mode pages, fits a 1280 × 800 window (the chat's transcript is at least 220 px).
  - **Edit Network:** its own tabs, Roads and MRT (`edit_tabs`), above the next-step banner and the selection
    editor. **MRT** (Metro Rail): Start deployment (`metro_button`, a train icon, checkable) and a summary of the
    lines, stops and km of track (`_show_metro_summary`, after every redraw). Deploying (`toggle_metro`,
    `metro_clicked`): every left click on the map is a stop (dragged from it, the pen tool's way: aimed, the track
    running through it that way, Shift in 15 degree steps; `metro_clicked(x, y, dx, dy)`, the aimed leg and a
    mirrored handle drawn as it is dragged, `_metro_ghost`); the first waits (`scene.metro_pending`, drawn) until the
    Paths of their own (from OpenStreetMap or the assistant) are drawn; one clicked is selected ("walkway": the status bar
    names it); Delete removes it (`delete_selection`, `delete_objects` too). Footpaths are a road's (the road
    panel's Footpath list, previewed on the map: `properties.footpath_preview`).
    **Pedestrian** tab (Edit Network's third): Crossings, **Add Zebra** (`zebra_button`) and **Add Overbridge**
    (`overbridge_button`), checkable, one at a time with the other tools (`_crossing_tool`); each road clicked gets one
    where clicked (`_place_crossing`: `Network.add_crossing`, refused with its reason in the banner; one undo step,
    selected, the tool still on). A crossing clicked is selected ("crossing": the status bar names it); Delete removes
    it.
    **Deploy Pedestrian** (Demand Generator's Demand list, `_placing_spawn_points`, `spawn_clicked`): choosing
    it shows its OD matrix (`spawn_matrix`; with no points yet, its hint); Place Deploy Points shows the map and
    makes every left click a deploy point (one undo step,
    drawn at once, the hint saying how far its nearest footpath is, `walking.footpath_join`); a right-click, Esc,
    another demand or tab finishes and shows their grid (`spawn_matrix`). A deploy point clicked is selected (it stays
    the Demand Generator's); Delete removes it and the walking to and from it.
    second makes the line (`add_metro_line`), and each after it joins the line's last stop (`add_metro_stop`: curving
    from it, a bend too sharp drawn in); one too near, or a leg crossing its own track, is refused with its reason in
    the banner. Before a click the leg it would add follows the mouse (`_show_pointer` → `_metro_ghost`,
    `Network.metro_leg`, drawn dashed by `scene.show_metro_ghost`: the line's colour, amber where it would cross
    another line, red where refused). A leg crossing another line asks (`_merge_or_discard`): **Merge** ends it at an
    interchange station where they cross, on both lines; **Discard Line** removes the line being drawn; Cancel
    places nothing. A right-click (`view.right_clicked`), Esc, another tool or tab
    finishes it and asks (`_accept_metro_line`, with the line's service: a train every, speed, carriages a train, passengers a carriage,
    fare, prefilled with `METRO_SERVICE`'s assumed values and labelled so): **Accept** keeps the line as final, one undo step, never edited after
    (a new deployment is always a new line); **Delete** removes it and its stops, leaving no undo step. A lone first stop
    is dropped without asking. Nothing is an undo step while deploying (undo merges only edits within 1.5 s); Ctrl+Z
    then takes back the unfinished line alone.
    An accepted line is selected with a selection box, as roads are (a window round its whole track, a crossing box
    touching it: "1 MRT line selected", outlined); Delete removes it and its stations, one undo step.
  - **Edit Network → Roads:** Add Node, Add Edge (Connection or End) and the selection editor. Ctrl+click adds a
    road to the selection or takes it out (`_toggle_road`); two roads meeting at a junction show Add Slip
    Road beside Delete Selected; its menu picks Single lane or Two lanes (`add_slip_road`: `build_slip_road`,
    the slip selected, one undo step).
  - **Mode Behavior:** the vehicle types: rename, add, delete. It comes before the Demand
    Generator, which splits trips between them. Click a type for its Colour chooser: twelve fixed paints or
    Random (stable per-vehicle display paint, retaining the type colour for charts). Saved and undoable.
  - **Demand Generator:** the Hourly/Daily switch, its five tabs and the OD pair editor; the OD matrix (or the
    tab's own grid, `show_grid` passing it the switch's mode) replaces the map when it opens (Input Grid brings the
    map back), except with Institutions chosen: the map, to build on. A Run's walkers follow the switch too
    (`_run`: `walking.walk_pairs` in its mode), as does the MRT report's design hour (`_metro_report`: daily,
    `metro_daily` by each pair's curve). An institution's people an hour or curves (`_set_institution_demand`) are
    validated first and one undo step per box or curve. Placing (`_placing`, `_place_institution`) ends the Edit and background tools; the building
    is one undo step and a run carries on (undoing it too, `_restore`). Esc, another tab or another
    Demand choice stops placing. Clicking a building selects it and opens Demand Generator →
    Institutions; Del deletes it.
    Institution placement and selection both offer total **Height** in metres. `_set_institution_height`
    validates a copied record first, updates scenery and its cache, and commits a merged undo step with
    `stop_run=False`. Heights save with institutions; Undo/Redo preserve a live Runner as for institution edits.
  - **AI Mode:** keys and the System 2 chat; the status bar always shows which model is connected.
  - **View Summary:** the project at a glance, rebuilt whenever the tab opens or the project
    changes while it's open.
  - **Extensions:** switch drop-in features from `extensions/` on and off (see extensions.py). The window's
    services for them (`app`): `run_batch` (`test_tsp`, `test_devl`: the emergency fleet in each run with DEVL's
    override, TSP and DEVL each with their own signal agent, its Emergency vehicles sheet, `emergency_report`; `devl_agents` deciding its lanes, every TSP agent with every DEVL
    agent when both are tested, paced whenever a model answers), `replay_run`, `tsp_batch` and `devl_batch` (the TSP
    and DEVL tabs' batch settings, `_agent_plan`: the model picked, else the one on in AI Mode for its System),
    `dispatch_emergency(origin, destination, every_min, check_only, after_s)`
    (into the live run; ValueError with no run, an unknown end or junction, or no route) and `places` (boundary
    ends, then junctions).
  - Tabs are icons (`TAB_ICONS`: glyphs of Windows' Segoe Fluent Icons, or Segoe MDL2 Assets on
    Windows 10), equal tiles filling the row edge to edge with no base line under them, each icon centred
    (`iconTabs` in theme.py; `_centre_tab_icons` pads the tiles to the row's width whenever it resizes, read off
    the row's own hint so the sidebar never widens from it); hovering a tab shows its name, and the open tab's
    name heads the sidebar beside "ACMS" on the brand's baseline (`page_title`, `_name_open_tab`). Without either
    font the tabs show their names.
  - The sidebar is as wide as its tab row or its widest page needs (each page's minimum width), plus
    its margins and scrollbar, and no wider (about 417 px with Segoe UI: the Extensions page's rows are as wide as their settings,
    open or folded), so the map gets the rest. The tab row spans the sidebar
    (document mode, expanding tabs), and every page's margins line up with its edges.
- **Canvas tools:** Add Node, Add Edge, Drag to Adjust and MRT deployment are mutually exclusive.
  A banner says the next step, and Esc leaves a tool or clears the selection.
- **`_build_edge`:** detects crossings before adding a road and asks: **Merge into
  Roundabout** (a roundabout node at each crossing; both roads split there) or
  **Overpass** (the new road bridges over). The road itself is built by
  `network_tools.build_edge`, which the assistant's `add_road` tool shares.
- **`build_interchange`:** the dialog chooses legs, keep/replace and freeway lanes,
  then calls the builders.
- **Undo/redo:**
  - Each finished action commits a deep-copied snapshot of the network plus the OD cells to
    `History` (`_commit`); one equal to the last committed is no action: no step, and a run carries on.
  - Typing into one field merges into one step.
  - Shortcuts: Ctrl+Z, and Ctrl+Y or Ctrl+Shift+Z.
  - Redo during an MRT deployment ends it first (`_end_modes_except`: accepted, the line is a new step, so there is
    nothing left to redo); undo takes the unfinished line back.
  - While a Scenario Builder build goes on (`_building`; it is one undo step, made when it ends) Undo and Redo say
    so and wait (`_wait_for_build`), and the map, the grids and the editing tabs (Create Network, Edit Network,
    Mode Behavior, Demand Generator) are disabled, so nothing done during a step's test joins the build.
- **File menu:** Save (Ctrl+S) and Load (Ctrl+O) JSON.

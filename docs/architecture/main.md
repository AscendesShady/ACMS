# main.py: the main window (Qt)

- **Sidebar tabs:**
  - **Create Network:** two `Collapsible` sections, fields at their natural size:
    - **Generate Network** (open at start): Driving side, applied to the whole network at once
      (`set_driving_side`: interchanges mirrored, the scene redrawn, one undo step, a run stopped,
      the status bar naming the interchanges); Speed limit, Lanes per direction and Lane changing, kept on the
      network as the defaults new roads get (a generated grid, Add Edge, interchanges), one undo step
      each (`_set_default`), the roads already there and a run untouched; Signal timing (Webster, TRANSYT,
      Synchro, SCATS, SCOOT, Fixed time), the network's `default_signal_method`, kept the same way: the method a
      generated grid's junctions and junctions added later start with (a Run retimes them from the demand; each
      junction's own method is changed in Edit Network); then Horizontal and Vertical roads (1–20 corridors) and
      Generate Network.
    - **Own Network:** the background to draw a network over (see background.py).
  - **Map controls** over the map's top-right corner (`NetworkView.set_corner`): Input Grid (only while
    Demand Generator is open), Power Saver (a leaf, checkable, `_power_saver`: the clock steps every 32 ms
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
  - **Edit Network:** Add Node, Add Edge (Connection or End) and the selection editor. Ctrl+click adds a
    road to the selection or takes it out (`_toggle_road`); two roads meeting at a junction show Add Slip
    Road beside Delete Selected; its menu picks Single lane or Two lanes (`add_slip_road`: `build_slip_road`,
    the slip selected, one undo step).
  - **Mode Behavior:** the vehicle types: rename, add, delete. It comes before the Demand
    Generator, which splits trips between them.
  - **Demand Generator:** the Demand drop-down and the OD pair editor; the OD matrix replaces the
    map when it opens (Input Grid brings the map back), except with Institutions chosen: the map, to
    build on. Placing (`_placing`, `_place_institution`) ends the Edit and background tools; the building
    is one undo step and a run carries on (undoing it too, `_restore`). Esc, another tab or another
    Demand choice stops placing. Clicking a building selects it and opens Demand Generator →
    Institutions; Del deletes it.
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
    Windows 10), all the same slim width (`iconTabs` in theme.py); hovering a tab shows its name, and
    the open tab's name heads the sidebar beside "ACMS" (`page_title`, `_name_open_tab`). Without either
    font the tabs show their names.
  - The sidebar is as wide as its tab row or its widest page needs (each page's minimum width), plus
    its margins and scrollbar, and no wider (about 408 px: the Extensions page's rows are as wide as their settings,
    open or folded), so the map gets the rest. The tab row spans the sidebar
    (document mode, expanding tabs), and every page's margins line up with its edges.
- **Canvas tools:** Add Node, Add Edge and Drag to Adjust are mutually exclusive.
  A banner says the next step, and Esc leaves a tool or clears the selection.
- **`_build_edge`:** detects crossings before adding a road and asks: **Merge into
  Roundabout** (a roundabout node at each crossing; both roads split there) or
  **Overpass** (the new road bridges over). The road itself is built by
  `network_tools.build_edge`, which the assistant's `add_road` tool shares.
- **`build_interchange`:** the dialog chooses legs, keep/replace and freeway lanes,
  then calls the builders.
- **Undo/redo:**
  - Each finished action commits a deep-copied snapshot of the network plus the OD cells to
    `History`.
  - Typing into one field merges into one step.
  - Shortcuts: Ctrl+Z, and Ctrl+Y or Ctrl+Shift+Z.
- **File menu:** Save (Ctrl+S) and Load (Ctrl+O) JSON.

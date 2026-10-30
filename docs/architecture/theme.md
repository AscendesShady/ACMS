# theme.py

- **Desktop workspace:** UI UX Pro Max's minimal enterprise guidance adapted to native Qt, with matte slate
  panels, blue primary actions and clear selected navigation. The canvas, model and simulation are independent
  of the theme. There are no external fonts, packages, web components or new animations.
- **Surfaces:** `WINDOW` is the dark workspace, `PANEL` the opaque sidebar, `SURFACE` the input/table background.
  `BUTTON` and `BUTTON_HOVER` distinguish controls; `BORDER` and `DIVIDER` separate controls and sections.
  `RADIUS_SMALL` 6 is used on controls and navigation, `RADIUS` 12 on panels. Typography uses installed Segoe UI,
  with a Consolas simulation clock; the sidebar's `brand` and `pageTitle` roles establish heading hierarchy.
- **Glass is limited to small floating toolbars:** the run controls and the map corner keep a faint white tint,
  upper-left rim and soft shadow. `float_glass(widget)` retains the `glassBar` object name and small-widget-only
  `QGraphicsDropShadowEffect`; the sidebar, buttons, inputs and tables are opaque. Glass tokens and the helper
  remain available to existing callers. No effect is applied to the map, sidebar or table contents.
- **Colour semantics:** `ACCENT` is blue (#2563eb), `ACCENT_HOVER` its deeper hover shade, `ACCENT_EDGE` the light
  blue focus/selection edge. `BANNER` marks the selected navigation tile, selected cells and next-step banners.
  `DANGER` outlines destructive actions; `STATUS` retains labelled running/passed/failed/waiting tints.
- **Vehicle assets:** `VEHICLE_PAINTS` holds twelve body paints (its first eight are the categorical type
  palette); `VEHICLE_INKS` holds glass, trim, lamps, red/blue markings and white equipment. They are non-text
  map details. The Colour chooser uses labelled choices; type identity never relies on random paint alone.
- **MRT:** `METRO_LINE` is the track/livery fuchsia; `METRO_STOP` is the flat station marker's pale fill.
  `METRO_INKS` supplies concrete, livery, rails, glazing, train body, canopy, undercarriage and a muted yellow
  tactile platform edge. These are non-text scenery materials.
- **Pedestrian paving:** `FOOTPATH` is warm reddish-brown brick; `FOOTPATH_SHADE` and `FOOTPATH_MORTAR` provide
  a second brick tone and quiet joints. `PAVING_SELECTED` preserves the yellow selected-path fill. The detailed
  herringbone pattern uses metre dimensions and fades to the base tint at overview zoom. `WALKER` is yellow.
- **Shut cells:** `BLOCKED_CELL` hatched with `BLOCKED_HATCH`: an OD grid's cell that takes no value (its
  diagonal; an MRT pair no ride joins), non-text.
- **OD panel editors:** `HANDLE_EDGE` (a handle's rim, and a fixed handle's grey fill), `HANDLE_FILL` (a handle)
  and `CURVE_GRID` (the daily curve's grid lines); the hourly share bars are `ACCENT` subdued. Non-text.
- **Cycle lane:** `CYCLE_LANE`, the green of painted cycle lanes, a non-text surface beside the carriageway.
- **Road map** (network_scene, non-text): `GRASS` (the scene's background, and the first of `MAP_COLOURS`, the
  plain backgrounds offered), `ROAD_SURFACE`, `LANE_LINE`, `ROAD_PAINT` (arrows, exit markers), `YELLOW_PAINT`
  (centre dividers, bus bays), `STOP_LINE`, `ZEBRA_STRIPE`, `PARAPET`, `MAP_SELECTED` (a selected junction's, end's or
  bay's outline; a selected road is `PAVING_SELECTED`), `JUNCTION`/`JUNCTION_EDGE`, `ISLAND`/`MINI_ISLAND`,
  `ENTRY_MARKER`, `MARKER_EDGE`, `VEHICLE_OUTLINE`, the signal lamps `SIGNAL_GREEN`/`SIGNAL_AMBER`/`SIGNAL_RED` in a
  `SIGNAL_BODY` housing ringed `SIGNAL_RIM`, `DETECTOR` and `BUILDING_ROOF`. `MAP_LABEL`, the map's names and ids, is
  text: its pair over `GRASS` is in `READABLE_PAIRS`.
- **Lane signal:** `LANE_SIGNAL_LIT` (its flashing green) and `LANE_SIGNAL_UNLIT` (dark), DEVL's square lane signal's
  lamp in a `SIGNAL_BODY` head rimmed `SIGNAL_RIM`; non-text map details.
- **Building materials:** `BUILDING_PAINTS` gives each institution kind a wall and roof paint; glazing,
  signage and trim reuse vehicle material ink tokens. These are non-text scenery materials, under a fixed
  matte light. Contact shadows reuse `SHADOW` with translucency.
- **Contrast:** `READABLE_PAIRS` covers text on window, panels, inputs, buttons, hover/primary/danger states,
  banners, tooltips and glass composited over the backdrop (`over`). Tests require at least 4.5:1.
- **Fusion with an explicit palette:** Windows light/dark settings cannot mix unreadable colours into the UI.
  Spin boxes and combos keep Fusion's arrows; checkboxes and radio buttons keep its checkmark and dot, so the
  checked state has a shape as well as colour. Their keyboard/disabled behaviour remains native.
- **Stable interaction:** button hover/focus/press states retain their size and content bounds. Ordinary and
  primary buttons use the same one-pixel border width when focused. Icon and mini buttons retain their existing
  hit areas and accessible labels. A collapsible section's title remains semibold and left-aligned, with a
  divider, hover feedback and keyboard outline.
- **Navigation and data:** sidebar icon tabs keep their order, accessible names and equal widths, filling
  the row with each icon centred and no base line under them (layout_conventions.md);
  selection uses a blue surface and light border. Text tabs share the same style, 12 px padding (icon tabs 10, so seven fill the row),
  their page 8 px below the row (`QTabWidget::pane`); `QPushButton[role="tab"]` draws a button row as tab tiles
  (the Demand Generator's tabs, three to a row). Tables, lists, menus and the
  status bar use the same semantic tokens. Inputs preserve text selection and focus visibility.
- **Click feedback:** `ClickFlash`, installed only once per application by `apply_theme`, keeps buttons
  `PRESSED` for `FLASH_MS` (250 ms) after release; primary buttons use `BANNER`. It never consumes the click.
  Mini buttons keep their hit area. A checked toolbar action remains visibly selected.

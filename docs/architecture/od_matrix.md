# od_matrix.py: demand (Qt)

- `ODMatrix`: rows are origins and columns destinations (boundary ends that can take
  traffic in or out; one-way ends only one way).
  - The **Demand** drop-down at the top of the Demand Generator tab (`ODCellPanel.demand` →
    `ODMatrix.set_mode`) picks the demand shown and edited: **Hourly demand (veh/h)** or **Daily
    demand (veh/day)**, or **Institutions**. Choosing one shows its options: for the selected pairs,
    Flow, or Trips per day and the day's curve; for Institutions, `InstitutionPanel` instead of the OD
    pair editor (below). The two are kept separately per pair. (A drop-down for two options, against
    the rulebook's dropdown threshold, by request.)
  - **Zoom and pan** (`set_zoom`, `fit`, `eventFilter` on the table's viewport), as on the map: the
    wheel zooms at the cursor (the cell under it stays put), a right or middle drag pans, and the
    title row (`ODMatrix.corner`) has Zoom Out, Zoom In (×1.25) and Fit Grid (the whole grid, up to
    200 %). 100 % is 56 × 40 px cells; 25–400 %. The cells' text and bars and the headers' text scale
    with it (the headers through their own style sheet, as the app's sets 10 pt); the scrollbars count
    pixels.
  - Cells stay blank until they have a value; a filled cell shows it over a bus/car/bike bar.
  - The diagonal is subdued and unselectable.
  - Ctrl+click selects several cells, and arrow keys or Tab move between them.
  - Keyboard entry, spreadsheet style (`ODCellPanel.eventFilter`): a digit typed on the matrix starts
    that cell's flow in the Flow box; Enter or double-click on a cell opens its flow. In the Flow box
    the arrow keys move to the neighbouring cell (`ODMatrix.step`, hopping the diagonal, staying at
    the edge) with its value selected, so typing replaces it.
- `ODCellPanel`: edits the selected pairs. Its **OD Pair** box shows only while a cell is selected; until
  then one line says to click a cell. Esc, a click beside the cells or
  on the diagonal (`ODMatrix.clear_selection`), and leaving the tab put it away; a cell clicked while
  Institutions is chosen switches the Demand drop-down back to the grid's demand (`MainWindow._cell_clicked`).
  - Flow in veh/h, or trips per day in daily mode ("—" = not entered); Enter jumps to the next cell.
  - Arrival pattern: Random, Poisson, Binomial or Negative binomial.
  - Mode split: `ModeSplitSlider` has one zone per vehicle type from Mode Behavior, in its colours,
    and one handle between each pair of neighbours. The shares always sum to 100 %. There is one
    % box per type, four to a row. Typing a type's share moves the handle after it, so the next
    type absorbs the difference.
  - **Timetabled types** (a headway in Mode Behavior, e.g. a bus every 10 min): `ODMatrix.split_of`
    fixes each one's share at its vehicles over the pair's trips (60 ÷ headway per hour against the
    hourly flow, else × 24 against the daily trips; `mode_behavior.fixed_split`). The other types share
    the rest in their stored proportions, so changing the flow moves only them. The stored split keeps
    those proportions and the fixed share is worked out whenever it is read (matrix bars, tooltips,
    summary, assistant), so small flows typed on the way lose nothing. The fixed type's % box is
    read-only and the slider handles beside it are locked (grey).
  - **Daily mode: Spread over the day.** A curve over 24 hours decides how the day's trips spread.
  - **`CurveEditor`:** shapes the curve like an image editor's Curves tool.
    - Drag a point, click to add one, double-click or right-click to remove one. The ends stay
      at 00:00 and 24:00.
    - Bars under the curve show each hour's share. Hovering shows the hour's share and trips.
  - **Equation dropdown:** Free curve (points), or Polynomial of degree 2–6 with editable
    coefficients a0…an. Switching keeps the shape as closely as the new kind allows.
  - **Weekday Shape** (a calendar icon beside the Equation choice) restores the default two-peak shape. The busiest hour and its veh/h are shown.
  - Copy the ticked items (pattern / modes / flow or trips per day / daily curve, ticked in a two-by-two
    grid so they fit the sidebar) to the row, the
    column or every empty cell.
  - Clear removes the demand of the kind shown from the selected pairs.
- `InstitutionPanel` (Demand → Institutions): one icon button per building type (`institutions.KINDS`);
  picking one loads its kind's default figures into the form (Area, the people its kind has, e.g.
  Students, Employees, Shift from–to, By car) with the car trips they make and their busiest hours, and
  starts placing on the map (`placing_changed`, the area; the footprint follows the Area box). The
  banner says the next step (red, green, the circle's radius and what it reaches). A building clicked on
  the map (`show_selected`) shows its figures, its mode shares (logit, or its car share), its door, its
  catchment and its people a day, with **Mode split** (`split_requested`) and Delete (Del).
- `ModeSplitDialog` (the selected building's Mode split, opened by `MainWindow._open_mode_split`): a row per
  mode (Walk and each vehicle type: use it, out-of-vehicle minutes, cost ¢, cost ¢/km, observed %), the three
  coefficients and the walking speed, prefilled with `network_tools.default_alternative`, `LOGIT_DEFAULTS` and
  `WALK_KMH`; the shares, people and vehicles a day, mean trip and constants update as they change (`compute`:
  `network_tools.institution_split`); **Apply** (`applied`, the arguments) goes through
  `MainWindow._institution_split`, as AI Mode's institution_mode_split does: one undo step.
- `ODCellPanel.planner` (set by the window: `network_tools.demand_plan` on its network) gives
  `generate_demand` its numbers; `_generate` types them in, one undo step (see AI Mode).
  `ODCellPanel.calibrator` (`network_tools.calibration_plan`) does the same for `calibrate_demand`
  (`_calibrate`: the hourly flows fitted to counts, typed in, one undo step), and
  `ODCellPanel.scaler` (`network_tools.scale_plan`) for `scale_demand` (`_scale`), and
  `ODCellPanel.equilibrator` (`MainWindow._equilibrate_demand`) for `equilibrate_demand` (`_equilibrate`: hourly
  flows moved to their elastic equilibrium, q₀·(t/t_ref)^ε with t from headless runs and t_ref from a reference
  run before the change, by successive averages until no pair moves over 2 %; typed in, one undo step). The planner and
  scaler get the matrix's demand of that kind and, for daily demand, each pair's 24 hourly shares
  (`_demand_now`), so a degree of saturation counts the pairs left out and the busiest hour.
  A demand tool naming no pairs works on every pair. `_type_in` types a tool's numbers (one undo step)
  and, for a degree of saturation, has `ODCellPanel.checker` (the window's `_check_demand`) run them,
  typing in the share that held; a failed check puts the matrix back.
- `ODCell(flow, daily, profile, pattern, split)`: the data for one pair. `split` is one percent
  per vehicle type, in Mode Behavior's order. Saved with the project (network_io) and in the undo
  history.

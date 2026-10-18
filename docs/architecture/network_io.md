# network_io.py: files

- **Projects**: `save_network(network, path, conversation, project)` with `project` (from `MainWindow._project`)
  writes a project file: `format` ({"app": "ACMS project", "version": 2}) and the network as below, plus
  `demand` ({shown, pairs: origin, destination, flow, daily, pattern, split, quarters (hourly
  demand's 15-minute shares; absent: an even hour, as in older files), profile {kind, points, coefficients}}), `vehicle_types` ({types: every VehicleMode field, default_split}) and `run` ({seed,
  speed}); written to a `.part` file and moved into place. `project_problem` checks those parts (1–8 named
  types with colours, headways and non-negative numbers; splits of whole percents adding to 100, one a
  type; pairs of two different ends, once each; flows 0–100,000 veh/h, 0–2,000,000 trips a day; known
  arrival patterns; quarters of four numbers, none negative, some above zero; curves of ordered points or 3–7 coefficients; a seed 0 to 2³¹ − 1 and a positive
  speed); `saved_project(path)` reads and checks them. JSON, not JSON Lines: one nested document read and
  checked whole (JSON Lines suits appended record streams). `MainWindow.load_file` builds everything first
  (VehicleMode(**record) refuses a setting it doesn't know; the conversation's messages and transcript must be
  lists) and changes nothing on a refusal; then the
  network, the cells, the types, the demand shown, seed and speed, and an undo history of its own (Undo can't
  bring the previous project back under this one's name, where Save would overwrite it). Unsaved changes (`unsaved`: another undo
  state, seed or speed than when saved or opened) show as * in the title and are asked about (Save, Discard,
  Cancel) before closing or opening another; offscreen (tests) never asks.
- Vehicle types include `random_colour` (boolean, defaults false for older projects). True varies only the
  rendered vehicle paint; the saved `colour` remains a valid hex categorical colour for charts. Invalid flag
  values are refused before save/load changes anything. Camera and building-visibility choices stay session-only.
- `save_network(network, path, conversation=None)`: synchronises lengths, validates, writes JSON with
  `network` (configuration), `nodes`, `roads`, `approach_nodes`, `approach_roads`, `bus_stops`
  (older files without it load with none), `institutions` (older files: none), `metro_stops`, `metro_lines` and `metro_demand` (older files: none), `interchanges` (older files: none, so their interchanges
  aren't mirrored on a change of side) and `background` (older files: the default grass green;
  an image is saved as its file's path)
  (older files' static mock-up `vehicles` are ignored on load),
  plus the AI Mode `conversation` when there is one. `saved_conversation(path)` reads it back. Strict JSON:
  NaN and ±Infinity are never written (`allow_nan=False`) and a file holding them is refused on load
  (`load_network`, `saved_project`).
- `load_network(path)`: parses, rejects duplicate IDs, validates, and rejects files whose
  stored `length_m` disagrees with the node geometry. Curves (`shape`, `curvature`),
  `overpass` and turn pockets (`pockets`; older files' roads have none) round-trip exactly.
- `route_choice` is saved beside the network's records and loaded back (older files have none: off).
- Institution records include optional `height_m` (finite 0.5–300 m, total including roof). Older records without
  it load as None, using the labelled assumed type height. `institution_problem` rejects invalid heights before
  installation, and the normal network/project save and undo paths preserve explicit heights.

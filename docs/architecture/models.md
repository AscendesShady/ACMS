# models.py: the network model (no Qt)

Dataclasses:
- `Node(id, x, y, row, column, control, supply_vph, demand_vph, signal_phases, signal_method, green_times_s,
  cycle_min_s, cycle_max_s, target_saturation, stop_weight_s)`.
  `control` is `"signalised"`, `"roundabout"` or `"uncontrolled"` (merges, diverges, bends).
  `supply_vph` / `demand_vph` are the flows entering / leaving at a boundary end.
  Signal timing: `signal_phases` is 2 or 4; `signal_method` one of `SIGNAL_METHODS` (`webster`,
  `transyt`, `synchro`, `scats`, `scoot`, `fixed`, `actuated`, `max_pressure`); `green_times_s`, one per phase, is used by `fixed`
  only; the cycle limits (20–240 s, default 40–120) by the coordinated methods; `target_saturation`
  (50–110 %, default 90 %) by SCATS and SCOOT; `stop_weight_s` (the delay a stop counts as, default
  10 s) by TRANSYT and SCOOT. Older files load with the defaults. A new junction (`add_node`, a generated grid, an
  interchange's) takes the network's `default_signal_method` (default `webster`, saved with the network).
- `Road(id, from_node, to_node, length_m, lanes_per_direction, lane_changing, speed_limit_kmh,
  direction, road_type, shape, overpass, curvature, single_lane)`. `shape` holds the interior points of a
  curved road (from → to); `direction` is `"two-way"` or `"one-way"` (traffic runs from → to).
  `single_lane` makes a one-way road of 1 lane per direction a single lane (a single-lane slip road);
  `validate` refuses it on any other road, and an edit that makes the road two-way or wider clears it.
  `lanes` (across the road), `lanes_each_way` (each direction its traffic runs) and `width_m` are what the
  map, the simulation, roundabout design and the rest read: one place for a road's lanes.
  `pockets` (turn pockets, short turn lanes): end node id → `{"lanes": 1 or 2, "length_m", "side": "kerb" |
  "offside"}`, on the approach to an end its traffic arrives at (`arrival_ends()`). A kerb pocket takes kerb
  turns, an offside one offside turns and U-turns. It opens `length_m` back from the stop line; the carriageway
  widens on that traffic's kerb side over `POCKET_TAPER_M` (20 m) before it (an offside pocket's through lanes
  move over to the kerb side there). `width_m` leaves pockets out. `pocket_problem` (run by `validate`) checks
  each one: the end is a signalised or uncontrolled junction (not a boundary end, and not a roundabout, which
  NCHRP 672 sizes without flares); lanes 1–2, length `POCKET_LENGTH_M` 15–150 m, a known side; the road allows
  lane changing and isn't single-lane; no bus bay serving that traffic overlaps the widened stretch; and all of
  the road's pockets and tapers fit between its stop lines. Pockets are kept by end node: `reverse_road` keeps
  them; `split_road` gives each to the part that reaches its end; `_retie` renames the key. Relative to the
  driving side, so `set_driving_side` leaves them as they are and each keeps serving the same turns.
- `BusStop(id, road, distance_m, direction, dwell_s)`: a bay beside the kerb. `distance_m` is the
  bay's middle, metres along the road's centre line from its `from_node`; `direction` is `"forward"`
  (from → to), `"backward"` or `"both"`; `dwell_s` is how long each bus stands at it (20 s by
  default, 0 to `MAX_DWELL_S` 600 s). `boarding_per_h` (passengers arriving to board, 0 to `MAX_BOARDING_PER_H`
  5,000) and `alighting_percent` (the share of a bus's load getting off, 0–100): either above 0 and the dwell
  comes from the passengers instead (simulation.md, Bus stops).
- `Institution(id, kind, x, y, area_m2, radius_m, people, employees, shift_start_h, shift_end_h, car_share,
  mode_shares, height_m=None)`:
  a building that makes trips (institutions.py). `kind` is one of `INSTITUTION_KINDS` (Residential, School,
  Hospital, Office, Shop); (x, y) is its square footprint's centre; `radius_m` its circle of influence;
  the shift in hours after midnight. `institution_problem` checks the figures (a known kind; area 1 m² to
  1 km²; a circle from its own walls to `MAX_INFLUENCE_M` 3 km; whole people and employees; a shift that
  starts and ends at different times; car share 0–100 %; `mode_shares` people % by mode name, each 0–100 and
  adding up to 100, or empty). `radius_m` is its catchment (institutions.md).
- `MetroStop(id, x, y)` and `MetroLine(id, stops)` in `Network.metro_stops` / `metro_lines` (Edit Network → Metro):
  a stop is a point in metres, apart from the roads; a line is its stops in order, the track running straight from
  each to the next (`metro_length_m`). `add_metro_line(first, second)` makes ML1, ML2... from its first two stops
  (stations M1, M2...: the metro's own network and OD matrix are labelled by them), and `add_metro_stop(line, x, y)`
  extends it; both are checked first (finite numbers, at least
  `METRO_MIN_SPACING_M` 150 m from the line's last stop, so platforms don't overlap: the app's floor) and a refusal
  changes nothing. `validate`: every line has its own ID and two stops or more, all on the map, and each stop is on
  exactly one line. `Network.metro_demand` is the metro's own OD matrix, {"M1->M2": passengers an hour}, each pair two
  different stations, 0 to `MAX_METRO_DEMAND` 100,000 (`validate`). `delete_metro_line` removes a line, its stops and
  the metro demand to or from them. A line's service (`headway_min`, `speed_kmh`,
  `cars_per_train`, `car_capacity`, `fare_cents`; `train_capacity` their product; `METRO_SERVICE`'s assumed defaults
  and ranges, `metro_line_problem`): metro.md.
  Listed in `as_records`.
- `Institution.height_m`: optional total visual height including the roof, 0.5–300 finite metres (no bool/text).
  `institution_problem` validates it before placement/load. None preserves an explicitly assumed kind default
  (`BUILDING_HEIGHTS_M`: Residential 9, School 7.2, Hospital/Office 18, Shop 4.5), not measured site data.
  Footprint area, catchment, people and the demand model do not depend on height.
- `Network`: the single source of truth. It holds `nodes` / `roads` (internal),
  `approach_nodes` / `approach_roads` (boundary ends), `bus_stops`, `institutions` and the defaults. Vehicles are
  not part of the network: a run makes them (simulation.py).
- `lane_end_inset(control, width, length, widest, diameter)`: where a road's lanes end at a junction
  (stop line, give-way line just outside a roundabout's circle, or an uncontrolled junction's edge).
  The simulation cuts its lanes there and bus stop bays keep clear of it.
- **Bus bay shape**: `BAY_ENTRY_M` 15 m taper, `BAY_STOP_M` 15 m stopping length, `BAY_EXIT_M`
  10 m taper (`BAY_LENGTH_M` 40 m), `BAY_WIDTH_M` 3 m; `bay_depth(t)` is how far out it reaches
  t metres in (smoothstep tapers). The map draws it and buses drive it.
- `stop_line_inset(road width, road length)`: where a controlled junction's stop line sits; the
  map draws it there and the simulation stops vehicles there.

`Network` operations:
- `road_points(road)`: the centre line from → to through `shape`.
- `curvature_at(road, distance, offset)`: signed curvature at a distance along a road or a lane.
- `reverse_road(id)`: swaps the ends; a curve's points reverse and its curvature
  reverses order and changes sign.
- `set_driving_side(side)`: left- or right-hand traffic for the whole network; returns the
  interchanges mirrored. Everything drawn and simulated from the side follows it. Each interchange in
  `interchanges` (name → `[x, y, ux, uy]`: where it was built and the freeway's direction, recorded by
  its builder) is mirrored across the freeway's normal there: its nodes (`DI1.1`, …) and the shapes of
  the roads between them (curvature changes sign), directions kept. A mirror image swaps keep-left
  and keep-right, so the result is exactly the interchange built for the other side (a test compares
  every road of a diamond, SPUI and trumpet, both ways). A road tying it to the rest of the network
  stays where it was, re-tied to the interchange node now at its old end and renamed "from-to"
  (`_retie`, its bus stops following). Merely reversing the ramps would be exact only for the
  diamond: a SPUI's and trumpet's exits and entries tie in at different distances.
- `set_road_length(id, metres)`: slides the far end along the road.
  - An internal grid road carries the rest of its corridor (same row or column) with it,
    so crossing roads lean instead. A shorter top edge over a longer bottom edge makes a
    trapezoid.
  - Boundary roads, off-grid nodes and curves move only the far end; a curve scales exactly.
- `move_nodes(ids, dx, dy)`: rubber-band drag. Only these nodes move and their roads
  stretch. A move that would leave any road shorter than 1 m is refused.
- `_carry_shapes(before)`: keeps curves attached when their end nodes move.
  - Each curve follows the similarity transform (move, rotate, scale) that maps its old
    chord onto the new one, computed with complex numbers.
  - Its curvature divides by the scale factor.
- `add_node(x, y)`: adds an off-grid junction `N#`.
- `add_road(a, b)`: adds a new two-way road with the network defaults.
  `connection_problem(a, b)` explains a refusal.
- `add_end(inner, x, y)`: adds a boundary end and its road.
  - The side is the dominant direction from the junction.
  - The number is placed between the neighbouring ends' numbers (the 1.5E rule).
- `crossings(points, touching)`: where a planned line crosses existing roads, nearest first.
  Touches within 2 m of a road end don't count.
- `split_road(id, node)`: cuts a road at a node.
  - Both parts keep the road's settings.
  - A curve splits at its nearest segment, and the cut segment's curvature goes to both parts.
- `delete_node`, `delete_road`: removal. A boundary road takes its end node with it; a road takes
  its bus stops.
- Bus stops:
  - `add_bus_stop(road, direction)`: the first mid-way between the road's junctions; each after it
    in the middle of the longest free stretch between the road's stops (all directions), or failing
    that at the road end farthest from them, so no new bay lands on another. Refused, changing
    nothing, on a road too short for a bay (40 m clear of both junctions) or with no room left (a
    bay's length clear of every other stop). Moving a stop afterwards may bring bays together.
  - `move_bus_stop(id, metres)`: slides it, clamped to `bus_stop_range(road)` (the whole bay clear of
    the junction at each end, `lane_end_inset`).
  - `bus_stop_distance(stop)`: where it is now, clamped again if the road has changed since.
  - `bus_stop_sides(stop)`: the traffic it serves (a one-way road only has from → to).
  - `reverse_road` measures a stop from the other end and flips forward/backward; `split_road`
    moves it to the part it is on.
- `objects_in_box(left, top, right, bottom, crossing)`: a selection box's `(kind, id)` objects
  (`node`, `end`, `road`, `bus_stop`). A window (`crossing` false) takes what lies wholly inside:
  a junction or end by its point, a road by every point of its line, a stop by its bay's stretch of road.
  A crossing box takes whatever it touches (`polyline_meets_box`).
- `validate()`: every invariant, including finite numbers in every number field of the network, its junctions,
  roads (shape points and curvature too), bus stops and background (`number_problem`: no NaN, ±infinity, text or
  bool, which every sign check would let through), IDs, references, positive lengths and speeds,
  control types, 2 or 4 phases with positive greens, a known timing method with sensible limits, one
  curvature value per segment, and bus
  stops on known roads with a valid direction and a dwell time of 0 to 600 s, and institutions (`institution_problem`).
- `synchronize_road_lengths()`: `length_m` is always the geometric length.
- `Network.route_choice`: {} (every vehicle the fastest route) or {"theta_per_min", "routes", "link_times_s"}
  (simulation.Graph.route_shares; AI Mode's assign_routes), checked by `route_choice_problem` in `validate()`
  (θ 0.01–10, 1–6 routes, positive seconds by link id), saved with the network and part of undo.

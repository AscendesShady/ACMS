# models.py: the network model (no Qt)

Dataclasses:
- `Node(id, x, y, row, column, control, supply_vph, demand_vph, signal_phases, signal_method, green_times_s,
  manual_greens_s, amber_s, cycle_min_s, cycle_max_s, target_saturation, stop_weight_s)`.
  `control` is `"signalised"`, `"roundabout"` or `"uncontrolled"` (merges, diverges, bends).
  `supply_vph` / `demand_vph` are the flows entering / leaving at a boundary end.
  Signal timing: `signal_phases` is 2 or 4; `signal_method` one of `SIGNAL_METHODS` (`webster`,
  `transyt`, `synchro`, `scats`, `scoot`, `fixed`, `actuated`, `max_pressure`, `manual`); `green_times_s`, one per phase, is used by `fixed`
  only; `manual_greens_s` (arriving road id -> [first, last] second of the cycle the arm goes, 1-based: 1-120 is the
  cycle's first 120 s; each key a road at the node, refused otherwise: `split_road` gives a window to the part
  reaching the node, `_retie` renames it and moves it with the road's end, `delete_road` drops it) and `amber_s` (3–6 s, default `MANUAL_AMBER_S` 3, the last seconds of each window) by
  `manual` only: red is the rest of the cycle, which ends at the latest window's last second. `_check_manual` refuses
  a window outside 1–`MANUAL_CYCLE_MAX_S` (300) or no longer than its amber, and two arms sharing a second unless
  they are opposite (more than 135° apart) with the same window. An arm with no window stays red; a window of a road
  no longer at the junction is ignored. `signal_arms(node)`: each arriving road, its compass name (N, E, S, W; the
  road's id too when two point the same way) and bearing, clockwise from north; the cycle limits (20–240 s, default 40–120) by the coordinated methods; `target_saturation`
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
  `cycle_lanes`: the arrival ends (node ids) whose traffic has a cycle lane, bikes only, the road's whole length, a
  bare list of ids (old files: none). `validate` refuses one at an end its traffic doesn't arrive at (made one-way)
  or twice. `cycle_lane_line(road, end)`: its centre line, `CYCLE_LANE_M` 1.5 m wide (an assumption: a common
  desirable minimum) just beyond the kerb lane on the driving side, moving out with the kerb along a pocket's taper
  at that end; the lane graph and the map both use it. `width_m` leaves it out. `split_road` runs each cycle lane on
  along both parts (to its end on one, to the cut on the other); `_retie` renames the end.
  `footpaths`: "left" and/or "right" (as seen from from_node to to_node), a bare list; `validate` refuses any other
  side or one twice; `reverse_road` swaps them and `split_road` gives both parts the same. `kerb_side(road, end)` is
  the side the kerb of the traffic arriving at `end` is on; `beside_line(road, side, metres)` a line that far beyond
  the carriageway's edge on a side, out along a pocket's taper there, which `cycle_lane_line` and `footpath_line`
  (`FOOTPATH_M` 2.0 m, beyond any cycle lane on that side) both use. `Network.walkways` ({P1: `Walkway(id, start,
  end, bends)`}: a path between two road nodes, `add_walkway`, `walkway_points`) and `Network.walk_demand` ({"1W->1E":
  people an hour}, two different boundary ends, 0 to `MAX_WALK_DEMAND` 100,000) are checked by `validate`, saved
  (network_io: `walkways`, `walk_demand`; older files have none) and pruned with the nodes and ends they name
  (`_prune_walking`, after `delete_road` and `delete_node`): walking.md. `Network.pedestrian_crossings` ({X1:
  `Crossing(id, road, distance_m, kind)`}: `add_crossing`, `crossing_problem`) follow their road as bus stops do (a
  rename, `reverse_road` measuring from the other end, `split_road` to the part they are on) and go with it; each at
  least `ZEBRA_CLEAR_M` from either end and beyond that junction's corners (`footpath_corner_m`: its widest road's
  half, a cycle lane and a footpath; a roundabout's circle and a footpath; 0 at a boundary end); validate
  refuses an edit that leaves one impossible (a footpath taken away, the road shortened past it). (`Network.crossings`
  is another thing: where a line crosses roads.) `Network.spawn_points` ({S1: `SpawnPoint(id, x, y)`},
  `add_spawn_point`, `delete_spawn_point` taking the walking to and from it) and `Network.spawn_demand` ({"S1->S2":
  people an hour}, two different deploy points, 0 to `MAX_WALK_DEMAND`) are checked by `validate` and saved
  (network_io: `spawn_points`, `spawn_demand`; older files have none). Daily demand of the three matrices,
  `Network.walk_daily`, `spawn_daily` and `metro_daily` ({"1W->1E": {"people": a day, "curve":
  `daily_profile.profile_record`}}), is kept apart from the hourly: `validate` checks each pair's places as the
  hourly's, its people 0 to 24 times the hourly limit and its curve (`daily_profile.from_record`); saved under the
  same names (older files: none) and pruned with their places as the hourly is.
- `BusStop(id, road, distance_m, direction, dwell_s)`: a bay beside the kerb. `distance_m` is the
  bay's middle, metres along the road's centre line from its `from_node`; `direction` is `"forward"`
  (from → to), `"backward"` or `"both"`; `dwell_s` is how long each bus stands at it (20 s by
  default, 0 to `MAX_DWELL_S` 600 s). `boarding_per_h` (passengers arriving to board, 0 to `MAX_BOARDING_PER_H`
  5,000) and `alighting_percent` (the share of a bus's load getting off, 0–100): either above 0 and the dwell
  comes from the passengers instead (simulation.md, Bus stops).
- `Institution(id, kind, x, y, area_m2, radius_m, people, employees, shift_start_h, shift_end_h, car_share,
  mode_shares, height_m=None, curves={}, per_hour={})`: `curves` and `per_hour` are its own day's
  arrive/leave curves and its people an hour (institutions.md), keyed "arrive" and "leave", checked by
  `institution_problem` (people an hour 0 to `MAX_INSTITUTION_PER_H`; a curve `from_record` reads);
  a building that makes trips (institutions.py). `kind` is one of `INSTITUTION_KINDS` (Residential, School,
  Hospital, Office, Shop); (x, y) is its square footprint's centre; `radius_m` its circle of influence;
  the shift in hours after midnight. `institution_problem` checks the figures (a known kind; area 1 m² to
  1 km²; a circle from its own walls to `MAX_INFLUENCE_M` 3 km; whole people and employees; a shift that
  starts and ends at different times; car share 0–100 %; `mode_shares` people % by mode name, each 0–100 and
  adding up to 100, or empty). `radius_m` is its catchment (institutions.md).
- `MetroStop(id, x, y)` and `MetroLine(id, stops, track)` in `Network.metro_stops` / `metro_lines` (Edit Network →
  MRT): a stop is a point in metres, apart from the roads; a line is its stops in order and its `track`, a polyline
  through them (`metro_track`; an older file's empty track: straight stop to stop; `metro_length_m` its length).
  `add_metro_line(first, second, merge)` makes ML1, ML2... from its first two stops (stations M1, M2...: the MRT's
  own network and OD matrix are labelled by them), and `add_metro_stop(line, x, y, merge)` extends it with the leg
  `metro_leg` works out (`heading`: aimed through the stop): `geometry.rail_leg` keeps the last stop's platform on
  straight track (`METRO_PLATFORM_M` 120 m: a 5-carriage train and room, an assumption), eases into a curve of
  `METRO_MIN_RADIUS_M` 300 m (a desirable urban MRT minimum: an assumption) through transitions of
  `METRO_TRANSITION_M` 60 m (L = v³ / (R C) at 80 km/h with C 0.6 m/s³: an assumption) and runs straight into the next
  platform; a point needing a turn of `METRO_MAX_TURN_DEG` 89 or more (no right-angle or acute bends), or inside the
  bend's reach, is drawn in like a rubber band to the most the leg may turn, as far out, and the stop goes where the leg
  ends; an aim the leg can't meet is refused. Refused,
  changing nothing: numbers off the map, a stop under `METRO_MIN_SPACING_M` 150 m (straight-line) from the line's last
  (platforms would overlap: the app's floor), or a leg crossing its own track. A leg crossing another line raises
  `MetroCrossing` (its `line_id`); with `merge` the leg ends at the first crossing in one stop on both lines (an
  interchange), put into the other line's order by where it lies along its track, refused under 150 m from the other
  line's stations. `validate`: every line has its own ID, two stops or more, all on the map, none twice, a track of
  two [x, y] points or more (or none), and every stop is on a line (on two or more: an interchange). `Network.metro_demand` is the MRT's own OD matrix, {"M1->M2": passengers an hour}, each pair two
  different stations, 0 to `MAX_METRO_DEMAND` 100,000 (`validate`). `delete_metro_line` removes a line, its stops (an interchange off
  the other line too, unless that would leave it under two) and
  the MRT demand to or from them. A line's service (`headway_min`, `speed_kmh`,
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
  - Bus stops and crossings stay put from the end that doesn't move: on a road whose from_node moves (a west
    or north boundary road runs from its outer end) their `distance_m` shifts by the change in length.
- `move_nodes(ids, dx, dy)`: rubber-band drag. Only these nodes move and their roads
  stretch. A move that would leave any road shorter than 1 m, or bring a curve's two ends together, is refused
  (False): the nodes and every road's shape and curvature are put back exactly.
- `_carry_shapes(before)`: keeps curves attached when their end nodes move.
  - Each curve follows the similarity transform (move, rotate, scale) that maps its old
    chord onto the new one, computed with complex numbers.
  - Its curvature divides by the scale factor.
  - Ends brought together leave no chord to follow: ValueError (`move_nodes` refuses the move).
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
    that at the road end farthest from them, so no new bay lands on another; the next of those spots
    when one fails `bus_stop_problem`. Refused, changing nothing, on a road too short for a bay (40 m
    clear of both junctions) or with no spot left (a bay's length clear of every other stop, and clear
    of the crossings and turn pockets).
  - `move_bus_stop(id, metres)`: slides it, clamped to `bus_stop_range(road)` (the whole bay clear of
    the junction at each end, `lane_end_inset`); refuses (returns False, nothing changed) a bay that
    fails `bus_stop_problem`, so a drag stops short of the clash.
  - `bus_stop_problem(stop)`: why its bay can't be where it is, or None: on another stop's bay (a bay's
    length apart on its road), on a crossing (`crossing_problem`'s clearance) or beside a turn pocket of
    the traffic it serves (`pocket_problem`).
  - `bus_stop_distance(stop)`: where it is now, clamped again if the road has changed since.
  - `bus_stop_sides(stop)`: the traffic it serves (a one-way road only has from → to).
  - `reverse_road` measures a stop from the other end and flips forward/backward; `split_road`
    moves it to the part it is on.
- `objects_in_box(left, top, right, bottom, crossing)`: a selection box's `(kind, id)` objects
  (`node`, `end`, `road`, `bus_stop`, `metro_line`). A window (`crossing` false) takes what lies wholly inside:
  a junction or end by its point, a road by every point of its line, a stop by its bay's stretch of road, an MRT
  line by every point of its track (`metro_track`).
  A crossing box takes whatever it touches (`polyline_meets_box`).
- `validate()`: every invariant, including finite numbers in every number field of the network, its junctions
  (each within `MAP_LIMIT_M`, 1,000 km, of the origin: x = 1e300 is finite but off any map),
  roads (shape points and curvature too), bus stops and background (`number_problem`: no NaN, ±infinity, text or
  bool, which every sign check would let through; an int field such as `lanes_per_direction`, `signal_phases`
  or a PDF's `page` a whole number, 2.0 refused too; an MRT line's `cars_per_train` and `car_capacity`
  whole, `metro_line_problem`), a background image on this computer (`image_path_problem`: a network share,
  `\\server\share` or `//server/share`, is refused, as drawing it would connect to that server and, on
  Windows, send the user's sign-in hash), IDs, references, positive lengths and speeds,
  control types, 2 or 4 phases with positive greens, a known timing method with sensible limits, one
  curvature value per segment, and bus
  stops on known roads with a valid direction and a dwell time of 0 to 600 s, and institutions (`institution_problem`).
- `synchronize_road_lengths()`: `length_m` is always the geometric length.
- `Network.route_choice`: {} (every vehicle the fastest route) or {"theta_per_min", "routes", "link_times_s"}
  (lane_graph.Graph.route_shares; AI Mode's assign_routes), checked by `route_choice_problem` in `validate()`
  (θ 0.01–10, 1–6 routes, positive seconds by link id), saved with the network and part of undo.

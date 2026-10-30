# background_network.py: a network from the background (no Qt)

- **OpenStreetMap** (`overpass_query`, `fetch_osm`, `osm_graph`): one Overpass API request (`OVERPASS_URLS`: three, the
  next tried when one fails, then all again after `OVERPASS_RETRY_S`, each given `OVERPASS_TIMEOUT_S` for its whole
  answer (40 s; at 90 a live build waited 6.5 minutes when all were down) and at most `OVERPASS_MAX_BYTES` (100 MB:
  `map_tiles.fetch`, which also turns http.client's errors, an answer cut short, into OSError). A server fails when
  it can't be reached, is too slow, cuts its answer short, sends what isn't JSON road data (an HTML rate-limit page)
  or reports its own `runtime error` in `remark` (its timeout or memory limit: the elements are partial); such an
  answer is never kept. Each road answer is kept in `OSM_CACHE` by its query, written whole (`results.write_whole`),
  copies untouched for `OSM_KEEP_DAYS` (90) deleted then: a copy younger than
  `OSM_FRESH_DAYS` (7) is used without asking any server, an older one when no server answers, either said to be a
  copy, from when and why (`cached_at`, `cached_why`); the app's User-Agent) for every way of the chosen `CLASSES` in the area's
  WGS84 bounds (not areas, not private), with their nodes. Each point goes through the map's georeference
  (`Frame.to_scene`), each way is cut to the area (Liang–Barsky), and a border node is made where it
  crosses the edge. Ways split into streets at nodes two ways share. A way's tags give its lanes per
  direction (half its `lanes`, or half rounded up on a one-way road, whose editor carriageway is two
  lanes per direction wide; else the class's; a one-way street of one lane in all, `lanes=1` or none tagged where its
  class has one a direction, is a single-lane road, `Road.single_lane`), speed (`maxspeed`, mph converted; else the class's),
  one-way (`oneway`, `-1` reversed; roundabouts and motorways), bridge, and road type (the class). Nodes
  tagged highway=traffic_signals are kept when they are a junction's signal (`JUNCTION_SIGNALS`: traffic_signals
  `signal`, `traffic_lights`, `yes`, `junction`, or none given); the rest (a pedestrian crossing's, `emergency`,
  `blinker`, `ramp_meter`, `level_crossing`, `lane`, bus or tram priority, `on_demand`) are only counted by kind
  (`Graph.skipped`), and the tool reports them. Mini-roundabouts are kept; junction=roundabout rings are grouped.
  OSM holds no signal timings (no established tag: a proposed `type=timing` relation is used 3 times in all), so a
  signalised junction starts with the editor's default, Webster from the demand at each Run; the Edit tab's
  Control and timing method (or `set_junction_control`) override any junction.
- **An image** (`raster_graph`): the mask (`background.BackgroundPainter.road_mask`, traced off the GUI thread;
  `BackgroundPainter.image` loads one image at a time under a lock, as PDFium, which renders a PDF page, isn't
  thread-safe and the map may be drawing: the image scaled to
  about `resolution_m` a pixel, each pixel within `colour_tolerance` of the colour on red, green and
  blue) padded, specks under 30 pixels dropped, thinned by Zhang–Suen to one-pixel lines; `_trace` groups
  touching node pixels (not exactly two neighbours), traces the lines between them, loops too, prunes
  spurs under 8 pixels, drops pieces of line under 60 m on their own (a label's halo), joins bends,
  simplifies (Douglas–Peucker, 1 pixel) and places them in scene
  metres by the image's affine. Line ends within half a road width of the image's edge snap to it and
  become boundary ends; the road width is the road pixels over the lines' length, and the lanes that
  width over 7 m (1–4).
- **Walking and cycling** (the map; walking.md): the query also takes footway, path, pedestrian and steps ways
  (`WALK_CLASSES`) and highway=crossing nodes. A road way's `sidewalk` / `sidewalk:left|right|both` tags (yes,
  separate) give its `Street.footpaths` (sides along its own way, swapped with it: oneway=-1, `_turned`), its
  `cycleway*` tags (lane, track, opposite_*: `CYCLE_TAGS`) `Street.cycle`; streets join only when alike in both; a
  road gets them (`dress`; cycle lanes for every way its traffic arrives). A crossing node marked zebra, marked,
  uncontrolled or traffic_signals (`ZEBRA_TAGS`; a signalised one taken as a zebra, with no pedestrian signals)
  goes into `Graph.crossings`, others into `Graph.walking_skipped` by kind; footways into `Graph.footways` as
  sidewalk, crossing (footway=crossing), bridge or path. **`add_walking(network, graph, footpaths)`** after build:
  footpaths "none" (the default: no walkways, crossings or paths come in; it returns at once), "tagged" (the tags, and each footway=sidewalk way on the side of the road within `SIDEWALK_M` 15 m it
  runs beside) or "all"; with either, a zebra on the road within `CROSSING_M` 8 m of its node, or where a crossing way
  cuts a road, a footbridge where it passes over one an overbridge, each giving its road footpaths both sides (one
  within `ZEBRA_CLEAR_M` of an end is counted, not placed: walkers cross a junction's arms in gaps); other footways
  paths between the road nodes they come within `PATH_SNAP_M` 15 m of (one reaching fewer than two: counted as not
  joined). It returns the counts, which `network_from_background` reports under `pedestrians`.
- **`build(template, graph, lanes=None)`**: roundabout rings become one junction each at their centre; junctions within
  `MERGE_M` 20 m become one (a dual carriageway's crossing, mapped as 2-4 nodes 12-19 m apart: as
  separate junctions, 2-3 cars of room between them gridlocked Dhanmondi; SUMO's netconvert joins such clusters too).
  Each street's lanes per direction are held within `lanes` ({"main" or "local": (fewest, most)}; local: unclassified,
  residential and living streets; an image's roads are main). **`_routable`** then keeps only the streets a car can
  drive between the area's ends: the largest strongly connected part, and the one-way chains into it from an
  entry-only end or out of it to an exit-only end. A one-way street into a dead end, a part cut off where the edge
  clips its loop, a road joined to nothing are left out (`graph.unroutable` counts them; SUMO's netconvert
  `--keep-edges.components` keeps the largest weakly connected parts, this the strongly connected one), so every origin end reaches every destination end: a 2 km OSM build had
  190 of 1,606 OD pairs with no route before, none after. A node on two alike streets goes (one
  street through it). A junction is a roundabout, signalised (3 or more arms and a junction signal within
  `SIGNAL_M` 30 m) or uncontrolled (a priority junction: its two most nearly aligned arms are the major road). Streets
  become roads with their shapes; two between the same junctions, a loop, or one crossing the whole area
  are cut in half with a junction there; one reaching the edge ends in a boundary end (`add_end`), after a
  bend junction near the edge if it curves, reversed if its traffic comes in. An entry road shorter than
  `ENTRY_ROAD_M` (60 m; cut just past a junction) is carried on out along its line to that length: a 5–15 m stub
  held one car, so traffic waited off the network and a demand check called real demand a jam (Dhanmondi). Ends are added in order along
  each side, so they are numbered 1, 2, 3 (E/W from the bottom, N/S from the left). Refused: no roads, or
  over `MAX_JUNCTIONS` (800).

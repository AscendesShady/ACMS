# background_network.py: a network from the background (no Qt)

- **OpenStreetMap** (`overpass_query`, `fetch_osm`, `osm_graph`): one Overpass API request (`OVERPASS_URLS`, the
  next tried when one is busy; the app's User-Agent) for every way of the chosen `CLASSES` in the area's
  WGS84 bounds (not areas, not private), with their nodes. Each point goes through the map's georeference
  (`Frame.to_scene`), each way is cut to the area (Liang–Barsky), and a border node is made where it
  crosses the edge. Ways split into streets at nodes two ways share. A way's tags give its lanes per
  direction (half its `lanes`, or half rounded up on a one-way road, whose editor carriageway is two
  lanes per direction wide; else the class's), speed (`maxspeed`, mph converted; else the class's),
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
- **`build(template, graph)`**: roundabout rings become one junction each at their centre; junctions within
  `MERGE_M` 12 m become one (a dual carriageway's two crossings); a node on two alike streets goes (one
  street through it). A junction is a roundabout, signalised (3 or more arms and a junction signal within
  `SIGNAL_M` 30 m) or uncontrolled (a priority junction: its two most nearly aligned arms are the major road). Streets
  become roads with their shapes; two between the same junctions, a loop, or one crossing the whole area
  are cut in half with a junction there; one reaching the edge ends in a boundary end (`add_end`), after a
  bend junction near the edge if it curves, reversed if its traffic comes in. Ends are added in order along
  each side, so they are numbered 1, 2, 3 (E/W from the bottom, N/S from the left). Refused: no roads, or
  over `MAX_JUNCTIONS` (800).

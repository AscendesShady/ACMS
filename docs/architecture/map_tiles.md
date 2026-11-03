# map_tiles.py and background.py: the map's background

- `models.Background(kind, colour, image, page, x_m, y_m, width_m, rotation_deg, opacity, lat, lon,
  anchor_x, anchor_y, map_rotation_deg, locked, reference)` on `Network.background`: `kind` is `colour`,
  `image` or `map`; the colour also shows under an image or the map. The image is turned `rotation_deg`
  clockwise about its top-left corner (x_m, y_m); the map `map_rotation_deg` clockwise about its anchor
  (0: north up). `locked` pins it. `reference` is Set Scale's line, `[x1, y1, x2, y2]` in image pixels
  and its true length in metres (empty: none; a new image clears it). Validated: a known kind, a
  `#rrggbb` colour, a positive width, opacity 0–1, latitude within Web Mercator's ±85°, longitude ±180°,
  turns within ±360°, a true/false lock, and a reference of two different points and a positive length.
- **Georeference** (`map_tiles.Frame`, no Qt): the scene point (anchor_x, anchor_y) sits at (lat, lon);
  round it the scene is Web Mercator metres (EPSG:3857) scaled by cos(latitude) of that point, true to
  scale there (1 km in the scene is 1 km on the ground to within a metre at 1 km), and turned by the
  map's rotation about the anchor. `to_scene`, `to_local` (metres east and south of the anchor, before
  the turn), `to_mercator`, `to_wgs84`; `zoom(pixels per metre)` picks the tile zoom whose pixels come
  nearest the screen's (0–19); `tiles(rect, z)` lists the OSM tiles covering a scene rectangle (the
  bounds of its corners on the turned map; their standard x/y names), dropping to a coarser zoom past
  80; `tile_rect` places one in the map's own metres, drawn translated to the anchor and turned.
- **Tiles** (`map_tiles.TileStore`): `tile.openstreetmap.org/{z}/{x}/{y}.png`, following the OSM Tile
  Usage Policy: an identifying User-Agent, at most two downloads at a time, each tile cached on disk
  (Qt's cache folder, `osm-tiles/z/x/y.png`) and reused for 7 days, only the tiles in view, a failed
  one not asked again for a minute. `ready` (from the download thread) asks the view to redraw. A tile is written
  whole (`results.write_whole`, the app's one file writer: a `.part` beside it, then `os.replace`), tiles untouched for `KEEP_S` (30 days) are deleted
  when a store opens (`prune`, on its download thread), and one on disk that can't be decoded is deleted and
  fetched again (`discard`, from `BackgroundPainter`). Every download (`fetch`: tiles, searches, Overpass) gives up,
  as an OSError, after `DOWNLOAD_S` (20 s) for the whole answer or past `DOWNLOAD_MAX_BYTES` (4 MB), and turns
  http.client's own errors (an answer cut short) into OSError.
- **Place search** (`map_tiles.PlaceSearch`): OSM's Nominatim (`nominatim.openstreetmap.org/search`, jsonv2, up
  to 5 places), following its Usage Policy: the same identifying User-Agent, one request at a time on its own
  thread and at most one a second from the whole app (`search_turn`, `SEARCH_GAP_S`: the assistant's place_map
  waits its turn too), each query's answer cached (case aside, per country), and only when the
  user asks (Enter, or a country picked; never as they type). `search(query, country)`: a country's ISO 3166 code
  keeps it inside that country (`countrycodes`). `ready(query, found)` gets (name, lat, lon, span_m) best
  first (`span_m`: the larger side of the place's bounding box on the ground, 0 without one), places beyond Web
  Mercator's ±85° left out, or a message (offline, an unreadable answer: whatever goes wrong, it answers).
- **Drawing** (`background.BackgroundPainter`, used by `NetworkView.drawBackground`, behind the whole
  scene, so redrawing the network never touches it): the colour; the image (PNG or JPG; a PDF's page
  rendered 2,400 px wide with QtPdf; decoded once per file, page and change, only the latest kept) through `image_transform` (its pixels scaled to its width, turned
  about its top-left corner, placed), its height from its proportions; or the tiles
  in view at the view's zoom (the device pixel ratio counted), 400 kept decoded. Tiles are drawn
  opaque, a screen pixel overlapping and without edge antialiasing, and the colour is laid back over
  them at 1 − opacity, which is the same blend as drawing them see-through without seams between
  tiles. `drawForeground` credits "© OpenStreetMap contributors" in the map's bottom-right corner
  while it shows, the points a tool has picked (`marks`: a cross and its label, screen-sized), Measure's
  line (`line`) and, over an image, the reference line dashed where the image now lies
  (`reference_line`: "Reference 50.00 m", and ", now 55.00 m" once the image has been resized since),
  and over the map its work zone (`zone`), the map outside it dimmed and its edge dashed (a diamond in the 3D view).
  `extent` lets the view scroll over the image (the bounds of its turned rectangle); over the map, its work zone
  and the junctions (any outside it too), or 5 km round the network for a map without one (older files); while
  the work zone is chosen, only the network's centre (a 1 m square), so the view can't scroll off it.
- **Create tab** (`background.BackgroundPanel`, the body of the Own Network section): Background (Colour, Image, Map; a kind
  chosen starts afresh, only the colour carried over, and leaving an image or the map asks first, Yes discarding it,
  one undo step, No keeping it as it is: `_kind_chosen`); for a
  colour, six swatches (`theme.MAP_COLOURS`: Grass, Meadow, Asphalt, Concrete, Paper, Night; hidden for an image or
  the map, whose veil keeps the last one); for an image, Open (PNG, JPG, PDF:
  laid as wide as the network and centred on it; one that can't be read is refused, nothing changed, and one on a
  network share is refused before it is opened, the file label saying why: `models.image_path_problem`, as saving does),
  the PDF's page, its width, top-left corner and turn; for the map, **Work zone** (its size and the place at its
  centre, "1.20 km × 0.98 km at 51.50720°, -0.12760°", or "Not chosen"; the globe, Change Work Zone, starts it
  again) and the map's turn; opacity for either.
  - **First steps** (the network hidden meanwhile, `NetworkGraphicsScene.set_hidden`; the tabs, the Roads
    and Generate sections and the map's Input Grid toggle off until they end, `MainWindow._background_tool`):
    - **Work zone** (`zone`; `models.Background.zone`, [x, y, width, height] in scene metres, validated: four
      finite numbers, positive sides): choosing Map (`_kind_chosen`, from a colour or an image) or Change Work
      Zone starts it. The section itself is off meanwhile (the bar is the only way in or out), and nothing of the
      setup is kept unless it is confirmed: ending it any other way (`start_tool`: Cancel, Esc, another tool) puts
      the background back, as do undo, redo, opening a project and saving, which end it first (`_restore`,
      `install_network`, `save_file`). The view turns to 2D, its corner controls give way to the zone's bar (`BackgroundPanel.bar`,
      opaque, over the map's top centre: `NetworkView.set_top`), and the scene is pinned to the network's centre,
      so no pan can leave it. A box fixed on screen (`zone_box`: `ZONE_INSET`, a fifth of the view in from each
      side, so it keeps the view's proportions) is drawn over the map, the map outside it dimmed, its size on it
      and a cross at its centre, which is the network's. It starts on the zone the map has, framed by the box, or
      else on the whole world across the view. The bar, in steps: **Country** (QLocale's territories with
      two-letter codes, typed to filter; picked, Nominatim is asked for it within its code and the map flies there,
      `fly`: that point under the box, the map georeferenced there, zoomed so the box's shorter side covers its
      bounding box, `ZONE_DEFAULT_M` 1.2 km to `FLY_MAX_M` 2,000 km); **Place** (a city, a street, an address:
      Enter asks Nominatim within the country chosen, none for an empty or unlisted one; one place is flown to at
      once, several fill the place list to pick from, none says so; a note names the place and credits Nominatim
      and OpenStreetMap; only the latest search's answer is taken, a cached one as at once as a fresh one);
      latitude and longitude, always the point under the box (within ±85° and ±180° however far the map is
      dragged), with Go (Enter or leaving a changed box does the same: flown to, the box covering the ground it
      covered, or 1.2 km when that was more than a zone may be); **Locate Work
      Zone** (checkable): only while it is on does a left drag slide the map under the box (the anchor moves, so
      there is no edge to reach) and the wheel zoom it, from the whole world to 100 px a metre (`zoom_range`; the
      view's `zoom_range` asks the window, `MainWindow._zoom_range`); **Confirm**, only while the box covers 100 m
      to 20 km a side (`ZONE_M`; a note says to zoom in or out), makes what it covers the zone, centred on the
      network, and georeferences the map at its centre (true to scale there): one undo step with everything the
      setup changed, nothing being kept before it. Cancel or Esc puts back the background from before. Before
      Locate Work Zone, Fit (F) and Reset (R) leave the
      zoom alone too. After it the view fits the zone, scrolls over the zone and whatever is drawn outside it
      (`extent`), and zooms out no further than that fills the view (`fit_scale`; Fit Network and Reset View too;
      `NetworkView.clamp_zoom` after a resize, a tilt or a background change); a colour or an image frees it.
    - **Set Scale** (`scale`): opening an image or PDF (`use_image`) starts it: click the start and the
      end of a line whose length is known (a scale bar), and the length is asked at once (`_ask_scale`,
      once the click is over): Set Scale's resize about the first point. Cancelled, click the line again;
      Esc skips it, the image left as wide as the network.
  - **Tools** (while one is on, the map view's left clicks go to it, not the
    network, `NetworkGraphicsScene.background_tool`; a banner says the next step; Esc, the button again
    or leaving the tab ends it; the Edit tab's tools end when one starts): **Move** (the section's
    primary button, full width under the Work zone and Turn rows: "Drag Map to Adjust" or "Drag Image to Adjust",
    "Done Adjusting" while on; a middle or right drag only pans the view) drags the image
    (its corner) or the map (its anchor, so the map slides under the network), one undo step a drag.
    **Two Points** (an image): click a point on the image, then where it belongs, then a second pair;
    `fit_two_points` solves the similarity q = T + a·p (a = s·e^(iθ), complex numbers) that puts both
    image pixels exactly on their points, setting the width, corner and turn at once (one undo step);
    the same image point twice is refused and asked again. The picks show as marked crosses.
    **Lock** (an image or the map) pins it: Move, Two Points and the place, size and turn fields are
    off until it is unlocked (one undo step each way). **Hide Background** (the eye among the map's corner controls; an image or the map)
    draws only the background colour, so the roads drawn over it stand alone (`BackgroundPainter.hidden`); it is
    only the view: not saved with the project, not an edit or an undo step, and the eye again shows it. **Measure** (any background, the network too):
    click two points, A and B; the line follows the mouse after the first click, and the banner gives
    the distance in metres (two decimals); another click starts a new line. Over an image, **Set
    Scale…** then asks the line's true length and resizes the image about A by true / measured (the
    similarity x' = A + k·(x − A), so A stays and the turn is kept), and keeps the line, in image
    pixels, as the reference. It works while locked: that's how a pinned drawing gets its scale. Roads
    are drawn in metres, so once the image is to scale the network drawn over it is too. The map needs
    no Set Scale: it is true to scale already.
  - Each change is one undo step; it doesn't stop a run, nor does undoing it
    (`MainWindow._background_changed`, `_restore`). A new grid keeps the background; over the map, the
    place under the old network's centre goes under the new one's, and the work zone with it (`network_tools.keep_background`, the assistant's generate_grid's too).
- **Position readout**: while the map shows, the status bar gives the mouse's WGS84 latitude and
  longitude (six decimals, `NetworkView.pointed` → `MainWindow._show_pointer`).

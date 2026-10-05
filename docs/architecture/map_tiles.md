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
  one not asked again for a minute. `ready` (from the download thread) asks the view to redraw.
- **Place search** (`map_tiles.PlaceSearch`): OSM's Nominatim (`nominatim.openstreetmap.org/search`, jsonv2, up
  to 5 places), following its Usage Policy: the same identifying User-Agent, one request at a time on its own
  thread and at most one a second (`SEARCH_GAP_S`), each query's answer cached (case aside), and only when the
  user asks (Enter or the search icon; never as they type). `ready(query, found)` gets (name, lat, lon) best
  first, places beyond Web Mercator's ±85° left out, or a message (offline, an unreadable answer).
- **Drawing** (`background.BackgroundPainter`, used by `NetworkView.drawBackground`, behind the whole
  scene, so redrawing the network never touches it): the colour; the image (PNG or JPG; a PDF's page
  rendered 2,400 px wide with QtPdf) through `image_transform` (its pixels scaled to its width, turned
  about its top-left corner, placed), its height from its proportions; or the tiles
  in view at the view's zoom (the device pixel ratio counted), 400 kept decoded. Tiles are drawn
  opaque, a screen pixel overlapping and without edge antialiasing, and the colour is laid back over
  them at 1 − opacity, which is the same blend as drawing them see-through without seams between
  tiles. `drawForeground` credits "© OpenStreetMap contributors" in the map's bottom-right corner
  while it shows, the points a tool has picked (`marks`: a cross and its label, screen-sized), Measure's
  line (`line`) and, over an image, the reference line dashed where the image now lies
  (`reference_line`: "Reference 50.00 m", and ", now 55.00 m" once the image has been resized since).
  `extent` lets the view scroll over the image (the bounds of its turned rectangle), or 5 km of map
  round the network.
- **Create tab** (`background.BackgroundPanel`, the body of the Own Network section): Background (Colour, Image, Map); for a
  colour, six swatches (Grass, Meadow, Asphalt, Concrete, Paper, Night; hidden for an image or the map,
  whose veil keeps the last one); for an image, Open (PNG, JPG, PDF:
  laid as wide as the network and centred on it; one that can't be read is refused, nothing changed),
  the PDF's page, its width, top-left corner and turn; for the map, **Place** (a name, an address: Enter or the
  search icon asks Nominatim through `BackgroundPainter.places`, its answer queued back to the GUI thread
  as `found`; one place goes under the network's centre at once, several are offered in a menu under the
  box, none says so; a note under it names the place and credits Nominatim and OpenStreetMap; it works
  during Choose Location, the place landing under the cross), latitude and longitude with Go
  (Enter or leaving a changed box does the same): that point goes under the network's centre, which
  becomes the anchor, and the view centres there; the boxes always show the point under the network's
  centre, wherever the map has been moved or turned; the map's turn; opacity for either.
  - **First steps** (the network hidden meanwhile, `NetworkGraphicsScene.set_hidden`; the tabs, the Roads
    and Generate sections and the map's Input Grid toggle off until they end, `MainWindow._background_tool`):
    - **Choose Location** (`locate`): choosing Map (`_kind_chosen`, from a colour or an image) or the globe
      icon beside Go starts it. The painter draws the hidden network's outline (`outline`, dashed) and a
      cross at its centre; a left drag slides the map under it (the anchor moves, as Move does, so there is
      no edge to reach, however far), the wheel zooms about the view's centre (the view is centred on the
      network's centre, `AnchorViewCenter`), and the boxes show the place under the cross after each drag;
      entering one goes there. **Use This Location** (the section's primary button meanwhile, Move
      hidden; `use_location`) georeferences that place at the network's centre (`place_map`), so the map
      is true to scale round it, and the view fits the network again. Esc (`cancel_tool`) puts back the
      background from before; choosing a colour or an image ends it too.
    - **Set Scale** (`scale`): opening an image or PDF (`use_image`) starts it: click the start and the
      end of a line whose length is known (a scale bar), and the length is asked at once (`_ask_scale`,
      once the click is over): Set Scale's resize about the first point. Cancelled, click the line again;
      Esc skips it, the image left as wide as the network.
  - **Tools** (while one is on, the map view's left clicks go to it, not the
    network, `NetworkGraphicsScene.background_tool`; a banner says the next step; Esc, the button again
    or leaving the tab ends it; the Edit tab's tools end when one starts): **Move** (the section's
    primary button, full width under the place fields: "Drag Map to Adjust" or "Drag Image to Adjust",
    "Done Adjusting" while on; a middle or right drag only pans the view) drags the image
    (its corner) or the map (its anchor, so the map slides under the network), one undo step a drag.
    **Two Points** (an image): click a point on the image, then where it belongs, then a second pair;
    `fit_two_points` solves the similarity q = T + a·p (a = s·e^(iθ), complex numbers) that puts both
    image pixels exactly on their points, setting the width, corner and turn at once (one undo step);
    the same image point twice is refused and asked again. The picks show as marked crosses.
    **Lock** (an image or the map) pins it: Move, Two Points and the place, size and turn fields are
    off until it is unlocked (one undo step each way). **Measure** (any background, the network too):
    click two points, A and B; the line follows the mouse after the first click, and the banner gives
    the distance in metres (two decimals); another click starts a new line. Over an image, **Set
    Scale…** then asks the line's true length and resizes the image about A by true / measured (the
    similarity x' = A + k·(x − A), so A stays and the turn is kept), and keeps the line, in image
    pixels, as the reference. It works while locked: that's how a pinned drawing gets its scale. Roads
    are drawn in metres, so once the image is to scale the network drawn over it is too. The map needs
    no Set Scale: it is true to scale already.
  - Each change is one undo step; it doesn't stop a run, nor does undoing it
    (`MainWindow._background_changed`, `_restore`). A new grid keeps the background; over the map, the
    place under the old network's centre goes under the new one's (`MainWindow.generate`).
- **Position readout**: while the map shows, the status bar gives the mouse's WGS84 latitude and
  longitude (six decimals, `NetworkView.pointed` → `MainWindow._show_pointer`).

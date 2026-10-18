# network_scene.py: drawing and map interaction (Qt)

`NetworkGraphicsScene.rebuild()` redraws the whole network from the model; every item is in metres.
- **Roads:** a path the carriageway's width wide.
  - Lane lines: dashed 3 m / 9 m where lane changing is allowed, solid where not.
  - Two-way roads get a yellow centre divider; one-way roads have none and show an ahead
    arrow in every lane.
  - Stop lines cross only the arriving lanes (and a turn pocket's); roundabout entries get give-way dashes.
  - Turn pockets (`_draw_pocket`, `Road.pockets`): the carriageway widened on that traffic's kerb side,
    from nothing over the 20 m taper to the pocket's lanes along the pocket, on into the junction; a dashed
    lane line beside each lane it adds; a turn arrow leaning to the kerb or the offside in each pocket lane,
    6 m before the stop line. Laid out as the simulation lays out the lanes (an offside pocket's through
    lanes move over to the kerb side), so vehicles drive on what is drawn. SCATS's stop-line loops cover
    the pocket's lanes too, where the lanes are there.
  - Overpasses draw above the roads below them (`OVERPASS_LIFT`) with parapets over each crossing.
- **Nodes:**
  - Signalised and roundabout junctions get a box or the true-size roundabout.
  - Uncontrolled nodes draw as plain carriageway (a joint disc).
  - Signal heads sit on the near-side kerb at each arriving stop line (`signal_lamps`). While a run
    goes they show its live states (`show_signals`: green, amber, red); otherwise east-west green.
  - An adaptive signal's loop detectors are outlined on each arriving lane where the simulation reads
    them (`_draw_detectors`): SCATS's over the last 4.5 m before the stop line, SCOOT's 2 m long, 15 m
    after the road's upstream end (a third of the lane on short roads).
  - Labels are screen-sized (`ItemIgnoresTransformations`) and sit off the road.
- **Ends:** triangles on their own lanes; yellow = entry (supply), white = exit (demand).
  A one-way end has only one of them.
- **Bus stops** (`_draw_bus_stop`): a bay beside the kerb of each direction served, shaped by
  `bay_depth` (the longer taper upstream), a broken yellow line where it meets the lane and a yellow
  flag where buses stop. Selected, dimension lines run from each end node to its middle, labelled
  "250.0 m from 1,1". Pressing on a stop selects it, and dragging slides it along its road
  (`nearest_along` of the mouse, keeping the grab offset; one undo step on release). With a stop
  selected, the view's arrow keys call `nudge_bus_stop`: 0.5 m (Shift: 5 m) along the road the way
  it runs nearest the arrow's screen direction; an arrow across the road does nothing (it scrolls).
- **Institutions** (`_draw_institution`): a square roof at true size, its kind's icon on it and
  "IN1 School" above; selected, its circle of influence (dashed, its radius in metres at its edge) and a
  ring round each junction and end it reaches (`drawForeground`, `_paint_zone`).
- **Hidden** (`set_hidden`): while the background's place or scale is set first, every item is drawn and then
  hidden, so Fit still knows where the network is.
- **Placing an institution** (`placing`, an `institutions.Placement`; `set_placing`): while it is set, the
  map's mouse moves and left clicks go to it before anything else: the footprint follows the mouse,
  green where it can stand, red and cross-hatched where it can't (SIGNAL_GREEN, SIGNAL_RED); the first
  click sets it down, then `NetworkView.wheelEvent` hands the wheel to `grow_placing` (the view doesn't
  zoom) and the circle grows or shrinks; the second click emits `placed`. `placement_changed` after each
  step. Right or middle drag still pans.
- **Metro** (`_draw_metro`, `METRO_Z` 7.5: above roads and signals, below vehicles): each line's track straight stop to
  stop (`METRO_LINE`, 4 px), each stop a ringed marker (`METRO_STOP`, `METRO_STOP_RADIUS_M` 12 m) labelled with its ID,
  and a deployment's pending first stop. While `metro_deploying`, a left click emits `metro_clicked(x, y)` before
  anything else. `NetworkView.right_clicked`: a right press and release that panned under `RIGHT_CLICK_PX` (4 px), so
  a right drag still pans.
- **Clicks:** clickable items carry `data(0) = (kind, id)`. The `clicked(kind, id, x, y,
  additive)` signal reports the object (`road`, `node`, `entry`, `exit`, `bus_stop`, `institution` or `""`) and the
  point in metres; Ctrl/Shift makes `additive` true.
- **Selection box** (while `box_select`, i.e. no tool is on): a press on empty map that moves 4 px
  draws a rubber band; released, `boxed(left, top, right, bottom, crossing, additive)` reports it in
  metres. Dragged left to right it is a window (solid accent outline), right to left a crossing box
  (dashed green), as in AutoCAD. A press that doesn't move is a click on empty map, sent on release.
  `set_group(objects)` highlights the selection: roads, junctions, ends and bays outlined together.
- **Drag mode:** `drag_nodes` set → mouse drags call `network.move_nodes` live, and
  `drag_finished` ends one undo step.
- **Vehicles:** one `VehicleLayer` item draws every vehicle of the run. On the OpenGL viewport the
  scene's `gpu.VehicleRenderer` (kept across rebuilds) draws one instanced batch per present style; elsewhere
  (the raster viewport, tests, or if the shader can't be built) QPainter draws them in one pass grouped by
  colour. Each is drawn part way (`frame_alpha`) between its last two simulated poses, so motion is
  smooth at any frame rate; zoomed far out they stay visible (QPainter: dots under 3 px; the shader: at
  least 2 px each side in top-down). `show_frame(alpha, frame)` draws a frame.
- **Vehicle models:** `vehicle_assets` selects appearance and stable random paint; `asset_mesh` supplies actual
  low-poly geometry shared by raster/GPU: tapered car bodies, raised sloped cabins, separate wheels and open
  bike frames/riders. Mesh coordinates expand inside the frame's physical L/W, and motion remains continuous.
  Below five projected pixels of width, details give way to cached low-poly silhouettes. See `asset_mesh.md`.
- **Buildings:** `BuildingItem` retains the square footprint in 2D, and paints metre-height facades and roofs
  from `building_assets` in 2.5D. `height_m` includes the roof; None uses a labelled assumed type default.
  The footprint/catchment/demand never scales with height. Roof labels move with the projection; scenery bounds
  reserve extrusion space so toggles need no network rebuild. Buildings remain below traffic and analytical
  overlays in the Qt scene; they do not share the vehicle GPU depth buffer.
- **Building visibility:** `set_buildings_visible(on)` hides/shows placed institutions' footprints, icons and
  name labels, and gates the selected catchment. Hidden buildings receive no map clicks. It invalidates the
  still-layer background cache even during a run, and emits `buildings_changed` for the corner control.
  Network records, demand, selection, frame/alpha, camera and scene/Fit bounds are unchanged. Rebuilds and
  whole-network hiding preserve the setting. Default visible; session-only. Active placement previews remain
  visible so building placement stays usable while existing buildings are hidden.
- **Still layer, cached during a run:** what `rebuild()` draws under the vehicles (z below `VEHICLE_Z`,
  but the signal lamps) is tagged `STILL`. While a run is on (`show_frame` with a frame;
  `cache_still`) those items get `ItemHasNoContents`: they still take clicks and tooltips but don't paint
  themselves; `paint_still` draws them in their stacking order from the background (the scene's
  `drawBackground`, or the view's over its map), and the view keeps that in Qt's `CacheBackground`
  pixmap. A display frame then draws only the cached image, the lamps, the vehicles, labels, markers and
  extensions' items. The cache is drawn again when the view zooms or resizes, for the strip a pan
  exposes, after any `rebuild()` (an edit or selection mid-run invalidates the background layer) and
  when the background changes (`NetworkView.redraw`: a map tile arrived, it moved). Extensions' items
  never join it, so they draw over the whole still layer during a run. Edit mode paints every item as
  before. Measured (8 × 8 grid, 1,713 items, RTX 3070, 1600 × 900): 4.7 ms a display frame before, 1.5
  ms cached.

`NetworkView`:
- **3D View:** an optional orthographic 2.5D camera, off at startup: 45 degree yaw, 35.2643897 degree
  elevation, zero roll. `projection_transform` maps world `(x, y)` metres to
  `(cos(yaw)*x - sin(yaw)*y, sin(elevation)*(sin(yaw)*x + cos(yaw)*y))`, followed by uniform zoom and pan.
  The matrix is affine: parallels stay parallel, scale does not depend on distance. `projection_lift` maps
  display-only body height straight screen-up. The road network, lane widths, frame poses, speed and collision
  dimensions are never projected back into the simulation. Equal world speed naturally looks slower along
  foreshortened directions; there is no motion compensation or direction snapping.
- `set_isometric(on)` changes only the view and vehicle skin, using the same frame and interpolation alpha.
  It preserves the world point at the viewport centre, orthographic scale (including fractional pixel offsets),
  selection, tools, clock and running Runner. No new timers, simulation steps or network rebuilds are needed.
  It emits `projection_changed` to keep the map's checkable 3D View control in step. This is a session display
  choice, not part of the project/network JSON.
- **Vehicle skin:** isometric bodies use the frame's exact length and width, at every depth and zoom. There
  is no minimum body size or dot substitution in this view. The raster and instanced GPU paths shade two
  visible mesh faces with fixed matte lighting. Car cabins, cycles and commercial bodies have distinct height
  profiles; these are display geometry, not new vehicle collision dimensions. Only old frames without class
  metadata retain the plain `0.7 * width` cuboid. Roads stay planar; buildings have explicit visual heights.
  Vehicle GPU meshes use depth testing; Qt scenery retains its existing scene layers.
  Legacy `scene.render` exports remain top-down. World-aligned selection boxes appear as ground diamonds;
  crossing/window follows screen drag direction, and their emitted bounds stay in world metres. Labels remain
  upright through `ItemIgnoresTransformations`. Bus-stop arrow keys are inverse-projected before nudging.
- **GPU:** `renderer` is the OpenGL renderer the driver offers (`gpu.renderer()`, asked through a hidden
  offscreen surface) and `on_gpu` whether it is a real GPU. Only then is the viewport a `QOpenGLWidget`
  with 4× multisampling (full-viewport updates, as OpenGL views are meant to repaint), the default version
  (Windows drivers give their newest; the vehicles' shader needs 3.3). A CPU stand-in (`gpu.software`:
  llvmpipe, GDI Generic, Basic Render Driver, SwiftShader) or no OpenGL keeps the raster viewport, which is
  faster than OpenGL emulated on the CPU. Offscreen (tests) it stays raster without asking.
- `set_power_saver(on)` (the Power Saver button): on a GPU, a new `QOpenGLWidget` viewport without
  multisampling (or with 4× again); QPainter's antialiasing still smooths edges. The vehicles' shader is
  rebuilt in the new context. Raster viewports have no multisampling to drop.
- Wheel changes orthographic scale at the cursor; right or middle drag pans. Background tiles choose detail
  from that scale rather than one rotated matrix coefficient; images and maps share the ground transform.
- F fits the projected network bounds with a 90 px screen margin; R restores unit zoom and recentres,
  retaining the active top-down/isometric orientation.
- `set_corner(widget)` keeps the map's controls over its top-right corner (8 px in, re-placed on resize).
- Tool cursors show the active tool.

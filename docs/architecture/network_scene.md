# network_scene.py: drawing and map interaction (Qt)

`NetworkGraphicsScene.rebuild()` redraws the whole network from the model; every item is in metres.
- **Roads:** a path the carriageway's width wide.
  - Lane lines: dashed 3 m / 9 m where lane changing is allowed, solid where not.
  - Two-way roads get a yellow centre divider; one-way roads have none and show an ahead
    arrow in every lane.
  - Stop lines cross only the arriving lanes; roundabout entries get give-way dashes.
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
  scene's `gpu.VehicleRenderer` (kept across rebuilds) draws them all in one instanced call; elsewhere
  (the raster viewport, tests, or if the shader can't be built) QPainter draws them in one pass grouped by
  colour. Each is drawn part way (`frame_alpha`) between its last two simulated poses, so motion is
  smooth at any frame rate; zoomed far out they stay visible (QPainter: dots under 3 px; the shader: at
  least 2 px each side). `show_frame(alpha, signals)` draws a frame.

`NetworkView`:
- **GPU:** `renderer` is the OpenGL renderer the driver offers (`gpu.renderer()`, asked through a hidden
  offscreen surface) and `on_gpu` whether it is a real GPU. Only then is the viewport a `QOpenGLWidget`
  with 4× multisampling (full-viewport updates, as OpenGL views are meant to repaint), the default version
  (Windows drivers give their newest; the vehicles' shader needs 3.3). A CPU stand-in (`gpu.software`:
  llvmpipe, GDI Generic, Basic Render Driver, SwiftShader) or no OpenGL keeps the raster viewport, which is
  faster than OpenGL emulated on the CPU. Offscreen (tests) it stays raster without asking.
- Wheel zooms at the cursor; right or middle drag pans.
- F fits the network with a 90 px screen margin; R resets the view.
- `set_corner(widget)` keeps the map's controls over its top-right corner (8 px in, re-placed on resize).
- Tool cursors show the active tool.

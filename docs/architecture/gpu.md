# gpu.py: the map on the GPU (Qt)

- `renderer()` reads GL_RENDERER from a hidden offscreen surface; `software(name)` identifies CPU stand-ins.
  Only a real GPU receives a QOpenGLWidget. Otherwise the map uses QPainter raster drawing and says why.
- `VehicleRenderer.draw(painter, frame, alpha, outline, height_lift=None)` builds a GLSL 3.30 shader, a static
  high/low mesh atlas from `asset_mesh`, a pose stream and a private VAO in the current viewport context.
  False falls back to raster when the paint engine/context/version is unsuitable or shader build fails.
- `_upload` runs once per distinct published frame: 14 floats per vehicle (previous/current pose, half L/W,
  resolved display colour and style). It packs vehicles by style and stores one group range per appearance.
  Each display frame makes one instanced draw per present style, at most nine. No Python vehicle loop runs at
  display rate. `uploads` counts frame uploads; `draws` counts actual instanced calls; `frames` the display frames drawn.
- People: `draw(..., people)` takes some of the frame's walkers (2.5D, 3 pixels tall or more, clear of every
  overbridge: `VehicleLayer._people`, worked out once a frame) as one more group, style `PEOPLE` (9): the
  pedestrian mesh is in the atlas in metres (half sizes 1, 1; its z halved there, since the shader doubles it),
  body paint `theme.WALKER`. They share the vehicles' depth buffer. Walkers within reach of an overbridge stay
  QPainter's (drawn raised on the deck, after the bridge), as do the top-down markers and people under 3 pixels.
- Position and continuous heading interpolate in the vertex shader, using the same snapshot and alpha as
  raster. Mesh vertices expand to metres; tyre radius uses width units. The painter's full affine matrix
  supplies orthographic projection with homogeneous w=1; height_lift maps real display height screen-up.
  Isometric minimum half size is zero; top-down retains two-pixel minimum markers. Below five projected
  pixels of group width the shader draws the cached low-detail model with no new pose upload.
- Material normals produce fixed matte lighting. GL_DEPTH_TEST/LEQUAL uses camera-depth coordinates relative
  to the visible world bounds, so opaque vehicle parts and instances occlude correctly. The vehicle depth
  layer is cleared per draw and depth testing disabled before returning to QPainter. A 2 mm render-depth bias
  for coplanar glazing and markings prevents z-fighting without moving any footprint or simulation coordinate.
  Qt buildings, roads and overlays retain their scene layers; they do not share this vehicle depth buffer.
- The limit was the interpreter, not the GPU: a physics thread and this (the UI) thread share Python's lock, and
  every call from Python into Qt gives it up and waits for it back. A live run's physics is therefore in a
  process of its own (physics_process.py; on a thread only when an extension decides in the run), and a display
  frame is kept to few Python calls: vehicles and people are instanced here, a signal clock replays one compiled
  drawing (`scene_items._clock_face`) and is skipped under 3 pixels, and the run clock's tooltip and colour are
  set only when they change. Measured, 2,200 vehicles at 10×: 60 frames a second and the physics at 10.0× (on a
  thread: 8.6 and 5.8×; before these changes 3.9); 200 vehicles at 60×: 62 frames a second, the physics at 59.8×.
- The material inks are set once at build; a display frame sets its eight uniforms by locations kept from then
  (`_at`), not by name.
- `main.main` requests desktop OpenGL only. Power Saver recreates the viewport without multisampling;
  context loss rebuilds the shader/atlas and uploads the next frame. Theme tokens supply every material colour.
- Hardware checks: `test_gpu.py` (the whole file: its children draw on the real driver into a hidden
  framebuffer; `--child vehicles`, `people`, `speed` or `window` runs one), and with QT_QPA_PLATFORM=windows
  `tests/test_vehicle_assets.py --gpu` and `test_projection.py --gpu`.

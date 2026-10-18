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
- `main.main` requests desktop OpenGL only. Power Saver recreates the viewport without multisampling;
  context loss rebuilds the shader/atlas and uploads the next frame. Theme tokens supply every material colour.
- Hardware checks: `tests/test_vehicle_assets.py --gpu`, `test_gpu.py --child vehicles` / `--child speed`,
  and `test_projection.py --gpu`, with QT_QPA_PLATFORM=windows. Full test_gpu also contains a separately
  documented pre-existing hidden-window cache/readback failure; framebuffer checks are run independently.

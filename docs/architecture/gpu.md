# gpu.py: the map on the GPU (Qt)

- `renderer()`: GL_RENDERER from a context on a hidden offscreen surface ("NVIDIA GeForce RTX 3070/PCIe/SSE2"),
  "" when none can be made; `software(name)`: a CPU stand-in or a GPU.
- `VehicleRenderer.draw(painter, frame, alpha, outline)`: inside the viewport's own painting
  (`beginNativePainting`), in its OpenGL context: built there the first time (and again if the context is
  replaced): a GLSL 3.30 program, a unit quad and a stream buffer of poses, set up in its own vertex array
  object (per-vehicle attributes with divisor 1) so QPainter's state is untouched. A new frame's poses go
  to the GPU once (`_upload`: 13 floats a vehicle, the pose a step ago and now, half length and width,
  colour); every display frame then sets three uniforms (scene to clip space from the painter's world
  transform, alpha, the minimum half size) and makes one `glDrawArraysInstanced` call: the vertex shader
  interpolates position and heading, turns the quad and never draws a vehicle under 2 px each side; the
  fragment shader adds the 0.1 m outline. No Python work per vehicle per display frame, so the physics
  thread keeps its share of the interpreter. False (the caller draws with QPainter) for a raster painter,
  OpenGL below 3.3 or ES, or a shader that won't build (`failed` says why). `uploads` and `draws` count.
- `main.main` sets `AA_UseDesktopOpenGL`: the graphics driver's OpenGL only, so Qt never falls back to the
  software OpenGL it ships (opengl32sw.dll). Without a GPU the status bar says the map draws on the CPU and
  why (Remote Desktop, an old driver); Map Controls' click names what draws it.

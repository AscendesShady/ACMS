"""The map's drawing on the GPU (Qt OpenGL).

`renderer()` asks the OpenGL driver what it would draw with, and `software(name)` says whether that
is a CPU stand-in (Mesa's llvmpipe, which Qt ships as opengl32sw.dll, Windows' GDI Generic or Basic
Render Driver over Remote Desktop, SwiftShader). The map view takes an OpenGL viewport only on a
real GPU; otherwise it draws with Qt's raster engine and says so, never with a software OpenGL
(network_scene.NetworkView, main.main).

`VehicleRenderer` draws all of a run's vehicles in one instanced call: each published frame's poses
go to the GPU once (13 floats a vehicle), and the vertex shader places, turns and interpolates every
vehicle between its last two simulated poses, so a display frame does no Python work per vehicle
and leaves the physics thread its share of the interpreter.
"""

from __future__ import annotations

from array import array
from math import hypot

from PySide6.QtGui import QColor, QMatrix4x4, QOffscreenSurface, QOpenGLContext, QPaintEngine, QPainter, QVector3D
from PySide6.QtOpenGL import QOpenGLBuffer, QOpenGLShader, QOpenGLShaderProgram, QOpenGLVertexArrayObject

GL_FLOAT, GL_TRIANGLE_STRIP, GL_RENDERER = 0x1406, 0x0005, 0x1F01
SOFTWARE = ("llvmpipe", "softpipe", "swiftshader", "gdi generic", "basic render", "software", "mesa offscreen")
FLOATS = 13  # per vehicle: its pose a step ago (x, y, ux, uy), its pose now, half length and width, colour
MIN_PIXELS = 2.0  # zoomed far out, a vehicle still covers this many pixels each side of its centre

VERTEX = """#version 330
layout(location = 0) in vec2 corner;   // the unit quad, -1..1
layout(location = 1) in vec4 before;   // x, y, ux, uy a step ago
layout(location = 2) in vec4 now;
layout(location = 3) in vec2 half_size;  // half length, half width (m)
layout(location = 4) in vec3 colour;
uniform mat4 scene_to_clip;
uniform float alpha;     // how far from `before` to `now` to draw
uniform float min_half;  // metres: never smaller than this
out vec3 v_colour;
out vec2 v_local;
out vec2 v_half;
out float v_rim;
void main() {
    vec2 centre = mix(before.xy, now.xy, alpha);
    vec2 u = mix(before.zw, now.zw, alpha);
    float n = length(u);
    u = n > 0.0 ? u / n : vec2(1.0, 0.0);
    vec2 h = max(half_size, vec2(min_half));
    vec2 local = corner * h;
    vec2 world = centre + u * local.x + vec2(-u.y, u.x) * local.y;  // along, then across (as the QPainter path)
    gl_Position = scene_to_clip * vec4(world, 0.0, 1.0);
    v_colour = colour;
    v_local = local;
    v_half = h;
    v_rim = half_size.y > min_half ? 1.0 : 0.0;  // an outline only while it is drawn at its own size
}
"""
FRAGMENT = """#version 330
in vec3 v_colour;
in vec2 v_local;
in vec2 v_half;
in float v_rim;
uniform vec3 outline;
out vec4 fragment;
void main() {
    vec2 edge = v_half - abs(v_local);
    fragment = vec4(v_rim > 0.5 && min(edge.x, edge.y) < 0.1 ? outline : v_colour, 1.0);  // a 0.1 m outline
}
"""


def software(name: str) -> bool:
    """Is this OpenGL renderer a CPU stand-in rather than a GPU?"""
    return any(word in name.lower() for word in SOFTWARE)


def renderer() -> str:
    """The OpenGL renderer this display would draw with ("NVIDIA GeForce RTX 3070/PCIe/SSE2"), or ""
    when no OpenGL context can be made. Asked through a hidden offscreen surface: nothing shows."""
    context = QOpenGLContext()
    if not context.create():
        return ""
    surface = QOffscreenSurface()
    surface.setFormat(context.format())
    surface.create()
    try:
        if not context.makeCurrent(surface):
            return ""
        name = context.functions().glGetString(GL_RENDERER) or ""
        context.doneCurrent()
        return str(name)
    finally:
        surface.destroy()


class VehicleRenderer:
    """One per map: the vehicles' shader, their quad and a buffer of the latest frame's poses, made in
    the viewport's context the first time it draws (and again if that context is replaced)."""

    def __init__(self) -> None:
        self._context: QOpenGLContext | None = None
        self._frame = None  # the frame whose poses are on the GPU
        self._count = 0
        self._colours: dict[str, tuple[float, float, float]] = {}
        self.failed = ""  # why the GPU can't draw them (then the QPainter path does)
        self.uploads = 0  # frames sent to the GPU
        self.draws = 0  # instanced draw calls made

    def draw(self, painter: QPainter, frame, alpha: float, outline: QColor) -> bool:
        """Draw the frame's vehicles through the painter's OpenGL context; False when it can't (not an
        OpenGL painter, OpenGL below 3.3, a shader that won't build), so the caller draws them itself."""
        if self.failed or painter.paintEngine().type() != QPaintEngine.Type.OpenGL2:
            return False
        context = QOpenGLContext.currentContext()
        if context is None:
            return False
        painter.beginNativePainting()
        try:
            if context is not self._context and not self._build(context):
                return False
            if frame is not self._frame:
                self._upload(frame)
            self._draw(painter, alpha, outline)
            return True
        finally:
            painter.endNativePainting()

    def _build(self, context: QOpenGLContext) -> bool:
        version = (context.format().majorVersion(), context.format().minorVersion())
        if context.isOpenGLES() or version < (3, 3):
            self.failed = f"OpenGL {version[0]}.{version[1]}: instanced drawing needs 3.3"
            return False
        program = QOpenGLShaderProgram()
        if not (program.addShaderFromSourceCode(QOpenGLShader.ShaderTypeBit.Vertex, VERTEX)
                and program.addShaderFromSourceCode(QOpenGLShader.ShaderTypeBit.Fragment, FRAGMENT) and program.link()):
            self.failed = f"the vehicle shader didn't build: {program.log().strip()}"
            return False
        extra = context.extraFunctions()
        vao = QOpenGLVertexArrayObject()
        vao.create()
        vao.bind()
        program.bind()
        corners = QOpenGLBuffer(QOpenGLBuffer.Type.VertexBuffer)
        corners.create()
        corners.bind()
        quad = array("f", (-1, -1, 1, -1, -1, 1, 1, 1)).tobytes()
        corners.allocate(quad, len(quad))
        program.enableAttributeArray(0)
        program.setAttributeBuffer(0, GL_FLOAT, 0, 2, 8)
        poses = QOpenGLBuffer(QOpenGLBuffer.Type.VertexBuffer)
        poses.setUsagePattern(QOpenGLBuffer.UsagePattern.StreamDraw)  # a new frame every half simulated second
        poses.create()
        poses.bind()
        poses.allocate(4 * FLOATS)
        for location, first, size in ((1, 0, 4), (2, 4, 4), (3, 8, 2), (4, 10, 3)):
            program.enableAttributeArray(location)
            program.setAttributeBuffer(location, GL_FLOAT, 4 * first, size, 4 * FLOATS)
            extra.glVertexAttribDivisor(location, 1)  # one value per vehicle, not per corner
        vao.release()  # the attribute set-up stays in our array object; QPainter's own is untouched
        program.release()
        poses.release()
        self._context, self._program, self._vao, self._corners, self._poses, self._extra = \
            context, program, vao, corners, poses, extra
        self._frame, self._count = None, 0
        context.aboutToBeDestroyed.connect(self._lost)
        return True

    def _lost(self) -> None:
        self._context, self._frame = None, None

    def _upload(self, frame) -> None:
        """The frame's poses, packed once, then drawn at every alpha until the next frame."""
        data = array("f")
        for vehicle in frame.vehicles:
            rgb = self._colours.get(vehicle.colour)
            if rgb is None:
                rgb = self._colours[vehicle.colour] = QColor(vehicle.colour).getRgbF()[:3]
            data.extend(vehicle.prev)
            data.extend((vehicle.x, vehicle.y, vehicle.ux, vehicle.uy, vehicle.length_m / 2, vehicle.width_m / 2))
            data.extend(rgb)
        raw = data.tobytes()
        self._poses.bind()
        self._poses.allocate(raw, len(raw))
        self._poses.release()
        self._frame, self._count = frame, len(frame.vehicles)
        self.uploads += 1

    def _draw(self, painter: QPainter, alpha: float, outline: QColor) -> None:
        if not self._count:
            return
        device, t = painter.device(), painter.worldTransform()
        width, height, ratio = device.width(), device.height(), device.devicePixelRatioF()
        # Scene metres to the painter's pixels (its world transform), then to clip space (y up).
        matrix = QMatrix4x4(2 * t.m11() / width, 2 * t.m21() / width, 0, 2 * t.dx() / width - 1,
                            -2 * t.m12() / height, -2 * t.m22() / height, 0, 1 - 2 * t.dy() / height,
                            0, 0, 1, 0,
                            0, 0, 0, 1)
        functions = self._context.functions()
        functions.glViewport(0, 0, round(width * ratio), round(height * ratio))
        program = self._program
        program.bind()
        program.setUniformValue("scene_to_clip", matrix)
        program.setUniformValue1f("alpha", float(alpha))
        program.setUniformValue1f("min_half", MIN_PIXELS / max(hypot(t.m11(), t.m12()), 1e-9))
        program.setUniformValue("outline", QVector3D(*outline.getRgbF()[:3]))
        self._vao.bind()
        self._extra.glDrawArraysInstanced(GL_TRIANGLE_STRIP, 0, 4, self._count)
        self._vao.release()
        program.release()
        self.draws += 1

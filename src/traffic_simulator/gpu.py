"""The map's drawing on the GPU (Qt OpenGL).

`renderer()` asks the OpenGL driver what it would draw with, and `software(name)` says whether that
is a CPU stand-in (Mesa's llvmpipe, which Qt ships as opengl32sw.dll, Windows' GDI Generic or Basic
Render Driver over Remote Desktop, SwiftShader). The map view takes an OpenGL viewport only on a
real GPU; otherwise it draws with Qt's raster engine and says so, never with a software OpenGL
(network_scene.NetworkView, main.main).

`VehicleRenderer` draws a run's vehicles in one instanced batch per appearance: each published frame's poses
go to the GPU once (14 floats a vehicle), and the vertex shader places, turns and interpolates every
vehicle between its last two simulated poses, so a display frame does no Python work per vehicle
and leaves the physics thread its share of the interpreter.
"""

from __future__ import annotations

from array import array
from math import hypot

from PySide6.QtCore import QPointF, QRectF
from PySide6.QtGui import (QColor, QMatrix4x4, QOffscreenSurface, QOpenGLContext, QPaintEngine, QPainter,
                          QVector2D, QVector3D)
from PySide6.QtOpenGL import QOpenGLBuffer, QOpenGLShader, QOpenGLShaderProgram, QOpenGLVertexArrayObject

from . import asset_mesh, vehicle_assets
from .theme import VEHICLE_INKS, VEHICLE_PAINTS

GL_FLOAT, GL_TRIANGLE_STRIP, GL_TRIANGLES, GL_RENDERER = 0x1406, 0x0005, 0x0004, 0x1F01
SOFTWARE = ("llvmpipe", "softpipe", "swiftshader", "gdi generic", "basic render", "software", "mesa offscreen")
FLOATS = 14  # before/current poses, half length/width, display colour and skin
MIN_PIXELS = 2.0  # zoomed far out, a vehicle still covers this many pixels each side of its centre
BODY_HEIGHT_RATIO = 0.7  # display skin height / actual width; not a collision dimension or a simulation input

VERTEX = """#version 330
layout(location=0) in vec4 corner;
layout(location=6) in vec4 edge_a;
layout(location=7) in vec4 edge_b;
layout(location=8) in float ink;
layout(location=1) in vec4 before;
layout(location=2) in vec4 now;
layout(location=3) in vec2 half_size;
layout(location=4) in vec3 colour;
layout(location=5) in float skin;
uniform mat4 scene_to_clip;
uniform float alpha;
uniform float min_half;
uniform vec2 height_lift;
uniform vec2 near_axis;
uniform vec2 depth_origin;
uniform float depth_span;
uniform vec3 asset_ink[6];
out vec3 v_colour;
out vec2 v_local;
out vec2 v_half;
out float v_visible;
flat out int v_skin;
vec3 expand_vertex(vec4 p, vec2 h) {
    float axle = skin>5.5 && skin<6.5 ? 0.55 : skin>6.5 && skin<7.5 ? 0.61 : skin>0.5 && skin<3.5 ? 0.63 : 0.68;
    float radius = skin>5.5 && skin<6.5 ? 0.48 : skin>6.5 && skin<7.5 ? 0.33 : 0.18;
    float fit = min(1.0,half_size.x*(1.0-axle)/(radius*2.0*half_size.y));
    return vec3(p.x*h.x + p.w*2.0*half_size.y*fit, p.y*h.y, p.z*2.0*half_size.y);
}
void main() {
    vec2 centre = mix(before.xy, now.xy, alpha);
    vec2 u = mix(before.zw, now.zw, alpha);
    u = length(u)>0.0 ? normalize(u) : vec2(1.0,0.0);
    vec2 h = max(half_size,vec2(min_half));
    vec3 p = expand_vertex(corner,h);
    vec3 n = normalize(cross(expand_vertex(edge_a,h),expand_vertex(edge_b,h)));
    n.xy = u*n.x + vec2(-u.y,u.x)*n.y;
    vec2 base = centre + u*p.x + vec2(-u.y,u.x)*p.y;
    vec2 world = base + height_lift*p.z;
    vec3 camera = normalize(vec3(near_axis,1.0));
    v_visible = dot(n,camera)>0.000001 ? 1.0 : 0.0;
    gl_Position = scene_to_clip*vec4(world,0.0,1.0);
    gl_Position.z = -dot(vec3(base-depth_origin,p.z),camera)/depth_span;
    // A two-millimetre depth bias keeps coplanar glazing/paint from z-fighting; footprints never move.
    if(ink==0.0 || ink==2.0 || ink==3.0 || ink==4.0) gl_Position.z-=0.002/depth_span;
    float shade = skin<0.5 && n.z>0.99 ? 1.0 : 0.68+0.32*max(0.0,dot(n,normalize(vec3(-0.6,-0.8,1.0))));
    v_colour = (ink<0.0 ? colour : asset_ink[int(ink)])*shade;
    v_local = p.xy;
    v_half = h;
    v_skin = skin<0.5 && n.z>0.99 && half_size.y>min_half ? 0 : 1;
}
"""
FRAGMENT = """#version 330
in vec3 v_colour;
in vec2 v_local;
in vec2 v_half;
in float v_visible;
flat in int v_skin;
uniform vec3 outline;
out vec4 fragment;
void main() {
    if(v_visible<0.5) discard;
    vec2 edge=v_half-abs(v_local);
    fragment=vec4(v_skin==0 && min(edge.x,edge.y)<0.1 ? outline : v_colour,1.0);
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
        self.draws = 0  # instanced draw calls made (one per appearance)
        self.frames = 0  # display frames drawn

    def draw(self, painter: QPainter, frame, alpha: float, outline: QColor, height_lift: QPointF | None = None) -> bool:
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
            self._draw(painter, alpha, outline, height_lift)
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
        # Static atlas: one shared low-poly model per appearance, expanded to metres in the shader.
        vertices = []
        self._meshes = {}
        for skin in range(9):
            for detail in (False,True):
                first = len(vertices)
                for face in (asset_mesh.vehicle_mesh(skin) if detail else asset_mesh.vehicle_low_mesh(skin)):
                    for i in range(1,len(face.vertices)-1):
                        tri=(face.vertices[0],face.vertices[i],face.vertices[i+1])
                        a=tuple(tri[1][j]-tri[0][j] for j in range(4))
                        b=tuple(tri[2][j]-tri[0][j] for j in range(4))
                        vertices.extend((*point,*a,*b,float(face.ink)) for point in tri)
                self._meshes[skin,detail]=(first,len(vertices)-first)
        mesh = array("f", (value for v in vertices for value in v)).tobytes()
        corners.allocate(mesh,len(mesh))
        for location,first,size in ((0,0,4),(6,4,4),(7,8,4),(8,12,1)):
            program.enableAttributeArray(location)
            program.setAttributeBuffer(location,GL_FLOAT,first*4,size,13*4)
        poses = QOpenGLBuffer(QOpenGLBuffer.Type.VertexBuffer)
        poses.setUsagePattern(QOpenGLBuffer.UsagePattern.StreamDraw)  # a new frame every half simulated second
        poses.create()
        poses.bind()
        poses.allocate(4 * FLOATS)
        for location, first, size in ((1, 0, 4), (2, 4, 4), (3, 8, 2), (4, 10, 3), (5, 13, 1)):
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
        groups = {}
        for vehicle in frame.vehicles:
            groups.setdefault(vehicle_assets.style(vehicle),[]).append(vehicle)
        self._groups = []
        for skin,vehicles in sorted(groups.items()):
            first = len(data)//FLOATS
            for vehicle in vehicles:
                colour = vehicle_assets.paint_colour(vehicle,VEHICLE_PAINTS)
                rgb = self._colours.get(colour)
                if rgb is None:
                    rgb = self._colours[colour] = QColor(colour).getRgbF()[:3]
                data.extend(vehicle.prev)
                data.extend((vehicle.x,vehicle.y,vehicle.ux,vehicle.uy,vehicle.length_m/2,vehicle.width_m/2))
                data.extend(rgb)
                data.append(float(skin))
            self._groups.append((skin,first,len(vehicles),max(v.width_m for v in vehicles)))
        raw = data.tobytes()
        self._poses.bind()
        self._poses.allocate(raw, len(raw))
        self._poses.release()
        self._frame, self._count = frame, len(frame.vehicles)
        self.uploads += 1

    def _draw(self, painter: QPainter, alpha: float, outline: QColor, height_lift: QPointF | None = None) -> None:
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
        program.setUniformValue1f("min_half", 0.0 if height_lift is not None else
                                  MIN_PIXELS / max(hypot(t.m11(), t.m12()), 1e-9))
        lift = height_lift or QPointF()
        program.setUniformValue("height_lift", QVector2D(lift.x(), lift.y()))
        # The camera's horizontal depth direction is opposite the ground-plane lift; normalisation is enough.
        program.setUniformValue("near_axis", QVector2D(-lift.x(), -lift.y()))
        for index, ink in enumerate(VEHICLE_INKS):
            program.setUniformValue(f"asset_ink[{index}]", QVector3D(*QColor(ink).getRgbF()[:3]))
        program.setUniformValue("outline", QVector3D(*outline.getRgbF()[:3]))
        inverse,_ = t.inverted()
        bounds = inverse.mapRect(QRectF(0,0,width,height))
        centre = bounds.center()
        program.setUniformValue("depth_origin", QVector2D(centre.x(),centre.y()))
        program.setUniformValue1f("depth_span", max(bounds.width()+bounds.height(),100.0)+100.0)
        self._vao.bind()
        self._poses.bind()
        functions.glEnable(0x0B71)  # GL_DEPTH_TEST: opaque parts and vehicles occlude in camera depth
        functions.glDepthMask(True)
        functions.glDepthFunc(0x0203)  # GL_LEQUAL permits coplanar markings
        functions.glClear(0x0100)  # QPainter's still network is a separate 2D layer
        self.frames += 1
        try:
            detail_scale=(hypot(t.m11(),t.m12())*hypot(t.m21(),t.m22()))**.5
            for skin,first,count,max_width in self._groups:
                for location,offset,size in ((1,0,4),(2,4,4),(3,8,2),(4,10,3),(5,13,1)):
                    program.setAttributeBuffer(location,GL_FLOAT,4*(first*FLOATS+offset),size,4*FLOATS)
                start,vertices=self._meshes[skin,max_width*detail_scale>=5]
                self._extra.glDrawArraysInstanced(GL_TRIANGLES,start,vertices,count)
                self.draws += 1
        finally:
            functions.glDisable(0x0B71)
            self._vao.release()
            self._poses.release()
            program.release()

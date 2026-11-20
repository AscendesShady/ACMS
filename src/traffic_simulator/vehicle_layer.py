"""The moving vehicles and walkers drawn each frame: 2D and 2.5D faces, cached by heading, raster or GPU.
Split out of network_scene.py."""

from __future__ import annotations

from collections import defaultdict
from functools import lru_cache
from math import atan2, cos, degrees, hypot, radians, sin, sqrt

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import QGraphicsItem, QStyleOptionGraphicsItem

from . import asset_mesh, gpu, vehicle_assets
from .scene_items import ISOMETRIC_YAW_DEG, VEHICLE_Z, _FaceDrawing, projection_lift
from .theme import VEHICLE_INKS, VEHICLE_OUTLINE, VEHICLE_PAINTS, WALKER


HEADING_STEP_DEG = 2.0  # 2.5D faces (raster vehicles, people): drawn at the nearest such heading (within 1°), so reused
WALKER_MIN_PX = 3  # a 2.5D person under this many pixels tall is drawn as the top-down marker


@lru_cache(maxsize=2048)
def _vehicle_faces(skin, colour, length, width, ux, uy, isometric, detail):
    """Reusable local polygons at the exact heading/dimensions; position and zoom are not mesh work."""
    lift = projection_lift() if isometric else None
    camera = (-lift.x(), -lift.y(), 1.) if lift else (0., 0., 1.)
    faces = []
    fit = asset_mesh.wheel_fit(skin, length, width)
    for face in (asset_mesh.vehicle_mesh(skin) if detail else asset_mesh.vehicle_low_mesh(skin)):
        points = [asset_mesh.expand(p, length, width, fit) for p in face.vertices]
        if face.part == 1 and skin not in (6, 7) and lift:
            side = -uy * camera[0] + ux * camera[1]
            if sum(p[1] for p in points) * side < 0:
                continue
        world = [(ux * a - uy * b, uy * a + ux * b, z) for a, b, z in points]
        n = asset_mesh.normal(world)
        if sum(a * b for a, b in zip(n, camera)) <= 1e-9:
            continue
        depth = sum(sum(a * b for a, b in zip(p, camera)) for p in world) / len(world)
        shade = .68 + .32 * max(0., (-.6 * n[0] - .8 * n[1] + n[2]) / sqrt(2))
        base = QColor(colour if face.ink < 0 else VEHICLE_INKS[face.ink])
        base = QColor.fromRgbF(base.redF() * shade, base.greenF() * shade, base.blueF() * shade)
        polygon = QPolygonF([QPointF(a, b) + (lift * z if lift else QPointF()) for a, b, z in world])
        faces.append((depth, face.ink, base, polygon))
    return _FaceDrawing((base, polygon) for _, _, base, polygon in
                        sorted(faces, key=lambda f: (f[1] in (0, 2, 3, 4), f[0])))


@lru_cache(maxsize=256)
def _pedestrian_faces(ux, uy):
    """Same metre-scaled person at a heading; reuse normals and projection between frames."""
    lift = projection_lift()
    camera = (-lift.x(), -lift.y(), 1.)
    faces = []
    for face in asset_mesh.pedestrian_mesh():
        points = [(ux * p[0] - uy * p[1], uy * p[0] + ux * p[1], p[2]) for p in face.vertices]
        n = asset_mesh.normal(points)
        if sum(a * b for a, b in zip(n, camera)) <= 1e-9:
            continue
        depth = sum(sum(a * b for a, b in zip(p, camera)) for p in points) / len(points)
        faces.append((depth, face.ink, points))
    return tuple((QColor(WALKER if ink < 0 else VEHICLE_INKS[ink]),
                  QPolygonF([QPointF(a, b) + lift * z for a, b, z in points]))
                 for _, ink, points in sorted(faces))


class VehicleLayer(QGraphicsItem):
    """Every vehicle of a run's latest frame in one item. On the map's OpenGL viewport the scene's
    gpu.VehicleRenderer draws one instanced batch per appearance, each frame's poses sent to the GPU once, and the
    2.5D people clear of every overbridge with them (`_people`); elsewhere (the raster viewport, tests) QPainter
    draws them grouped by colour. Either way each
    vehicle is drawn part way (frame_alpha) between its last two simulated poses, so motion stays
    smooth at any frame rate, and zoomed far out they stay visible (dots, or a few pixels)."""

    OUTLINE = QColor(VEHICLE_OUTLINE)

    def __init__(self, scene: NetworkGraphicsScene) -> None:  # noqa: F821 (network_scene's, named only as a string)
        super().__init__()
        self._scene = scene
        self._split = (None, 0, (), ())  # _people's last answer: (frame, overbridges, for the GPU, for QPainter)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemUsesExtendedStyleOption)
        self.setZValue(VEHICLE_Z)

    def boundingRect(self) -> QRectF:  # type intentionally inherited from Qt
        return self._scene.sceneRect()

    def _people(self, frame) -> tuple[tuple, tuple]:
        """A frame's walkers, (for the GPU, for QPainter), worked out once a frame. QPainter keeps those within reach
        of an overbridge: it draws them raised on the deck, after the bridge (OverbridgeItem.paint), and a GPU
        instance has no height. With no overbridge every walker is the GPU's, and no display frame loops over them."""
        bridges = self._scene._overbridges
        if self._split[0] is not frame or self._split[1] != len(bridges):
            near, far = [], []
            for walker in frame.walkers if bridges else ():
                (x0, y0, *_), on = walker[4], False
                for item in bridges:
                    on = on or item.bounds.contains(walker[0], walker[1]) or item.bounds.contains(x0, y0)
                (near if on else far).append(walker)
            self._split = (frame, len(bridges), tuple(far) if bridges else frame.walkers, tuple(near))
        return self._split[2], self._split[3]

    def paint(self, painter: QPainter, option, widget=None) -> None:  # type intentionally inherited from Qt
        frame = self._scene.frame
        if frame is None or not (frame.vehicles or frame.walkers):
            return
        alpha = self._scene.frame_alpha
        transform = painter.worldTransform()
        # Legacy scene.render exports remain top-down; an isometric view supplies the yaw/elevation transform.
        isometric = self._scene.isometric and abs(transform.m12()) + abs(transform.m21()) > 1e-9
        lift = projection_lift() if isometric else None
        exposed = option.exposedRect if option is not None and not option.exposedRect.isEmpty() else None
        pixels_per_metre = QStyleOptionGraphicsItem.levelOfDetailFromTransform(transform)
        # People big enough to be people (2.5D): the GPU's, but for those near an overbridge
        people, others = self._people(frame) if isometric and frame.walkers and (
            pixels_per_metre * asset_mesh.PEDESTRIAN_HEIGHT_M >= WALKER_MIN_PX) else ((), frame.walkers)
        if (frame.vehicles or people) and self._scene.gpu.draw(painter, frame, alpha, self.OUTLINE, lift, people):
            self._paint_walkers(painter, frame, alpha, lift, exposed=exposed, walkers=others)
            return
        if not frame.vehicles:
            self._paint_walkers(painter, frame, alpha, lift, exposed=exposed)
            return
        groups: dict[str, list] = defaultdict(list)
        marker_margin = 3 / max(pixels_per_metre, 1e-9) if not isometric else 0.
        for vehicle in frame.vehicles:
            (x0, y0, ux0, uy0), (x1, y1, ux1, uy1) = vehicle.prev, (vehicle.x, vehicle.y, vehicle.ux, vehicle.uy)
            x, y = x0 + (x1 - x0) * alpha, y0 + (y1 - y0) * alpha
            # Include the full footprint and any roof extrusion, even when the anchor is offscreen.
            margin = max(vehicle.length_m + 3 * vehicle.width_m, marker_margin)
            if exposed is not None and not exposed.adjusted(-margin, -margin, margin, margin).contains(x, y):
                continue
            ux, uy = ux0 + (ux1 - ux0) * alpha, uy0 + (uy1 - uy0) * alpha
            norm = hypot(ux, uy) or 1.0
            groups[vehicle_assets.paint_colour(vehicle, VEHICLE_PAINTS)].append(
                (x, y, ux / norm, uy / norm,
                 vehicle.length_m / 2, vehicle.width_m / 2, vehicle_assets.style(vehicle)))
        dots = not isometric and pixels_per_metre * 4.5 < 3  # isometric bodies keep their real footprint
        yaw = radians(ISOMETRIC_YAW_DEG)
        near = QPointF(sin(yaw), cos(yaw))
        for colour, shapes in groups.items():
            if dots:
                pen = QPen(QColor(colour), 4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
                pen.setCosmetic(True)
                painter.setPen(pen)
                painter.drawPoints([QPointF(x, y) for x, y, *_ in shapes])
                continue
            painter.setPen(QPen(self.OUTLINE, 0.1))
            painter.setBrush(QColor(colour))
            for x, y, ux, uy, half_length, half_width, skin in shapes:
                if skin:
                    self._paint_mesh(painter, skin, colour, x, y, ux, uy, 2 * half_length, 2 * half_width, lift,
                                     pixels_per_metre*2*half_width>=5)
                    continue
                painter.setPen(QPen(self.OUTLINE, 0.1))
                painter.setBrush(QColor(colour))
                footprint = QPolygonF([
                    QPointF(x + ux * half_length - uy * half_width, y + uy * half_length + ux * half_width),
                    QPointF(x + ux * half_length + uy * half_width, y + uy * half_length - ux * half_width),
                    QPointF(x - ux * half_length + uy * half_width, y - uy * half_length - ux * half_width),
                    QPointF(x - ux * half_length - uy * half_width, y - uy * half_length + ux * half_width),
                ])
                if lift is None:
                    painter.drawPolygon(footprint)
                    roof_offset = QPointF()
                else:
                    # Height is a cosmetic skin only. Footprints still use the frame's exact L/W.
                    roof_offset = lift * (2 * half_width * gpu.BODY_HEIGHT_RATIO)
                    roof = QPolygonF([point + roof_offset for point in footprint])
                    normals = ((ux, uy), (uy, -ux), (-ux, -uy), (-uy, ux))
                    base_colour = QColor(colour)
                    for index, (nx, ny) in enumerate(normals):
                        if nx * near.x() + ny * near.y() <= 0:
                            continue
                        following = (index + 1) % 4
                        shade = 0.7 + 0.15 * (nx * -0.6 + ny * -0.8)
                        painter.setBrush(QColor.fromRgbF(base_colour.redF() * shade, base_colour.greenF() * shade,
                                                        base_colour.blueF() * shade))
                        painter.drawPolygon(QPolygonF([footprint[index], footprint[following],
                                                       roof[following], roof[index]]))
                    painter.setBrush(base_colour)
                    painter.drawPolygon(roof)
        self._paint_walkers(painter, frame, alpha, lift, exposed=exposed)

    # Also called by scene_items.OverbridgeItem.paint (walkers on a footbridge deck): keep the name and arguments.
    def _paint_walkers(self, painter: QPainter, frame, alpha: float, lift=None, bridge=None, exposed=None,
                       walkers=None) -> None:
        """Interpolate the same world poses: 2D markers, metre-scaled people in 2.5D. `walkers`: only these of the
        frame's (the rest are on the GPU); a bridge's are those within its reach."""
        if walkers is None:
            walkers = self._people(frame)[1] if bridge is not None and lift is not None else frame.walkers
        if not walkers:
            return
        pixels = QStyleOptionGraphicsItem.levelOfDetailFromTransform(painter.worldTransform())
        if exposed is not None:
            margin = max(asset_mesh.FOOTBRIDGE_DECK_M + asset_mesh.PEDESTRIAN_HEIGHT_M + 1., 3 / max(pixels, 1e-9))
            exposed = exposed.adjusted(-margin, -margin, margin, margin)
        if lift is not None and pixels * asset_mesh.PEDESTRIAN_HEIGHT_M < WALKER_MIN_PX:
            if bridge is not None:
                return  # too small to be people: the layer's own call marks every walker, a bridge's too
            lift = None
        if lift is not None:
            step = radians(HEADING_STEP_DEG)
            poses = []
            for x1, y1, ux1, uy1, (x0, y0, ux0, uy0) in walkers:
                x, y = x0 + (x1 - x0) * alpha, y0 + (y1 - y0) * alpha
                if exposed is not None and not exposed.contains(x, y):
                    continue
                height = bridge.height_at(x, y) if bridge else max(
                    (item.height_at(x, y) for item in self._scene._overbridges), default=0.)
                if (height > 0) != (bridge is not None):
                    continue
                # The nearest HEADING_STEP_DEG, as _paint_mesh: roads running every way had more headings than the
                # faces kept, so every walker's were built again on every display frame
                heading = round(atan2(uy0 + (uy1 - uy0) * alpha, ux0 + (ux1 - ux0) * alpha) / step) * step
                poses.append((x, y, round(cos(heading), 9), round(sin(heading), 9), height))
            for x, y, ux, uy, height in sorted(poses, key=lambda p: p[0] + p[1] + p[4]):
                anchor = QPointF(x, y) + lift * height
                painter.setPen(Qt.PenStyle.NoPen)
                for colour, polygon in _pedestrian_faces(ux, uy):
                    painter.setBrush(colour)
                    painter.drawPolygon(polygon.translated(anchor))
            return
        points = [QPointF(x0 + (x1 - x0) * alpha, y0 + (y1 - y0) * alpha)
                  for x1, y1, _ux, _uy, (x0, y0, _px, _py) in walkers]
        if exposed is not None:
            points = [point for point in points if exposed.contains(point)]
        pen = QPen(QColor(WALKER), 3 if pixels * 0.6 < 3 else 0.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
        pen.setCosmetic(pixels * 0.6 < 3)
        painter.setPen(pen)
        painter.drawPoints(points)

    @staticmethod
    def _paint_mesh(painter, skin, colour, x, y, ux, uy, length, width, lift, detail=True):
        """Opaque faces, sorted within the object; the GPU uses these same metre-expanded meshes. Kept by heading
        (_vehicle_faces), they would rarely be found again on a network whose roads run every way: top-down, one
        drawing heading east is turned to the vehicle's heading (its slopes then lit as heading east); in 2.5D, where
        the view's angle decides which faces show, the heading is taken to the nearest HEADING_STEP_DEG."""
        painter.setPen(Qt.PenStyle.NoPen)
        if lift is None:
            _vehicle_faces(skin, colour, length, width, 1., 0., False, detail).paint(painter, x, y, degrees(atan2(uy, ux)))
            return
        step = radians(HEADING_STEP_DEG)
        heading = round(atan2(uy, ux) / step) * step
        _vehicle_faces(skin, colour, length, width, round(cos(heading), 9), round(sin(heading), 9), True,
                       detail).paint(painter, x, y)

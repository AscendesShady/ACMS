"""Map items drawn to scale in metres: paving, metro lines and stops, signal countdown clocks, overbridges and
buildings, plus the orthographic projection helpers they share. Split out of network_scene.py."""

from __future__ import annotations

from functools import lru_cache
from math import cos, hypot, radians, sin, sqrt, tan

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QImage, QPainter, QPainterPath, QPen, QPicture, QPolygonF, QTransform
from PySide6.QtWidgets import QGraphicsItem, QGraphicsPathItem, QGraphicsPolygonItem, QStyleOptionGraphicsItem

from . import asset_mesh, building_assets, metro_assets
from .models import FOOTPATH_M
from . import theme
from .theme import (BUILDING_PAINTS, BUILDING_ROOF, FOOTPATH, FOOTPATH_MORTAR, FOOTPATH_SHADE, MAP_SELECTED, METRO_INKS,
                    METRO_LINE, METRO_STOP, PAVING_SELECTED, SHADOW, VEHICLE_INKS)


SELECTED_COLOR = QColor(MAP_SELECTED)
SIGNAL_GREEN, SIGNAL_AMBER, SIGNAL_RED = QColor(theme.SIGNAL_GREEN), QColor(theme.SIGNAL_AMBER), QColor(theme.SIGNAL_RED)
SIGNAL_BODY, SIGNAL_RIM = QColor(theme.SIGNAL_BODY), QColor(theme.SIGNAL_RIM)  # a signal head's (and clock's) housing
# A signal's countdown clock (SignalClock): seven-segment digits this big (metres), beside the head; in 2.5D on a pole
CLOCK_DIGIT_W, CLOCK_DIGIT_H, CLOCK_STROKE, CLOCK_PAD = 0.6, 1.0, 0.13, 0.2
CLOCK_OFFSET_M, CLOCK_POLE_M = 1.7, 2.5
CLOCK_SEGMENTS = ("abcdef", "bc", "abdeg", "abcdg", "bcfg", "acdfg", "acdefg", "abc", "abcdefg", "abcdfg")
BUILDING_COLOR = QColor(BUILDING_ROOF)  # an institution's roof
VEHICLE_Z = 8  # the vehicle layer: above roads and signals, below labels and end markers
METRO_STOP_RADIUS_M = 12.0  # a stop's marker on the map
ISOMETRIC_YAW_DEG = 45.0
ISOMETRIC_ELEVATION_DEG = 35.264389682754654  # true isometric; a camera angle, not a simulation parameter


def projection_transform(isometric: bool = False, scale: float = 1.0) -> QTransform:
    """World metres to orthographic ground coordinates. No perspective, roll or direction-dependent timing."""
    if not isometric:
        return QTransform.fromScale(scale, scale)
    yaw, elevation = radians(ISOMETRIC_YAW_DEG), radians(ISOMETRIC_ELEVATION_DEG)
    return QTransform(scale * cos(yaw), scale * sin(elevation) * sin(yaw),
                      -scale * sin(yaw), scale * sin(elevation) * cos(yaw), 0, 0)


def projection_lift() -> QPointF:
    """A metre of cosmetic body height, expressed in the ground plane for Qt's affine painter.

    The ground transform maps this to straight screen-up with length cos(elevation); world footprints stay put.
    """
    yaw, elevation = radians(ISOMETRIC_YAW_DEG), radians(ISOMETRIC_ELEVATION_DEG)
    return QPointF(-sin(yaw) / tan(elevation), -cos(yaw) / tan(elevation))


def _screen_pen(color: QColor, pixels: float) -> QPen:
    pen = QPen(color, pixels)
    pen.setCosmetic(True)
    return pen


Points = list[tuple[float, float]]


def _path(points: Points) -> QPainterPath:
    path = QPainterPath(QPointF(*points[0]))
    for point in points[1:]:
        path.lineTo(QPointF(*point))
    return path


@lru_cache(maxsize=1)
def brick_brush():
    """A 0.4 m repeat of 0.2 by 0.1 m herringbone bricks; decorative, with no geometry changes."""
    tile = QImage(64, 64, QImage.Format.Format_ARGB32_Premultiplied)
    tile.fill(QColor(FOOTPATH))
    painter = QPainter(tile)
    painter.setPen(QPen(QColor(FOOTPATH_MORTAR), 1.4))
    for x in range(-3, 7):
        for y in range(-3, 7):
            phase = (x - y) % 4
            if phase not in (0, 2):
                continue
            painter.setBrush(QColor(FOOTPATH if (x + y) % 4 == 0 else FOOTPATH_SHADE))
            painter.drawRect(QRectF(x * 16, (y if phase == 0 else y - 1) * 16,
                                   32 if phase == 0 else 16, 16 if phase == 0 else 32))
    painter.end()
    brush = QBrush(tile)
    brush.setTransform(QTransform.fromScale(.4 / 64, .4 / 64))
    return brush


class PavingItem(QGraphicsPathItem):
    """Brick tint at overview scales; joints appear only when a brick is at least four pixels long."""

    def __init__(self, path, selected=False):
        super().__init__(path)
        self.selected = selected
        self.setPen(Qt.PenStyle.NoPen)
        self.setBrush(QColor(PAVING_SELECTED if selected else FOOTPATH))

    def paint(self, painter, option, widget=None):
        painter.setPen(self.pen())
        pixels = QStyleOptionGraphicsItem.levelOfDetailFromTransform(painter.worldTransform())
        painter.setBrush(brick_brush() if pixels >= 20 and not self.selected else self.brush())
        painter.drawPath(self.path())


class _FaceDrawing:
    """Compile vector commands once; replay in Qt rather than cross Python for every face on every pan."""

    def __init__(self, faces):
        self.faces = tuple(faces)
        self.picture, self.hints = None, None

    def paint(self, painter, x=0., y=0., turn=0.):
        """At (x, y), turned `turn` degrees (clockwise on the screen, y down)."""
        hints = painter.renderHints()
        if self.picture is None or hints != self.hints:
            self.picture, self.hints = QPicture(), hints
            record = QPainter(self.picture)
            record.setRenderHints(hints)
            record.setPen(Qt.PenStyle.NoPen)
            for colour, polygon in self.faces:
                record.setBrush(colour)
                record.drawPolygon(polygon)
            record.end()
        painter.save()
        painter.translate(x, y)
        painter.rotate(turn)
        # QPicture applies print/display DPI scaling. These vectors are world metres, so cancel that scaling.
        device = painter.device()
        painter.scale(self.picture.logicalDpiX() / device.logicalDpiX(),
                      self.picture.logicalDpiY() / device.logicalDpiY())
        painter.drawPicture(QPointF(), self.picture)
        painter.restore()


class MetroItem(QGraphicsItem):
    """One static line: flat schematic in 2D, metre-height viaduct/stations and parked preview in 2.5D."""

    def __init__(self, owner, stops, cars=5, track=None, selected=False):
        super().__init__()
        self.owner, self.stops, self.selected = owner, stops, selected
        self.track = _path(track or stops)  # the line's track, curves included (default: stop to stop)
        self.faces = metro_assets.line_mesh(stops, cars, track)
        lift = projection_lift()
        projected = [QPointF(x, y) + lift * z for face in self.faces for x, y, z in face.vertices]
        flat = QPolygonF([QPointF(*p) for p in track or stops]).boundingRect().adjusted(
            -METRO_STOP_RADIUS_M, -METRO_STOP_RADIUS_M, METRO_STOP_RADIUS_M, METRO_STOP_RADIUS_M)
        self.bounds = flat.united(QPolygonF(projected).boundingRect()).adjusted(-2, -2, 2, 2)
        # Project and sort once. Camera yaw/elevation are fixed; zoom and pan do not change face order.
        camera = (-lift.x(), -lift.y(), 1.)
        visible = []
        for face in self.faces:
            n = asset_mesh.normal(face.vertices)
            if sum(a * b for a, b in zip(n, camera)) <= 1e-9:
                continue
            depth = sum(sum(a * b for a, b in zip(p, camera)) for p in face.vertices) / len(face.vertices)
            shade = .68 + .32 * max(0., (-.6 * n[0] - .8 * n[1] + n[2]) / sqrt(2))
            colour = QColor(METRO_INKS[face.ink])
            colour = QColor.fromRgbF(colour.redF() * shade, colour.greenF() * shade, colour.blueF() * shade)
            polygon = QPolygonF([QPointF(x, y) + lift * z for x, y, z in face.vertices])
            # Glazing is coplanar: its tiny sorting bias keeps the supporting facade behind it.
            visible.append((depth + (.002 if face.ink == 3 else 0.), colour, polygon))
        self.projected_faces = sorted(visible, key=lambda f: f[0])
        self._drawing = _FaceDrawing((colour, polygon) for _, colour, polygon in self.projected_faces)

    def boundingRect(self):
        return self.bounds

    def paint(self, painter, option, widget=None):
        t = painter.worldTransform()
        if not self.owner.isometric or abs(t.m12()) + abs(t.m21()) < 1e-9:
            painter.setPen(_screen_pen(SELECTED_COLOR if self.selected else QColor(METRO_LINE), 4))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(self.track)
            painter.setPen(_screen_pen(SELECTED_COLOR if self.selected else QColor(METRO_LINE), 3))
            painter.setBrush(QColor(METRO_STOP))
            for x, y in self.stops:
                painter.drawEllipse(QPointF(x, y), METRO_STOP_RADIUS_M, METRO_STOP_RADIUS_M)
            return
        painter.setPen(Qt.PenStyle.NoPen)
        self._drawing.paint(painter)
        if self.selected:  # outlined along the deck
            painter.setPen(_screen_pen(SELECTED_COLOR, 3))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(self.track.translated(projection_lift() * metro_assets.TRACK_M))


class SignalClock(QGraphicsItem):
    """A signal head's countdown, a digital clock beside it: whole seconds until its signal changes, lit in the
    signal's colour over the unlit segments. Drawn upright on screen at the map's scale (it grows as you zoom), so it
    reads the same in 2D and, standing on a pole, in 2.5D."""

    def __init__(self, owner, x: float, y: float) -> None:
        super().__init__()
        self.owner, self.anchor, self.reading = owner, QPointF(x, y), None  # reading: (seconds, "G"/"Y"/"R")
        self.setZValue(VEHICLE_Z + 0.5)  # over the traffic, as a label is

    def boundingRect(self):
        reach = CLOCK_POLE_M * 2 + CLOCK_DIGIT_W * 4  # the 2.5D pole, foreshortened, and three digits' width
        return QRectF(self.anchor.x() - reach, self.anchor.y() - reach, 2 * reach, 2 * reach)

    def set_reading(self, reading: tuple[int, str] | None) -> None:
        if reading != self.reading:
            self.reading = reading
            self.update()

    def paint(self, painter, option, widget=None):
        if self.reading is None:
            return
        seconds, state = self.reading
        digits = str(min(seconds, 999))
        t = painter.worldTransform()
        metre = hypot(t.m11(), t.m21())  # screen units per metre, in 2D and 2.5D alike
        ground = t.map(self.anchor)
        top = t.map(self.anchor + projection_lift() * CLOCK_POLE_M) if self.owner.isometric else ground
        painter.save()
        painter.setWorldTransform(QTransform.fromTranslate(top.x(), top.y()).scale(metre, metre))
        width = len(digits) * CLOCK_DIGIT_W + (len(digits) - 1) * CLOCK_STROKE + 2 * CLOCK_PAD
        height = CLOCK_DIGIT_H + 2 * CLOCK_PAD
        if self.owner.isometric:  # the pole, from the kerb up to the clock's foot
            painter.setPen(QPen(SIGNAL_RIM, 0.1))
            painter.drawLine(QPointF(0, (ground.y() - top.y()) / metre), QPointF(0, height / 2))
        painter.setPen(QPen(SIGNAL_RIM, 0.08))
        painter.setBrush(SIGNAL_BODY)
        painter.drawRoundedRect(QRectF(-width / 2, -height / 2, width, height), 0.15, 0.15)
        lit = {"G": SIGNAL_GREEN, "Y": SIGNAL_AMBER}.get(state, SIGNAL_RED)
        unlit = QColor(lit)
        unlit.setAlphaF(0.12)
        painter.setPen(Qt.PenStyle.NoPen)
        w, h, s = CLOCK_DIGIT_W, CLOCK_DIGIT_H, CLOCK_STROKE
        across, down = w - 2 * s, h / 2 - 1.5 * s
        segments = {"a": QRectF(s, 0, across, s), "g": QRectF(s, (h - s) / 2, across, s), "d": QRectF(s, h - s, across, s),
                    "f": QRectF(0, s, s, down), "b": QRectF(w - s, s, s, down),
                    "e": QRectF(0, (h + s) / 2, s, down), "c": QRectF(w - s, (h + s) / 2, s, down)}
        x = -width / 2 + CLOCK_PAD
        for digit in digits:
            on = CLOCK_SEGMENTS[int(digit)]
            for name, rect in segments.items():
                painter.setBrush(lit if name in on else unlit)
                painter.drawRect(rect.translated(x, -h / 2))
            x += w + s
        painter.restore()


class OverbridgeItem(QGraphicsPolygonItem):
    """Keep the 2D crossing footprint; the 2.5D deck stands at the assumed stair-climb height."""

    def __init__(self, owner, outline, x, y, ux, uy, half, selected):
        super().__init__(outline)
        self.owner, self.origin, self.heading, self.half = owner, (x, y), (ux, uy), half
        self.selected = selected
        self.faces = metro_assets.placed(asset_mesh.footbridge_mesh(half), x, y, ux, uy)
        lift = projection_lift()
        self.polygons = []
        camera = (-lift.x(), -lift.y(), 1.)
        for face in self.faces:
            n = asset_mesh.normal(face.vertices)
            if sum(a * b for a, b in zip(n, camera)) <= 1e-9:
                continue
            depth = sum(sum(a * b for a, b in zip(p, camera)) for p in face.vertices) / len(face.vertices)
            shade = .68 + .32 * max(0., (-.6 * n[0] - .8 * n[1] + n[2]) / sqrt(2))
            colour = QColor(FOOTPATH if face.ink < 0 else VEHICLE_INKS[face.ink])
            colour = QColor.fromRgbF(colour.redF() * shade, colour.greenF() * shade, colour.blueF() * shade)
            polygon = QPolygonF([QPointF(a, b) + lift * z for a, b, z in face.vertices])
            self.polygons.append((depth, colour, polygon))
        self.polygons.sort(key=lambda p: p[0])
        self.extrusion = QPainterPath()
        for _, _, polygon in self.polygons:
            part = QPainterPath()
            part.addPolygon(polygon)
            self.extrusion = self.extrusion.united(part)
        self.bounds = super().boundingRect().united(self.extrusion.boundingRect()).adjusted(-2, -2, 2, 2)

    def boundingRect(self):
        return self.bounds

    def shape(self):
        return self.extrusion if self.owner.isometric else super().shape()

    def height_at(self, x, y):
        ox, oy = self.origin
        ux, uy = self.heading
        along, across = (x - ox) * ux + (y - oy) * uy, -(x - ox) * uy + (y - oy) * ux
        if abs(along) > .6 or abs(across) >= self.half:
            return 0.
        # Display-only ramp at each kerb. Frames have XY poses, not a simulated stair trajectory.
        return asset_mesh.FOOTBRIDGE_DECK_M * min(1., (self.half - abs(across)) / FOOTPATH_M)

    def paint(self, painter, option, widget=None):
        t = painter.worldTransform()
        if not self.owner.isometric or abs(t.m12()) + abs(t.m21()) < 1e-9:
            super().paint(painter, option, widget)
            return
        painter.setPen(_screen_pen(SELECTED_COLOR, 1) if self.selected else Qt.PenStyle.NoPen)
        for _, colour, polygon in self.polygons:
            painter.setBrush(colour)
            painter.drawPolygon(polygon)
        if self.owner.frame is not None:  # walkers on the deck: vehicle_layer.VehicleLayer._paint_walkers
            self.owner.vehicle_layer._paint_walkers(painter, self.owner.frame, self.owner.frame_alpha,
                                                    projection_lift(), self)


class BuildingItem(QGraphicsItem):
    """One institution, with the exact ground footprint in 2D and metre-height architecture in 2.5D."""

    def __init__(self, scene, institution):
        super().__init__()
        self.owner, self.institution = scene, institution
        self.side = sqrt(institution.area_m2)
        self.height_m = building_assets.height(institution)
        half, lift = self.side / 2, projection_lift() * self.height_m
        self.footprint = QRectF(-half, -half, self.side, self.side)
        self.bounds = self.footprint.united(self.footprint.translated(lift)).adjusted(-2, -2, 2, 2)
        self.faces = building_assets.mesh(institution.kind, institution.area_m2, self.height_m)
        self._projected_faces = None
        self._shape_3d = None
        self.setCacheMode(QGraphicsItem.CacheMode.DeviceCoordinateCache)
        self.setPos(institution.x, institution.y)
        self.setData(0, ("institution", institution.id))
        self.setZValue(5)
        self.setToolTip(f"Height {self.height_m:g} m" + (" (assumed)" if institution.height_m is None else ""))

    def boundingRect(self):
        return self.bounds

    def shape(self):
        if self.owner.isometric and self._shape_3d is not None:
            return self._shape_3d
        path = QPainterPath()
        if not self.owner.isometric:
            path.addRect(self.footprint)
        else:
            lift = projection_lift()
            for face in self.faces:
                points = [asset_mesh.expand(p, self.side, self.side) for p in face.vertices]
                polygon = QPolygonF([QPointF(x, y) + lift * z for x, y, z in points])
                part = QPainterPath()
                part.addPolygon(polygon)
                path = path.united(part)
            self._shape_3d = path
        return path

    def paint(self, painter, option, widget=None):
        selected = self.owner.selection == ("institution", self.institution.id)
        t = painter.worldTransform()
        if not self.owner.isometric or abs(t.m12()) + abs(t.m21()) < 1e-9:
            painter.setBrush(BUILDING_COLOR)
            painter.setPen(_screen_pen(SELECTED_COLOR if selected else QColor(SHADOW), 3 if selected else 1))
            painter.drawRect(self.footprint)
            return
        lift = projection_lift()
        camera = (-lift.x(), -lift.y(), 1.)
        body, roof = BUILDING_PAINTS[self.institution.kind]
        inks = (VEHICLE_INKS[0], roof, *VEHICLE_INKS[2:])
        shadow = QColor(SHADOW)
        shadow.setAlpha(65)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(shadow)
        painter.drawRect(self.footprint.translated(1.5, 1.5))
        if self._projected_faces is None:
            faces = []
            for face in self.faces:
                points = [asset_mesh.expand(p, self.side, self.side) for p in face.vertices]
                n = asset_mesh.normal(points)
                if sum(a * b for a, b in zip(n, camera)) <= 1e-9:
                    continue
                depth = sum(sum(a * b for a, b in zip(p, camera)) for p in points) / len(points)
                shade = .68 + .32 * max(0., (-.6 * n[0] - .8 * n[1] + n[2]) / sqrt(2))
                colour = QColor(body if face.ink < 0 else inks[face.ink])
                colour = QColor.fromRgbF(colour.redF() * shade, colour.greenF() * shade, colour.blueF() * shade)
                polygon = QPolygonF([QPointF(x, y) + lift * z for x, y, z in points])
                faces.append((depth, face.ink, colour, polygon))
            self._projected_faces = _FaceDrawing((colour, polygon) for _, _, colour, polygon in
                                                 sorted(faces, key=lambda f: (f[1] in (0, 3), f[0])))
        self._projected_faces.paint(painter)
        if selected:
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(_screen_pen(SELECTED_COLOR, 2))
            painter.drawRect(self.footprint)

"""Vector rendering and camera interaction for the network model."""

from __future__ import annotations

from collections import defaultdict
from functools import lru_cache
from math import atan2, cos, hypot, radians, sin, sqrt, tan

from PySide6.QtCore import QPoint, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (
    QBrush, QColor, QFont, QFontDatabase, QGuiApplication, QImage, QMouseEvent, QPainter, QPainterPath, QPainterPathStroker,
    QPen, QPolygonF, QSurfaceFormat,
    QTransform, QWheelEvent,
)
from PySide6.QtOpenGLWidgets import QOpenGLWidget
from PySide6.QtWidgets import (
    QGraphicsEllipseItem,
    QGraphicsItem,
    QGraphicsLineItem,
    QGraphicsPathItem,
    QGraphicsPolygonItem,
    QGraphicsRectItem,
    QGraphicsScene,
    QGraphicsSceneMouseEvent,
    QGraphicsTextItem,
    QGraphicsView,
    QStyleOptionGraphicsItem,
    QWidget,
)

from . import asset_mesh, building_assets, gpu, metro_assets, vehicle_assets
from .geometry import nearest_along, offset_polyline, point_along, polyline_length, sub_polyline
from .institutions import KINDS, Placement, links
from .models import (BAY_ENTRY_M, BAY_LENGTH_M, BAY_STOP_M, BAY_WIDTH_M, CYCLE_LANE_M, FOOTPATH_M, LANE_WIDTH_M,
                     POCKET_TAPER_M, BusStop, Institution, Network, Node, Road, bay_depth, stop_line_inset)
from .widgets import ICON_FONTS
from .roundabouts import design_roundabout
from .signal_timing import ADAPTIVE, STOP_LINE_LOOP_M, UPSTREAM_LOOP_AT_M, UPSTREAM_LOOP_M
from .walking import footpath_join
from .theme import (ACCENT_EDGE, BUILDING_PAINTS, CYCLE_LANE, DANGER, FOOTPATH, FOOTPATH_SHADE, FOOTPATH_MORTAR,
                    PAVING_SELECTED, WALKER, METRO_INKS, METRO_LINE, METRO_STOP, SHADOW, STATUS, TEXT,
                    VEHICLE_INKS, VEHICLE_PAINTS)


# Everything physical is drawn to scale in metres. Only labels and selection
# outlines keep a fixed screen size so they stay readable at any zoom.
SIGNAL_SIZE = 1.2  # signal head footprint
CENTRE_LINE_WIDTH = 0.15
LANE_LINE_WIDTH = 0.1
LANE_DASH_M, LANE_GAP_M = 3.0, 9.0
STOP_LINE_WIDTH = 0.3
SELECTED_COLOR = QColor("#ff8b23")
ROAD_COLOR = QColor("#20292d")
PARAPET_COLOR = QColor("#d9dde2")
OVERPASS_LIFT = 5.0  # z offset: above ground roads and junctions, below vehicles and labels
PAINT_COLOR = QColor("#e8eef0")
SIGNAL_GREEN, SIGNAL_AMBER, SIGNAL_RED = QColor("#b6e21c"), QColor("#ffb000"), QColor("#ff2020")
BUS_STOP_PAINT = QColor("#f7cf28")  # bay markings and the stop's flag
DETECTOR_COLOR = QColor("#8fdcf5")  # loop detectors' outlines
BUILDING_COLOR = QColor("#8a7a66")  # an institution's roof
BUS_STOP_NUDGE_M, BUS_STOP_SHIFT_NUDGE_M = 0.5, 5.0  # an arrow key's step, and with Shift
METRO_AIM_PX = 6  # a drag this long, deploying metro, aims the stop instead of placing it plainly
METRO_HANDLE_M = 60.0  # an aimed stop's handle each way on the map (half a platform)
VEHICLE_Z = 8  # the vehicle layer: above roads and signals, below labels and end markers
METRO_Z = 7.5  # flat metro markers; the overhead structures use VEHICLE_Z + 1
METRO_STOP_RADIUS_M = 12.0  # a stop's marker on the map
RIGHT_CLICK_PX = 4  # a right press and release moving less than this is a click, not a pan
# Item data key: the item is in the still layer, what rebuild() draws under the vehicles (but the
# signal lamps). During a run the view caches it with the background (NetworkGraphicsScene.cache_still).
STILL = 1
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


class MetroItem(QGraphicsItem):
    """One static line: flat schematic in 2D, metre-height viaduct/stations and parked preview in 2.5D."""

    def __init__(self, owner, stops, cars=5, track=None):
        super().__init__()
        self.owner, self.stops = owner, stops
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

    def boundingRect(self):
        return self.bounds

    def paint(self, painter, option, widget=None):
        t = painter.worldTransform()
        if not self.owner.isometric or abs(t.m12()) + abs(t.m21()) < 1e-9:
            painter.setPen(_screen_pen(QColor(METRO_LINE), 4))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(self.track)
            painter.setPen(_screen_pen(QColor(METRO_LINE), 3))
            painter.setBrush(QColor(METRO_STOP))
            for x, y in self.stops:
                painter.drawEllipse(QPointF(x, y), METRO_STOP_RADIUS_M, METRO_STOP_RADIUS_M)
            return
        painter.setPen(Qt.PenStyle.NoPen)
        for _, colour, polygon in self.projected_faces:
            painter.setBrush(colour)
            painter.drawPolygon(polygon)


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
        if self.owner.frame is not None:
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
        self._shape_3d = None
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
        faces = []
        for face in self.faces:
            points = [asset_mesh.expand(p, self.side, self.side) for p in face.vertices]
            n = asset_mesh.normal(points)
            if sum(a * b for a, b in zip(n, camera)) <= 1e-9:
                continue
            depth = sum(sum(a * b for a, b in zip(p, camera)) for p in points) / len(points)
            shade = .68 + .32 * max(0., (-.6 * n[0] - .8 * n[1] + n[2]) / sqrt(2))
            faces.append((depth, face.ink, points, shade))
        for _, ink, points, shade in sorted(faces, key=lambda f: (f[1] in (0, 3), f[0])):
            colour = QColor(body if ink < 0 else inks[ink])
            painter.setBrush(QColor.fromRgbF(colour.redF() * shade, colour.greenF() * shade, colour.blueF() * shade))
            painter.drawPolygon(QPolygonF([QPointF(x, y) + lift * z for x, y, z in points]))
        if selected:
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(_screen_pen(SELECTED_COLOR, 2))
            painter.drawRect(self.footprint)


class NetworkGraphicsScene(QGraphicsScene):
    buildings_changed = Signal(bool)
    # (kind, id, x, y): the clicked object and the click point in metres. kind is
    # "road", "node", "entry" (end supply marker), "exit" (end demand marker)
    # "bus_stop", "institution" or "" for empty canvas. The last value is True for Ctrl/Shift+click.
    clicked = Signal(str, str, float, float, bool)
    # A selection box dragged over empty map (left, top, right, bottom in metres, crossing?, additive?):
    # dragged right to left it is a crossing box (takes what it touches), left to right a window.
    boxed = Signal(float, float, float, float, bool, bool)
    drag_finished = Signal()
    bus_stop_moved = Signal(str)  # nudged by an arrow key (a drag ends with drag_finished)
    redrawn = Signal()  # after every rebuild(), which clears the scene: extensions draw again
    placement_changed = Signal()  # the building being placed moved, was set down or its circle changed
    placed = Signal()  # the second click: `placing` is where it goes and how far its circle reaches
    # A left click while deploying metro (metro_deploying): a stop goes there (x, y), aimed along (dx, dy) when it was
    # dragged (the pen tool's way: the track runs through the stop that way), else (0, 0); and the aim as it is dragged
    metro_clicked = Signal(float, float, float, float)
    metro_aiming = Signal(float, float, float, float)
    spawn_clicked = Signal(float, float)  # a left click while placing spawn points (spawn_placing): one goes there
    still_cached_changed = Signal(bool)  # a run started (the still layer is cached) or ended

    def __init__(self, network: Network | None = None) -> None:
        super().__init__()
        self.network = network
        self.selection: tuple[str, str] | None = None
        self.multi_nodes: set[str] = set()  # junctions picked together (Ctrl+click)
        self._roundabouts: dict = {}  # node id -> RoundaboutDesign, refreshed each rebuild
        # Nodes carried when the selected object is dragged; empty = drag mode off.
        self.drag_nodes: list[str] = []
        self._drag_from: QPointF | None = None
        # A bus stop grabbed with the mouse slides along its road: (its id, metres from the grab
        # point to its middle, whether it has moved yet).
        self._slide: tuple[str, float, bool] | None = None
        self.setBackgroundBrush(QColor("#075322"))
        # The run as last published (simulation.Frame): set by the main window, drawn each frame.
        self.frame = None
        self.frame_alpha = 1.0  # where between the last two simulated steps to draw vehicles
        self.isometric = False  # display only; neither this nor the camera enters the network or published frame
        self.buildings_visible = True
        self._building_items: list[QGraphicsItem] = []
        self._metro_items: list[MetroItem] = []
        self._metro_labels: list[QGraphicsTextItem] = []
        self._overbridges: list[OverbridgeItem] = []
        self.gpu = gpu.VehicleRenderer()  # draws them on the GPU when the view is OpenGL (kept across rebuilds)
        self.signal_lamps: dict[tuple[str, str], tuple[QGraphicsEllipseItem, QColor]] = {}
        self._signal_states: dict | None = None
        self.extent = None  # (the network's rect) -> what the view may scroll over, with its background
        # While the background panel's Move or Two Points is on, left clicks go to it (press, move, release).
        self.background_tool = None
        self._tool_held = False
        self.group: set[tuple[str, str]] = set()  # a selection box's objects
        self.box_select = True  # dragging over empty map draws a selection box (off while a tool is on)
        self._footpath_preview: list = []  # the road panel's footpath choice, highlighted before Apply
        self.metro_deploying = False  # Edit Network → Metro's Start deployment: each left click places a stop
        self._metro_press: QPointF | None = None  # deploying: where the left button went down (a drag aims it)
        self.spawn_placing = False  # Demand Generator → Pedestrian spawn points: each left click places one
        self.metro_pending: tuple[float, float] | None = None  # a deployment's first stop, until the second makes the line
        self._box: tuple[QPointF, bool] | None = None  # where the press was, additive?
        self._band: QGraphicsRectItem | None = None
        self._metro_ghost: QGraphicsPathItem | None = None  # deploying: the leg the next click would add
        self.network_rect = QRectF()
        # An institution being placed (institutions.Placement), drawn over everything: it takes the map's
        # left clicks, mouse moves and (once set down) the wheel. None: not placing.
        self.placing: Placement | None = None
        self._zone: tuple | None = None  # the selected institution's (x, y, radius, links)
        self.hidden = False  # the whole network hidden: the background's place or scale is set first
        self.still_cached = False  # a run is on: the still layer is drawn once, with the background

    def set_network(self, network: Network) -> None:
        self.network = network
        self.set_selection("")

    def set_selection(self, kind: str, identifier: str = "") -> None:
        self.selection = (kind, identifier) if kind else None
        self.multi_nodes, self.group = set(), set()
        self.rebuild()

    def set_multi_selection(self, node_ids: list[str]) -> None:
        self.selection, self.multi_nodes, self.group = None, set(node_ids), set()
        self.rebuild()

    def set_group(self, objects) -> None:
        """A box's selection, (kind, id) of junctions, roads, ends and bus stops, highlighted together."""
        self.selection, self.multi_nodes, self.group = None, set(), set(objects)
        self.rebuild()

    def set_hidden(self, hidden: bool) -> None:
        """Hide the whole network (while the background's place or scale is set), or show it again."""
        if hidden != self.hidden:
            self.hidden = hidden
            self.rebuild()

    def set_buildings_visible(self, visible: bool) -> None:
        """Show/hide institution visuals only. Demand, selection and camera bounds stay intact."""
        visible = bool(visible)
        if visible == self.buildings_visible:
            return
        self.buildings_visible = visible
        for item in self._building_items:
            item.setVisible(visible and not self.hidden)
        self.invalidate(self.sceneRect(), QGraphicsScene.SceneLayer.AllLayers)
        self.buildings_changed.emit(visible)

    def project_building_labels(self):
        lift = projection_lift() if self.isometric else QPointF()
        for item in self._building_items:
            if isinstance(item, QGraphicsTextItem) and item.data(2):
                x, y, height = item.data(2)
                item.setPos(QPointF(x, y) + lift * height)
        self.invalidate(self.sceneRect(), QGraphicsScene.SceneLayer.AllLayers)

    def project_metro(self):
        """Raise scenery above traffic only in 2.5D; overhead items cannot join the background cache."""
        if self._metro_ghost is not None:
            self._metro_ghost.setPos(projection_lift() * metro_assets.TRACK_M if self.isometric else QPointF())
        for item in self._metro_items:
            item.setZValue(VEHICLE_Z + 1 if self.isometric else METRO_Z)
            item.setData(STILL, not self.isometric)
            item.setFlag(QGraphicsItem.GraphicsItemFlag.ItemHasNoContents, self.still_cached and not self.isometric)
            item.setCacheMode(QGraphicsItem.CacheMode.DeviceCoordinateCache if self.isometric
                              else QGraphicsItem.CacheMode.NoCache)
            item.update()
        lift = projection_lift() if self.isometric else QPointF()
        for label in self._metro_labels:
            x, y, height, preview = label.data(2)
            label.setPos(QPointF(x, y) + lift * height)
            label.setVisible(not self.hidden and (self.isometric or not preview))
        self.invalidate(self.sceneRect(), QGraphicsScene.SceneLayer.AllLayers)

    def set_placing(self, area_m2: float | None) -> None:
        """Start placing a building this big, or resize the one being placed; None stops."""
        if area_m2 is None:
            self.placing = None
        elif self.placing is None:
            self.placing = Placement(area_m2)
        else:
            self.placing.resize(self.network, area_m2)
        self.update()
        self.placement_changed.emit()

    def grow_placing(self, steps: int) -> bool:
        """The wheel, once the building is set down: its circle of influence grows (up) or shrinks."""
        if self.placing is None or not self.placing.grow(self.network, steps):
            return False
        self.update()
        self.placement_changed.emit()
        return True

    def mousePressEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        # Clickable items carry (kind, id) in data(0); labels and paint don't.
        pos = event.scenePos()
        if self.placing is not None:  # placing an institution: set it down, then finish
            self.placing.move(self.network, pos.x(), pos.y())
            if self.placing.click(self.network):
                self.placed.emit()
            self.update()
            self.placement_changed.emit()
            event.accept()
            return
        if self.metro_deploying:  # deploying metro: every click is a stop (the window adds it on release)
            self._metro_press = pos
            event.accept()
            return
        if self.spawn_placing:  # placing pedestrian spawn points: every click is one (the window adds it)
            self.spawn_clicked.emit(pos.x(), pos.y())
            event.accept()
            return
        if self.background_tool is not None and self.background_tool.press(pos.x(), pos.y()):
            self._tool_held = True  # lining up the background: the network doesn't see the click
            event.accept()
            return
        targets = [tuple(item.data(0)) for item in self.items(pos) if item.data(0)]
        if self.drag_nodes and self.selection in targets:
            self._drag_from = pos  # grabbed the selected object
        elif not targets and self.box_select:  # on empty map: a click, or a selection box once it moves
            self._box = (pos, bool(event.modifiers() & (Qt.KeyboardModifier.ControlModifier
                                                        | Qt.KeyboardModifier.ShiftModifier)))
        else:
            kind, identifier = targets[0] if targets else ("", "")
            additive = bool(event.modifiers() & (Qt.KeyboardModifier.ControlModifier
                                                 | Qt.KeyboardModifier.ShiftModifier))
            self.clicked.emit(kind, identifier, pos.x(), pos.y(), additive)
            if kind == "bus_stop" and self.selection == (kind, identifier):  # selected by the click: drag slides it
                stop = self.network.bus_stops[identifier]  # type: ignore[union-attr]
                self._slide = (identifier, self.network.bus_stop_distance(stop) - self._along(stop, pos), False)  # type: ignore[union-attr]
        event.accept()

    def _along(self, stop: BusStop, pos: QPointF) -> float:
        """Metres along the stop's road nearest the point."""
        return nearest_along(self.network.road_points(self.network.road(stop.road)), (pos.x(), pos.y()))  # type: ignore[union-attr]

    def _metro_aim(self, event: QGraphicsSceneMouseEvent) -> tuple[float, float]:
        """The direction dragged from the stop pressed (a unit vector; Shift: to the nearest 15 degrees), or (0, 0)
        for a drag under METRO_AIM_PX: a plain click."""
        moved = event.screenPos() - event.buttonDownScreenPos(Qt.MouseButton.LeftButton)
        if hypot(moved.x(), moved.y()) < METRO_AIM_PX:
            return 0.0, 0.0
        dx, dy = event.scenePos().x() - self._metro_press.x(), event.scenePos().y() - self._metro_press.y()
        angle = atan2(dy, dx)
        if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            angle = round(angle / radians(15)) * radians(15)
        return cos(angle), sin(angle)

    def mouseMoveEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        if self._metro_press is not None:  # aiming the stop pressed: its leg follows the aim
            self.metro_aiming.emit(self._metro_press.x(), self._metro_press.y(), *self._metro_aim(event))
            event.accept()
            return
        if self.placing is not None:  # the footprint follows the mouse until it is set down
            self.placing.move(self.network, event.scenePos().x(), event.scenePos().y())
            self.update()
            self.placement_changed.emit()
            event.accept()
            return
        if self._tool_held:
            self.background_tool.move(event.scenePos().x(), event.scenePos().y())
            event.accept()
            return
        if self._box is not None:
            start, pos = self._box[0], event.scenePos()
            shift = event.screenPos() - event.buttonDownScreenPos(Qt.MouseButton.LeftButton)
            if self._band is None and shift.manhattanLength() < 4:
                return  # still a click
            crossing = shift.x() < 0  # screen right to left, including in the rotated orthographic view
            if self._band is None:
                self._band = QGraphicsRectItem()
                self._band.setZValue(20)
                self.addItem(self._band)
            colour = QColor(STATUS["passed"] if crossing else ACCENT_EDGE)
            pen = _screen_pen(colour, 1.5)
            pen.setStyle(Qt.PenStyle.DashLine if crossing else Qt.PenStyle.SolidLine)
            fill = QColor(colour)
            fill.setAlpha(40)
            self._band.setPen(pen)
            self._band.setBrush(fill)
            self._band.setRect(QRectF(start, pos).normalized())
            event.accept()
            return
        if self._slide is not None:
            identifier, grab, _ = self._slide
            stop = self.network.bus_stops[identifier]  # type: ignore[union-attr]
            self.network.move_bus_stop(identifier, self._along(stop, event.scenePos()) + grab)  # type: ignore[union-attr]
            self._slide = (identifier, grab, True)
            self.rebuild()
            event.accept()
            return
        if self._drag_from is None:
            super().mouseMoveEvent(event)
            return
        delta = event.scenePos() - self._drag_from
        if self.network.move_nodes(self.drag_nodes, delta.x(), delta.y()):  # type: ignore[union-attr]
            self._drag_from = event.scenePos()
            # ponytail: full redraw per mouse move; redraw only the touched roads if big grids lag.
            self.rebuild()
        event.accept()

    def mouseReleaseEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        if self._metro_press is not None and event.button() == Qt.MouseButton.LeftButton:
            press, aim = self._metro_press, self._metro_aim(event)
            self._metro_press = None
            self.metro_clicked.emit(press.x(), press.y(), *aim)
            event.accept()
            return
        if self._box is not None and event.button() == Qt.MouseButton.LeftButton:
            (start, additive), self._box = self._box, None
            if self._band is None:  # it didn't move: a click on empty map
                self.clicked.emit("", "", start.x(), start.y(), additive)
            else:
                rect = self._band.rect()
                self.removeItem(self._band)
                self._band = None
                self.boxed.emit(rect.left(), rect.top(), rect.right(), rect.bottom(),
                                event.screenPos().x() < event.buttonDownScreenPos(Qt.MouseButton.LeftButton).x(), additive)
            event.accept()
            return
        if self._tool_held and event.button() == Qt.MouseButton.LeftButton:
            self._tool_held = False
            self.background_tool.release(event.scenePos().x(), event.scenePos().y())
            event.accept()
            return
        if self._slide is not None and event.button() == Qt.MouseButton.LeftButton:
            moved, self._slide = self._slide[2], None
            if moved:
                self.drag_finished.emit()  # one undo step for the whole slide
            event.accept()
            return
        if self._drag_from is not None and event.button() == Qt.MouseButton.LeftButton:
            self._drag_from = None
            self.drag_finished.emit()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def rebuild(self) -> None:
        self.clear()
        self._metro_ghost = None
        self._footpath_preview = []
        self._building_items = []
        self._metro_items, self._metro_labels = [], []
        self._overbridges = []
        self.signal_lamps = {}
        if self.network is None:
            return
        # Roundabouts are sized from their approach roads (see roundabouts.py).
        self._roundabouts = {node.id: design_roundabout(self.network, node.id)
                             for node in self.network.nodes.values() if node.control == "roundabout"}
        roads_at: dict[str, list[Road]] = {}
        for road in self.network.all_roads():
            self._draw_road(road)
            roads_at.setdefault(road.from_node, []).append(road)
            roads_at.setdefault(road.to_node, []).append(road)
        for stop in self.network.bus_stops.values():
            self._draw_bus_stop(stop)
        for node in self.network.nodes.values():
            self._draw_node(node, roads_at.get(node.id, []))
        for road in self.network.approach_roads.values():
            self._draw_end(road)
        self._zone = None
        for item in self.network.institutions.values():
            self._draw_institution(item)
        self._draw_walkways()
        self._draw_crossings()
        self._draw_spawn_points()
        self._draw_metro()
        bounds = self.itemsBoundingRect()
        margin = max(80.0, min(max(bounds.width(), bounds.height()) * 0.05, 500.0))
        if bounds.isNull():
            bounds = QRectF(-100, -100, 200, 200)
        self.network_rect = bounds  # what Fit Network fits (the vehicle layer covers the whole scene)
        rect = bounds.adjusted(-margin, -margin, margin, margin)
        self.setSceneRect(self.extent(rect) if self.extent else rect)  # with the background's image or map
        for item in self._building_items:
            item.setVisible(self.buildings_visible)
        self.project_building_labels()
        for item in self.items():  # extensions' items come later (redrawn): they never join it
            if item.zValue() < VEHICLE_Z:
                item.setData(STILL, True)
        for lamp, _ in self.signal_lamps.values():
            lamp.setData(STILL, False)  # a run lights them each frame
        if self.still_cached:
            self._hold_still()
        self.vehicle_layer = VehicleLayer(self)
        self.addItem(self.vehicle_layer)
        states, self._signal_states = self._signal_states, None
        self.show_signals(states)  # the new signal heads show the run's states too
        self.redrawn.emit()
        if self.hidden:  # drawn (so Fit still knows where it is), then hidden
            for item in self.items():
                item.hide()

    def show_signals(self, states: dict | None) -> None:
        """Light each signal head from a run's states, (junction, road) -> "G", "Y" or "R";
        None: the static layout colours (east-west green) while nothing runs."""
        if states == self._signal_states and self.signal_lamps:
            return
        self._signal_states = states
        colours = {"G": SIGNAL_GREEN, "Y": SIGNAL_AMBER, "R": SIGNAL_RED}
        for key, (lamp, idle) in self.signal_lamps.items():
            lamp.setBrush(colours[states[key]] if states and key in states else idle)

    def show_frame(self, alpha: float, frame=None) -> None:
        """Draw a published frame of a run (None: no run): vehicles part way to their latest
        step, signals lit. Frames the physics published in between are simply skipped."""
        self.frame, self.frame_alpha = frame, alpha
        self.cache_still(frame is not None)
        self.show_signals(frame.signals if frame else None)
        self.vehicle_layer.update()
        for bridge in self._overbridges:
            bridge.update()

    def cache_still(self, on: bool) -> None:
        """During a run the still layer is drawn once with the background (drawBackground) and the view
        keeps it (QGraphicsView.CacheBackground) until the view moves or zooms or the map is rebuilt, so a
        display frame draws only what moves. Its items still take clicks and tooltips; they just don't
        paint themselves. Edit mode, with no frames to draw, paints them as ever."""
        if on != self.still_cached:
            self.still_cached = on
            self._hold_still()
            self.still_cached_changed.emit(on)

    def _hold_still(self) -> None:
        for item in self.items():
            if item.data(STILL):
                item.setFlag(QGraphicsItem.GraphicsItemFlag.ItemHasNoContents, self.still_cached)
        self.invalidate(self.sceneRect(), QGraphicsScene.SceneLayer.BackgroundLayer)  # the views' caches

    def drawBackground(self, painter: QPainter, rect: QRectF) -> None:  # type intentionally inherited from Qt
        super().drawBackground(painter, rect)
        self.paint_still(painter, rect)

    def paint_still(self, painter: QPainter, rect: QRectF) -> None:
        """While a run is on: the still layer's items over `rect`, in their stacking order."""
        if not self.still_cached:
            return
        t = painter.worldTransform()
        pad = 4 / max(hypot(t.m11(), t.m12()), 1e-9)  # screen-sized outlines reach a few pixels past an item
        option = QStyleOptionGraphicsItem()
        for item in self.items(rect.adjusted(-pad, -pad, pad, pad), Qt.ItemSelectionMode.IntersectsItemBoundingRect,
                               Qt.SortOrder.AscendingOrder):
            if item.data(STILL) and item.isVisible():
                option.exposedRect = item.boundingRect()
                painter.save()
                painter.setTransform(item.sceneTransform(), True)
                item.paint(painter, option)
                painter.restore()

    def _keep_side(self) -> float:
        """+1 when traffic keeps left, -1 when it keeps right."""
        return 1.0 if self.network.driving_side == "left" else -1.0  # type: ignore[union-attr]

    def _add_label(self, text: str, x: float, y: float, dx: float, dy: float,
                   pixels: int = 13, family: str = "Segoe UI") -> QGraphicsTextItem:
        """Screen-sized label placed beside world point (x, y), pushed towards (dx, dy).

        (dx, dy) is -1/0/1 per axis (or a unit vector): the label's near edge
        sits a few pixels from the anchor on that side, so it never covers
        the road the anchor was chosen beside. (0, 0) centres it on the point.
        """
        label = QGraphicsTextItem(text)
        label.setDefaultTextColor(QColor("#f4f7f2"))
        label.document().setDocumentMargin(0)
        font = QFont(family)
        font.setPixelSize(pixels)
        font.setBold(family == "Segoe UI")
        label.setFont(font)
        label.setTextWidth(label.document().idealWidth() + 1)
        option = label.document().defaultTextOption()
        option.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        label.document().setDefaultTextOption(option)
        label.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations)
        label.setPos(x, y)
        width, height, gap = label.boundingRect().width(), label.boundingRect().height(), 4
        label.setTransform(QTransform.fromTranslate(dx * (width / 2 + gap) - width / 2,
                                                    dy * (height / 2 + gap) - height / 2))
        label.setZValue(10)
        self.addItem(label)
        return label

    def _draw_road(self, road: Road) -> None:
        points = self.network.road_points(road)  # type: ignore[union-attr]
        lanes, total_width = road.lanes, road.width_m
        one_way = road.direction == "one-way"
        selected = self.selection == ("road", road.id) or ("road", road.id) in self.group
        centre = _path(points)

        surface = QGraphicsPathItem(centre)
        surface.setPen(QPen(QColor("#f7d91e") if selected else ROAD_COLOR, total_width,
                            Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap, Qt.PenJoinStyle.RoundJoin))
        lift = OVERPASS_LIFT if road.overpass else 0.0  # bridges draw above the roads they cross
        surface.setZValue(1 + lift)
        self.addItem(surface)
        length = self.network.road_geometry_length(road)  # type: ignore[union-attr]
        if road.overpass:
            self._draw_parapets(road, points, total_width, lift)

        for lane_index in range(1, lanes):
            offset = (lane_index - lanes / 2) * LANE_WIDTH_M
            # A two-way road's middle line is the hard divider. A one-way road
            # merges both halves into one carriageway, so it has none.
            divider = lane_index * 2 == lanes and not one_way
            dashed = not divider and road.lane_changing  # dashed = lane changing allowed
            pen = QPen(QColor("#f7cf28") if divider else QColor("#bcc5c5"),
                       CENTRE_LINE_WIDTH if divider else LANE_LINE_WIDTH,
                       Qt.PenStyle.DashLine if dashed else Qt.PenStyle.SolidLine,
                       Qt.PenCapStyle.FlatCap, Qt.PenJoinStyle.RoundJoin)
            if dashed:  # Qt dash lengths are in pen widths
                pen.setDashPattern([LANE_DASH_M / LANE_LINE_WIDTH, LANE_GAP_M / LANE_LINE_WIDTH])
            marking = QGraphicsPathItem(_path(offset_polyline(points, offset)))
            marking.setPen(pen)
            marking.setZValue(2 + lift)
            self.addItem(marking)
        for end, pocket in road.pockets.items():
            self._draw_pocket(road, points, end, pocket, lift)
        for side in road.footpaths:  # paving beside the carriageway (and any cycle lane): walking.py's own line
            stroker = QPainterPathStroker()
            stroker.setWidth(FOOTPATH_M)
            stroker.setCapStyle(Qt.PenCapStyle.FlatCap)
            stroker.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            paving = PavingItem(stroker.createStroke(_path(self.network.footpath_line(road, side))))
            paving.setZValue(1 + lift)
            self.addItem(paving)
        for end in road.cycle_lanes:  # a green strip beyond the kerb lane (the lane graph's own line)
            strip = QGraphicsPathItem(_path(self.network.cycle_lane_line(road, end)))  # type: ignore[union-attr]
            strip.setPen(QPen(QColor(CYCLE_LANE), CYCLE_LANE_M, Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap,
                              Qt.PenJoinStyle.RoundJoin))
            strip.setZValue(1 + lift)
            self.addItem(strip)

        # Stop lines cross only the lanes arriving at each junction end. Traffic
        # travelling along the road (u) keeps to -n on the left, so arrivals at
        # the to-end use -n and arrivals at the from-end +n. Boundary ends,
        # merges and bends (uncontrolled nodes) get none; roundabout entries get
        # a dashed give-way line at the circle's edge.
        half = total_width / 2
        side = self._keep_side()

        def stop_at(node_id: str) -> tuple[float, bool] | None:
            """(inset from the node, give way?) for the line at this end, or None."""
            node = self.network.nodes.get(node_id)  # type: ignore[union-attr]
            if node is None or node.control == "uncontrolled":
                return None
            if node_id in self._roundabouts:
                return self._roundabouts[node_id].inscribed_diameter_m / 2 + 0.5, True
            return stop_line_inset(total_width, length), False

        stops = []
        if (to_end := stop_at(road.to_node)) is not None:
            x, y, back_x, back_y = point_along(points[::-1], to_end[0])
            stops.append((x, y, -back_x, -back_y, -side, to_end[1], road.to_node))
        if not one_way and (from_end := stop_at(road.from_node)) is not None:
            stops.append((*point_along(points, from_end[0]), side, from_end[1], road.from_node))
        for anchor_x, anchor_y, ux, uy, lane_side, give_way, end in stops:
            nx, ny = -uy, ux
            wider = road.pockets[end]["lanes"] * LANE_WIDTH_M if end in road.pockets else 0.0  # across a turn pocket
            near, far = -lane_side * half if one_way else 0.0, lane_side * (half + wider)
            stop_line = QGraphicsLineItem(anchor_x + nx * near, anchor_y + ny * near,
                                          anchor_x + nx * far, anchor_y + ny * far)
            pen = QPen(QColor("#ffffff"), STOP_LINE_WIDTH, Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap)
            if give_way:  # 0.6 m marks, 0.3 m gaps; Qt dash lengths are in pen widths
                pen.setDashPattern([0.6 / STOP_LINE_WIDTH, 0.3 / STOP_LINE_WIDTH])
            stop_line.setPen(pen)
            stop_line.setZValue(3 + lift)
            self.addItem(stop_line)

        if one_way:  # painted arrow in every lane at mid-block
            mid_x, mid_y, ux, uy = point_along(points, length / 2)
            for lane in range(lanes):
                offset = (lane + 0.5 - lanes / 2) * LANE_WIDTH_M
                self._add_lane_arrow(mid_x - uy * offset, mid_y + ux * offset, ux, uy, 2 + lift)

        hit = QGraphicsPathItem(centre)
        hit.setPen(QPen(QColor(0, 0, 0, 0), total_width, Qt.PenStyle.SolidLine,
                        Qt.PenCapStyle.FlatCap, Qt.PenJoinStyle.RoundJoin))
        hit.setData(0, ("road", road.id))
        hit.setZValue(4 + lift)
        self.addItem(hit)

    def _draw_pocket(self, road: Road, points: Points, end: str, pocket: dict, lift: float) -> None:
        """A turn pocket (Road.pockets) as simulation.py lays out its lanes: the carriageway widened on its
        traffic's kerb side, over a taper and then the pocket, a lane line beside each lane it adds and a turn
        arrow in each of the pocket's lanes."""
        line = points if end == road.to_node else points[::-1]  # its traffic's way
        total, keep, extra = polyline_length(line), self._keep_side(), pocket["lanes"]
        stop = total - self.network.lane_inset(end, road)  # type: ignore[union-attr]
        opens = stop - pocket["length_m"]
        count = road.lanes_each_way
        half = count / 2 if road.direction == "one-way" else count  # lanes between the centre line and the kerb

        def at(d: float, lanes_out: float) -> tuple[float, float]:  # `lanes_out` lane widths kerbward of the centre
            x, y, ux, uy = point_along(line, d)
            return x + uy * keep * lanes_out * LANE_WIDTH_M, y - ux * keep * lanes_out * LANE_WIDTH_M

        along = [opens - POCKET_TAPER_M + (total - opens + POCKET_TAPER_M) * i / 40 for i in range(41)]
        outer = [at(d, half + extra * min(max((d - opens + POCKET_TAPER_M) / POCKET_TAPER_M, 0.0), 1.0)) for d in along]
        surface = QGraphicsPolygonItem(QPolygonF([QPointF(*p) for p in [at(d, half) for d in along] + outer[::-1]]))
        surface.setBrush(ROAD_COLOR)
        surface.setPen(QPen(Qt.PenStyle.NoPen))
        surface.setZValue(1 + lift)
        self.addItem(surface)
        pen = QPen(QColor("#bcc5c5"), LANE_LINE_WIDTH, Qt.PenStyle.DashLine, Qt.PenCapStyle.FlatCap)
        pen.setDashPattern([LANE_DASH_M / LANE_LINE_WIDTH, LANE_GAP_M / LANE_LINE_WIDTH])
        for k in range(extra):  # the old kerb edge, and between two pocket lanes
            marking = QGraphicsPathItem(_path([at(opens + (stop - opens) * i / 20, half + k) for i in range(21)]))
            marking.setPen(pen)
            marking.setZValue(2 + lift)
            self.addItem(marking)
        kerb = pocket["side"] == "kerb"
        for k in range(extra):  # lane k's centre (lane_graph.Graph._add_links's offsets in the pocket)
            index = k if kerb else count + k
            x, y = at(stop - 6.0, half + extra - 0.5 - index)
            _, _, ux, uy = point_along(line, stop - 6.0)
            turn = 0.6 if kerb else -0.6  # leaning towards the kerb or the offside
            dx, dy = ux + uy * keep * turn, uy - ux * keep * turn
            size = hypot(dx, dy)
            self._add_lane_arrow(x, y, dx / size, dy / size, 2 + lift)

    def _draw_bus_stop(self, stop: BusStop) -> None:
        """A bus stop: a bay beside the kerb of each direction it serves, the shape buses drive
        (bay_depth), with a broken line where it meets the lane and a flag where buses stop.
        Selected, it shows its distance from the node at each end of its road."""
        network = self.network
        road = network.road(stop.road)  # type: ignore[union-attr]
        points = network.road_points(road)  # type: ignore[union-attr]
        length = network.road_geometry_length(road)  # type: ignore[union-attr]
        middle = network.bus_stop_distance(stop)  # type: ignore[union-attr]
        half = road.width_m / 2
        lift = OVERPASS_LIFT if road.overpass else 0.0
        selected = self.selection == ("bus_stop", stop.id)
        outlined = selected or ("bus_stop", stop.id) in self.group
        start = middle - BAY_LENGTH_M / 2
        tooltip = f"Bus stop {stop.id}"
        sides = [(forward, -self._keep_side() if forward else self._keep_side())  # the kerb of that traffic
                 for forward in network.bus_stop_sides(stop)]  # type: ignore[union-attr]
        for forward, side in sides:
            def at(d: float, out: float) -> QPointF:  # `out` metres from the centre line on this kerb's side
                x, y, ux, uy = point_along(points, d)
                return QPointF(x - uy * side * out, y + ux * side * out)

            along = [start + BAY_LENGTH_M * i / 40 for i in range(41)]
            into = [d - start if forward else start + BAY_LENGTH_M - d for d in along]  # metres in, its traffic's way
            outer = [at(d, half + BAY_WIDTH_M * bay_depth(t)) for d, t in zip(along, into)]
            inner = [at(d, half - 0.1) for d in along]
            bay = QGraphicsPolygonItem(QPolygonF(inner + outer[::-1]))
            bay.setBrush(ROAD_COLOR)
            bay.setPen(_screen_pen(SELECTED_COLOR, 3) if outlined else QPen(Qt.PenStyle.NoPen))
            bay.setData(0, ("bus_stop", stop.id))
            bay.setToolTip(tooltip)
            bay.setZValue(1 + lift)
            self.addItem(bay)
            edge = QGraphicsPathItem(_path([(p.x(), p.y()) for p in (at(d, half) for d in along)]))
            pen = QPen(BUS_STOP_PAINT, LANE_LINE_WIDTH * 2, Qt.PenStyle.DashLine, Qt.PenCapStyle.FlatCap)
            pen.setDashPattern([1.0 / (LANE_LINE_WIDTH * 2), 1.0 / (LANE_LINE_WIDTH * 2)])  # 1 m marks, 1 m gaps
            edge.setPen(pen)
            edge.setZValue(2 + lift)
            self.addItem(edge)
            t_stop = BAY_ENTRY_M + BAY_STOP_M - 1.0  # where a bus's front stops (as in the simulation)
            flag = at(start + t_stop if forward else start + BAY_LENGTH_M - t_stop, half + BAY_WIDTH_M + 0.9)
            sign = QGraphicsEllipseItem(flag.x() - 0.7, flag.y() - 0.7, 1.4, 1.4)
            sign.setBrush(BUS_STOP_PAINT)
            sign.setPen(QPen(QColor("#121719"), 0.15))
            sign.setToolTip(tooltip)
            sign.setZValue(6 + lift)
            self.addItem(sign)
        if not selected:
            return
        # Dimension lines out beyond the bay, from each end node to its middle, labelled in metres.
        side, out = sides[0][1], half + BAY_WIDTH_M + 3.0
        for a, b, node_id in ((0.0, middle, road.from_node), (middle, length, road.to_node)):
            line = QGraphicsPathItem(_path(offset_polyline(sub_polyline(points, a, b), side * out)))
            line.setPen(_screen_pen(SELECTED_COLOR, 1.5))
            line.setZValue(10)
            self.addItem(line)
            x, y, ux, uy = point_along(points, (a + b) / 2)
            nx, ny = -uy * side, ux * side
            self._add_label(f"{b - a:.1f} m from {node_id}", x + nx * out, y + ny * out, nx, ny)
        for d in (0.0, middle, length):  # ticks across the line at its ends and at the stop
            x, y, ux, uy = point_along(points, d)
            nx, ny = -uy * side, ux * side
            tick = QGraphicsLineItem(x + nx * (out - 1.5), y + ny * (out - 1.5), x + nx * (out + 1.5), y + ny * (out + 1.5))
            tick.setPen(_screen_pen(SELECTED_COLOR, 1.5))
            tick.setZValue(10)
            self.addItem(tick)

    def _draw_metro(self) -> None:
        """The same saved stop coordinates drive the flat markers and display-only overhead structures."""
        network = self.network
        for line in network.metro_lines.values():
            stops = [network.metro_stops[stop_id] for stop_id in line.stops]
            item = MetroItem(self, [(stop.x, stop.y) for stop in stops], line.cars_per_train, network.metro_track(line.id))
            item.setToolTip("Overhead metro (assumed heights)")
            self.addItem(item)
            self._metro_items.append(item)
            for stop in stops:
                self._metro_label(stop.id, stop.x, stop.y - METRO_STOP_RADIUS_M, metro_assets.CANOPY_M)
            first = stops[0]
            self._metro_label(f"{line.id} train preview", first.x, first.y, metro_assets.TRAIN_BASE_M + metro_assets.TRAIN_HEIGHT_M,
                              preview=True)
        if self.metro_pending is not None:
            item = MetroItem(self, [self.metro_pending])
            item.setToolTip("First stop: choose next")
            self.addItem(item)
            self._metro_items.append(item)
        self.project_metro()

    def _draw_spawn_points(self) -> None:
        """Each pedestrian spawn point: a disc in the walkers' colour with its name, and a dashed line to where its
        walkers join the footpaths, the nearest place on any footpath or path (walking.WalkGraph.attach's way);
        clickable as ("spawn", id), outlined when selected."""
        for point in self.network.spawn_points.values():  # type: ignore[union-attr]
            near = footpath_join(self.network, point.x, point.y)  # type: ignore[arg-type]
            if near is not None:
                link = QGraphicsPathItem(_path([(point.x, point.y), near]))
                pen = _screen_pen(QColor(WALKER), 1.5)
                pen.setStyle(Qt.PenStyle.DashLine)
                link.setPen(pen)
                link.setZValue(VEHICLE_Z + 1)
                self.addItem(link)
            selected = self.selection == ("spawn", point.id)
            disc = QGraphicsEllipseItem(QRectF(-7.0, -7.0, 14.0, 14.0))  # 14 pixels at any zoom: a pin, not a footprint
            disc.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations)
            disc.setPos(point.x, point.y)
            disc.setBrush(QColor(WALKER))
            disc.setPen(_screen_pen(QColor(ACCENT_EDGE) if selected else QColor(ROAD_COLOR), 2.5 if selected else 1.5))
            disc.setZValue(VEHICLE_Z + 2)
            disc.setData(0, ("spawn", point.id))
            disc.setToolTip(f"Spawn point {point.id}")
            self.addItem(disc)
            label = self._add_label(point.id, point.x, point.y, 0, -1, pixels=11)
            label.setTransform(QTransform.fromTranslate(0, -6), True)  # a pixel gap from the screen-sized pin

    def _draw_crossings(self) -> None:
        """Each pedestrian crossing across its road: a zebra's white stripes on the carriageway, an overbridge's
        deck above the traffic (from footpath to footpath); clickable as ("crossing", id), outlined when selected."""
        for crossing in self.network.pedestrian_crossings.values():  # type: ignore[union-attr]
            road = self.network.road(crossing.road)  # type: ignore[union-attr]
            x, y, ux, uy = point_along(self.network.road_points(road), crossing.distance_m)  # type: ignore[union-attr]
            nx, ny = -uy, ux
            selected = self.selection == ("crossing", crossing.id)
            zebra = crossing.kind == "zebra"
            half = road.width_m / 2 + (0.0 if zebra else FOOTPATH_M + CYCLE_LANE_M * bool(road.cycle_lanes))
            along = 1.5  # half its width along the road (simulation.ZEBRA_HALF_M)
            outline = QPolygonF([QPointF(x + nx * a * half + ux * b * along, y + ny * a * half + uy * b * along)
                                 for a, b in ((-1, -1), (1, -1), (1, 1), (-1, 1))])
            if zebra:
                stripes = QPainterPath()
                offset = -half + 0.25
                while offset < half:
                    stripes.moveTo(x + nx * offset - ux * along, y + ny * offset - uy * along)
                    stripes.lineTo(x + nx * offset + ux * along, y + ny * offset + uy * along)
                    offset += 1.0
                marks = QGraphicsPathItem(stripes)
                marks.setPen(QPen(QColor("#f5f5f5"), 0.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap))
                marks.setZValue(2.5)
                self.addItem(marks)
            item = QGraphicsPolygonItem(outline) if zebra else OverbridgeItem(self, outline, x, y, ux, uy, half, selected)
            if not zebra:
                self._overbridges.append(item)
            item.setBrush(Qt.BrushStyle.NoBrush if zebra else QColor(FOOTPATH))
            item.setPen(_screen_pen(QColor("#f7d91e"), 2) if selected else
                        (QPen(Qt.PenStyle.NoPen) if zebra else _screen_pen(QColor(ROAD_COLOR), 1.5)))
            item.setZValue(2.6 if zebra else VEHICLE_Z + 1)  # a bridge over the traffic
            item.setData(0, ("crossing", crossing.id))
            item.setToolTip("Zebra crossing" if zebra else "Pedestrian overbridge")
            self.addItem(item)

    def _draw_walkways(self) -> None:
        """Each path (Network.walkways) as paving its width wide, under the roads it meets (clickable: selected,
        Delete removes it), and the one being drawn, dashed."""
        for way in self.network.walkways.values():  # type: ignore[union-attr]
            selected = self.selection == ("walkway", way.id)
            stroker = QPainterPathStroker()  # its outline filled: only the paving takes a click, not what it bends round
            stroker.setWidth(FOOTPATH_M)
            stroker.setCapStyle(Qt.PenCapStyle.RoundCap)
            stroker.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            line = self.network.walkway_points(way)  # type: ignore[union-attr]
            item = PavingItem(stroker.createStroke(_path(line)), selected)
            item.setZValue(0.9)
            item.setData(0, ("walkway", way.id))
            item.setToolTip(f"Path {way.id}")
            self.addItem(item)

    def preview_footpaths(self, road_id: str, sides: tuple) -> None:
        """Highlight where the footpaths chosen for a road would go (the road panel's choice, before Apply): each
        side's paving outlined in the selection colour, a side that would go dashed; nothing for an unknown road."""
        for item in self._footpath_preview:
            if item.scene() is self:
                self.removeItem(item)
        self._footpath_preview = []
        try:
            road = self.network.road(road_id)  # type: ignore[union-attr]
        except KeyError:
            return
        for side in ("left", "right"):
            if side not in sides and side not in road.footpaths:
                continue
            stroker = QPainterPathStroker()
            stroker.setWidth(FOOTPATH_M)
            stroker.setCapStyle(Qt.PenCapStyle.FlatCap)
            item = QGraphicsPathItem(stroker.createStroke(_path(self.network.footpath_line(road, side))))  # type: ignore[union-attr]
            colour = QColor(ACCENT_EDGE)
            pen = _screen_pen(colour, 2)
            if side not in sides:  # there now, gone on Apply
                pen.setStyle(Qt.PenStyle.DashLine)
            else:
                colour.setAlpha(110)
                item.setBrush(colour)
            item.setPen(pen)
            item.setZValue(21)
            self.addItem(item)
            self._footpath_preview.append(item)

    def show_metro_ghost(self, points: list, state: str = "ok", handle: tuple | None = None) -> None:
        """While deploying a line: the leg the next click would add and its stop, dashed, before it is placed (amber:
        it crosses another line; red: refused); [] hides it. `handle`, (x, y, dx, dy): a stop being aimed, drawn as
        the pen tool draws a smooth point's handles, the same length both ways."""
        if self._metro_ghost is None:
            self._metro_ghost = QGraphicsPathItem()
            self._metro_ghost.setZValue(21)
            self.addItem(self._metro_ghost)
        path = _path(points) if points else QPainterPath()
        if points:
            path.addEllipse(QPointF(*points[-1]), METRO_STOP_RADIUS_M, METRO_STOP_RADIUS_M)
        if handle is not None:
            x, y, dx, dy = handle
            reach = METRO_HANDLE_M
            path.moveTo(x - dx * reach, y - dy * reach)
            path.lineTo(x + dx * reach, y + dy * reach)
            for sign in (-1, 1):
                path.addEllipse(QPointF(x + sign * dx * reach, y + sign * dy * reach), 4.0, 4.0)
        pen = _screen_pen(QColor({"ok": METRO_LINE, "crossing": STATUS["waiting"], "refused": DANGER}[state]), 3)
        pen.setStyle(Qt.PenStyle.DashLine)
        self._metro_ghost.setPen(pen)
        self._metro_ghost.setPath(path)
        self._metro_ghost.setData(3, state)
        self._metro_ghost.setPos(projection_lift() * metro_assets.TRACK_M
                                if self.isometric else QPointF())

    def _metro_label(self, name, x, y, height, preview=False):
        label = self._add_label(name, x, y, 0, -1, pixels=11)
        label.setData(2, (x, y, height, preview))
        self._metro_labels.append(label)

    def nudge_bus_stop(self, dx: float, dy: float, metres: float) -> bool:
        """Move the selected bus stop `metres` along its road the way its road runs nearest the
        screen direction (dx, dy) (an arrow key). False if nothing moved: no bus stop selected, or
        the key points across the road."""
        if not self.selection or self.selection[0] != "bus_stop":
            return False
        network = self.network
        stop = network.bus_stops[self.selection[1]]  # type: ignore[union-attr]
        middle = network.bus_stop_distance(stop)  # type: ignore[union-attr]
        _, _, ux, uy = point_along(network.road_points(network.road(stop.road)), middle)  # type: ignore[union-attr]
        towards = dx * ux + dy * uy
        if abs(towards) < 0.2:
            return False
        network.move_bus_stop(stop.id, middle + (metres if towards > 0 else -metres))  # type: ignore[union-attr]
        self.bus_stop_moved.emit(stop.id)
        return True

    def _draw_parapets(self, road: Road, points, width: float, lift: float) -> None:
        """Bridge edges along an overpass wherever it crosses a road below it."""
        for distance, _, _, lower_id in self.network.crossings(points, {road.from_node, road.to_node}):  # type: ignore[union-attr]
            lower = self.network.road(lower_id)  # type: ignore[union-attr]
            if lower.overpass:
                continue
            span = lower.width_m / 2 + 3
            x, y, ux, uy = point_along(points, distance)
            for side in (-1, 1):
                edge_x, edge_y = -uy * side * (width / 2 + 0.3), ux * side * (width / 2 + 0.3)
                parapet = QGraphicsLineItem(x + edge_x - ux * span, y + edge_y - uy * span,
                                            x + edge_x + ux * span, y + edge_y + uy * span)
                parapet.setPen(QPen(PARAPET_COLOR, 0.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap))
                parapet.setZValue(1.5 + lift)
                self.addItem(parapet)

    def _add_lane_arrow(self, cx: float, cy: float, ux: float, uy: float, z: float = 2) -> None:
        """5 m ahead-only road arrow centred on (cx, cy), pointing along (ux, uy)."""
        nx, ny = -uy, ux

        def along(a: float, b: float) -> QPointF:  # a metres ahead, b metres across
            return QPointF(cx + ux * a + nx * b, cy + uy * a + ny * b)

        arrow = QGraphicsPolygonItem(QPolygonF([
            along(-2.5, -0.15), along(1.0, -0.15), along(1.0, -0.6), along(2.5, 0.0),
            along(1.0, 0.6), along(1.0, 0.15), along(-2.5, 0.15),
        ]))
        arrow.setBrush(PAINT_COLOR)
        arrow.setPen(QPen(Qt.PenStyle.NoPen))
        arrow.setZValue(z)
        self.addItem(arrow)

    def _draw_node(self, node: Node, roads: list[Road]) -> None:
        arms = []  # (unit vector away from the node, road)
        for road in roads:
            points = self.network.road_points(road)  # type: ignore[union-attr]
            next_x, next_y = points[1] if road.from_node == node.id else points[-2]  # curves leave along their shape
            length = hypot(next_x - node.x, next_y - node.y) or 1.0
            arms.append(((next_x - node.x) / length, (next_y - node.y) / length, road))
        road_width = max([road.width_m
                          for *_, road in arms] or [2 * LANE_WIDTH_M])
        # Merges and bends are just carriageway; junctions get a little extra for corner space.
        diameter = road_width * (1.0 if node.control == "uncontrolled" else 1.28)
        if node.control == "roundabout":
            design = self._roundabouts[node.id]  # true-scale design diameter
            diameter = design.inscribed_diameter_m
            rect = QRectF(node.x - diameter / 2, node.y - diameter / 2, diameter, diameter)
            item = QGraphicsEllipseItem(rect)
            ring = design.circulatory_width_m
            island = QGraphicsEllipseItem(rect.adjusted(ring, ring, -ring, -ring))
            # A mini-roundabout's island is painted and drive-over; others are landscaped.
            island.setBrush(QColor("#c9ced3") if design.mini else QColor("#0b6a2c"))
            island.setPen(QPen(QColor("#eef5ed"), 0.3))
            island.setZValue(6)
            self.addItem(island)
        else:
            rect = QRectF(node.x - diameter / 2, node.y - diameter / 2, diameter, diameter)
            item = QGraphicsRectItem(rect)
            if node.control == "signalised":
                for ux, uy, road in arms:
                    if road.direction == "two-way" or road.to_node == node.id:  # traffic arrives here
                        self._draw_signal(node, ux, uy, road, diameter / 2)
                if node.signal_method in ADAPTIVE:
                    self._draw_detectors(node, roads)
        selected = self.selection == ("node", node.id) or node.id in self.multi_nodes or ("node", node.id) in self.group
        item.setData(0, ("node", node.id))
        item.setZValue(5)
        self.addItem(item)
        if node.control == "uncontrolled":
            # Merges, diverges and bends read as plain carriageway: a joint disc under
            # the lane markings fills the seam; the box above only takes clicks.
            joint = QGraphicsEllipseItem(rect)
            joint.setBrush(ROAD_COLOR)
            joint.setPen(QPen(Qt.PenStyle.NoPen))
            joint.setZValue(1.5)
            self.addItem(joint)
            item.setBrush(Qt.BrushStyle.NoBrush)
            item.setPen(_screen_pen(SELECTED_COLOR, 3) if selected else QPen(Qt.PenStyle.NoPen))
            return
        item.setBrush(QColor("#343c3e"))
        item.setPen(_screen_pen(SELECTED_COLOR if selected else QColor("#ffeb2d"), 3 if selected else 1.5))
        # In the grass off the top-left corner.
        self._add_label(self.network.node_label(node),  # type: ignore[union-attr]
                        node.x - diameter / 2, node.y - diameter / 2, -1, -1)

    def _draw_detectors(self, node: Node, roads: list[Road]) -> None:
        """An adaptive signal's loop detectors, outlined on every lane arriving at it where the
        simulation reads them: SCATS's at the stop line, SCOOT's near the road's upstream end."""
        network, keep = self.network, self._keep_side()
        scats = node.signal_method == "scats"
        tip = "SCATS stop-line detector" if scats else "SCOOT upstream detector"
        for road in roads:
            arriving = road.to_node == node.id
            if not arriving and road.direction == "one-way":
                continue
            points = network.road_points(road)  # type: ignore[union-attr]
            line = points if arriving else points[::-1]
            upstream = road.from_node if arriving else road.to_node
            start = network.lane_inset(upstream, road)  # type: ignore[union-attr]
            stop = network.lane_inset(node.id, road)  # type: ignore[union-attr]
            count = road.lanes_each_way
            half = count / 2 if road.direction == "one-way" else count
            # SCATS's loops at the stop line: across a turn pocket's lanes too, where the pocket has moved them
            extra = road.pockets[node.id]["lanes"] if scats and node.id in road.pockets else 0
            for index in range(count + extra):
                lane = offset_polyline(line, -keep * (half + extra - 0.5 - index) * LANE_WIDTH_M)  # as simulation.py
                end = polyline_length(lane) - stop
                if scats:
                    first, last = end - STOP_LINE_LOOP_M, end
                else:
                    first = start + min(UPSTREAM_LOOP_AT_M, (end - start) / 3)
                    last = first + UPSTREAM_LOOP_M
                piece = sub_polyline(lane, first, last)
                outline = offset_polyline(piece, 0.8) + offset_polyline(piece, -0.8)[::-1]
                loop = QGraphicsPolygonItem(QPolygonF([QPointF(x, y) for x, y in outline]))
                loop.setPen(_screen_pen(DETECTOR_COLOR, 1))
                loop.setToolTip(tip)
                loop.setZValue(2.5 + (OVERPASS_LIFT if road.overpass else 0.0))
                self.addItem(loop)

    def _draw_signal(self, node: Node, ux: float, uy: float, road: Road,
                     junction_half: float) -> None:
        """Signal head on the nearside kerb of an incoming arm, at its stop line."""
        width = road.width_m
        # Keep the head clear of the junction box when the stop line is close to it.
        inset = max(stop_line_inset(width, self.network.road_geometry_length(road)),  # type: ignore[union-attr]
                    junction_half + SIGNAL_SIZE / 2 + 0.5)
        # Incoming traffic travels (-ux, -uy); its left kerb lies along (-uy, ux).
        # A one-way road's single carriageway uses the same kerb.
        side = self._keep_side()
        kerb = width / 2 + SIGNAL_SIZE / 2 + 0.5
        cx = node.x + ux * inset - uy * kerb * side
        cy = node.y + uy * inset + ux * kerb * side
        signal = QGraphicsEllipseItem(cx - SIGNAL_SIZE / 2, cy - SIGNAL_SIZE / 2,
                                      SIGNAL_SIZE, SIGNAL_SIZE)
        signal.setBrush(QColor("#121719"))
        signal.setPen(QPen(QColor("#eef5ed"), 0.1))
        signal.setZValue(6)
        self.addItem(signal)
        lamp = QGraphicsEllipseItem(signal.rect().adjusted(0.25, 0.25, -0.25, -0.25))
        # While nothing runs: east-west arms green, north-south red (a run lights them for real).
        idle = SIGNAL_GREEN if abs(ux) >= abs(uy) else SIGNAL_RED
        lamp.setBrush(idle)
        lamp.setPen(QPen(Qt.PenStyle.NoPen))
        lamp.setZValue(7)
        self.addItem(lamp)
        self.signal_lamps[(node.id, road.id)] = (lamp, idle)

    def _draw_end(self, road: Road) -> None:
        """Boundary end: entry (supply) and exit (demand) markers on their own lanes.

        A one-way boundary road only carries traffic one way, so it gets just
        the entry or the exit marker, across its whole carriageway.
        """
        network = self.network
        if road.from_node in network.approach_nodes:  # type: ignore[union-attr]
            end, inner = network.node(road.from_node), network.node(road.to_node)  # type: ignore[union-attr]
        else:
            end, inner = network.node(road.to_node), network.node(road.from_node)  # type: ignore[union-attr]
        length = hypot(end.x - inner.x, end.y - inner.y)
        ux, uy = (end.x - inner.x) / length, (end.y - inner.y) / length  # outward
        width = road.width_m
        one_way = road.direction == "one-way"
        inbound = road.to_node == inner.id
        # Inbound traffic travels (-ux, -uy); its keep-side is (-uy, ux) when driving on the left.
        offset = 0.0 if one_way else width / 4 * self._keep_side()
        size = (width if one_way else width / 2) * 0.35
        base_x, base_y = end.x - ux * size * 2, end.y - uy * size * 2
        flows = []
        if not one_way or inbound:
            self._add_marker(base_x - uy * offset, base_y + ux * offset, -ux, -uy, size,
                             "entry", end, QColor("#ffe82c"),
                             f"{end.id} entry: set supply")
            flows.append(("in", end.supply_vph))
        if not one_way or not inbound:
            self._add_marker(base_x + uy * offset, base_y - ux * offset, ux, uy, size,
                             "exit", end, PAINT_COLOR,
                             f"{end.id} exit: set demand")
            flows.append(("out", end.demand_vph))

        text = end.id
        if any(value for _, value in flows):
            text += "\n" + " · ".join(f"{name} {value:g}" for name, value in flows)
        self._add_label(text, end.x, end.y, ux, uy)

    def _add_marker(self, cx: float, cy: float, dx: float, dy: float, size: float,
                    kind: str, node: Node, fill: QColor, tooltip: str) -> None:
        """Triangle pointing along (dx, dy), the direction of travel on its lanes."""
        tip = QPointF(cx + dx * size, cy + dy * size)
        back_x, back_y = cx - dx * size * 0.7, cy - dy * size * 0.7
        marker = QGraphicsPolygonItem(QPolygonF([
            tip,
            QPointF(back_x - dy * size, back_y + dx * size),
            QPointF(back_x + dy * size, back_y - dx * size),
        ]))
        selected = self.selection == (kind, node.id) or ("end", node.id) in self.group
        marker.setBrush(SELECTED_COLOR if selected else fill)
        marker.setPen(_screen_pen(QColor("#111719"), 1))
        marker.setToolTip(tooltip)
        marker.setData(0, (kind, node.id))
        marker.setZValue(9)
        self.addItem(marker)

    def _draw_institution(self, item: Institution) -> None:
        """A building at its true size, its kind's icon on it and its name above. Selected, its circle
        of influence and the junctions and ends it reaches show too (drawForeground)."""
        half = sqrt(item.area_m2) / 2
        selected = self.selection == ("institution", item.id)
        roof = BuildingItem(self, item)
        self.addItem(roof)
        self._building_items.append(roof)
        family = next((family for family in ICON_FONTS if family in QFontDatabase.families()), None)
        if family:
            self._building_items.append(self._add_label(chr(KINDS[item.kind].glyph), item.x, item.y, 0, 0, 18, family))
        self._building_items.append(self._add_label(f"{item.id} {item.kind}", item.x, item.y - half, 0, -1))
        for label in self._building_items[-2:]:
            if isinstance(label, QGraphicsTextItem):
                label.setData(2, (label.pos().x(), label.pos().y(), building_assets.height(item)))
        if selected:
            self._zone = (item.x, item.y, item.radius_m, links(self.network, item.x, item.y, item.radius_m))  # type: ignore[arg-type]

    def drawForeground(self, painter: QPainter, rect: QRectF) -> None:  # type intentionally inherited from Qt
        """Over everything: the building being placed (green where it can stand, red where it can't)
        and its circle once set down, or the selected building's circle."""
        placing = self.placing
        if placing is not None and placing.x is not None:
            half = sqrt(placing.area_m2) / 2
            footprint = QRectF(placing.x - half, placing.y - half, 2 * half, 2 * half)
            colour = QColor(SIGNAL_RED if placing.problem else SIGNAL_GREEN)  # vivid on grass, as in an RTS
            fill = QColor(colour)
            fill.setAlpha(130)
            painter.setPen(_screen_pen(colour, 2))
            painter.setBrush(fill)
            painter.drawRect(footprint)
            if placing.problem:  # cross-hatched too, so red and green differ without their colours
                painter.setBrush(QBrush(colour, Qt.BrushStyle.DiagCrossPattern))
                painter.drawRect(footprint)
            if placing.radius_m is not None:
                self._paint_zone(painter, placing.x, placing.y, placing.radius_m, placing.links)
        elif self._zone is not None and self.buildings_visible:
            self._paint_zone(painter, *self._zone)

    def _paint_zone(self, painter: QPainter, x: float, y: float, radius: float, reached: list[str]) -> None:
        """A circle of influence, a ring round each junction and end it reaches, and its radius in metres."""
        colour = QColor(ACCENT_EDGE)
        fill = QColor(colour)
        fill.setAlpha(35)
        pen = _screen_pen(colour, 2)
        pen.setStyle(Qt.PenStyle.DashLine)
        painter.setPen(pen)
        painter.setBrush(fill)
        painter.drawEllipse(QPointF(x, y), radius, radius)
        pixel = 1 / max(QStyleOptionGraphicsItem.levelOfDetailFromTransform(painter.worldTransform()), 1e-9)
        painter.setPen(_screen_pen(colour, 3))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        for node_id in reached:
            node = self.network.node(node_id)  # type: ignore[union-attr]
            painter.drawEllipse(QPointF(node.x, node.y), 12 * pixel, 12 * pixel)
        edge = painter.worldTransform().map(QPointF(x + radius, y))
        painter.save()
        painter.resetTransform()
        font = QFont("Segoe UI")
        font.setPixelSize(13)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QColor(TEXT))
        painter.drawText(edge + QPointF(6, 5), f"{radius:.0f} m")
        painter.restore()


class VehicleLayer(QGraphicsItem):
    """Every vehicle of a run's latest frame in one item. On the map's OpenGL viewport the scene's
    gpu.VehicleRenderer draws one instanced batch per appearance, each frame's poses sent to the GPU once;
    elsewhere (the raster viewport, tests) QPainter draws them grouped by colour. Either way each
    vehicle is drawn part way (frame_alpha) between its last two simulated poses, so motion stays
    smooth at any frame rate, and zoomed far out they stay visible (dots, or a few pixels)."""

    OUTLINE = QColor("#dffcff")

    def __init__(self, scene: NetworkGraphicsScene) -> None:
        super().__init__()
        self._scene = scene
        self.setZValue(VEHICLE_Z)

    def boundingRect(self) -> QRectF:  # type intentionally inherited from Qt
        return self._scene.sceneRect()

    def paint(self, painter: QPainter, option, widget=None) -> None:  # type intentionally inherited from Qt
        frame = self._scene.frame
        if frame is None or not (frame.vehicles or frame.walkers):
            return
        alpha = self._scene.frame_alpha
        transform = painter.worldTransform()
        # Legacy scene.render exports remain top-down; an isometric view supplies the yaw/elevation transform.
        isometric = self._scene.isometric and abs(transform.m12()) + abs(transform.m21()) > 1e-9
        lift = projection_lift() if isometric else None
        if not frame.vehicles or self._scene.gpu.draw(painter, frame, alpha, self.OUTLINE, lift):
            self._paint_walkers(painter, frame, alpha, lift)
            return
        groups: dict[str, list] = defaultdict(list)
        for vehicle in frame.vehicles:
            (x0, y0, ux0, uy0), (x1, y1, ux1, uy1) = vehicle.prev, (vehicle.x, vehicle.y, vehicle.ux, vehicle.uy)
            ux, uy = ux0 + (ux1 - ux0) * alpha, uy0 + (uy1 - uy0) * alpha
            norm = hypot(ux, uy) or 1.0
            groups[vehicle_assets.paint_colour(vehicle, VEHICLE_PAINTS)].append(
                (x0 + (x1 - x0) * alpha, y0 + (y1 - y0) * alpha, ux / norm, uy / norm,
                 vehicle.length_m / 2, vehicle.width_m / 2, vehicle_assets.style(vehicle)))
        pixels_per_metre = QStyleOptionGraphicsItem.levelOfDetailFromTransform(painter.worldTransform())
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
        self._paint_walkers(painter, frame, alpha, lift)

    def _paint_walkers(self, painter: QPainter, frame, alpha: float, lift=None, bridge=None) -> None:
        """Interpolate the same world poses: 2D markers, metre-scaled people in 2.5D."""
        if not frame.walkers:
            return
        if lift is not None:
            poses = []
            for x1, y1, ux1, uy1, (x0, y0, ux0, uy0) in frame.walkers:
                x, y = x0 + (x1 - x0) * alpha, y0 + (y1 - y0) * alpha
                height = bridge.height_at(x, y) if bridge else max(
                    (item.height_at(x, y) for item in self._scene._overbridges), default=0.)
                if (height > 0) != (bridge is not None):
                    continue
                ux, uy = ux0 + (ux1 - ux0) * alpha, uy0 + (uy1 - uy0) * alpha
                size = hypot(ux, uy) or 1.
                poses.append((x, y, ux / size, uy / size, height))
            for x, y, ux, uy, height in sorted(poses, key=lambda p: p[0] + p[1] + p[4]):
                anchor = QPointF(x, y) + lift * height
                faces = []
                camera = (-lift.x(), -lift.y(), 1.)
                for face in asset_mesh.pedestrian_mesh():
                    points = [(ux * p[0] - uy * p[1], uy * p[0] + ux * p[1], p[2]) for p in face.vertices]
                    n = asset_mesh.normal(points)
                    if sum(a * b for a, b in zip(n, camera)) <= 1e-9:
                        continue
                    depth = sum(sum(a * b for a, b in zip(p, camera)) for p in points) / len(points)
                    faces.append((depth, face.ink, points))
                painter.setPen(Qt.PenStyle.NoPen)
                for _, ink, points in sorted(faces):
                    painter.setBrush(QColor(WALKER if ink < 0 else VEHICLE_INKS[ink]))
                    painter.drawPolygon(QPolygonF([anchor + QPointF(a, b) + lift * z for a, b, z in points]))
            return
        points = [QPointF(x0 + (x1 - x0) * alpha, y0 + (y1 - y0) * alpha)
                  for x1, y1, _ux, _uy, (x0, y0, _px, _py) in frame.walkers]
        pixels = QStyleOptionGraphicsItem.levelOfDetailFromTransform(painter.worldTransform())
        pen = QPen(QColor(WALKER), 3 if pixels * 0.6 < 3 else 0.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
        pen.setCosmetic(pixels * 0.6 < 3)
        painter.setPen(pen)
        painter.drawPoints(points)

    @staticmethod
    def _paint_mesh(painter, skin, colour, x, y, ux, uy, length, width, lift, detail=True):
        """Opaque faces, sorted within the object; the GPU uses these same metre-expanded meshes."""
        camera = (-lift.x(), -lift.y(), 1.) if lift else (0., 0., 1.)
        faces = []
        fit = asset_mesh.wheel_fit(skin, length, width)
        for face in (asset_mesh.vehicle_mesh(skin) if detail else asset_mesh.vehicle_low_mesh(skin)):
            points = [asset_mesh.expand(p, length, width, fit) for p in face.vertices]
            if face.part == 1 and skin not in (6, 7) and lift:
                side = -uy * camera[0] + ux * camera[1]
                if sum(p[1] for p in points) * side < 0:
                    continue  # the opaque body hides the far-side tyres
            world = [(ux * a - uy * b, uy * a + ux * b, z) for a, b, z in points]
            n = asset_mesh.normal(world)
            if sum(a * b for a, b in zip(n, camera)) <= 1e-9:
                continue
            depth = sum(sum(a * b for a, b in zip(p, camera)) for p in world) / len(world)
            shade = .68 + .32 * max(0., (-.6 * n[0] - .8 * n[1] + n[2]) / sqrt(2))
            faces.append((depth, world, face.ink, shade))
        painter.setPen(Qt.PenStyle.NoPen)
        for _, points, ink, shade in sorted(faces, key=lambda f: (f[2] in (0, 2, 3, 4), f[0])):
            base = QColor(colour if ink < 0 else VEHICLE_INKS[ink])
            painter.setBrush(QColor.fromRgbF(base.redF() * shade, base.greenF() * shade, base.blueF() * shade))
            painter.drawPolygon(QPolygonF([QPointF(x + a, y + b) + (lift * z if lift else QPointF())
                                           for a, b, z in points]))


class NetworkView(QGraphicsView):
    pointed = Signal(float, float)  # the mouse moved over the map, to this scene point (metres)
    projection_changed = Signal(bool)
    right_clicked = Signal()  # a right press and release that didn't pan (finishes a metro deployment)

    def __init__(self, scene: NetworkGraphicsScene) -> None:
        super().__init__(scene)
        self.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.TextAntialiasing)
        self.setViewportUpdateMode(QGraphicsView.ViewportUpdateMode.SmartViewportUpdate)
        self._panning = False
        self._pan_start = QPoint()
        self._pan_moved = 0  # pixels a right or middle drag has panned: under RIGHT_CLICK_PX, a right click
        self._tool_cursor = Qt.CursorShape.ArrowCursor
        self.isometric = False
        # The OpenGL renderer the map would draw with; on a real GPU it gets an OpenGL viewport with 4x
        # multisampling (the GPU redraws the whole view each frame, as OpenGL views are meant to), and the
        # vehicles their instanced shader. Without one (a CPU stand-in such as llvmpipe, or no OpenGL at
        # all) the view keeps Qt's raster viewport rather than emulate OpenGL on the CPU. Offscreen (tests):
        # raster, unasked.
        self.renderer = "" if QGuiApplication.platformName() in ("offscreen", "minimal") else gpu.renderer()
        self.on_gpu = bool(self.renderer) and not gpu.software(self.renderer)
        if self.on_gpu:
            self._use_opengl(4)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setDragMode(QGraphicsView.DragMode.NoDrag)
        self.viewport().setMouseTracking(True)  # moves without a button too: the map's position readout
        self.background = None  # background.BackgroundPainter: the network's background, behind the scene
        self._corner: QWidget | None = None
        # During a run the background and the still network are drawn once and kept (scene.cache_still).
        scene.still_cached_changed.connect(lambda on: self.setCacheMode(
            QGraphicsView.CacheModeFlag.CacheBackground if on else QGraphicsView.CacheModeFlag.CacheNone))
        scene.buildings_changed.connect(lambda _: self.redraw())

    def _use_opengl(self, samples: int) -> None:
        opengl = QOpenGLWidget()
        surface = QSurfaceFormat()
        surface.setSamples(samples)  # the default version: Windows drivers give their newest, 3.3 or more for the shader
        opengl.setFormat(surface)
        self.setViewport(opengl)  # the vehicles' shader is built again in the new context (gpu.VehicleRenderer)
        self.setViewportUpdateMode(QGraphicsView.ViewportUpdateMode.FullViewportUpdate)
        opengl.setMouseTracking(True)
        opengl.setCursor(self._tool_cursor)
        if getattr(self, "_corner", None) is not None:
            self._corner.raise_()  # a new viewport is stacked over the map's corner controls

    def set_power_saver(self, on: bool) -> None:
        """Power Saver: on a GPU, the map without multisampling (its edges still antialiased by QPainter)."""
        if self.on_gpu:
            self._use_opengl(0 if on else 4)

    def redraw(self) -> None:
        """The background changed (a map tile arrived, it moved): drawn again, and the run's cache of it."""
        self.resetCachedContent()
        self.viewport().update()

    def projection_scale(self) -> float:
        """Orthographic pixels per metre before ground foreshortening; zoom never changes world distances."""
        compression = sin(radians(ISOMETRIC_ELEVATION_DEG)) if self.isometric else 1.0
        return sqrt(abs(self.transform().determinant()) / compression)

    def set_isometric(self, on: bool) -> None:
        """Switch only the camera/vehicle skin, retaining the same frame, centre, zoom, tools and selection."""
        on = bool(on)
        if on == self.isometric:
            return
        centre = self.mapToScene(self.viewport().rect().center())
        scale = self.projection_scale()
        self.isometric = self.scene().isometric = on
        self.scene().project_building_labels()
        self.scene().project_metro()
        anchor = self.transformationAnchor()
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.NoAnchor)
        try:
            self.setTransform(projection_transform(on, scale))
            self.centerOn(centre)
            # Scrollbars round to integer pixels. Retain the fractional offset so repeated toggles never drift.
            correction = self.mapToScene(self.viewport().rect().center()) - centre
            self.translate(correction.x(), correction.y())
        finally:
            self.setTransformationAnchor(anchor)
        self.redraw()
        self.projection_changed.emit(on)

    def set_corner(self, widget: QWidget) -> None:
        """Controls kept over the map's top-right corner (fit, reset, help)."""
        widget.setParent(self)
        self._corner = widget
        widget.show()
        self._place_corner()

    def resizeEvent(self, event) -> None:  # type intentionally inherited from Qt
        super().resizeEvent(event)
        self._place_corner()

    def _place_corner(self) -> None:
        if self._corner is not None:
            self._corner.adjustSize()
            area = self.viewport().geometry()
            self._corner.move(area.right() - self._corner.width() - 8, area.top() + 8)

    def drawBackground(self, painter: QPainter, rect: QRectF) -> None:  # type intentionally inherited from Qt
        network = self.scene().network
        painter.setRenderHints(self.renderHints())  # a run's cache is drawn with a painter of its own
        if self.background is None or network is None:
            super().drawBackground(painter, rect)  # the scene's: its colour, and during a run the still layer
            return
        self.background.paint(painter, rect, network.background, self.projection_scale() * self.devicePixelRatioF())
        self.scene().paint_still(painter, rect)

    def drawForeground(self, painter: QPainter, rect: QRectF) -> None:  # type intentionally inherited from Qt
        super().drawForeground(painter, rect)
        network = self.scene().network
        if self.background is not None and network is not None:
            self.background.paint_marks(painter, network.background)
            if network.background.kind == "map":
                self.background.paint_attribution(painter, QRectF(self.viewport().rect()))

    def set_tool_cursor(self, shape: Qt.CursorShape) -> None:
        """Cursor that signals the active editing tool; panning restores it afterwards."""
        self._tool_cursor = shape
        self.viewport().setCursor(shape)

    def wheelEvent(self, event: QWheelEvent) -> None:
        turn = event.angleDelta().y()
        if turn and self.scene().grow_placing(1 if turn > 0 else -1):  # an institution's circle of influence
            event.accept()
            return
        factor = 1.18 if event.angleDelta().y() > 0 else 1 / 1.18
        current = self.projection_scale()
        if 0.0005 < current * factor < 100.0:
            self.scale(factor, factor)
        event.accept()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() in (Qt.MouseButton.MiddleButton, Qt.MouseButton.RightButton):
            self._panning = True
            self._pan_start = event.position().toPoint()
            self._pan_moved = 0
            self.viewport().setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        where = self.mapToScene(event.position().toPoint())
        self.pointed.emit(where.x(), where.y())
        if self._panning:
            delta = event.position().toPoint() - self._pan_start
            self._pan_start = event.position().toPoint()
            self._pan_moved += delta.manhattanLength()
            self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - delta.x())
            self.verticalScrollBar().setValue(self.verticalScrollBar().value() - delta.y())
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if self._panning and event.button() in (Qt.MouseButton.MiddleButton, Qt.MouseButton.RightButton):
            self._panning = False
            self.viewport().setCursor(self._tool_cursor)
            if event.button() == Qt.MouseButton.RightButton and self._pan_moved < RIGHT_CLICK_PX:
                self.right_clicked.emit()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def fit_network(self) -> None:
        rect = self.scene().network_rect
        if rect.isNull():
            return
        # Margin in screen pixels: labels keep a fixed pixel size, so a margin
        # in metres would clip them on large networks.
        margin = 90
        view = self.viewport().rect()
        projected = projection_transform(self.isometric).mapRect(rect)
        scale = min((view.width() - 2 * margin) / projected.width(),
                    (view.height() - 2 * margin) / projected.height())
        if scale > 0:
            self.setTransform(projection_transform(self.isometric, scale))
            self.centerOn(rect.center())

    def reset_view(self) -> None:
        self.setTransform(projection_transform(self.isometric))
        self.centerOn(self.scene().sceneRect().center())

    ARROWS = {Qt.Key.Key_Left: (-1, 0), Qt.Key.Key_Right: (1, 0), Qt.Key.Key_Up: (0, -1), Qt.Key.Key_Down: (0, 1)}

    def keyPressEvent(self, event) -> None:  # type intentionally inherited from Qt
        if event.key() in self.ARROWS:  # with a bus stop selected they nudge it, otherwise they scroll
            shift = event.modifiers() & Qt.KeyboardModifier.ShiftModifier
            inverse, invertible = self.transform().inverted()
            direction = inverse.map(QPointF(*self.ARROWS[event.key()])) - inverse.map(QPointF())
            norm = hypot(direction.x(), direction.y()) or 1.0
            if invertible and self.scene().nudge_bus_stop(direction.x() / norm, direction.y() / norm,
                                           BUS_STOP_SHIFT_NUDGE_M if shift else BUS_STOP_NUDGE_M):
                event.accept()
                return
        if event.key() == Qt.Key.Key_F:
            self.fit_network()
            event.accept()
        elif event.key() == Qt.Key.Key_R:
            self.reset_view()
            event.accept()
        else:
            super().keyPressEvent(event)

"""Vector rendering and camera interaction for the network model."""

from __future__ import annotations

import pickle
from collections import defaultdict
from math import atan2, ceil, cos, hypot, radians, sin, sqrt

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

from . import building_assets, gpu, metro_assets
from .geometry import convex_hull, nearest_along, offset_polyline, point_along, polyline_length, sub_polyline
from .lane_graph import phase_letters
from .institutions import KINDS, Placement, links
from .models import (BAY_ENTRY_M, BAY_LENGTH_M, BAY_STOP_M, BAY_WIDTH_M, CYCLE_LANE_M, FOOTPATH_M, LANE_WIDTH_M,
                     POCKET_TAPER_M, BusStop, Institution, Network, Node, Road, bay_depth, stop_line_inset)
from .widgets import ICON_FONTS
from .roundabouts import design_roundabout
from .signal_timing import ADAPTIVE, STOP_LINE_LOOP_M, UPSTREAM_LOOP_AT_M, UPSTREAM_LOOP_M
from .walking import footpath_join
from .theme import (ACCENT_EDGE, CYCLE_LANE, DANGER, DETECTOR, ENTRY_MARKER, FOOTPATH,
                    GRASS, ISLAND, JUNCTION, JUNCTION_EDGE, LANE_LINE, MAP_LABEL,
                    MARKER_EDGE, MINI_ISLAND, PARAPET, PAVING_SELECTED, ROAD_PAINT, ROAD_SURFACE,
                    STOP_LINE, WALKER, METRO_LINE, STATUS, TEXT,
                    YELLOW_PAINT, ZEBRA_STRIPE)
# The standalone map items and the projection now live in scene_items; some names are only re-exported here.
from .scene_items import (  # noqa: F401
    CLOCK_OFFSET_M, ISOMETRIC_ELEVATION_DEG, ISOMETRIC_YAW_DEG, METRO_STOP_RADIUS_M, SELECTED_COLOR, SIGNAL_AMBER,
    SIGNAL_BODY, SIGNAL_GREEN, SIGNAL_RED, SIGNAL_RIM, VEHICLE_Z, BuildingItem, MetroItem, OverbridgeItem, PavingItem,
    Points, SignalClock, _FaceDrawing, _path, _screen_pen, projection_lift, projection_transform)
from .vehicle_layer import VehicleLayer, _vehicle_faces  # noqa: F401 (_vehicle_faces re-exported: the same cache)


# Everything physical is drawn to scale in metres. Only labels and selection
# outlines keep a fixed screen size so they stay readable at any zoom.
SIGNAL_SIZE = 1.2  # signal head footprint
CENTRE_LINE_WIDTH = 0.15
LANE_LINE_WIDTH = 0.1
LANE_DASH_M, LANE_GAP_M = 3.0, 9.0
STOP_LINE_WIDTH = 0.3
ROAD_COLOR = QColor(ROAD_SURFACE)
PARAPET_COLOR = QColor(PARAPET)
OVERPASS_LIFT = 5.0  # z offset: above ground roads and junctions, below vehicles and labels
PAINT_COLOR = QColor(ROAD_PAINT)
BUS_STOP_PAINT = QColor(YELLOW_PAINT)  # bay markings and the stop's flag
DETECTOR_COLOR = QColor(DETECTOR)  # loop detectors' outlines
BUS_STOP_NUDGE_M, BUS_STOP_SHIFT_NUDGE_M = 0.5, 5.0  # an arrow key's step, and with Shift
METRO_AIM_PX = 6  # a drag this long, deploying metro, aims the stop instead of placing it plainly
METRO_HANDLE_M = 60.0  # an aimed stop's handle each way on the map (half a platform)
METRO_Z = 7.5  # flat metro markers; the overhead structures use VEHICLE_Z + 1
RIGHT_CLICK_PX = 4  # a right press and release moving less than this is a click, not a pan
# Item data key: the item is in the still layer, what rebuild() draws under the vehicles (but the
# signal lamps). During a run the view caches it with the background (NetworkGraphicsScene.cache_still).
STILL = 1


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
    # After every rebuild(), which clears the scene, and each step of a drag (the network changed): extensions draw again
    redrawn = Signal()
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
        # Each object rebuild() draws, its items (addItem), so it alone can be drawn again (_redraw): ("road", id)...
        self._drawn: dict[tuple[str, str], list[QGraphicsItem]] = defaultdict(list)
        self._drawing: tuple[str, str] | None = None  # the object being drawn (_draw)
        self._state = None  # what the map was last drawn whole from (_drawn_state)
        self.network = network
        self.selection: tuple[str, str] | None = None
        self.multi_nodes: set[str] = set()  # junctions picked together (Ctrl+click)
        self._roundabouts: dict = {}  # node id -> RoundaboutDesign, refreshed each rebuild
        # Nodes carried when the selected object is dragged; empty = drag mode off.
        self.drag_nodes: list[str] = []
        self._drag_from: QPointF | None = None
        self._dragged = False  # the grabbed object moved: its release is an edit
        # A bus stop grabbed with the mouse slides along its road: (its id, metres from the grab
        # point to its middle, whether it has moved yet).
        self._slide: tuple[str, float, bool] | None = None
        self.setBackgroundBrush(QColor(GRASS))
        # The run as last published (simulation.Frame): set by the main window, drawn each frame.
        self.frame = None
        self.frame_alpha = 1.0  # where between the last two simulated steps to draw vehicles
        self.isometric = False  # display only; neither this nor the camera enters the network or published frame
        self.buildings_visible = False  # off by default; Show Buildings or Demand → Institutions shows them
        self._building_items: list[QGraphicsItem] = []
        self._metro_items: list[MetroItem] = []
        self._metro_labels: list[QGraphicsTextItem] = []
        self._overbridges: list[OverbridgeItem] = []
        self.gpu = gpu.VehicleRenderer()  # draws them on the GPU when the view is OpenGL (kept across rebuilds)
        self.signal_lamps: dict[tuple[str, str], tuple[QGraphicsEllipseItem, QColor]] = {}
        self._signal_states: dict | None = None
        self.signal_clocks: dict[tuple[str, str], SignalClock] = {}
        self.extent = None  # (the network's rect) -> what the view may scroll over, with its background
        # While the background panel's Move or Two Points is on, left clicks go to it (press, move, release).
        self.background_tool = None
        self._tool_held = False
        self.group: set[tuple[str, str]] = set()  # a selection box's objects
        self.box_select = True  # dragging over empty map draws a selection box (off while a tool is on)
        self._footpath_preview: list = []  # the road panel's footpath choice, highlighted before Apply
        self.metro_deploying = False  # Edit Network → MRT's Start deployment: each left click places a stop
        self._metro_press: QPointF | None = None  # deploying: where the left button went down (a drag aims it)
        self.spawn_placing = False  # Demand Generator → Deploy Pedestrian: each left click places one
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
        self._select((kind, identifier) if kind else None, set(), set())

    def set_multi_selection(self, node_ids: list[str]) -> None:
        self._select(None, set(node_ids), set())

    def set_group(self, objects) -> None:
        """A box's selection, (kind, id) of junctions, roads, ends, bus stops and metro lines, highlighted together."""
        self._select(None, set(), set(objects))

    def _select(self, selection, multi_nodes: set, group: set) -> None:
        """Highlight these. Only the objects picked or dropped are drawn again (all of an imported network took a
        second a click), unless the network has changed since it was drawn (_drawn_state): the window selects again
        after every edit, undo or drag, and then it is all drawn again (rebuild)."""
        before = self._picked()
        self.selection, self.multi_nodes, self.group = selection, multi_nodes, group
        if self.network is None or self._state != self._drawn_state():
            self.rebuild()
            return
        self.preview_footpaths("", ())  # as a rebuild does: a road's footpath choice shows until the selection changes
        if self._metro_ghost is not None:
            self.removeItem(self._metro_ghost)
            self._metro_ghost = None
        self._redraw({key for key, _ in before ^ self._picked()})

    def _picked(self) -> set:
        """(the drawn object, how it is picked) for each the selection highlights; an end's markers are drawn with
        its boundary road (_draw_end)."""
        picked = {(("node", node_id), "multi") for node_id in self.multi_nodes}
        picked |= {(key, "group") for key in self.group}
        if self.selection:
            picked.add((self.selection, "one"))
        ends = self.network.approach_roads.values() if self.network is not None else ()
        return {pick for (kind, identifier), how in picked for pick in
                ([(("end", road.id), how + kind) for road in ends if identifier in (road.from_node, road.to_node)]
                 if kind in ("entry", "exit", "end") else [((kind, identifier), how)])}

    def _drawn_state(self) -> tuple:
        """What rebuild() draws besides the selection: the network (pickled: any change to it shows) and a metro
        line's first stop. Unpicklable: never the same, so always drawn whole."""
        try:
            return id(self.network), self.metro_pending, pickle.dumps(self.network)
        except (pickle.PicklingError, TypeError, AttributeError):
            return (object(),)

    def addItem(self, item: QGraphicsItem) -> None:  # type intentionally inherited from Qt
        """Qt's, keeping each item under the object being drawn (_draw), so that object can be drawn again alone."""
        super().addItem(item)
        if self._drawing is not None:
            self._drawn[self._drawing].append(item)

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
            self._drag_from, self._dragged = pos, False  # grabbed the selected object
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
            stop = self.network.bus_stops.get(identifier)  # type: ignore[union-attr]
            if stop is None:  # undone or deleted mid-slide: the slide ends there
                self._slide = None
                event.accept()
                return
            self.network.move_bus_stop(identifier, self._along(stop, event.scenePos()) + grab)  # type: ignore[union-attr]
            self._slide = (identifier, grab, True)
            self._redraw([("bus_stop", identifier)])  # drawn whole once it is let go (the window selects again)
            self.redrawn.emit()
            event.accept()
            return
        if self._drag_from is None:
            super().mouseMoveEvent(event)
            return
        delta = event.scenePos() - self._drag_from
        moved = self._carried()  # where its roads stand now: their overpasses, before the move too
        if self.network.move_nodes(self.drag_nodes, delta.x(), delta.y()):  # type: ignore[union-attr]
            self._drag_from, self._dragged = event.scenePos(), True
            self._redraw(dict.fromkeys(moved + self._carried()))  # drawn whole once it is let go
            self.redrawn.emit()
        event.accept()

    def _carried(self) -> list:
        """What a drag of drag_nodes draws again: their roads, the junctions at those roads' ends (their arms turn)
        and their boundary ends, the bus stops and crossings on them, the paths from the nodes, every overpass
        whose line's box meets the roads' (its parapets) and, when a footpath moves, the spawn points (their links
        to the nearest)."""
        network, moved = self.network, set(self.drag_nodes)
        roads = [road for road in network.all_roads() if road.from_node in moved or road.to_node in moved]  # type: ignore[union-attr]
        ids = {road.id for road in roads}
        ends = moved | {road.from_node for road in roads} | {road.to_node for road in roads}
        ways = [way.id for way in network.walkways.values() if way.start in moved or way.end in moved]  # type: ignore[union-attr]

        def box(road):
            return QPolygonF([QPointF(*point) for point in network.road_points(road)]).boundingRect()  # type: ignore[union-attr]

        area = QRectF()
        for road in roads:
            area = area.united(box(road))
        keys = [("road", road.id) for road in network.all_roads()  # type: ignore[union-attr]
                if road.id in ids or road.overpass and box(road).intersects(area)]
        keys += [("bus_stop", stop.id) for stop in network.bus_stops.values() if stop.road in ids]  # type: ignore[union-attr]
        keys += [("node", node_id) for node_id in ends if node_id in network.nodes]  # type: ignore[union-attr]
        keys += [("end", road.id) for road in roads if road.id in network.approach_roads]  # type: ignore[union-attr]
        keys += [("walkway", way) for way in ways]
        keys += [("crossing", crossing.id) for crossing in network.pedestrian_crossings.values()  # type: ignore[union-attr]
                 if crossing.road in ids]
        if ways or any(road.footpaths for road in roads):
            keys += [("spawn", point.id) for point in network.spawn_points.values()]  # type: ignore[union-attr]
        return keys

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
            if self._dragged:  # a press and release without moving is no edit
                self.drag_finished.emit()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def rebuild(self) -> None:
        self.clear()
        self._box = self._band = None  # a selection box being dragged went with the items
        self._metro_ghost = None
        self._footpath_preview = []
        self._building_items = []
        self._metro_items, self._metro_labels = [], []
        self._overbridges = []
        self.signal_lamps, self.signal_clocks = {}, {}
        self._drawn.clear()
        self._state = self._drawn_state()
        if self.network is None:
            return
        network = self.network
        # Roundabouts are sized from their approach roads (see roundabouts.py).
        self._roundabouts = {node.id: design_roundabout(network, node.id)
                             for node in network.nodes.values() if node.control == "roundabout"}
        roads_at: dict[str, list[Road]] = {}
        for road in network.all_roads():
            self._draw(("road", road.id))
            roads_at.setdefault(road.from_node, []).append(road)
            roads_at.setdefault(road.to_node, []).append(road)
        for stop in network.bus_stops.values():
            self._draw(("bus_stop", stop.id))
        for node in network.nodes.values():
            self._draw(("node", node.id), roads_at.get(node.id, []))
        for road in network.approach_roads.values():
            self._draw(("end", road.id))
        self._zone = None
        for kind, objects in (("institution", network.institutions), ("walkway", network.walkways),
                              ("crossing", network.pedestrian_crossings), ("spawn", network.spawn_points),
                              ("metro_line", network.metro_lines)):
            for identifier in objects:
                self._draw((kind, identifier))
        if self.metro_pending is not None:
            item = MetroItem(self, [self.metro_pending])
            item.setToolTip("First stop: choose next")
            self.addItem(item)
            self._metro_items.append(item)
        self.project_metro()
        bounds = self.itemsBoundingRect()
        margin = max(80.0, min(max(bounds.width(), bounds.height()) * 0.05, 500.0))
        if bounds.isNull():
            bounds = QRectF(-100, -100, 200, 200)
        self.network_rect = bounds  # what Fit Network fits (the vehicle layer covers the whole scene)
        rect = bounds.adjusted(-margin, -margin, margin, margin)
        area = self.extent(rect) if self.extent else rect  # with the background's image or map
        if area != self.sceneRect():  # each view stays on what it showed (Qt keeps its scroll values, not its centre)
            centres = [(view, view.viewportTransform().inverted()[0].map(QRectF(view.viewport().rect()).center()))
                       for view in self.views()]
            self.setSceneRect(area)
            for view, centre in centres:
                view.centerOn(centre)
        self.project_building_labels()
        self._settle(self.items())  # extensions' items come later (redrawn): they never join the still layer
        self.vehicle_layer = VehicleLayer(self)
        self.addItem(self.vehicle_layer)
        self.redrawn.emit()
        if self.hidden:  # drawn (so Fit still knows where it is), then hidden
            for item in self.items():
                item.hide()

    def _draw(self, key: tuple[str, str], roads: list[Road] | None = None) -> None:
        """Draw one object, its items kept under `key` (addItem): ("road", id), ("end", its boundary road's id),
        ("node", id) with its `roads` (found when not given), ("bus_stop", id), ("institution", id), ("walkway", id),
        ("crossing", id), ("spawn", id) or ("metro_line", id). KeyError, drawing nothing, for one not in the network."""
        kind, identifier = key
        network = self.network
        self._drawing = key
        try:
            if kind == "road":
                self._draw_road(network.road(identifier))  # type: ignore[union-attr]
            elif kind == "end":
                self._draw_end(network.approach_roads[identifier])  # type: ignore[union-attr]
            elif kind == "node":
                self._draw_node(network.nodes[identifier], roads if roads is not None else  # type: ignore[union-attr]
                                [road for road in network.all_roads() if identifier in (road.from_node, road.to_node)])  # type: ignore[union-attr]
            elif kind == "bus_stop":
                self._draw_bus_stop(network.bus_stops[identifier])  # type: ignore[union-attr]
            elif kind == "institution":
                self._draw_institution(network.institutions[identifier])  # type: ignore[union-attr]
            elif kind == "walkway":
                self._draw_walkway(network.walkways[identifier])  # type: ignore[union-attr]
            elif kind == "crossing":
                self._draw_crossing(network.pedestrian_crossings[identifier])  # type: ignore[union-attr]
            elif kind == "spawn":
                self._draw_spawn_point(network.spawn_points[identifier])  # type: ignore[union-attr]
            elif kind == "metro_line":
                self._draw_metro_line(network.metro_lines[identifier])  # type: ignore[union-attr]
        finally:
            self._drawing = None

    def _redraw(self, keys) -> None:
        """Draw these objects (_draw's keys) again, each where it stood in the stacking order (items of one height
        stack in the order added), and nothing else: a selection click's objects, or what a drag carries."""
        drawn = []
        if any(kind == "institution" for kind, _ in keys):
            self._zone = None  # the selected one's circle: set again as it is drawn
        for key in keys:
            old = self._drawn.pop(key, [])
            try:
                self._draw(key)
            except KeyError:
                pass  # gone from the network: only taken away
            new = self._drawn.get(key, [])
            for item in new if old else ():
                item.stackBefore(old[0])
            for item in old:
                self.removeItem(item)
            drawn += new
        here = lambda item: item.scene() is self  # noqa: E731
        self._building_items = [item for item in self._building_items if here(item)]
        self._metro_items = [item for item in self._metro_items if here(item)]
        self._metro_labels = [item for item in self._metro_labels if here(item)]
        self._overbridges = [item for item in self._overbridges if here(item)]
        self.signal_lamps = {key: lamp for key, lamp in self.signal_lamps.items() if here(lamp[0])}
        self.signal_clocks = {key: clock for key, clock in self.signal_clocks.items() if here(clock)}
        if any(kind == "metro_line" for kind, _ in keys):
            self.project_metro()
        if any(kind == "institution" for kind, _ in keys):
            self.project_building_labels()
        self._settle(drawn)
        if self.hidden:
            for item in drawn:
                item.hide()
        self.update()  # the selected building's circle (drawForeground) too

    def _settle(self, items) -> None:
        """What each item drawn gets: the still layer's mark under the vehicles (held while a run is on:
        cache_still), but on the signal lamps, which a run lights each frame; the clocks and lamps showing the
        run's; buildings shown or hidden."""
        for item in items:
            if item.zValue() < VEHICLE_Z:
                item.setData(STILL, True)
        for lamp, _ in self.signal_lamps.values():
            lamp.setData(STILL, False)
        for item in self._building_items:
            item.setVisible(self.buildings_visible)
        self.show_clocks(self.frame)
        if self.still_cached:
            for item in items:
                if item.data(STILL):
                    item.setFlag(QGraphicsItem.GraphicsItemFlag.ItemHasNoContents, True)
            self.invalidate(self.sceneRect(), QGraphicsScene.SceneLayer.BackgroundLayer)  # the views' caches
        states, self._signal_states = self._signal_states, None
        self.show_signals(states)

    def show_signals(self, states: dict | None) -> None:
        """Light each signal head from a run's states, (junction, road) -> "G", "Y" or "R";
        None: the static layout colours (the first phase's arms green, by lane_graph.two_phases) while nothing runs."""
        if states == self._signal_states and self.signal_lamps:
            return
        self._signal_states = states
        colours = {"G": SIGNAL_GREEN, "Y": SIGNAL_AMBER, "R": SIGNAL_RED}
        for key, (lamp, idle) in self.signal_lamps.items():
            lamp.setBrush(colours[states[key]] if states and key in states else idle)

    def show_clocks(self, frame) -> None:
        """Each signal head's clock: the whole seconds until its signal changes, in its colour; blank while
        nothing runs, or on an arm whose countdown the frame leaves out (Simulation.signal_countdowns)."""
        countdowns = frame.countdowns if frame else {}
        for key, clock in self.signal_clocks.items():
            left = countdowns.get(key)
            clock.set_reading(None if left is None else (max(0, int(ceil(left - 1e-6))), frame.signals.get(key, "R")))

    def show_frame(self, alpha: float, frame=None) -> None:
        """Draw a published frame of a run (None: no run): vehicles part way to their latest
        step, signals lit. Frames the physics published in between are simply skipped."""
        self.frame, self.frame_alpha = frame, alpha
        self.cache_still(frame is not None)
        self.show_signals(frame.signals if frame else None)
        self.show_clocks(frame)
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
        label.setDefaultTextColor(QColor(MAP_LABEL))
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
        surface.setPen(QPen(QColor(PAVING_SELECTED) if selected else ROAD_COLOR, total_width,
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
            pen = QPen(QColor(YELLOW_PAINT) if divider else QColor(LANE_LINE),
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
            pen = QPen(QColor(STOP_LINE), STOP_LINE_WIDTH, Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap)
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
        hit.setPen(QPen(QColor(Qt.GlobalColor.transparent), total_width, Qt.PenStyle.SolidLine,
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
        pen = QPen(QColor(LANE_LINE), LANE_LINE_WIDTH, Qt.PenStyle.DashLine, Qt.PenCapStyle.FlatCap)
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
            sign.setPen(QPen(SIGNAL_BODY, 0.15))
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

    def _draw_metro_line(self, line) -> None:
        """The same saved stop coordinates drive the flat markers and display-only overhead structures."""
        network = self.network
        stops = [network.metro_stops[stop_id] for stop_id in line.stops]  # type: ignore[union-attr]
        item = MetroItem(self, [(stop.x, stop.y) for stop in stops], line.cars_per_train, network.metro_track(line.id),  # type: ignore[union-attr]
                         ("metro_line", line.id) in self.group)
        item.setToolTip("Overhead MRT (assumed heights)")
        self.addItem(item)
        self._metro_items.append(item)
        for stop in stops:
            self._metro_label(stop.id, stop.x, stop.y - METRO_STOP_RADIUS_M, metro_assets.CANOPY_M)
        first = stops[0]
        self._metro_label(f"{line.id} train preview", first.x, first.y, metro_assets.TRAIN_BASE_M + metro_assets.TRAIN_HEIGHT_M,
                          preview=True)

    def _draw_spawn_point(self, point) -> None:
        """A pedestrian spawn point: a disc in the walkers' colour with its name, and a dashed line to where its
        walkers join the footpaths, the nearest place on any footpath or path (walking.WalkGraph.attach's way);
        clickable as ("spawn", id), outlined when selected."""
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
        disc.setToolTip(f"Deploy point {point.id}")
        self.addItem(disc)
        label = self._add_label(point.id, point.x, point.y, 0, -1, pixels=11)
        label.setTransform(QTransform.fromTranslate(0, -6), True)  # a pixel gap from the screen-sized pin

    def _draw_crossing(self, crossing) -> None:
        """A pedestrian crossing across its road: a zebra's white stripes on the carriageway, an overbridge's deck
        above the traffic (from footpath to footpath); clickable as ("crossing", id), outlined when selected."""
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
            marks.setPen(QPen(QColor(ZEBRA_STRIPE), 0.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap))
            marks.setZValue(2.5)
            self.addItem(marks)
        item = QGraphicsPolygonItem(outline) if zebra else OverbridgeItem(self, outline, x, y, ux, uy, half, selected)
        if not zebra:
            self._overbridges.append(item)
        item.setBrush(Qt.BrushStyle.NoBrush if zebra else QColor(FOOTPATH))
        item.setPen(_screen_pen(QColor(PAVING_SELECTED), 2) if selected else
                    (QPen(Qt.PenStyle.NoPen) if zebra else _screen_pen(QColor(ROAD_COLOR), 1.5)))
        item.setZValue(2.6 if zebra else VEHICLE_Z + 1)  # a bridge over the traffic
        item.setData(0, ("crossing", crossing.id))
        item.setToolTip("Zebra crossing" if zebra else "Pedestrian overbridge")
        self.addItem(item)

    def _draw_walkway(self, way) -> None:
        """A path (Network.walkways) as paving its width wide, under the roads it meets (clickable: selected,
        Delete removes it)."""
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
        if network.move_bus_stop(stop.id, middle + (metres if towards > 0 else -metres)):  # type: ignore[union-attr]
            self.bus_stop_moved.emit(stop.id)  # refused (another stop's bay, a crossing): nothing to commit
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
            island.setBrush(QColor(MINI_ISLAND) if design.mini else QColor(ISLAND))
            island.setPen(QPen(SIGNAL_RIM, 0.3))
            island.setZValue(6)
            self.addItem(island)
        else:
            rect = QRectF(node.x - diameter / 2, node.y - diameter / 2, diameter, diameter)
            # A junction's box takes the shape its roads meet in: round each road's end (its kerbs from the node out
            # to its mouth, half the box out along it), so it turns and narrows with them rather than sitting square
            # across them, and covers each road's flat end at the node even where they all come from one side.
            half, mouths = diameter / 2, []
            for ux, uy, road in arms:
                for side in (-1, 1):
                    kerb_x, kerb_y = -uy * side * road.width_m / 2, ux * side * road.width_m / 2
                    mouths += [(node.x + kerb_x, node.y + kerb_y),
                               (node.x + ux * half + kerb_x, node.y + uy * half + kerb_y)]
            if node.control == "uncontrolled" or len(arms) < 2:
                item = QGraphicsRectItem(rect)
            else:
                item = QGraphicsPolygonItem(QPolygonF([QPointF(x, y) for x, y in convex_hull(mouths)]))
            if node.control == "signalised":
                arriving = [(ux, uy, road) for ux, uy, road in arms
                            if road.direction == "two-way" or road.to_node == node.id]  # traffic arrives here
                # Each head's phase letter (A goes first: AA BB with two phases, A B C D with four, a manual
                # timing's shared windows sharing one), and while nothing runs phase A's arms green, the rest red
                letters = phase_letters(self.network, node.id)
                for ux, uy, road in arriving:
                    self._draw_signal(node, ux, uy, road, diameter / 2, letters.get(road.id, ""))
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
        item.setBrush(QColor(JUNCTION))
        item.setPen(_screen_pen(SELECTED_COLOR if selected else QColor(JUNCTION_EDGE), 3 if selected else 1.5))
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
                     junction_half: float, letter: str) -> None:
        """Signal head on the nearside kerb of an incoming arm, at its stop line, with its phase letter just upstream
        of it (the timing editor's names); phase A's are lit green while nothing runs."""
        green = letter == "A"
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
        signal.setBrush(SIGNAL_BODY)
        signal.setPen(QPen(SIGNAL_RIM, 0.1))
        signal.setZValue(6)
        self.addItem(signal)
        lamp = QGraphicsEllipseItem(signal.rect().adjusted(0.25, 0.25, -0.25, -0.25))
        idle = SIGNAL_GREEN if green else SIGNAL_RED  # while nothing runs (a run lights them for real)
        lamp.setBrush(idle)
        lamp.setPen(QPen(Qt.PenStyle.NoPen))
        lamp.setZValue(7)
        self.addItem(lamp)
        self.signal_lamps[(node.id, road.id)] = (lamp, idle)
        clock = SignalClock(self, cx - uy * CLOCK_OFFSET_M * side, cy + ux * CLOCK_OFFSET_M * side)  # off the kerb
        self.addItem(clock)
        self.signal_clocks[(node.id, road.id)] = clock
        if letter:  # upstream of the head, along the arm: clear of the clock beside it and of the junction
            self._add_label(letter, cx, cy, ux, uy, pixels=14).setToolTip(f"Phase {letter}")

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
                             "entry", end, QColor(ENTRY_MARKER),
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
        marker.setPen(_screen_pen(QColor(MARKER_EDGE), 1))
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


class NetworkView(QGraphicsView):
    pointed = Signal(float, float)  # the mouse moved over the map, to this scene point (metres)
    projection_changed = Signal(bool)
    right_clicked = Signal()  # a right press and release that didn't pan (finishes a metro deployment)
    zoomed = Signal()  # the wheel zoomed, or the view was resized: what it shows changed size

    def __init__(self, scene: NetworkGraphicsScene) -> None:
        super().__init__(scene)
        self.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.TextAntialiasing)
        self.setViewportUpdateMode(QGraphicsView.ViewportUpdateMode.SmartViewportUpdate)
        self._panning = False
        self._pan_start = QPoint()
        self._pan_moved = 0  # pixels a right or middle drag has panned: under RIGHT_CLICK_PX, a right click
        self._pan_image = None
        self._pan_transform = None
        self._pan_recording = False
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
        # Panned by dragging, the arrows and the wheel: scroll bars coming and going as the scene's area changed
        # resized the map, moving everything on it (a node placed was no longer under the mouse)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.viewport().setMouseTracking(True)  # moves without a button too: the map's position readout
        self.background = None  # background.BackgroundPainter: the network's background, behind the scene
        # How far the wheel zooms, (fewest, most) pixels a metre; None: not at all (the window sets it: a map's
        # work zone fills the view at the most zoomed out)
        self.zoom_range = lambda: (0.0005, 100.0)
        self._corner: QWidget | None = None
        self._top: QWidget | None = None
        # During a run the background and the still network are drawn once and kept (scene.cache_still).
        scene.still_cached_changed.connect(lambda on: self.setCacheMode(
            QGraphicsView.CacheModeFlag.CacheBackground if on else QGraphicsView.CacheModeFlag.CacheNone))
        scene.buildings_changed.connect(lambda _: self.redraw())
        scene.redrawn.connect(self._clear_pan_image)
        scene.changed.connect(self._clear_pan_image)  # live overlay/extension edits invalidate the captured pixels too

    def _clear_pan_image(self):
        self._pan_image = None
        self._pan_transform = None

    def paintEvent(self, event):
        scene = self.scene()
        # Editing a static map: scroll a padded viewport image while dragging, as a tile renderer would.
        # Live runs and active placement/measurement tools keep their ordinary, continuously updated drawing.
        background_tool = self.background is not None and (
            self.background.marks or self.background.line is not None or self.background.framing is not None
            or self.background.guide is not None)
        if (not self._panning or self._pan_moved < RIGHT_CLICK_PX or scene.frame is not None
                or background_tool or scene.hidden or scene.placing or scene.metro_deploying or scene.spawn_placing):
            self._clear_pan_image()
            super().paintEvent(event)
            return
        pad = 192
        transform = self.viewportTransform()
        previous = self._pan_transform
        if previous is not None and (any(getattr(transform, name)() != getattr(previous, name)()
                                          for name in ("m11", "m12", "m21", "m22"))
                                     or self._pan_image.devicePixelRatioF() != self.devicePixelRatioF()):
            self._clear_pan_image()
            previous = None
        dx, dy = (transform.dx() - previous.dx(), transform.dy() - previous.dy()) if previous else (0., 0.)
        if self._pan_image is None or abs(dx) > pad or abs(dy) > pad:
            source = self.viewport().rect().adjusted(-pad, -pad, pad, pad)
            ratio = self.devicePixelRatioF()
            width, height = int(source.width() * ratio + .5), int(source.height() * ratio + .5)
            if width * height > 16_000_000:  # bound the extra buffer to 64 MB, even on a high-DPI display
                super().paintEvent(event)
                return
            image = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
            image.setDevicePixelRatio(ratio)
            image.fill(Qt.GlobalColor.transparent)
            painter = QPainter(image)
            self._pan_recording = True
            try:
                self.render(painter, QRectF(0, 0, source.width(), source.height()), source,
                            Qt.AspectRatioMode.IgnoreAspectRatio)
            finally:
                self._pan_recording = False
                painter.end()
            self._pan_image, self._pan_transform = image, transform
            dx, dy = 0., 0.
        painter = QPainter(self.viewport())
        painter.drawImage(QPointF(dx - pad, dy - pad), self._pan_image)
        if self.background is not None and scene.network is not None and scene.network.background.kind == "map":
            self.background.paint_attribution(painter, QRectF(self.viewport().rect()))
        painter.end()

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
        self._clear_pan_image()
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
        self._clear_pan_image()
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
        self.clamp_zoom()  # tilted, a work zone's diamond needs a different fit
        self.redraw()
        self.projection_changed.emit(on)

    def set_corner(self, widget: QWidget) -> None:
        """Controls kept over the map's top-right corner (fit, reset, help)."""
        widget.setParent(self)
        self._corner = widget
        widget.show()
        self.place_overlays()

    def set_top(self, widget: QWidget) -> None:
        """A bar kept over the map's top centre (the work zone's, shown while it is chosen)."""
        widget.setParent(self)
        self._top = widget
        self.place_overlays()

    def resizeEvent(self, event) -> None:  # type intentionally inherited from Qt
        self._clear_pan_image()
        super().resizeEvent(event)
        self.place_overlays()
        self.clamp_zoom()
        self.zoomed.emit()

    def place_overlays(self) -> None:
        """The corner controls top right; the top bar centred above, as tall as its wrapped text needs."""
        area = self.viewport().geometry()
        if self._corner is not None:
            self._corner.adjustSize()
            self._corner.move(area.right() - self._corner.width() - 8, area.top() + 8)
        if self._top is not None:
            width = min(self._top.maximumWidth(), area.width() - 16)
            height = self._top.heightForWidth(width)
            self._top.resize(width, height if height > 0 else self._top.sizeHint().height())
            self._top.move(area.left() + (area.width() - width) // 2, area.top() + 8)
            self._top.raise_()

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
            self.background.paint_marks(painter, network.background, QRectF(self.viewport().rect()))
            if network.background.kind == "map" and not self._pan_recording:
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
        current, limits = self.projection_scale(), self.zoom_range()
        if limits is not None and (current * factor <= limits[1] if factor > 1 else current * factor >= limits[0]):
            self.scale(factor, factor)
            self.zoomed.emit()
        event.accept()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() in (Qt.MouseButton.MiddleButton, Qt.MouseButton.RightButton):
            self._clear_pan_image()
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
            self._clear_pan_image()
            self.viewport().update()
            self.viewport().setCursor(self._tool_cursor)
            if event.button() == Qt.MouseButton.RightButton and self._pan_moved < RIGHT_CLICK_PX:
                self.right_clicked.emit()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def fit_scale(self, rect: QRectF, margin: int = 0) -> float:
        """Pixels a metre that show all of `rect`, `margin` screen pixels in from each side."""
        view = self.viewport().rect()
        projected = projection_transform(self.isometric).mapRect(rect)
        return min((view.width() - 2 * margin) / max(projected.width(), 1e-9),
                   (view.height() - 2 * margin) / max(projected.height(), 1e-9))

    def fit_network(self) -> None:
        # Margin in screen pixels: labels keep a fixed pixel size, so a margin
        # in metres would clip them on large networks.
        self.fit_rect(self.scene().network_rect, 90)

    def fit_rect(self, rect: QRectF, margin: int = 0) -> None:
        """Show all of `rect` (edge to edge by default: a map's work zone), within `zoom_range` (none: the zoom
        isn't the user's to change, as before Locate Work Zone)."""
        limits, scale = self.zoom_range(), self.fit_scale(rect, margin)
        if rect.isNull() or limits is None or scale <= 0:
            return
        self.setTransform(projection_transform(self.isometric, min(max(scale, limits[0]), limits[1])))
        self.centerOn(rect.center())
        self.zoomed.emit()

    def reset_view(self) -> None:
        limits = self.zoom_range()  # one pixel a metre, unless a work zone would no longer fill the view
        if limits is None:
            return
        self.setTransform(projection_transform(self.isometric, min(max(1.0, limits[0]), limits[1])))
        self.centerOn(self.scene().sceneRect().center())
        self.zoomed.emit()

    def clamp_zoom(self) -> None:
        """Back within `zoom_range` when its limits moved (a work zone chosen, the view resized or tilted)."""
        limits, scale = self.zoom_range(), self.projection_scale()
        if limits is not None and not limits[0] <= scale <= limits[1]:
            centre = self.mapToScene(self.viewport().rect().center())
            self.setTransform(projection_transform(self.isometric, min(max(scale, limits[0]), limits[1])))
            self.centerOn(centre)

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

"""The map's view: pan, zoom, fit, right-click versus pan, arrow-key nudges and the OpenGL viewport.
Split out of network_scene.py."""

from __future__ import annotations

from math import hypot, radians, sin, sqrt

from PySide6.QtCore import QPoint, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QGuiApplication, QImage, QMouseEvent, QPainter, QSurfaceFormat, QWheelEvent
from PySide6.QtOpenGLWidgets import QOpenGLWidget
from PySide6.QtWidgets import QGraphicsView, QWidget

from . import gpu
from .scene_items import ISOMETRIC_ELEVATION_DEG, projection_transform


BUS_STOP_NUDGE_M, BUS_STOP_SHIFT_NUDGE_M = 0.5, 5.0  # an arrow key's step, and with Shift
RIGHT_CLICK_PX = 4  # a right press and release moving less than this is a click, not a pan


class NetworkView(QGraphicsView):
    pointed = Signal(float, float)  # the mouse moved over the map, to this scene point (metres)
    projection_changed = Signal(bool)
    right_clicked = Signal()  # a right press and release that didn't pan (finishes a metro deployment)
    zoomed = Signal()  # the wheel zoomed, or the view was resized: what it shows changed size

    def __init__(self, scene: NetworkGraphicsScene) -> None:  # noqa: F821 (network_scene's, named only as a string)
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

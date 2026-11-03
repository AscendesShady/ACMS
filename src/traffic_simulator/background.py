"""The map's background (Qt): a solid colour, an image (PNG, JPG or a PDF page) laid at a place, width
and turn in metres, or OpenStreetMap under the network, turned to any bearing (map_tiles.py).
BackgroundPainter draws it in the view's background layer, behind everything the scene draws;
BackgroundPanel is its section in the Create Network tab, with its tools: Move (drag the image or map)
and Two Points (click two points on the image and where each belongs: the image is scaled, turned and
moved to fit them exactly) line it up; Lock pins it; Measure gives the distance between two clicks in
metres, and over an image Set Scale makes a measured line its true length, so roads drawn over it are
true to size too. Two steps come first, the network hidden meanwhile: choosing the map starts its work
zone (the world map under a box fixed on screen; a bar over the map finds a country and a place, or
takes coordinates, Locate Work Zone drags and zooms the map, Confirm keeps what the box covers, and
the view keeps to that zone from then on), and opening an image starts Set Scale (click a line of
known length, enter its length). The settings live on the network (models.Background), so they are
saved with it and undone like any edit."""

from __future__ import annotations

import threading
from cmath import phase
from collections import OrderedDict
from copy import deepcopy
from math import cos, degrees, hypot, pi, radians
from pathlib import Path

from PySide6.QtCore import (QLocale, QObject, QPointF, QRectF, QSize, QSizeF, QStandardPaths, Qt, QTimer,
                            Signal)
from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath, QPen, QPixmap, QPolygonF, QTransform
from PySide6.QtWidgets import (QButtonGroup, QComboBox, QCompleter, QDoubleSpinBox, QFileDialog, QFormLayout,
                               QHBoxLayout, QInputDialog, QLabel, QMessageBox, QPushButton, QSizePolicy, QSpinBox,
                               QVBoxLayout, QWidget)

from .map_tiles import ATTRIBUTION, EARTH_RADIUS_M, ZONE_DEFAULT_M, ZONE_M, Frame, PlaceSearch, TileStore
from .models import Background, image_path_problem
from .widgets import ChoiceButtons, hint_label, icon_button, styled_button
from .theme import ACCENT_EDGE, MAP_COLOURS, PANEL, SHADOW, TEXT, TOOLTIP, float_glass

COLOURS = MAP_COLOURS  # solid backgrounds (map colours, like the scene's own road and grass colours): name -> colour
PDF_WIDTH_PX = 2400  # a PDF page is drawn at this width
TILE_MEMORY = 400  # tiles kept decoded in memory
ZONE_INSET = 0.2  # the work zone's box, while it is chosen: this share of the view in from each side
FLY_MAX_M = 2_000_000.0  # and at most this: a country's box can span the globe (France with its overseas parts)
# QLocale's two-letter territories that aren't ISO 3166-1 countries Nominatim's countrycodes knows (groupings,
# pseudo-locales, Antarctica, and parts with codes of their own only in CLDR: Canary Islands, Ceuta and Melilla...)
NOT_COUNTRIES = {"EU", "EZ", "UN", "QO", "XA", "XB", "AQ", "IC", "EA", "AC", "TA", "DG", "CP"}
ALIGN_STEPS = ("Click a point you can recognise on the image (1 of 2).",
               "Click where that point belongs on the map or network.",
               "Click a second point on the image, well apart from the first (2 of 2).",
               "Click where that second point belongs.")


def _pdf_page(path: str, page: int) -> QImage | None:
    from PySide6.QtPdf import QPdfDocument  # only when a PDF is opened

    document = QPdfDocument()
    if document.load(path) != QPdfDocument.Error.None_ or not 0 <= page < document.pageCount():
        return None
    size = document.pagePointSize(page)
    if size.width() <= 0:
        return None
    image = document.render(page, QSize(PDF_WIDTH_PX, round(PDF_WIDTH_PX * size.height() / size.width())))
    return None if image.isNull() else image


def map_frame(background: Background) -> Frame:
    return Frame(background.lat, background.lon, background.anchor_x, background.anchor_y, background.map_rotation_deg)


class BackgroundPainter(QObject):
    """Draws a network's background; `tile_ready` asks for a redraw when a map tile arrives. `marks`
    are points a tool has picked, (x, y, label) in scene metres, drawn over the map."""

    tile_ready = Signal()
    found = Signal(str, object)  # a place search's answer (map_tiles.PlaceSearch), delivered on the GUI thread

    def __init__(self, tile_folder: Path | None = None, download=None) -> None:
        super().__init__()
        folder = tile_folder or Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.CacheLocation)) / "osm-tiles"
        kwargs = {"download": download} if download else {}
        self.tiles = TileStore(folder, ready=lambda *_: self.tile_ready.emit(), **kwargs)
        self.places = PlaceSearch(ready=self.found.emit)  # emitted from its thread: Qt queues it to ours
        self._image: tuple[tuple, QImage | None] = ((), None)  # the last image loaded, by its key: only that one kept
        # Loading is one at a time: the assistant traces roads off the GUI thread (road_mask) while the map may be
        # drawing, and a PDF page is rendered by PDFium, which is not thread-safe
        self._loading = threading.Lock()
        self._pixmaps: OrderedDict[tuple[int, int, int], QPixmap] = OrderedDict()
        self.marks: list[tuple[float, float, str]] = []
        self.line: tuple[float, float, float, float, str] | None = None  # Measure's line: x1, y1, x2, y2, label
        self.guide: tuple[float, float, float, float, str] | None = None  # dashed, the same: Add Node's next distance
        self.framing: str | None = None  # while the work zone is chosen: the box's label (the box is fixed on screen)
        self.hidden = False  # Hide Background: only its colour drawn, to see the roads alone; a view, never saved

    def image(self, background: Background) -> QImage | None:
        """The background's image (a PDF's page rendered), loaded once per file, page and change; only the
        latest is kept (an image swapped for another, or a PDF paged through, would pile up decoded pages)."""
        path = background.image
        try:
            key = (path, background.page, Path(path).stat().st_mtime)
        except OSError:
            return None
        with self._loading:
            if self._image[0] != key:
                if path.lower().endswith(".pdf"):
                    image = _pdf_page(path, background.page)
                else:
                    image = QImage(path)
                    image = None if image.isNull() else image
                self._image = (key, image)
            return self._image[1]

    def image_transform(self, background: Background) -> QTransform | None:
        """Image pixels to scene metres: scaled to its width, turned about its top-left corner, placed."""
        image = self.image(background)
        if image is None:
            return None
        scale = background.width_m / image.width()
        transform = QTransform()
        transform.translate(background.x_m, background.y_m)
        transform.rotate(background.rotation_deg)
        transform.scale(scale, scale)
        return transform

    def road_mask(self, background: Background, colour: str, tolerance: float, metres: float) -> tuple:
        """The image's pixels within `tolerance` (on each of red, green and blue) of `colour`, on a grid of
        about `metres` a pixel: (width, height, mask, affine, metres a pixel), the mask one byte a pixel
        row by row (1: road), the affine (a, b, c, d, e, f) putting grid pixel (px, py) at scene
        (a·px + c·py + e, b·px + d·py + f). For network_tools.network_from_background (no Qt there)."""
        image, transform = self.image(background), self.image_transform(background)
        if image is None or transform is None:
            raise ValueError("The background image can't be read.")
        per_pixel = background.width_m / image.width()  # metres an image pixel, as laid
        step = max(metres / per_pixel, 1.0)  # image pixels a grid pixel
        width, height = max(2, round(image.width() / step)), max(2, round(image.height() / step))
        if width * height > 4_000_000:
            raise ValueError("That resolution makes too big a grid: choose a larger resolution_m.")
        small = image.scaled(width, height, Qt.AspectRatioMode.IgnoreAspectRatio,
                             Qt.TransformationMode.SmoothTransformation).convertToFormat(QImage.Format.Format_RGB32)
        target, limit = QColor(colour), int(tolerance)
        red, green, blue = target.red(), target.green(), target.blue()
        data, line = bytes(small.constBits()), small.bytesPerLine()
        mask = bytearray(width * height)
        for y in range(height):
            row = y * line
            for x in range(width):
                at = row + 4 * x  # 0xffRRGGBB in memory as B, G, R, A
                if abs(data[at + 2] - red) <= limit and abs(data[at + 1] - green) <= limit and abs(data[at] - blue) <= limit:
                    mask[y * width + x] = 1
        sx, sy = image.width() / width, image.height() / height  # image pixels a grid pixel, each way
        t = transform
        affine = (t.m11() * sx, t.m12() * sx, t.m21() * sy, t.m22() * sy,
                  t.m11() * 0.5 * sx + t.m21() * 0.5 * sy + t.dx(), t.m12() * 0.5 * sx + t.m22() * 0.5 * sy + t.dy())
        return width, height, mask, affine, per_pixel * (sx + sy) / 2

    def image_rect(self, background: Background) -> QRectF | None:
        """Where the image lies (the bounds of its turned rectangle), or None."""
        image = self.image(background) if background.kind == "image" else None
        transform = self.image_transform(background) if image is not None else None
        return None if transform is None else transform.mapRect(QRectF(image.rect()))

    def extent(self, background: Background, network: QRectF, nodes: QRectF, drawn: QRectF) -> QRectF:
        """What the view may scroll over: the network (`network` drawn with a margin round it, `drawn` without
        one, `nodes` its junctions and ends), with the image; over the map, its work zone and whatever is drawn
        outside it, or 5 km round the network before there is one. While the zone is chosen, only the
        network's centre: the view stays on it, under the box."""
        if self.framing is not None:
            return QRectF(nodes.center() - QPointF(0.5, 0.5), QSizeF(1, 1))
        if background.kind == "map":
            if background.zone:
                return QRectF(*background.zone).united(drawn)
            return network.adjusted(-5000, -5000, 5000, 5000)
        rect = self.image_rect(background)
        return network.united(rect) if rect is not None else network

    def _tile(self, z: int, x: int, y: int) -> QPixmap | None:
        key = (z, x, y)
        if key in self._pixmaps:
            self._pixmaps.move_to_end(key)
            return self._pixmaps[key]
        data = self.tiles.get(z, x, y)
        if data is None:
            return None
        pixmap = QPixmap()
        if not pixmap.loadFromData(data):
            self.tiles.discard(z, x, y)
            return None
        self._pixmaps[key] = pixmap
        while len(self._pixmaps) > TILE_MEMORY:
            self._pixmaps.popitem(last=False)
        return pixmap

    def paint(self, painter: QPainter, rect: QRectF, background: Background, pixels_per_metre: float) -> None:
        """Fill `rect` (scene metres) with the background at this scale."""
        colour = QColor(background.colour)
        painter.fillRect(rect, colour)
        if background.kind == "colour" or self.hidden:
            return
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)  # no blended seams between tiles
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        if background.kind == "image":
            image, transform = self.image(background), self.image_transform(background)
            if image is not None:
                painter.setTransform(transform, True)
                painter.drawImage(QPointF(0, 0), image)
        else:
            frame = map_frame(background)
            z, tiles = frame.tiles(rect.left(), rect.top(), rect.right(), rect.bottom(), frame.zoom(pixels_per_metre))
            painter.translate(background.anchor_x, background.anchor_y)  # tiles are in the map's own metres
            painter.rotate(background.map_rotation_deg)
            overlap = 1 / max(pixels_per_metre, 1e-6)  # a screen pixel, so neighbours leave no gap
            for x, y in tiles:
                pixmap = self._tile(z, x, y)
                if pixmap is not None:
                    left, top, size = frame.tile_rect(z, x, y)
                    painter.drawPixmap(QRectF(left, top, size + overlap, size + overlap), pixmap, QRectF(pixmap.rect()))
        painter.restore()
        if background.opacity < 1:  # the colour laid back over it: the same as drawing it see-through, seamless
            colour.setAlphaF(1 - background.opacity)
            painter.fillRect(rect, colour)

    def reference_line(self, background: Background) -> tuple[float, float, float, float, str] | None:
        """Set Scale's reference line where the image now lies, labelled with its true length (and
        what it measures now, if the image has been resized since)."""
        transform = self.image_transform(background) if background.kind == "image" and background.reference else None
        if transform is None:
            return None
        x1, y1, x2, y2, length = background.reference
        a, b = transform.map(QPointF(x1, y1)), transform.map(QPointF(x2, y2))
        now = hypot(b.x() - a.x(), b.y() - a.y())
        label = f"Reference {length:.2f} m" + (f", now {now:.2f} m" if abs(now - length) > 0.005 * length else "")
        return a.x(), a.y(), b.x(), b.y(), label

    @staticmethod
    def zone_box(viewport: QRectF) -> QRectF:
        """The work zone's box while it is chosen: fixed on screen, centred, ZONE_INSET of the view in from each
        side (so it keeps the view's proportions, and the zone fills the view once chosen)."""
        return viewport.adjusted(viewport.width() * ZONE_INSET, viewport.height() * ZONE_INSET,
                                 -viewport.width() * ZONE_INSET, -viewport.height() * ZONE_INSET)

    def paint_marks(self, painter: QPainter, background: Background | None = None,
                    viewport: QRectF | None = None) -> None:
        """Over the map, screen-sized: the work zone (the box being framed, or the zone chosen) with the map
        outside it dimmed, the reference line (dashed), Measure's line, and the points a tool has picked (a
        cross and its label)."""
        lines = [(line, dashed) for line, dashed in ((self.reference_line(background) if background else None, True),
                                                     (self.line, False), (self.guide, True)) if line]
        zone = None
        if viewport is not None and self.framing is not None:
            zone = QPolygonF(self.zone_box(viewport))
        elif background is not None and background.kind == "map" and background.zone:
            zone = painter.transform().map(QPolygonF(QRectF(*background.zone)))  # a diamond in the 3D view
        if not self.marks and not lines and zone is None:
            return
        places = [(painter.transform().map(QPointF(x, y)), label) for x, y, label in self.marks]
        ends = [(painter.transform().map(QPointF(x1, y1)), painter.transform().map(QPointF(x2, y2)), label, dashed)
                for (x1, y1, x2, y2, label), dashed in lines]
        painter.save()
        painter.resetTransform()
        if zone is not None:  # the map outside dimmed; the edge dashed over a dark one, so it shows on any map
            outside, inside = QPainterPath(), QPainterPath()
            outside.addRect(QRectF(-1e5, -1e5, 2e5, 2e5))  # past any view (a pan's padded image too)
            inside.addPolygon(zone)
            veil = QColor(SHADOW)
            veil.setAlphaF(0.45 if self.framing is not None else 0.3)
            painter.fillPath(outside.subtracted(inside), veil)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            for colour, width in ((QColor(TOOLTIP), 4 if self.framing is not None else 3),
                                  (QColor(ACCENT_EDGE), 2 if self.framing is not None else 1)):
                pen = QPen(colour, width)
                pen.setStyle(Qt.PenStyle.DashLine if colour == QColor(ACCENT_EDGE) else Qt.PenStyle.SolidLine)
                painter.setPen(pen)
                painter.drawPolygon(zone)
            if self.framing:  # its size in its top-left corner, and a cross at its centre
                box = zone.boundingRect()
                label = QRectF(box.topLeft() + QPointF(6, 6),
                               QSizeF(painter.fontMetrics().horizontalAdvance(self.framing) + 12, 22))
                painter.fillRect(label, QColor(TOOLTIP))
                painter.setPen(QColor(TEXT))
                painter.drawText(label, Qt.AlignmentFlag.AlignCenter, self.framing)
                for colour, width in ((QColor(TOOLTIP), 4), (QColor(ACCENT_EDGE), 2)):
                    painter.setPen(QPen(colour, width))
                    painter.drawLine(box.center() + QPointF(-8, 0), box.center() + QPointF(8, 0))
                    painter.drawLine(box.center() + QPointF(0, -8), box.center() + QPointF(0, 8))
        for a, b, label, dashed in ends:
            for colour, width in ((QColor(TOOLTIP), 4), (QColor(ACCENT_EDGE), 2)):
                pen = QPen(colour, width)
                pen.setStyle(Qt.PenStyle.DashLine if dashed and width == 2 else Qt.PenStyle.SolidLine)
                painter.setPen(pen)
                painter.drawLine(a, b)
            places.append(((a + b) / 2, label))
        for point, label in places:
            for colour, width in ((QColor(TOOLTIP), 4), (QColor(ACCENT_EDGE), 2)):
                painter.setPen(QPen(colour, width))
                painter.drawLine(point + QPointF(-8, 0), point + QPointF(8, 0))
                painter.drawLine(point + QPointF(0, -8), point + QPointF(0, 8))
            box = QRectF(point.x() + 8, point.y() - 22, painter.fontMetrics().horizontalAdvance(label) + 8, 18)
            painter.fillRect(box, QColor(TOOLTIP))
            painter.setPen(QColor(TEXT))
            painter.drawText(box, Qt.AlignmentFlag.AlignCenter, label)
        painter.restore()

    @staticmethod
    def paint_attribution(painter: QPainter, viewport: QRectF) -> None:
        """OSM's required credit, in the map's bottom-right corner (screen pixels)."""
        painter.save()
        painter.resetTransform()
        metrics = painter.fontMetrics()
        box = QRectF(0, 0, metrics.horizontalAdvance(ATTRIBUTION) + 12, metrics.height() + 6)
        box.moveBottomRight(viewport.bottomRight() - QPointF(4, 4))
        painter.fillRect(box, QColor(TOOLTIP))
        painter.setPen(QColor(TEXT))
        painter.drawText(box, Qt.AlignmentFlag.AlignCenter, ATTRIBUTION)
        painter.restore()

    def close(self) -> None:
        self.tiles.close()
        self.places.close()


def fit_two_points(picked: list[QPointF], width_px: int) -> tuple[float, float, float, float]:
    """(x_m, y_m, width_m, rotation_deg) that put image pixels p1, p2 exactly on scene points q1, q2
    (picked is [p1, q1, p2, q2]): the similarity q = T + a·p, a = s·e^(iθ), in complex numbers."""
    p1, q1, p2, q2 = (complex(point.x(), point.y()) for point in picked)
    a = (q2 - q1) / (p2 - p1)
    corner = q1 - a * p1
    return corner.real, corner.imag, abs(a) * width_px, degrees(phase(a))


def _framed(span: float) -> float:
    """The ground a place found is framed by: its bounding box, ZONE_DEFAULT_M at least (a street, or no box),
    FLY_MAX_M at most."""
    return min(max(span, ZONE_DEFAULT_M), FLY_MAX_M)


def _distance(metres: float) -> str:
    """1,200 m as "1.20 km", 850 m as "850 m"; a continent's width without decimals."""
    if metres < 1000:
        return f"{metres:.0f} m"
    return f"{metres / 1000:,.{2 if metres < 100_000 else 0}f} km"


class BackgroundPanel(QWidget):
    """The Create Network tab's Own Network section: the background. `changed(key)` follows each edit (an undo merge
    key; ("background", "zone") when a work zone was confirmed); `tool_changed(name)` when a tool starts or ends
    ("move", "align", "measure", "scale", "zone" or None). While a tool is on, the map's clicks come here (press,
    move, release, in scene metres); `redraw` redraws the view while dragging. `bar` is the work zone's bar,
    shown over the map (`view`) while the zone is chosen."""

    changed = Signal(object)
    tool_changed = Signal(object)

    def __init__(self, painter: BackgroundPainter) -> None:
        super().__init__()
        self.painter = painter
        self._network = lambda: None  # set by attach: the network, and where its junctions lie
        self._bounds = lambda: QRectF(0, 0, 1000, 1000)
        self.redraw = lambda: None
        self.view = None  # the map view (network_scene.NetworkView): the work zone is framed on it
        self.tool: str | None = None
        self._picks: list[QPointF] = []  # the two-point tool's clicks: image pixel, scene point, ...
        self._measure: list[QPointF] = []  # Measure's clicks, scene metres (a second one ends a line)
        self._before: Background | None = None  # the background before the work zone's setup, for Esc
        self._asked = ""  # the latest search: only its answer is taken
        self._shown_kind = ""  # the kind last shown: Hide Background is undone by another kind
        self._country_query = ""  # the same, when it was a country: its answer frames the country, not a place
        self._grab: tuple[float, float] | None = None
        self._moved = False
        form = QFormLayout(self)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.FieldsStayAtSizeHint)
        self.kind = ChoiceButtons([("Colour", "colour"), ("Image", "image"), ("Map", "map")])
        self.kind.setToolTip("What shows behind the network")
        self.kind.changed.connect(self._kind_chosen)
        form.addRow("Background", self.kind)

        swatches = QWidget()
        row = QHBoxLayout(swatches)
        row.setContentsMargins(0, 0, 0, 0)
        self.swatches = QButtonGroup(self)
        for name, colour in COLOURS.items():
            swatch = QPushButton()
            swatch.setCheckable(True)
            swatch.setFixedSize(26, 26)
            swatch.setToolTip(f"{name} ({colour})")
            swatch.setAccessibleName(f"{name} background")
            swatch.setStyleSheet(f"QPushButton {{ background: {colour}; min-height: 0; padding: 0; }}")
            self.swatches.addButton(swatch)
            swatch.clicked.connect(lambda _=False, colour=colour: self._set("colour", colour))
            row.addWidget(swatch)
        row.addStretch(1)
        self.colour_label, self.swatches_row = QLabel("Colour"), swatches  # shown for a colour only
        form.addRow(self.colour_label, swatches)

        # An image or a PDF: the file, its page, where its top-left corner sits, how wide, how turned.
        files = QWidget()
        row = QHBoxLayout(files)
        row.setContentsMargins(0, 0, 0, 0)
        self.open_button = icon_button(0xE8E5, "Open Image or PDF")
        self.open_button.clicked.connect(self.open_image)
        self.file_label = hint_label("No file")
        row.addWidget(self.open_button)
        row.addWidget(self.file_label, 1)
        self.page = QSpinBox()
        self.page.setRange(1, 9999)
        self.page.setToolTip("PDF page to show")
        self.width_m = self._number(1.0, 1_000_000.0, " m", "Image width on the ground")
        self.x_m = self._number(-1_000_000.0, 1_000_000.0, " m", "Top-left corner, metres east")
        self.y_m = self._number(-1_000_000.0, 1_000_000.0, " m", "Top-left corner, metres south")
        self.rotation = self._number(-360.0, 360.0, "°", "Image turn, clockwise")
        self.page.valueChanged.connect(lambda value: self._set("page", value - 1))
        for spin, name in ((self.width_m, "width_m"), (self.x_m, "x_m"), (self.y_m, "y_m"),
                           (self.rotation, "rotation_deg")):
            spin.valueChanged.connect(lambda value, name=name: self._set(name, value))
        corner = QWidget()
        row = QHBoxLayout(corner)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self.x_m)
        row.addWidget(self.y_m)
        self.image_rows = []
        for text, widget in (("File", files), ("Page", self.page), ("Width", self.width_m), ("Top-left", corner),
                             ("Turn", self.rotation)):
            label = QLabel(text)
            form.addRow(label, widget)
            self.image_rows.append((label, widget))

        # OpenStreetMap: its work zone (chosen on the map itself, with the bar over it) and its bearing.
        self.zone_label = hint_label("Not chosen")
        self.zone_button = icon_button(0xE774, "Change Work Zone")  # globe
        self.zone_button.clicked.connect(lambda: self.start_tool("zone"))
        zone = QWidget()
        row = QHBoxLayout(zone)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self.zone_label, 1)
        row.addWidget(self.zone_button)
        self.map_rotation = self._number(-360.0, 360.0, "°", "Map turn, clockwise")
        self.map_rotation.valueChanged.connect(lambda value: self._set("map_rotation_deg", value))
        self.map_rows = []
        for text, widget in (("Work zone", zone), ("Turn", self.map_rotation)):
            label = QLabel(text)
            form.addRow(label, widget)
            self.map_rows.append((label, widget))
        self.bar = self._zone_bar()
        painter.found.connect(self.show_found)

        # Dragging the image or map is the way to line it up, so it's the section's primary button: a middle
        # or right drag only pans the view, moving the network and the background together.
        self.move_button = icon_button(0xE7C2, "Drag to Adjust", "primary", checkable=True)
        self.move_button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        form.addRow(self.move_button)

        # Tools to line the image up by points, pin it, measure; and the next step while one is on.
        tools = QWidget()
        row = QHBoxLayout(tools)
        row.setContentsMargins(0, 0, 0, 0)
        self.lock_button = icon_button(0xE72E, "Lock Background", checkable=True)
        self.lock_button.clicked.connect(lambda checked: self._set("locked", checked))
        row.addWidget(self.lock_button)
        # Hide Background: the roads alone once they're drawn over it; only the view, so not an edit or an undo step.
        # It is the map's own control: the window puts it among the map's corner controls, shown over an image or map.
        self.hide_button = icon_button(0xE890, "Hide Background", checkable=True)  # an eye
        self.hide_button.setParent(self)  # until the window places it (never a window of its own)
        self.hide_button.hide()
        self.hide_button.toggled.connect(self._hide)
        self.align_button = icon_button(0xE718, "Two Points", checkable=True)
        self.measure_button = icon_button(0xECC6, "Measure", checkable=True)  # ruler glyph
        for button, name in ((self.move_button, "move"), (self.align_button, "align"),
                             (self.measure_button, "measure")):
            button.clicked.connect(lambda _=False, name=name: self.start_tool(name))
            if button is not self.move_button:
                row.addWidget(button)
        self.scale_button = styled_button("Set Scale…", tooltip="Set the line's true length")
        self.scale_button.clicked.connect(self.set_scale)
        self.scale_button.hide()
        row.addWidget(self.scale_button)
        row.addStretch(1)
        self.tools_label = QLabel("Tools")
        form.addRow(self.tools_label, tools)
        self.tools_row = tools
        self.step = hint_label()
        self.step.setProperty("role", "banner")
        self.step.hide()
        form.addRow(self.step)

        self.opacity = QSpinBox()
        self.opacity.setRange(0, 100)
        self.opacity.setSuffix(" %")
        self.opacity.setToolTip("Background opacity")
        self.opacity.valueChanged.connect(lambda value: self._set("opacity", value / 100))
        self.opacity_label = QLabel("Opacity")
        form.addRow(self.opacity_label, self.opacity)

    @staticmethod
    def _number(low: float, high: float, suffix: str, tip: str) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setRange(low, high)
        spin.setDecimals(1)
        spin.setSuffix(suffix)
        spin.setKeyboardTracking(False)
        spin.setToolTip(tip)
        return spin

    def _zone_bar(self) -> QWidget:
        """The work zone's steps, floating over the map while it is chosen: a country and a place (or coordinates)
        take the map there, Locate Work Zone lets the mouse drag and zoom it under the box, Confirm makes what
        the box covers the work zone, and Cancel (Esc) puts everything back."""
        bar = float_glass(QWidget())
        bar.setStyleSheet(f"QWidget#glassBar {{ background: {PANEL}; }}")  # opaque: its notes read over any map
        bar.hide()
        column = QVBoxLayout(bar)
        column.setContentsMargins(10, 8, 10, 8)
        column.setSpacing(6)
        self.country, self.place = QComboBox(), QComboBox()
        for combo, hint, tip in ((self.country, "Country", "Choose a country"),
                                 (self.place, "Place: a city, a street, an address", "Find a place, then Enter")):
            combo.setEditable(True)
            combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
            combo.lineEdit().setPlaceholderText(hint)
            combo.setToolTip(tip)
        countries = {(QLocale.territoryToString(territory), QLocale.territoryToCode(territory))
                     for territory in QLocale.Country}
        for name, code in sorted((name, code) for name, code in countries
                                 if len(code) == 2 and code.isalpha() and code not in NOT_COUNTRIES):
            self.country.addItem(name, code)
        self.country.setCurrentIndex(-1)
        self.country.completer().setFilterMode(Qt.MatchFlag.MatchContains)  # "land" finds Poland, Finland...
        self.country.completer().setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        self.country.activated.connect(self.find_country)
        self.country.lineEdit().returnPressed.connect(self.find_country)  # Enter on the country shown: no activated
        self.place.setMinimumWidth(320)  # room for a street address
        self.place.lineEdit().returnPressed.connect(self.find_place)
        self.place.activated.connect(lambda index: self.go_to(self.place.itemText(index), *self.place.itemData(index)))
        row = QHBoxLayout()
        row.addWidget(self.country)
        row.addWidget(self.place, 1)
        column.addLayout(row)
        self.lat, self.lon = QDoubleSpinBox(), QDoubleSpinBox()
        for spin, limit, name in ((self.lat, 85.0, "Latitude"), (self.lon, 180.0, "Longitude")):
            spin.setRange(-limit, limit)
            spin.setDecimals(6)
            spin.setSingleStep(0.001)
            spin.setSuffix("°")
            spin.setKeyboardTracking(False)
            spin.setToolTip(f"{name} of box centre")
            spin.editingFinished.connect(self._place_if_moved)
        self.go_button = icon_button(0xE707, "Go to Coordinates")
        self.go_button.clicked.connect(self.place_map)
        self.locate_button = styled_button("Locate Work Zone", tooltip="Drag and zoom the map", checkable=True)
        self.locate_button.toggled.connect(self._locate)
        self.confirm_button = styled_button("Confirm", "primary", "Use this work zone")
        self.confirm_button.clicked.connect(self.confirm_zone)
        cancel = icon_button(0xE711, "Cancel (Esc)")
        cancel.clicked.connect(self.end_tool)
        row = QHBoxLayout()
        for widget in (self.lat, self.lon, self.go_button, self.locate_button, self.confirm_button, cancel):
            row.addWidget(widget)
        column.addLayout(row)
        self.zone_note = hint_label()  # the next step, and the ground the box covers
        self.search_note = hint_label()  # a search's progress, and the place found with its credit
        self.search_note.hide()
        column.addWidget(self.zone_note)
        column.addWidget(self.search_note)
        bar.setMaximumWidth(720)
        return bar

    def attach(self, network, bounds) -> None:
        """`network()` gives the network whose background this edits; `bounds()` where its junctions lie."""
        self._network, self._bounds = network, bounds

    def show_background(self, background: Background) -> None:
        """Fill the section from the background; filling doesn't write back to it. The work zone is shown by
        its size and the place at its centre."""
        widgets = (self.page, self.width_m, self.x_m, self.y_m, self.rotation, self.map_rotation, self.opacity)
        for widget in widgets:
            widget.blockSignals(True)
        self.kind.set_value(background.kind)
        for button in self.swatches.buttons():
            button.setChecked(button.toolTip().endswith(f"({background.colour})"))
        self.file_label.setText(Path(background.image).name if background.image else "No file")
        self.file_label.setToolTip(Path(background.image).name if background.image else "")
        self.page.setValue(background.page + 1)
        self.width_m.setValue(background.width_m)
        self.x_m.setValue(background.x_m)
        self.y_m.setValue(background.y_m)
        self.rotation.setValue(background.rotation_deg)
        if background.zone:
            x, y, width, height = background.zone
            lat, lon = map_frame(background).to_wgs84(x + width / 2, y + height / 2)
            self.zone_label.setText(f"{_distance(width)} × {_distance(height)} at {lat:.5f}°, {lon:.5f}°")
        else:
            self.zone_label.setText("Not chosen")
        self.map_rotation.setValue(background.map_rotation_deg)
        self.opacity.setValue(round(background.opacity * 100))
        for widget in widgets:
            widget.blockSignals(False)
        image, pdf = background.kind == "image", background.image.lower().endswith(".pdf")
        for label, widget in self.image_rows:
            shown = image and (widget is not self.page or pdf)
            label.setVisible(shown)
            widget.setVisible(shown)
        for label, widget in self.map_rows:
            label.setVisible(background.kind == "map")
            widget.setVisible(background.kind == "map")
        pinned = background.locked and background.kind != "colour"
        for label, widget in self.image_rows + self.map_rows:
            if widget is not self.page:
                widget.setEnabled(not pinned)
        self.lock_button.setChecked(background.locked)
        self.colour_label.setVisible(background.kind == "colour")
        self.swatches_row.setVisible(background.kind == "colour")
        self.lock_button.setVisible(background.kind != "colour")
        if background.kind != self._shown_kind:  # another kind (switched, undone, opened, the assistant's): shown
            self.hide_button.setChecked(False)
        self._shown_kind = background.kind
        self.hide_button.setVisible(background.kind != "colour")
        if self.view is not None:
            self.view.place_overlays()  # the map's corner controls, one more or fewer
        self.move_button.setVisible(background.kind != "colour" and self.tool != "zone")
        self.align_button.setVisible(image)
        self.move_button.setEnabled(not pinned and (background.kind == "map" or self.painter.image(background) is not None))
        self._name_move(background)
        self.align_button.setEnabled(not pinned)
        self.opacity_label.setVisible(background.kind != "colour")
        self.opacity.setVisible(background.kind != "colour")
        if self.tool is not None and not self._usable(self.tool, background):
            self.end_tool()
        elif self.tool == "measure":
            self._show_step()  # Set Scale follows the background's kind

    def _name_move(self, background: Background) -> None:
        self.move_button.setText("Done Adjusting" if self.tool == "move" else
                                 f"Drag {'Image' if background.kind == 'image' else 'Map'} to Adjust")

    @staticmethod
    def _usable(tool: str, background: Background) -> bool:
        """Measure works over any background, Set Scale over an image and the work zone over the map, locked
        or not; Move needs an image or the map, Two Points an image, and neither works while it is locked."""
        if tool == "measure":
            return True
        if tool in ("scale", "zone"):
            return background.kind == ("image" if tool == "scale" else "map")
        if background.locked:
            return False
        return background.kind == "image" if tool == "align" else background.kind in ("image", "map")

    def _hide(self, hidden: bool) -> None:
        """Hide Background on or off: the map is drawn again (tile_ready redraws it), the project unchanged."""
        self.painter.hidden = hidden
        self.painter.tile_ready.emit()

    def _set(self, name: str, value) -> None:
        network = self._network()
        if network is None:
            return
        setattr(network.background, name, value)
        self.show_background(network.background)
        self.changed.emit(("background", name))

    def _kind_chosen(self) -> None:
        """Another kind of background, started afresh: only the colour carries over (it shows under every kind).
        Leaving an image or the map asks first, as the switch discards it (its file and scale, or its place and work
        zone; undo brings it back). Colour or Image: at once. Map: its work zone first (Esc goes back to this one)."""
        network = self._network()
        if network is None:
            return
        old, kind = network.background, self.kind.value()
        if kind == old.kind:
            return
        if old.kind in ("image", "map") and QMessageBox.question(
                self, "Switch background",
                f"Switch to {({'colour': 'a ', 'image': 'an '}).get(kind, 'the ')}{kind}? The current "
                f"{'image and its scale' if old.kind == 'image' else 'map and its work zone'} will be discarded."
        ) != QMessageBox.StandardButton.Yes:
            self.kind.set_value(old.kind)  # kept as it is
            return
        network.background = Background(kind=kind, colour=old.colour)
        if kind != "map":
            self.show_background(network.background)
            self.changed.emit(("background", "kind"))
            return
        self.start_tool("zone")  # an undo step once the zone is confirmed, with the switch
        self._before = old

    def open_image(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Open Background", "", "Images and PDFs (*.png *.jpg *.jpeg *.pdf)")
        if path:
            self.use_image(path)

    def use_image(self, path: str) -> bool:
        """Lay the image (or a PDF's first page) under the network, as wide as it and centred on it,
        unturned. False, changing nothing, if it can't be read or is on a network share (refused before it is
        opened, as Network.validate refuses it: models.image_path_problem)."""
        network = self._network()
        problem = image_path_problem(path)
        if problem:
            self.file_label.setText(problem)
            return False
        trial = Background(kind="image", image=path)
        if network is None or self.painter.image(trial) is None:
            self.file_label.setText("Couldn't read that file")
            return False
        image, bounds = self.painter.image(trial), self._bounds()
        width = max(bounds.width(), 100.0)
        height = width * image.height() / image.width()
        centre = bounds.center()
        background = network.background
        background.kind, background.image, background.page, background.rotation_deg = "image", path, 0, 0.0
        background.reference = []  # another drawing: its own scale
        background.width_m, background.x_m, background.y_m = width, centre.x() - width / 2, centre.y() - height / 2
        self.show_background(background)
        self.changed.emit(("background", "image"))
        self.start_tool("scale")  # its scale first
        return True

    # --- the work zone: chosen on the map, under a box fixed on screen ------------------------------

    def _country_code(self) -> str:
        """The country chosen (ISO 3166), or "" when the box holds none of the list's names."""
        index = self.country.currentIndex()
        if index < 0 or self.country.currentText() != self.country.itemText(index):
            return ""
        return str(self.country.itemData(index))

    def find_country(self) -> None:
        """A country chosen: the map goes there, framed (its answer comes to show_found)."""
        name, code = " ".join(self.country.currentText().split()), self._country_code()
        if code and self._asked != name:  # (Enter can bring activated and returnPressed both: asked once)
            self._ask(name, country=True)
            self.painter.places.search(name, code)  # a cached answer comes at once, so asked first

    def find_place(self) -> None:
        """Look the place typed up, within the country chosen (the answer comes to show_found). A place picked
        from the list is gone to by `activated`, not looked up again."""
        text = " ".join(self.place.currentText().split())
        if text and self.place.findText(self.place.currentText()) < 0:
            self._ask(text, country=False)
            self.painter.places.search(text, self._country_code())

    def _ask(self, query: str, country: bool) -> None:
        """The search about to go: only its answer is taken (one still out for an older one is dropped)."""
        self._asked, self._country_query = query, query if country else ""
        self._note(f"Finding {query}…" if country else f"Searching for “{query}”…")

    def show_found(self, query: str, found: object) -> None:
        """The answer to the latest search, while the work zone is chosen: a country is framed; one place is gone
        to at once, several offered in the place list; none, or a failure, says so."""
        if self.tool != "zone" or query != self._asked:
            return
        self._asked = ""
        if isinstance(found, str):
            self._note(found)
        elif not found:
            self._note(f"Nothing found for “{query}”. Try a fuller name, or enter the coordinates.")
        elif query == self._country_query:
            self.fly(*found[0][1:3], _framed(found[0][3]))
            self._note("")
        else:
            self.place.clear()
            for name, *where in found:
                self.place.addItem(name, where)
            self.place.setEditText(query)
            if len(found) == 1:
                self.go_to(*found[0])
            else:
                self._note(f"{len(found)} places match “{query}”: pick one.")
                self.place.showPopup()

    def go_to(self, name: str, lat: float, lon: float, span: float) -> None:
        """A place found: the box goes over it, framing it (`_framed`)."""
        self.fly(lat, lon, _framed(span))
        self._note(f"{name} (search: Nominatim, © OpenStreetMap contributors)")

    def _note(self, text: str) -> None:
        self.search_note.setText(text)
        self.search_note.setVisible(bool(text))
        if self.view is not None:
            self.view.place_overlays()  # the bar as tall as its notes

    def _centre(self) -> tuple[float, float]:
        """(lat, lon) under the box: within Web Mercator's ±85° and ±180° however far the map was dragged."""
        centre = self._bounds().center()
        lat, lon = map_frame(self._network().background).to_wgs84(centre.x(), centre.y())
        return max(-85.0, min(85.0, lat)), (lon + 180) % 360 - 180

    def _place_if_moved(self) -> None:
        if self._network() is not None and self.tool == "zone" and (round(self.lat.value(), 6), round(
                self.lon.value(), 6)) != tuple(round(value, 6) for value in self._centre()):
            self.place_map()

    def place_map(self) -> None:
        """Go: the coordinates entered go under the box, covering the ground it covers now, or ZONE_DEFAULT_M if
        that is more than a work zone may be."""
        span = min(self._zone_m())
        self.fly(self.lat.value(), self.lon.value(), span if max(self._zone_m()) <= ZONE_M[1] else ZONE_DEFAULT_M)

    def fly(self, lat: float, lon: float, span: float) -> None:
        """The box goes over (lat, lon): that point under the network's centre, which the box is on, and the map
        georeferenced there (true to scale), zoomed so the box's shorter side covers `span` metres."""
        network = self._network()
        if network is None or self.view is None:
            return
        background, centre = network.background, self._bounds().center()
        background.kind, background.lat, background.lon = "map", lat, lon
        background.anchor_x, background.anchor_y = centre.x(), centre.y()
        box = self.painter.zone_box(QRectF(self.view.viewport().rect()))
        self._zoom(min(box.width(), box.height()) / span)
        self.show_zone()

    def _zoom_limits(self) -> tuple[float, float]:
        """Pixels a metre while the work zone is chosen: from the whole world across the view to a street."""
        world = 2 * pi * EARTH_RADIUS_M * map_frame(self._network().background).scale  # its width, scene metres
        return self.view.viewport().width() / world, 100.0

    def _zoom(self, scale: float) -> None:
        low, high = self._zoom_limits()
        scale = max(low, min(high, scale))
        self.view.setTransform(QTransform.fromScale(scale, scale))  # the scene is pinned: the box stays put

    def zoom_range(self) -> tuple[float, float] | None:
        """How far the wheel zooms while the work zone is chosen: only with Locate Work Zone on (else None)."""
        return self._zoom_limits() if self.locate_button.isChecked() else None

    def _zone_m(self) -> tuple[float, float]:
        """The ground the box covers now, metres (true at its centre)."""
        network = self._network()
        if network is None or self.view is None:
            return 0.0, 0.0
        frame = map_frame(network.background)
        box = self.painter.zone_box(QRectF(self.view.viewport().rect()))
        per_pixel = cos(radians(self._centre()[0])) / frame.scale / self.view.projection_scale()  # Mercator's scale
        return box.width() * per_pixel, box.height() * per_pixel

    @staticmethod
    def _zone_check(width: float, height: float) -> str:
        """What a box covering width × height metres needs before it can be a work zone (ZONE_M); "" when nothing."""
        if max(width, height) > ZONE_M[1]:
            return f"Zoom in until the box covers {_distance(ZONE_M[1])} or less."
        if min(width, height) < ZONE_M[0]:
            return f"Zoom out until the box covers {_distance(ZONE_M[0])} or more."
        return ""

    def _locate(self, on: bool) -> None:
        """Locate Work Zone: the mouse drags the map under the box and the wheel zooms it."""
        if self.view is not None and self.tool == "zone":
            self.view.set_tool_cursor(Qt.CursorShape.SizeAllCursor if on else Qt.CursorShape.ArrowCursor)
        self.show_zone()

    def show_zone(self) -> None:
        """The bar and the box follow the map: the coordinates under the box's centre, the ground it covers
        (Confirm only while that is a work zone's size), and the next step."""
        if self._network() is None or self.tool != "zone":
            return
        for spin, value in zip((self.lat, self.lon), self._centre()):
            spin.blockSignals(True)
            spin.setValue(value)
            spin.blockSignals(False)
        width, height = self._zone_m()
        self.painter.framing = f"{_distance(width)} × {_distance(height)}"
        step = ("Drag the map under the box; scroll to zoom." if self.locate_button.isChecked() else
                "Choose a country and a place, or enter coordinates; then Locate Work Zone to drag and zoom the map.")
        check = self._zone_check(width, height)
        self.confirm_button.setEnabled(not check)
        self.zone_note.setText(f"{step} {check or 'Confirm when the box frames your work area.'} Esc cancels.")
        if self.view is not None:
            self.view.place_overlays()
        self.redraw()

    def _frame_start(self) -> None:
        """The work zone's setup starts on the zone it has, framed by the box, or else on the whole world."""
        network = self._network()
        if self.view is None or network is None:
            return
        zone, box = network.background.zone, self.painter.zone_box(QRectF(self.view.viewport().rect()))
        self._zoom(min(box.width() / zone[2], box.height() / zone[3]) if zone else 0.0)
        self._note("")
        self.show_zone()

    def confirm_zone(self) -> None:
        """Confirm: what the box covers becomes the work zone, centred on the network, the map georeferenced at
        its centre (true to scale there); one undo step with everything the setup changed."""
        network = self._network()
        if network is None or self.tool != "zone":
            return
        width, height = self._zone_m()
        if self._zone_check(width, height):  # measured now, whatever the button last showed
            self.show_zone()
            return
        background, centre = network.background, self._bounds().center()
        background.lat, background.lon = self._centre()
        background.anchor_x, background.anchor_y = centre.x(), centre.y()
        background.zone = [centre.x() - width / 2, centre.y() - height / 2, width, height]
        self._before = None  # kept: ending the tool doesn't put it back
        self.end_tool()
        self.changed.emit(("background", "zone"))

    # --- tools: the map view's clicks, in scene metres, while one is on ---------------------------

    def start_tool(self, name: str | None) -> None:
        """Start Move, Two Points, Measure, Set Scale or the work zone (again: ends it), where it can work
        (`_usable`). The work zone ended any way but Confirm puts the background back as it was; while it is
        chosen the section is off, the bar over the map its only way in or out."""
        network = self._network()
        if name is not None and (network is None or not self._usable(name, network.background)):
            name = None
        name = None if name == self.tool else name
        if self.tool == "zone" and name != "zone" and self._before is not None and network is not None:
            network.background = self._before  # left unconfirmed: nothing of it kept
        self.tool = name
        self._picks, self._measure, self._grab, self._moved = [], [], None, False
        self._before = deepcopy(network.background) if self.tool == "zone" else None
        self._asked = self._country_query = ""  # a search still out belongs to no setup now
        self.painter.line = None
        self.painter.framing = "" if self.tool == "zone" else None  # the view keeps to the box from now
        if self.tool in ("zone", "scale"):
            self.hide_button.setChecked(False)  # its place or scale is set by looking at it: shown
        self.locate_button.setChecked(False)
        self.bar.setVisible(self.tool == "zone")
        self.setEnabled(self.tool != "zone")
        self.move_button.setChecked(self.tool == "move")
        if network is not None:
            self._name_move(network.background)
        self.align_button.setChecked(self.tool == "align")
        self.measure_button.setChecked(self.tool == "measure")
        if network is not None:
            self.show_background(network.background)
        self._show_step()
        self.tool_changed.emit(self.tool)
        if self.tool == "zone":
            self._frame_start()

    def end_tool(self) -> None:
        """End the tool on (Esc, Cancel, another network or a save coming)."""
        if self.tool is not None:
            self.start_tool(self.tool)

    def _show_step(self, note: str = "") -> None:
        if self.tool == "move":
            text = "Drag the image or map to line it up; the network stays put. Esc finishes."
        elif self.tool == "align":
            text = (note + " " if note else "") + ALIGN_STEPS[len(self._picks)] + " Esc cancels."
        elif self.tool == "measure":
            text = ("Click the first point." if not self._measure else "Click the second point."
                    if len(self._measure) == 1 else f"{self.measured():.2f} m. Click again to measure another.")
            text += " Esc finishes."
        elif self.tool == "scale":
            text = note + ("Set the image's scale: click the start of a line whose length you know (a scale bar, "
                           "a plot's side)." if not self._measure else "Click the line's end." if len(self._measure) == 1
                           else f"{self.measured():.2f} m as laid: enter its true length.")
            text += " Esc skips: it stays as wide as the network."
        elif self.tool == "zone":
            text = "Choose the work zone on the map, with the bar over it. Esc cancels."
        else:
            text = ""
        self.step.setText(text)
        self.step.setVisible(bool(text))
        network = self._network()
        self.scale_button.setVisible(self.tool == "measure" and self.measured() is not None and network is not None
                                     and network.background.kind == "image")
        self._show_marks()

    def measured(self) -> float | None:
        """Measure's distance in metres, once both points are clicked."""
        if len(self._measure) < 2:
            return None
        a, b = self._measure
        return hypot(b.x() - a.x(), b.y() - a.y())

    def hover(self, x: float, y: float) -> None:
        """The mouse over the map: after Measure's first click, the line follows it."""
        if self.tool in ("measure", "scale") and len(self._measure) == 1:
            a = self._measure[0]
            self.painter.line = (a.x(), a.y(), x, y, f"{hypot(x - a.x(), y - a.y()):.2f} m")
            self.redraw()

    def set_scale(self) -> None:
        """Ask the measured line's true length; resize the image about its first point so the line is
        that long, and keep it (in image pixels) as the reference. Works while locked: it's how a pinned
        drawing gets its scale."""
        network, measured = self._network(), self.measured()
        transform = self.painter.image_transform(network.background) if network is not None else None
        if measured is None or transform is None or network.background.kind != "image" or measured < 1e-6:
            return
        length, ok = QInputDialog.getDouble(self, "Set Scale", "The line's true length (m):", round(measured, 2),
                                            0.01, 1_000_000.0, 2)
        if not ok:
            return
        background, factor = network.background, length / measured
        if not 0 < background.width_m * factor <= 1_000_000.0:
            self.step.setText("That scale would make the image too large. Esc finishes.")
            return
        inverse, _ = transform.inverted()
        (a, b), pixels = self._measure, [inverse.map(point) for point in self._measure]
        background.width_m *= factor  # a similarity about a: a stays, everything else scales by factor
        background.x_m = a.x() - factor * (a.x() - background.x_m)
        background.y_m = a.y() - factor * (a.y() - background.y_m)
        background.reference = [pixels[0].x(), pixels[0].y(), pixels[1].x(), pixels[1].y(), length]
        self.end_tool()
        self.show_background(background)
        self.changed.emit(("background", "scale"))

    def _show_marks(self) -> None:
        network = self._network()
        transform = self.painter.image_transform(network.background) if network is not None else None
        marks = []
        marks += [(point.x(), point.y(), "AB"[i]) for i, point in enumerate(self._measure)]
        for i, point in enumerate(self._picks):
            number = i // 2 + 1
            if i % 2 == 0 and transform is not None:  # an image pixel: where it is now
                spot = transform.map(point)
                marks.append((spot.x(), spot.y(), f"{number} on the image"))
            elif i % 2:
                marks.append((point.x(), point.y(), f"{number} goes here"))
        self.painter.marks = marks
        self.redraw()

    def press(self, x: float, y: float) -> bool:
        """A left press on the map view; True if the tool took it (the network doesn't see it)."""
        network = self._network()
        if self.tool is None or network is None:
            return False
        background = network.background
        if self.tool in ("move", "zone"):  # the work zone's map moves only with Locate Work Zone on
            self._grab = (x, y) if self.tool == "move" or self.locate_button.isChecked() else None
            self._moved = False
            return True
        if self.tool in ("measure", "scale"):
            self._measure = [QPointF(x, y)] if len(self._measure) != 1 else self._measure + [QPointF(x, y)]
            a, b = self._measure[0], self._measure[-1]
            self.painter.line = (a.x(), a.y(), b.x(), b.y(), f"{self.measured():.2f} m") if len(self._measure) == 2 else None
            self._show_step()
            return True
        transform = self.painter.image_transform(background)
        if transform is None:
            self.end_tool()
            return True
        if len(self._picks) % 2 == 0:  # on the image: which pixel
            inverse, _ = transform.inverted()
            self._picks.append(inverse.map(QPointF(x, y)))
        else:
            self._picks.append(QPointF(x, y))
        if len(self._picks) < 4:
            self._show_step()
            return True
        if (self._picks[0] - self._picks[2]).manhattanLength() < 1:  # the same image point twice
            self._picks = []
            self._show_step("The two points on the image must differ.")
            return True
        image = self.painter.image(background)
        background.x_m, background.y_m, background.width_m, background.rotation_deg = \
            fit_two_points(self._picks, image.width())
        background.rotation_deg = (background.rotation_deg + 180) % 360 - 180
        self.end_tool()
        self.show_background(background)
        self.changed.emit(("background", "align"))
        return True

    def move(self, x: float, y: float) -> None:
        network = self._network()
        if self.tool not in ("move", "zone") or self._grab is None or network is None:
            return
        dx, dy = x - self._grab[0], y - self._grab[1]
        self._grab = (x, y)
        background = network.background
        if background.kind == "image":
            background.x_m += dx
            background.y_m += dy
        elif background.kind == "map":
            background.anchor_x += dx
            background.anchor_y += dy
        self._moved = True
        if self.tool == "zone":
            self.show_zone()  # the coordinates under the box follow (and it redraws)
        else:
            self.redraw()

    def release(self, x: float, y: float) -> None:
        network = self._network()
        if self.tool == "move" and self._moved and network is not None:  # the work zone's waits for Confirm
            self.show_background(network.background)
            self.changed.emit(("background", "move"))
        self._grab, self._moved = None, False
        if self.tool == "scale" and self.measured() is not None:
            QTimer.singleShot(0, self._ask_scale)  # once the click is over

    def _ask_scale(self) -> None:
        """Set Scale's line is drawn: ask its length. Cancelled (or impossible), click it again."""
        if self.tool != "scale" or self.measured() is None:
            return
        self.set_scale()
        if self.tool == "scale":
            self._measure, self.painter.line = [], None
            self._show_step("Not set. ")

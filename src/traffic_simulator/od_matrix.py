"""Origin-destination matrix: a compact grid plus a side-panel editor for one pair, and the
Demand Generator's Institutions panel (institutions.py places the buildings).

Two kinds of demand: hourly (veh/h per pair) or daily (trips per day per pair, spread over
the 24 hours by a curve the user shapes; see daily_profile.py).

A Run spawns vehicles from the values shown (hourly or daily); a project file saves them
(main.MainWindow._project, network_io).
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from copy import deepcopy
from dataclasses import dataclass, field, replace
from itertools import accumulate
from math import sqrt

from PySide6.QtCore import QEvent, QPoint, QPointF, QRectF, Qt, QTime, Signal
from PySide6.QtGui import QColor, QFont, QKeySequence, QPainter, QPainterPath, QPen, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QSizePolicy,
    QSpinBox,
    QStyle,
    QStyledItemDelegate,
    QTableWidget,
    QTableWidgetItem,
    QTableWidgetSelectionRange,
    QTimeEdit,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from . import theme
from .daily_profile import ARRIVAL_PATTERNS, DEGREES, HOURS, MIN_GAP_H, DailyProfile, phf_of, phf_quarters, quarter_shares
from .institutions import KINDS, catchment, door, hourly_people, hourly_trips, shares
from .mode_behavior import VehicleModes, fixed_split, redistribute, whole_percent
from .models import BUILDING_HEIGHTS_M, MAX_AREA_M2, MAX_BUILDING_HEIGHT_M, Institution, Network, institution_problem
from .building_assets import height as building_height
from .properties_panel import hint_label, icon_button, styled_button

# Categorical slots 1-3 of the dataviz reference palette (its dark-surface steps), in
# fixed order. Validated on the grid grey: all checks pass, worst adjacent colour-blind
# dE 9.4. On the lighter panel grey bus/car dip under 3:1, so the slider always labels them.
DEFAULT_SPLIT = (10, 85, 5)  # the three starting vehicle types' shares (bus, car, bike)
TEXT = QColor(theme.TEXT)
SUBDUED = QColor("#2a2e33")  # diagonal cells, a shade darker than the grid: no trip there
SELECTED_FILL, SELECTED_EDGE = QColor(theme.BANNER), QColor(theme.ACCENT_EDGE)
HANDLE_EDGE = QColor("#9ca3af")
GRID = QColor("#454b53")  # curve editor grid lines
BARS = QColor(37, 99, 235, 110)  # hourly share bars: the accent, subdued
UNITS = {"hourly": "veh/h", "daily": "veh/day"}
CELL_PX = (56, 40)  # a grid cell's width and height at 100 %
ZOOM_RANGE, FIT_MAX = (0.25, 4.0), 2.0  # Fit Grid stops at 200 % so a small grid doesn't balloon
PAN_BUTTONS = (Qt.MouseButton.MiddleButton, Qt.MouseButton.RightButton)  # as on the map


@dataclass
class ODCell:
    flow: int | None = None  # hourly demand, veh/h; None until entered
    daily: int | None = None  # daily demand, trips per day; None until entered
    profile: DailyProfile = field(default_factory=DailyProfile)  # how the day's trips spread
    pattern: str = "Random"
    split: tuple[int, ...] = DEFAULT_SPLIT  # percent per vehicle type, in Mode Behavior's order; sums to 100
    quarters: tuple[float, ...] = ()  # hourly demand: each 15 minutes' share of the hour (its PHF); () even


def _mode_text(names: list[str], split: tuple[int, ...]) -> str:
    return " · ".join(f"{name} {share}%" for name, share in zip(names, split))


class ModeSplitSlider(QWidget):
    """Handles cut 0-100 % into one zone per vehicle type (Mode Behavior's list, in its colours),
    so the shares always sum to 100: one handle between each pair of neighbouring types.

    Drag a handle (or click the track to move the nearest one). With the slider focused,
    Left/Right move the active handle 1 % (Page Up/Down 10 %) and Space moves to the next
    handle. set_share() takes a typed percentage for one type.
    """

    changed = Signal()  # the user moved a handle; set_split() stays quiet
    MARGIN, TRACK_TOP, TRACK_HEIGHT, HANDLE_RADIUS = 10, 8, 12, 8

    def __init__(self, vehicle_modes: VehicleModes) -> None:
        super().__init__()
        self.vehicle_modes = vehicle_modes
        self.cuts = self._cuts(vehicle_modes.default_split)  # where each zone ends, but the last
        self.fixed: set[int] = set()  # timetabled types: their share follows their headway
        self.active = 0
        self._tie: list[int] = []  # handles pressed where they overlap: the first move picks one
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMinimumHeight(30)
        self.setToolTip("Split trips between types")

    @staticmethod
    def _cuts(split) -> list[int]:
        return list(accumulate(split))[:-1]

    def split(self) -> tuple[int, ...]:
        bounds = [0, *self.cuts, 100]
        return tuple(end - start for start, end in zip(bounds, bounds[1:]))

    def set_split(self, split, fixed: set[int] | None = None) -> None:
        self.cuts = self._cuts(split)
        if fixed is not None:
            self.fixed = set(fixed)
        free = self._free_handles()
        if self.active not in free:
            self.active = free[0] if free else 0
        self.update()

    def _locked(self, handle: int) -> bool:
        """A handle beside a timetabled type can't move: that would change its fixed share."""
        return handle in self.fixed or handle + 1 in self.fixed

    def _free_handles(self) -> list[int]:
        return [handle for handle in range(len(self.cuts)) if not self._locked(handle)]

    def set_share(self, index: int, value: int) -> None:
        """Give type `index` this share by moving the handle after it (the last type, or one
        before a timetabled type: the handle before it), so that neighbour absorbs the
        difference and the total stays 100. Handles it pushes past move along too; a
        timetabled type's own share can't be set."""
        bounds = [0, *self.cuts, 100]
        last = len(bounds) - 2
        if last == 0 or index in self.fixed:
            return  # one type is always 100 %; a timetabled one follows its headway
        value = min(max(int(value), 0), 100)
        if index < last and not self._locked(index):
            right = min(bounds[index] + value, 100)
            bounds[index + 1] = right
            if index > 0:
                bounds[index] = right - value  # only moves when the right edge hit 100
            for j in range(index + 2, last + 1):
                bounds[j] = max(bounds[j], right)
        elif index > 0 and not self._locked(index - 1):
            bounds[index] = max(bounds[index + 1] - value, 0)
        else:
            return
        for j in range(min(index, last) - 1, 0, -1):
            bounds[j] = min(bounds[j], bounds[j + 1])
        if bounds[1:-1] != self.cuts:
            self.cuts = bounds[1:-1]
            self.changed.emit()
            self.update()

    def _x(self, value: float) -> float:
        return self.MARGIN + (self.width() - 2 * self.MARGIN) * value / 100

    def _value(self, x: float) -> int:
        share = (x - self.MARGIN) / max(1, self.width() - 2 * self.MARGIN) * 100
        return round(min(max(share, 0), 100))

    def _move(self, index: int, value: int) -> None:
        if self._locked(index):
            return
        low = self.cuts[index - 1] if index > 0 else 0
        high = self.cuts[index + 1] if index < len(self.cuts) - 1 else 100
        value = min(max(value, low), high)
        if value != self.cuts[index]:
            self.cuts[index] = value
            self.changed.emit()
            self.update()

    def paintEvent(self, event) -> None:  # type intentionally inherited from Qt
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        top, height = self.TRACK_TOP, self.TRACK_HEIGHT
        bounds = [0, *self.cuts, 100]
        for mode, (start, end) in zip(self.vehicle_modes.modes, zip(bounds, bounds[1:])):
            left, right = self._x(start), self._x(end)
            # 2 px surface gap between segments; rounded outer ends only.
            left += 1 if start > 0 else 0
            right -= 1 if end < 100 else 0
            if right - left > 0.5:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(mode.colour))
                painter.drawRoundedRect(QRectF(left, top, right - left, height), 4, 4)
        for index, cut in enumerate(self.cuts):
            active = self.hasFocus() and index == self.active
            painter.setPen(QPen(SELECTED_EDGE if active else HANDLE_EDGE, 2.5 if active else 1.5))
            painter.setBrush(HANDLE_EDGE if self._locked(index) else QColor("#ffffff"))  # grey: fixed
            painter.drawEllipse(QRectF(self._x(cut) - self.HANDLE_RADIUS, top + height / 2 - self.HANDLE_RADIUS,
                                       2 * self.HANDLE_RADIUS, 2 * self.HANDLE_RADIUS))

    def mousePressEvent(self, event) -> None:  # type intentionally inherited from Qt
        free = self._free_handles()
        if not free:
            return
        value = self._value(event.position().x())
        nearest = min(abs(value - self.cuts[i]) for i in free)
        closest = [i for i in free if abs(value - self.cuts[i]) == nearest]
        stacked = [i for i in free if self.cuts[i] == self.cuts[closest[0]]]
        self._tie = stacked if len(stacked) > 1 and nearest <= 2 else []
        if not self._tie:
            self.active = closest[0]
            self._move(self.active, value)
        self.update()

    def mouseMoveEvent(self, event) -> None:  # type intentionally inherited from Qt
        if not self.cuts:
            return
        value = self._value(event.position().x())
        if self._tie and value != self.cuts[self._tie[0]]:  # stacked handles: pull out the one heading that way
            self.active, self._tie = (min(self._tie) if value < self.cuts[self._tie[0]] else max(self._tie)), []
        if not self._tie:
            self._move(self.active, value)

    def keyPressEvent(self, event) -> None:  # type intentionally inherited from Qt
        steps = {Qt.Key.Key_Left: -1, Qt.Key.Key_Right: 1, Qt.Key.Key_PageDown: -10, Qt.Key.Key_PageUp: 10}
        if not self.cuts:
            super().keyPressEvent(event)
        elif event.key() in steps:
            self._move(self.active, self.cuts[self.active] + steps[event.key()])
        elif event.key() == Qt.Key.Key_Space:
            free = self._free_handles()
            self.active = next((i for i in free if i > self.active), free[0] if free else 0)
            self.update()
        else:
            super().keyPressEvent(event)

    def focusInEvent(self, event) -> None:  # type intentionally inherited from Qt
        self.update()
        super().focusInEvent(event)

    def focusOutEvent(self, event) -> None:  # type intentionally inherited from Qt
        self.update()
        super().focusOutEvent(event)


class CurveEditor(QWidget):
    """A day's demand curve to shape by hand, like an image editor's Curves tool.

    Free curve: drag a point; click empty space to add one; double-click or right-click a
    point to remove it (the two ends stay, at 00:00 and 24:00). A polynomial curve is shaped
    by its coefficients instead. The bars under the curve are each hour's share of the day.
    """

    changed = Signal()  # an edit finished: a drag, or a point added or removed
    LEFT, RIGHT, TOP, BOTTOM, RADIUS = 10, 10, 8, 20, 5

    def __init__(self) -> None:
        super().__init__()
        self.profile = DailyProfile()
        self.daily_total = 0  # trips per day, for the hour tooltips
        self._drag: int | None = None  # index of the point being dragged
        self._top = 1.0  # the level at the top of the plot
        self.setMinimumHeight(200)
        self.setMouseTracking(True)
        # ponytail: mouse only; add arrow-key nudging of a focused point if keyboard use matters.

    def set_profile(self, profile: DailyProfile) -> None:
        self.profile = deepcopy(profile)
        self._drag = None
        self.update()

    def _plot(self) -> QRectF:
        return QRectF(self.LEFT, self.TOP, self.width() - self.LEFT - self.RIGHT,
                      self.height() - self.TOP - self.BOTTOM)

    def _screen(self, hour: float, level: float) -> QPointF:
        plot = self._plot()
        return QPointF(plot.left() + plot.width() * hour / HOURS, plot.bottom() - plot.height() * level / self._top)

    def _hour_level(self, position: QPointF) -> tuple[float, float]:
        plot = self._plot()
        hour = (position.x() - plot.left()) / max(plot.width(), 1) * HOURS
        level = (plot.bottom() - position.y()) / max(plot.height(), 1) * self._top
        return min(max(hour, 0.0), float(HOURS)), min(max(level, 0.0), 1.0)

    def _point_at(self, position: QPointF) -> int | None:
        if self.profile.kind != "points":
            return None
        for index, (hour, level) in enumerate(self.profile.points):
            point = self._screen(hour, level)
            if (point.x() - position.x()) ** 2 + (point.y() - position.y()) ** 2 <= (self.RADIUS + 4) ** 2:
                return index
        return None

    def paintEvent(self, event) -> None:  # type intentionally inherited from Qt
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        plot = self._plot()
        levels = self.profile.hourly_levels()
        curve = [(i * HOURS / 240, self.profile.level(i * HOURS / 240)) for i in range(241)]
        self._top = 1.0 if self.profile.kind == "points" else max(1.0, max(level for _, level in curve) * 1.05)
        painter.fillRect(plot, QColor(theme.SURFACE))
        painter.setPen(QPen(GRID, 1))
        for fraction in (0.25, 0.5, 0.75):
            y = plot.bottom() - plot.height() * fraction
            painter.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
        font = QFont(self.font())
        font.setPointSizeF(8)
        painter.setFont(font)
        for hour in range(0, HOURS + 1, 3):
            x = plot.left() + plot.width() * hour / HOURS
            painter.setPen(QPen(GRID, 1))
            painter.drawLine(QPointF(x, plot.top()), QPointF(x, plot.bottom()))
            painter.setPen(QColor(theme.SECONDARY_TEXT))
            painter.drawText(QRectF(x - 15, plot.bottom() + 3, 30, 14), Qt.AlignmentFlag.AlignCenter, f"{hour:02d}")
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(BARS)
        width = plot.width() / HOURS
        for hour, level in enumerate(levels):  # 2 px gaps between bars
            top = self._screen(hour, level).y()
            painter.drawRect(QRectF(plot.left() + hour * width + 1, top, width - 2, plot.bottom() - top))
        path = QPainterPath(self._screen(*curve[0]))
        for hour, level in curve[1:]:
            path.lineTo(self._screen(hour, min(level, self._top)))
        painter.setPen(QPen(SELECTED_EDGE, 2))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(path)
        if self.profile.kind == "points":
            for index, (hour, level) in enumerate(self.profile.points):
                painter.setPen(QPen(SELECTED_EDGE, 2))
                painter.setBrush(SELECTED_EDGE if index == self._drag else QColor("#ffffff"))
                painter.drawEllipse(self._screen(hour, level), self.RADIUS, self.RADIUS)

    def mousePressEvent(self, event) -> None:  # type intentionally inherited from Qt
        index = self._point_at(event.position())
        if event.button() == Qt.MouseButton.RightButton:
            self._remove(index)
        elif event.button() == Qt.MouseButton.LeftButton and self.profile.kind == "points":
            if index is None:  # a new point here, if it isn't crowding a neighbour
                hour, level = self._hour_level(event.position())
                points = self.profile.points
                if all(abs(hour - existing) >= MIN_GAP_H for existing, _ in points):
                    points.append((hour, level))
                    points.sort()
                    index = points.index((hour, level))
            self._drag = index
            self.update()

    def mouseMoveEvent(self, event) -> None:  # type intentionally inherited from Qt
        hour, level = self._hour_level(event.position())
        if self._drag is None:
            slot = min(int(hour), HOURS - 1)
            share = self.profile.shares()[slot]
            trips = f" ≈ {self.daily_total * share:.0f} veh" if self.daily_total > 0 else ""
            QToolTip.showText(event.globalPosition().toPoint(),
                              f"{slot:02d}:00–{slot + 1:02d}:00: {share:.1%} of the day{trips}", self)
            return
        points = self.profile.points
        if self._drag in (0, len(points) - 1):  # the ends stay at midnight
            hour = points[self._drag][0]
        else:
            hour = min(max(hour, points[self._drag - 1][0] + MIN_GAP_H), points[self._drag + 1][0] - MIN_GAP_H)
        points[self._drag] = (hour, level)
        self.update()

    def mouseReleaseEvent(self, event) -> None:  # type intentionally inherited from Qt
        if self._drag is not None:
            self._drag = None
            self.update()
            self.changed.emit()

    def mouseDoubleClickEvent(self, event) -> None:  # type intentionally inherited from Qt
        self._remove(self._point_at(event.position()))

    def _remove(self, index: int | None) -> None:
        points = self.profile.points
        if index is not None and 0 < index < len(points) - 1:
            del points[index]
            self._drag = None
            self.update()
            self.changed.emit()


class _CellDelegate(QStyledItemDelegate):
    """Paints a cell: blank until it has a flow, then the value over a thin mode bar."""

    def __init__(self, matrix: ODMatrix) -> None:
        super().__init__(matrix)
        self.matrix = matrix

    def paint(self, painter: QPainter, option, index) -> None:
        rect = option.rect
        pair = self.matrix.pair_at(index.row(), index.column())
        painter.save()
        if pair is None:
            painter.fillRect(rect, SUBDUED)
            painter.restore()
            return
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        if selected:
            painter.fillRect(rect, SELECTED_FILL)
        cell = self.matrix.cells.get(pair)
        value = None if cell is None else self.matrix.value(cell)
        if value is not None:
            zoom = self.matrix.zoom
            font = QFont(option.font)
            font.setPointSizeF(9 * zoom)
            painter.setFont(font)
            painter.setPen(TEXT)
            painter.drawText(QRectF(rect).adjusted(0, 2 * zoom, 0, -9 * zoom), Qt.AlignmentFlag.AlignCenter, f"{value:g}")
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            left, width, y = rect.left() + 7 * zoom, rect.width() - 14 * zoom, rect.bottom() - 8 * zoom
            start = 0.0
            for colour, share in zip((mode.colour for mode in self.matrix.vehicle_modes.modes), self.matrix.split_of(cell)):
                if share:
                    x0 = left + width * start / 100 + (1 if start > 0 else 0)
                    x1 = left + width * (start + share) / 100 - (1 if start + share < 100 else 0)
                    painter.setPen(Qt.PenStyle.NoPen)
                    painter.setBrush(QColor(colour))
                    painter.drawRoundedRect(QRectF(x0, y, max(x1 - x0, 1), 4 * zoom), 2 * zoom, 2 * zoom)
                start += share
        if selected:
            painter.setPen(QPen(SELECTED_EDGE, 2))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(rect.adjusted(1, 1, -1, -1))
        painter.restore()

    def helpEvent(self, event, view, option, index) -> bool:
        pair = self.matrix.pair_at(index.row(), index.column())
        if pair is None:
            text = "A trip can't start and end at the same boundary end."
        else:
            cell = self.matrix.cell(pair)
            value, unit = self.matrix.value(cell), UNITS[self.matrix.mode]
            flow = f"no {self.matrix.mode} demand entered" if value is None else f"{value:g} {unit}"
            if self.matrix.mode == "daily" and value is not None:
                shares = cell.profile.shares()
                peak = max(range(HOURS), key=shares.__getitem__)
                flow += f", peak {peak:02d}:00–{peak + 1:02d}:00 ≈ {value * shares[peak]:.0f} veh/h"
            text = f"From {pair[0]} to {pair[1]}: {flow}\n{cell.pattern} arrivals · {_mode_text(self.matrix.vehicle_modes.names(), self.matrix.split_of(cell))}"
        QToolTip.showText(event.globalPos(), text, view)
        return True


class ODMatrix(QWidget):
    """The grid. Rows are origins (from), columns destinations (to). It zooms and pans like the
    map: the wheel zooms at the cursor, a right or middle drag pans, and its title row has Zoom Out,
    Zoom In and Fit Grid."""

    current_changed = Signal()  # the current cell or the selection changed

    def __init__(self, vehicle_modes: VehicleModes | None = None) -> None:
        super().__init__()
        self.vehicle_modes = vehicle_modes or VehicleModes()  # Mode Behavior's list: the split's types
        self.vehicle_modes.changed.connect(self._vehicle_modes_changed)
        self.cells: dict[tuple[str, str], ODCell] = {}  # kept across rebuilds, keyed by end names
        self.origins: list[str] = []
        self.destinations: list[str] = []
        self.mode = "hourly"  # which demand the grid shows and edits: "hourly" or "daily" (set_mode)
        self.zoom = 1.0
        self._pan: QPoint | None = None  # where a right or middle drag last was
        title = QLabel("Origin–Destination Matrix")
        title.setStyleSheet("font-size: 18px; font-weight: 700;")
        self.corner = QHBoxLayout()  # the grid's controls, top right (main.py adds the Input Grid toggle)
        self.corner.setSpacing(4)
        for glyph, name, action in ((0xE71F, "Zoom Out", lambda: self.set_zoom(self.zoom / 1.25)),
                                    (0xE8A3, "Zoom In", lambda: self.set_zoom(self.zoom * 1.25)),
                                    (0xE9A6, "Fit Grid", self.fit)):
            button = icon_button(glyph, name)
            button.clicked.connect(action)
            self.corner.addWidget(button)
        title_row = QHBoxLayout()
        title_row.addWidget(title)
        title_row.addStretch(1)
        title_row.addLayout(self.corner)
        self.summary = hint_label()
        self.empty = hint_label("This network has no boundary ends yet. Add ends in the "
                                "Edit Network tab (Add Edge → End) to fill the matrix.")
        self.table = QTableWidget()
        # Ctrl+click adds/removes cells, Shift+click or Shift+arrows select a range.
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setItemDelegate(_CellDelegate(self))
        self.table.setCornerButtonEnabled(False)
        # Cells are sized by the zoom (set_zoom); the scrollbars count pixels, so panning is smooth.
        for header in (self.table.horizontalHeader(), self.table.verticalHeader()):
            header.setMinimumSectionSize(4)
            header.setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        self.table.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.table.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.table.viewport().installEventFilter(self)  # the wheel zooms, right or middle drag pans
        self.set_zoom(1.0)
        self.table.currentCellChanged.connect(lambda *_: self.current_changed.emit())
        self.table.itemSelectionChanged.connect(self.current_changed.emit)
        layout = QVBoxLayout(self)
        layout.addLayout(title_row)
        layout.addWidget(hint_label("Rows are origins (From), columns are destinations (To). "
                                    "Click a cell to edit it in the side panel; Ctrl+click to "
                                    "pick several and edit them together. Arrow keys move between "
                                    "cells; type a number to enter the flow. Empty cells have no "
                                    "data yet."))
        layout.addWidget(self.summary)
        layout.addWidget(self.empty)
        layout.addWidget(self.table, 1)

    def set_ends(self, origins: list[str], destinations: list[str]) -> None:
        """Rebuild for these ends; entered pairs that still exist keep their data."""
        if (origins, destinations) == (self.origins, self.destinations):
            return
        self.origins, self.destinations = origins, destinations
        self.empty.setVisible(not origins or not destinations)
        self.table.setVisible(bool(origins and destinations))
        self.table.clear()
        self.table.setRowCount(len(origins))
        self.table.setColumnCount(len(destinations))
        self.table.setVerticalHeaderLabels([f"From {end}" for end in origins])
        self.table.setHorizontalHeaderLabels([f"To {end}" for end in destinations])
        for row, origin in enumerate(origins):
            for column, destination in enumerate(destinations):
                item = QTableWidgetItem()
                if origin == destination:
                    item.setFlags(Qt.ItemFlag.NoItemFlags)  # not selectable; the cursor skips it
                self.table.setItem(row, column, item)
        self._update_summary()
        self.current_changed.emit()

    def set_mode(self, mode: str) -> None:
        """Show and edit hourly or daily demand (the Demand Generator's Demand drop-down)."""
        if mode != self.mode:
            self.mode = mode
            self.refresh()
            self.current_changed.emit()

    def set_zoom(self, zoom: float, at: QPoint | None = None) -> None:
        """Cells, their text and the headers at `zoom` (1: 56 × 40 px cells), keeping the point under
        `at` (viewport pixels; the middle by default) where it is, as the map zooms at the cursor."""
        zoom = min(max(zoom, ZOOM_RANGE[0]), ZOOM_RANGE[1])
        table, ratio = self.table, zoom / self.zoom
        at = at or table.viewport().rect().center()
        bars = ((table.horizontalScrollBar(), at.x()), (table.verticalScrollBar(), at.y()))
        spots = [(bar.value() + offset) * ratio - offset for bar, offset in bars]
        self.zoom = zoom
        for header, size in zip((table.horizontalHeader(), table.verticalHeader()), CELL_PX):
            header.setStyleSheet(f"QHeaderView {{ font-size: {10 * zoom:.2f}pt; }}")  # the app's sheet sets 10 pt
            header.setDefaultSectionSize(int(size * zoom))
        table.updateGeometries()  # the scroll ranges now, not on the next pass of the event loop
        for (bar, _), spot in zip(bars, spots):
            bar.setValue(round(spot))
        table.viewport().update()

    def fit(self) -> None:
        """Zoom so the whole grid shows, up to FIT_MAX."""
        table = self.table
        if not (self.origins and self.destinations):
            return
        for _ in range(3):  # the headers' text scales but their padding doesn't: repeat until it settles
            width = table.width() - 2 * table.frameWidth() - table.verticalHeader().width()
            height = table.height() - 2 * table.frameWidth() - table.horizontalHeader().height()
            self.set_zoom(min(width / (len(self.destinations) * CELL_PX[0]),
                              height / (len(self.origins) * CELL_PX[1]), FIT_MAX))

    def eventFilter(self, watched, event) -> bool:  # type intentionally inherited from Qt
        """On the grid, as on the map: the wheel zooms at the cursor, a right or middle drag pans."""
        kind, bars = event.type(), (self.table.horizontalScrollBar(), self.table.verticalScrollBar())
        if kind == QEvent.Type.Wheel and event.angleDelta().y():  # a sideways wheel still scrolls
            self.set_zoom(self.zoom * (1.18 if event.angleDelta().y() > 0 else 1 / 1.18), event.position().toPoint())
            return True
        if kind == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
            index = self.table.indexAt(event.position().toPoint())
            if not index.isValid() or self.pair_at(index.row(), index.column()) is None:
                self.clear_selection()  # beside the cells or on the diagonal: no pair, the editor goes
                return True
        if kind in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonDblClick) and event.button() in PAN_BUTTONS:
            self._pan = event.position().toPoint()
            watched.setCursor(Qt.CursorShape.ClosedHandCursor)
            return True
        if kind == QEvent.Type.MouseMove and self._pan is not None:
            delta, self._pan = event.position().toPoint() - self._pan, event.position().toPoint()
            bars[0].setValue(bars[0].value() - delta.x())
            bars[1].setValue(bars[1].value() - delta.y())
            return True
        if kind == QEvent.Type.MouseButtonRelease and event.button() in PAN_BUTTONS and self._pan is not None:
            self._pan = None
            watched.unsetCursor()
            return True
        return super().eventFilter(watched, event)

    def clear_selection(self) -> None:
        """No cell selected (Esc, another tab): the OD Pair editor goes."""
        self.table.clearSelection()
        self.table.setCurrentCell(-1, -1)

    def value(self, cell: ODCell) -> int | None:
        """The cell's demand of the kind shown: veh/h, or trips per day."""
        return cell.flow if self.mode == "hourly" else cell.daily

    def pair_at(self, row: int, column: int) -> tuple[str, str] | None:
        if not (0 <= row < len(self.origins) and 0 <= column < len(self.destinations)):
            return None
        origin, destination = self.origins[row], self.destinations[column]
        return None if origin == destination else (origin, destination)

    def selected_pairs(self) -> list[tuple[str, str]]:
        """Selected pairs in grid order (the diagonal is never selectable)."""
        indexes = sorted((index.row(), index.column()) for index in self.table.selectedIndexes())
        return [pair for pair in (self.pair_at(*rc) for rc in indexes) if pair is not None]

    def current_pair(self) -> tuple[str, str] | None:
        """The pair the panel shows: the last-clicked cell, if still selected."""
        selected = self.selected_pairs()
        current = self.pair_at(self.table.currentRow(), self.table.currentColumn())
        return current if current in selected else (selected[0] if selected else None)

    def cell(self, pair: tuple[str, str]) -> ODCell:
        """A copy (edits go through store()); a pair with no data yet starts from the default split."""
        return replace(self.cells[pair]) if pair in self.cells else ODCell(split=self.vehicle_modes.default_split)

    def _vehicle_modes_changed(self, action: str, index: int) -> None:
        """Keep every pair's shares in step with the vehicle types: a new type starts at 0 %,
        a deleted type's share goes to the others in proportion. A headway changes nothing
        stored: split_of() fixes timetabled shares when read. (Undo restores cells itself.)"""
        for cell in self.cells.values():
            if action == "added":
                cell.split = (*cell.split, 0)
            elif action == "deleted":
                cell.split = redistribute(cell.split, index)
        self.refresh()

    def split_of(self, cell: ODCell) -> tuple[int, ...]:
        """The split in effect: each timetabled type (a headway in Mode Behavior) fixed at its
        vehicles over the pair's trips, per hour against the hourly flow, else per day against
        the daily trips (running all 24 hours); the other types share the rest in their stored
        proportions. Stored splits keep those proportions, so no flow typed on the way loses them."""
        per_hour = self.vehicle_modes.vehicles_per_hour()
        if cell.flow is not None:
            return fixed_split(cell.split, per_hour, cell.flow)
        return fixed_split(cell.split, {i: count * 24 for i, count in per_hour.items()}, cell.daily)

    def store(self, pair: tuple[str, str], cell: ODCell | None) -> None:
        """Save a pair's data (None clears it) and repaint."""
        if cell is None:
            self.cells.pop(pair, None)
        else:
            self.cells[pair] = cell
        self.refresh()

    def refresh(self) -> None:
        self.table.viewport().update()
        self._update_summary()

    def pairs(self, scope: str) -> list[tuple[str, str]]:
        """Pairs in the current cell's "row" or "column", or every "empty" pair (no flow)."""
        current = self.current_pair()
        every = [(o, d) for o in self.origins for d in self.destinations if o != d]
        if scope == "empty":
            return [pair for pair in every if self.value(self.cells.get(pair, ODCell())) is None]
        if current is None:
            return []
        position = 0 if scope == "row" else 1
        return [pair for pair in every if pair[position] == current[position]]

    def move_next(self) -> None:
        """Select the next pair to the right, wrapping to the next row and skipping the diagonal."""
        rows, columns = len(self.origins), len(self.destinations)
        position = self.table.currentRow() * columns + self.table.currentColumn()
        for step in range(1, rows * columns + 1):
            row, column = divmod((position + step) % (rows * columns), columns)
            if self.pair_at(row, column) is not None:
                self.table.setCurrentCell(row, column)
                return

    def step(self, rows: int, columns: int) -> None:
        """Move to the neighbouring pair up, down, left or right, hopping the diagonal; stays at the edge."""
        row, column = self.table.currentRow() + rows, self.table.currentColumn() + columns
        while 0 <= row < len(self.origins) and 0 <= column < len(self.destinations):
            if self.pair_at(row, column) is not None:
                self.table.setCurrentCell(row, column)
                return
            row, column = row + rows, column + columns

    def _update_summary(self) -> None:
        total = sum(1 for o in self.origins for d in self.destinations if o != d)
        entered = sum(1 for pair, cell in self.cells.items() if self.value(cell) is not None
                      and pair[0] in self.origins and pair[1] in self.destinations)
        self.summary.setText(f"{entered} of {total} pairs have {self.mode} demand: a Run spawns from them, "
                             "and Save Project keeps them.")


class ODCellPanel(QWidget):
    """Side-panel editor for the selected pair; stays open while you move through the grid."""

    changed = Signal(object)  # OD data changed; arg = undo merge key (typing into one cell)
    demand_chosen = Signal(str)  # the Demand drop-down: "hourly", "daily" or "institutions"

    def __init__(self, matrix: ODMatrix) -> None:
        super().__init__()
        self.matrix = matrix
        # generate_demand's numbers: (pairs, args, values, shares) -> (plan {pair: value}, info), set by the
        # window (network_tools.demand_plan on its network); None: that tool can't run here. scale_demand's
        # likewise: (values, pairs, args, shares) -> (plan, info) (network_tools.scale_plan).
        self.planner = None
        self.scaler = None
        # A degree of saturation, checked in a run: (pairs typed) -> (share of them that holds, report), set by
        # the window; None: not checked.
        self.checker = None
        self.calibrator = None  # calibrate_demand's numbers: (hourly flows, args) -> (plan, info), set by the window
        # equilibrate_demand's: (hourly flows, args, check_only) -> (plan, info), set by the window (its runs)
        self.equilibrator = None
        self.chooser = None  # choose_modes' split: (pairs, args, values) -> ({pair: {type: share}}, info), the window's
        # Which demand to enter: choosing one shows its options for the selected pairs below.
        self.demand = QComboBox()
        self.demand.addItem("Hourly demand (veh/h)", "hourly")
        self.demand.addItem("Daily demand (veh/day)", "daily")
        self.demand.addItem("Institutions", "institutions")
        self.demand.setToolTip("Hourly or daily demand")
        self.demand.currentIndexChanged.connect(self._demand_chosen)
        demand_form = QFormLayout()
        demand_form.addRow("Demand", self.demand)
        self.institutions = InstitutionPanel()
        self.institutions.hide()
        # The OD Pair editor shows only while a cell is selected; until then, one line says how.
        self.empty_hint = hint_label("Click a cell in the grid to edit its demand.")
        self.empty_hint.setToolTip("Ctrl+click to select several")
        self.pair_group = group = QGroupBox("OD Pair")
        layout = QVBoxLayout(group)
        self.fields = QWidget()
        fields = QVBoxLayout(self.fields)
        fields.setContentsMargins(0, 0, 0, 0)
        self.pair_label = QLabel()
        self.pair_label.setStyleSheet("font-weight: 600;")
        fields.addWidget(self.pair_label)
        self.selection_hint = hint_label("Showing the last-clicked pair. A change to a field sets "
                                         "that field on every selected pair; their other values stay.")
        fields.addWidget(self.selection_hint)
        form = QFormLayout()
        self.flow = QSpinBox()
        self.flow.setRange(-1, 100_000)  # -1 shows "—": not entered yet
        self.flow.setSpecialValueText("—")
        self.flow.setSingleStep(1)
        self.flow.setSuffix(" veh/h")
        self.flow.setToolTip("Trips per hour")
        self.pattern = QComboBox()
        self.pattern.addItems(ARRIVAL_PATTERNS)
        # Hourly demand's peak-hour factor (HCM): the busiest 15 minutes run at the hour's flow / PHF.
        self.phf = QDoubleSpinBox()
        self.phf.setRange(0.25, 1.0)
        self.phf.setDecimals(2)
        self.phf.setSingleStep(0.05)
        self.phf.setToolTip("Peak-hour factor")
        self.modes = ModeSplitSlider(matrix.vehicle_modes)
        self.flow_label = QLabel("Flow")
        form.addRow(self.flow_label, self.flow)
        form.addRow("Arrival pattern", self.pattern)
        self.phf_label = QLabel("PHF")
        form.addRow(self.phf_label, self.phf)
        form.addRow("Modes", self.modes)
        # One box per vehicle type: swatch + name (the legend) over its number box.
        self.mode_shares: list[QSpinBox] = []
        self.shares_widget = QWidget()
        self.shares = QGridLayout(self.shares_widget)
        self.shares.setContentsMargins(0, 0, 0, 0)
        self.shares.setHorizontalSpacing(8)
        self._build_shares()
        form.addRow(self.shares_widget)
        fields.addLayout(form)
        # Daily demand: how the day's trips spread over 24 hours.
        self.daily_box = QWidget()
        daily = QVBoxLayout(self.daily_box)
        daily.setContentsMargins(0, 4, 0, 4)
        heading = QLabel("Spread over the day")
        heading.setStyleSheet("font-weight: 600;")
        daily.addWidget(heading)
        self.curve = CurveEditor()
        self.curve.setToolTip("Drag points to shape day")
        daily.addWidget(self.curve)
        self.peak = hint_label()
        daily.addWidget(self.peak)
        curve_form = QFormLayout()
        curve_form.setContentsMargins(0, 0, 0, 0)
        self.curve_kind = QComboBox()
        self.curve_kind.addItem("Free curve (drag points)", "points")
        for degree in DEGREES:
            self.curve_kind.addItem(f"Polynomial, degree {degree}", degree)
        self.curve_kind.setToolTip("Curve kind")
        self.curve_kind.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.curve_kind.setMinimumContentsLength(12)  # may shorten its text rather than widen the sidebar
        kind_row = QHBoxLayout()
        kind_row.addWidget(self.curve_kind, 1)
        self.reset_curve = icon_button(0xE787, "Weekday Shape")
        kind_row.addWidget(self.reset_curve)
        curve_form.addRow("Equation", kind_row)
        self.coefficients: list[QDoubleSpinBox] = []
        grid = QGridLayout()
        for power in range(max(DEGREES) + 1):
            box = QDoubleSpinBox()
            box.setRange(-1e6, 1e6)
            box.setDecimals(3)
            box.setSingleStep(0.05)
            box.setPrefix(f"a{power} = ")
            # Share the row instead of sizing for ±1,000,000, which would widen the whole sidebar.
            box.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
            box.setMinimumWidth(90)
            box.valueChanged.connect(lambda value, power=power: self._coefficient_edited(power, value))
            grid.addWidget(box, power // 3, power % 3)
            self.coefficients.append(box)
        self.coefficient_grid = QWidget()
        self.coefficient_grid.setLayout(grid)
        grid.setContentsMargins(0, 0, 0, 0)
        curve_form.addRow(self.coefficient_grid)
        self.formula = hint_label()
        curve_form.addRow(self.formula)
        daily.addLayout(curve_form)
        fields.addWidget(self.daily_box)
        fields.addWidget(hint_label("Copy the ticked items from this cell to its row, its column "
                                    "or every empty cell:"))
        self.copy_choices = {"pattern": QCheckBox("Arrival pattern"), "modes": QCheckBox("Modes"),
                             "flow": QCheckBox("Flow"), "curve": QCheckBox("Daily curve")}
        self.copy_choices["pattern"].setChecked(True)
        self.copy_choices["modes"].setChecked(True)
        self.copy_choices["flow"].setToolTip("Needs a flow here")
        copy_grid = QGridLayout()  # two by two: one row of four is wider than the sidebar
        for index, box in enumerate(self.copy_choices.values()):
            box.toggled.connect(self._refresh_copy)
            copy_grid.addWidget(box, index // 2, index % 2)
        copy_grid.setColumnStretch(2, 1)
        fields.addLayout(copy_grid)
        self.copy_buttons = []
        apply_row = QHBoxLayout()
        for scope, text, tip in (("row", "Row", "Pairs from this origin"),
                                 ("column", "Column", "Pairs to this destination"),
                                 ("empty", "Empty Cells", "Pairs with no flow yet")):
            button = styled_button(text, tooltip=tip)
            button.clicked.connect(lambda _=False, scope=scope: self.apply_to(scope))
            apply_row.addWidget(button)
            self.copy_buttons.append(button)
        fields.addLayout(apply_row)
        self.clear_button = styled_button("Clear Cell", "danger", "Clear the selected pairs")
        self.clear_button.clicked.connect(self.clear_cell)
        fields.addWidget(self.clear_button)
        self.status = hint_label()
        fields.addWidget(self.status)
        layout.addWidget(self.fields)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addLayout(demand_form)
        outer.addWidget(self.empty_hint)
        outer.addWidget(group)
        outer.addWidget(self.institutions)

        self.flow.valueChanged.connect(lambda: self._edited("flow"))
        self.pattern.currentIndexChanged.connect(lambda: self._edited("pattern"))
        self.phf.valueChanged.connect(lambda: self._edited("quarters"))
        self.modes.changed.connect(lambda: self._edited("modes"))
        self.modes.changed.connect(self._sync_shares)
        self.curve.changed.connect(lambda: self._edited("profile"))
        self.curve_kind.activated.connect(self._kind_chosen)
        self.reset_curve.clicked.connect(lambda: self._set_profile(DailyProfile()))
        for key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            shortcut = QShortcut(QKeySequence(key), self.flow)
            shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            shortcut.activated.connect(self._next_cell)
        # Spreadsheet keys (eventFilter): type a number on the matrix, arrows in the Flow box.
        matrix.table.installEventFilter(self)
        self.flow.installEventFilter(self)
        matrix.table.activated.connect(self._edit_flow)  # Enter or double-click on a cell
        matrix.current_changed.connect(self.show_current)
        matrix.vehicle_modes.changed.connect(self._vehicle_modes_changed)
        self.show_current()

    def _demand_chosen(self) -> None:
        """Hourly or daily: the OD pair editor over the grid. Institutions: its panel (the map)."""
        kind = self.demand.currentData()
        self.institutions.setVisible(kind == "institutions")
        if kind != "institutions":
            self.matrix.set_mode(kind)
        self._show_editor()
        self.demand_chosen.emit(kind)

    def _show_editor(self) -> None:
        """The OD Pair editor while a cell is selected (hourly or daily); else the one-line hint."""
        pairs = self.demand.currentData() != "institutions"
        picked = self.matrix.current_pair() is not None
        self.pair_group.setVisible(pairs and picked)
        self.empty_hint.setVisible(pairs and not picked)

    def show_current(self) -> None:
        pair = self.matrix.current_pair()
        self._show_editor()
        self.status.clear()
        if pair is None:
            return
        cell = self.matrix.cell(pair)
        count = len(self.matrix.selected_pairs())
        self.pair_label.setText(f"{count} pairs selected" if count > 1 else f"From {pair[0]} to {pair[1]}")
        self.selection_hint.setVisible(count > 1)
        self.clear_button.setText(f"Clear {count} Cells" if count > 1 else "Clear Cell")
        for widget in (self.flow, self.pattern, self.phf, self.modes):
            widget.blockSignals(True)  # loading a cell must not mark it as entered
        daily = self.matrix.mode == "daily"
        self.flow_label.setText("Trips per day" if daily else "Flow")
        self.flow.setSuffix(f" {UNITS[self.matrix.mode]}")
        self.flow.setMaximum(2_000_000 if daily else 100_000)
        self.copy_choices["flow"].setText("Trips/day" if daily else "Flow")
        self.copy_choices["curve"].setVisible(daily)
        self.daily_box.setVisible(daily)
        self.clear_button.setToolTip(f"Clear selected {self.matrix.mode} demand")
        value = self.matrix.value(cell)
        self.flow.setValue(-1 if value is None else value)
        self.pattern.setCurrentText(cell.pattern)
        self.phf.setValue(phf_of(cell.quarters))
        self.phf.setVisible(not daily)  # no PHF on a daily curve
        self.phf_label.setVisible(not daily)
        self.modes.set_split(self.matrix.split_of(cell), set(self.matrix.vehicle_modes.vehicles_per_hour()))
        self._sync_shares()
        for widget in (self.flow, self.pattern, self.phf, self.modes):
            widget.blockSignals(False)
        self._show_profile(cell.profile)
        self._refresh_copy()
        if self.flow.hasFocus():
            self.flow.selectAll()

    def _edited(self, field: str) -> None:
        """Set just this field on every selected pair."""
        pairs = self.matrix.selected_pairs()
        for pair in pairs:
            cell = self.matrix.cell(pair)
            if field == "flow":
                value = None if self.flow.value() < 0 else self.flow.value()
                if self.matrix.mode == "daily":
                    cell.daily = value
                else:
                    cell.flow = value
            elif field == "profile":
                cell.profile = deepcopy(self.curve.profile)
            elif field == "pattern":
                cell.pattern = self.pattern.currentText()
            elif field == "quarters":
                cell.quarters = phf_quarters(self.phf.value())
            else:
                cell.split = self.modes.split()  # the other types' proportions, as set
            self.matrix.cells[pair] = cell
        if pairs and field in ("flow", "modes"):  # timetabled shares follow the pair's own trips
            self.modes.set_split(self.matrix.split_of(self.matrix.cell(self.matrix.current_pair())))
            self._sync_shares()
        if pairs:
            self.matrix.refresh()
            self.changed.emit(("od", tuple(pairs), field))  # typing into one field: one undo step
        self._refresh_copy()
        self._show_peak()

    def _show_profile(self, profile: DailyProfile) -> None:
        """Load a curve into the editor, the equation dropdown and its coefficients."""
        self.curve.set_profile(profile)
        poly = profile.kind == "poly"
        self.curve_kind.setCurrentIndex(self.curve_kind.findData(len(profile.coefficients) - 1 if poly else "points"))
        self.coefficient_grid.setVisible(poly)
        for power, box in enumerate(self.coefficients):
            box.setVisible(poly and power < len(profile.coefficients))
            box.blockSignals(True)
            box.setValue(profile.coefficients[power] if poly and power < len(profile.coefficients) else 0.0)
            box.blockSignals(False)
        terms = " + ".join(["a0", "a1·t", *(f"a{p}·t{'²³⁴⁵⁶'[p - 2]}" for p in range(2, len(profile.coefficients)))])
        self.formula.setText(f"level = {terms}, with t = hour ÷ 24; below zero counts as zero." if poly else
                             "Drag the points to shape the day; click to add one; double-click or "
                             "right-click to remove one. Only the shape matters: the hours share the "
                             "day's trips in proportion to the curve.")
        self._show_peak()

    def _set_profile(self, profile: DailyProfile) -> None:
        self._show_profile(profile)
        self._edited("profile")

    def _kind_chosen(self) -> None:
        """Change the curve's kind, keeping its shape as closely as the new kind allows."""
        kind, current = self.curve_kind.currentData(), self.curve.profile
        if kind == "points":
            self._set_profile(current if current.kind == "points" else current.as_points())
        else:
            self._set_profile(current.as_polynomial(kind))

    def _coefficient_edited(self, power: int, value: float) -> None:
        profile = self.curve.profile
        if profile.kind == "poly" and power < len(profile.coefficients):
            profile.coefficients[power] = value
            self.curve.update()
            self._edited("profile")

    def _show_peak(self) -> None:
        """The busiest hour of the curve, and its trips when a daily total is entered."""
        total = self.flow.value() if self.matrix.mode == "daily" else -1
        self.curve.daily_total = max(total, 0)
        shares = self.curve.profile.shares()
        peak = max(range(HOURS), key=shares.__getitem__)
        trips = f" ≈ {total * shares[peak]:.0f} veh/h" if total > 0 else ""
        self.peak.setText(f"Busiest hour {peak:02d}:00–{peak + 1:02d}:00: {shares[peak]:.1%} of the day{trips}.")

    def _build_shares(self) -> None:
        """One % box per vehicle type, four to a row, each headed by its swatch and name."""
        while self.shares.count():
            gone = self.shares.takeAt(0).widget()
            gone.hide()
            gone.deleteLater()
        self.mode_shares = []
        per_hour = self.matrix.vehicle_modes.vehicles_per_hour()
        for index, mode in enumerate(self.matrix.vehicle_modes.modes):
            column = QWidget()
            stack = QVBoxLayout(column)
            stack.setContentsMargins(0, 0, 0, 0)
            stack.setSpacing(2)
            heading = QHBoxLayout()
            swatch = QLabel()
            swatch.setFixedSize(10, 10)
            swatch.setStyleSheet(f"background: {mode.colour}; border-radius: 2px;")
            heading.addWidget(swatch)
            heading.addWidget(QLabel(mode.name), 1)
            stack.addLayout(heading)
            share = QSpinBox()
            share.setRange(0, 100)
            share.setSuffix(" %")
            share.setToolTip(f"{mode.name} share of trips")
            if index in per_hour:  # timetabled: shown, not typed
                share.setReadOnly(True)
                share.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
                share.setToolTip(f"{mode.name}: fixed by its timetable")
            share.valueChanged.connect(lambda value, index=index: self.modes.set_share(index, value))
            stack.addWidget(share)
            self.shares.addWidget(column, index // 4, index % 4)
            self.mode_shares.append(share)
        for column in range(4):
            self.shares.setColumnStretch(column, 1)

    def _vehicle_modes_changed(self, *_) -> None:
        """The vehicle types changed in Mode Behavior: new % boxes, and the pair's new shares."""
        self._build_shares()
        pair = self.matrix.current_pair()
        self.modes.set_split(self.matrix.split_of(self.matrix.cell(pair)) if pair else self.matrix.vehicle_modes.default_split,
                             set(self.matrix.vehicle_modes.vehicles_per_hour()))
        self._sync_shares()

    def _sync_shares(self) -> None:
        """Show the slider's shares in the number boxes (without echoing back)."""
        for box, share in zip(self.mode_shares, self.modes.split()):
            box.blockSignals(True)
            box.setValue(share)
            box.blockSignals(False)

    def _next_cell(self) -> None:
        self.matrix.move_next()
        self._edit_flow()

    def _edit_flow(self, *_) -> None:
        """Into the Flow box, its value selected so typing replaces it."""
        self.flow.setFocus()
        self.flow.selectAll()

    def eventFilter(self, watched, event) -> bool:  # type intentionally inherited from Qt
        """Typing a number on the matrix starts that cell's flow; in the Flow box the arrow
        keys move to the neighbouring cell (Enter moves to the next one, see _next_cell)."""
        if event.type() == QEvent.Type.KeyPress and self.matrix.current_pair() is not None:
            if watched is self.matrix.table and event.text().isdigit():
                self._edit_flow()
                QApplication.sendEvent(self.flow, event)  # the digit replaces the selected value
                return True
            steps = {Qt.Key.Key_Up: (-1, 0), Qt.Key.Key_Down: (1, 0), Qt.Key.Key_Left: (0, -1),
                     Qt.Key.Key_Right: (0, 1)}
            if watched is self.flow and event.key() in steps and not event.modifiers():
                self.matrix.step(*steps[event.key()])
                self._edit_flow()
                return True
        return super().eventFilter(watched, event)

    def _refresh_copy(self) -> None:
        """Flow can only be copied from a cell that has one; copying needs something ticked."""
        has_flow = self.flow.value() >= 0
        flow_box = self.copy_choices["flow"]
        flow_box.setEnabled(has_flow)
        if not has_flow:
            flow_box.setChecked(False)
        anything = any(box.isChecked() for box in self.copy_choices.values())
        for button in self.copy_buttons:
            button.setEnabled(anything)

    def apply_to(self, scope: str) -> None:
        pairs = self.matrix.pairs(scope)  # "empty" is decided before any flow is copied
        copying = {key for key, box in self.copy_choices.items() if box.isChecked() and box.isEnabled()}
        pattern, split, flow = self.pattern.currentText(), self.modes.split(), self.flow.value()
        daily = self.matrix.mode == "daily"
        if not daily:
            copying.discard("curve")
        for pair in pairs:
            cell = self.matrix.cell(pair)
            if "pattern" in copying:
                cell.pattern = pattern
            if "modes" in copying:
                cell.split = split
            if "flow" in copying:
                if daily:
                    cell.daily = flow
                else:
                    cell.flow = flow
            if "curve" in copying:
                cell.profile = deepcopy(self.curve.profile)
            self.matrix.cells[pair] = cell
        self.matrix.refresh()
        self.changed.emit(None)
        names = [label for key, label in (("pattern", f"{pattern} arrivals"), ("modes", _mode_text(self.matrix.vehicle_modes.names(), split)),
                                          ("flow", f"{flow} {UNITS[self.matrix.mode]}"),
                                          ("curve", "the daily curve")) if key in copying]
        self.status.setText(f"Copied {', '.join(names)} to {len(pairs)} pair{'s' if len(pairs) != 1 else ''}.")

    def clear_cell(self) -> None:
        """Remove the demand of the kind shown (hourly, or daily with its curve) from every
        selected pair; a pair with no demand left is cleared entirely."""
        pairs = self.matrix.selected_pairs()
        for pair in pairs:
            cell = self.matrix.cell(pair)
            if self.matrix.mode == "daily":
                cell.daily, cell.profile = None, DailyProfile()
            else:
                cell.flow = None
            if cell.flow is None and cell.daily is None:
                self.matrix.cells.pop(pair, None)
            else:
                self.matrix.cells[pair] = cell
        if pairs:
            self.matrix.refresh()
            self.show_current()
            self.changed.emit(None)


    # --- Worked from the chat: AI Mode's demand tools use these same controls -----------

    def command(self, name: str, args: dict, check_only: bool = False) -> dict:
        """Run an AI Mode demand tool (read_od_matrix, set_od_demand, shape_daily_curve) by
        working this panel's controls the way a user does: select the pairs in the grid,
        type the value, pick the pattern, set the mode shares, load the curve. What the tool
        changes shows in the Demand Generator as it happens, and it is one undo step.
        Raises ValueError, with a message the model can act on, for anything it can't do;
        check_only runs just those checks, so a bad change is refused before the user is asked."""
        if name == "read_od_matrix":
            return self._read_for_assistant()
        if not self.matrix.origins or not self.matrix.destinations:
            raise ValueError("The network has no boundary ends to make trips between.")
        shown = self.matrix.mode
        if name in ("generate_demand", "calibrate_demand", "scale_demand", "choose_modes", "equilibrate_demand"):
            done = {"generate_demand": self._generate, "calibrate_demand": self._calibrate,
                    "scale_demand": self._scale, "choose_modes": self._choose,
                    "equilibrate_demand": self._equilibrate}[name](args, check_only)
        else:
            done = self._enter(name, args, check_only)
        if not check_only and isinstance(done, dict):  # which demand a Run now uses
            done["runs_use"] = f"{self.matrix.mode} demand"
            if self.matrix.mode != shown:
                done["note"] = (f"The matrix now shows {self.matrix.mode} demand, so a Run uses it, not the "
                                f"{shown} demand.")
        return done

    def _enter(self, name: str, args: dict, check_only: bool) -> dict:
        """set_od_demand and shape_daily_curve: tried first (and put back) when only checking, so one that
        would change nothing, or a pattern or split alone for pairs with no such demand, is refused."""
        pairs = self._pairs_from(args)
        plan = self._demand_plan(args) if name == "set_od_demand" else _profile_from(args)
        if name == "set_od_demand" and plan[1] is None:  # a pattern or split alone
            daily = plan[0] == "daily"
            if not any((self.matrix.cell(pair).daily if daily else self.matrix.cell(pair).flow) for pair in pairs):
                raise ValueError(f"Those pairs have no {plan[0]} demand yet: give a value too. Nothing was changed.")

        def apply() -> dict:
            return self._set_demand(pairs, *plan) if name == "set_od_demand" else self._shape_curve(pairs, plan)

        before, mode = deepcopy(self.matrix.cells), self.matrix.mode
        self.blockSignals(True)  # each control would fire "changed": the tool is one undo step
        try:
            done = apply()
            changed = self.matrix.cells != before
            if check_only:  # put it all back: this was only the check
                self.matrix.cells = before
                self._show_mode(mode)
                self.matrix.refresh()
                self.show_current()  # the panel's fields as they were
        finally:
            self.blockSignals(False)
        if not changed:
            raise ValueError("Nothing would change: those pairs already have that. Nothing was changed.")
        if check_only:
            return {"ok": True}
        self.changed.emit(None)
        return done

    def _generate(self, args: dict, check_only: bool) -> dict:
        """generate_demand: the planner's gravity-model numbers typed into the grid pair by pair (pairs
        with the same number selected together), pairs it gives none cleared; one undo step."""
        if self.planner is None:
            raise ValueError("Demand can't be generated here: there's no network to measure routes on.")
        scope = self._pairs_from(args)
        demand, _, pattern, shares, _ = self._demand_plan({**args, "value": 0})  # checks demand, pattern, split
        plan, info = self.planner(scope, {**args, "demand": demand}, *self._demand_now(demand, scope))
        if check_only:
            return {"ok": True}
        self._type_in({pair: plan.get(pair) for pair in scope}, demand, args, info, pattern, shares)
        return info

    def _type_in(self, plan: dict, demand: str, args: dict, info: dict, pattern=None, shares=None) -> None:
        """A demand tool's numbers typed into the grid ({pair: value}; None or 0 clears), pairs with the same
        number selected together, the whole tool one undo step. With a degree_of_saturation the window's
        checker then runs it: what doesn't hold is backed off and typed in again (info says how). A failed
        check puts the matrix back."""
        before = deepcopy(self.matrix.cells)

        def type_values(values: dict, with_options: bool) -> None:
            groups: dict[int, list] = defaultdict(list)
            for pair, value in values.items():
                groups[value or -1].append(pair)  # -1: none, "—"
            for value, pairs in groups.items():
                self._set_demand(pairs, demand, value, *((pattern, shares) if with_options else (None, None)))

        self.blockSignals(True)
        try:
            type_values(plan, True)
            if args.get("degree_of_saturation") is not None and self.checker is not None:
                typed = {pair: value for pair, value in plan.items() if value}
                share, report = self.checker(list(typed))
                info["checked_in_a_run"] = report
                if share is not None and share < 1:
                    type_values({pair: round(value * share) for pair, value in typed.items()}, False)
                    _lead_with_what_was_kept(info, share, sum(round(value * share) for value in typed.values()))
        except Exception:
            self.matrix.cells = before
            self.matrix.refresh()
            raise
        finally:
            self.blockSignals(False)
        if self.matrix.cells != before:
            self.changed.emit(None)

    def _demand_now(self, demand: str, scope: list) -> tuple[dict, dict | None]:
        """The matrix's demand of this kind ({pair: veh/h or trips/day}) and, for daily demand, the 24
        hourly shares of those pairs and of `scope` (a new pair's: the default curve's)."""
        daily = demand == "daily"
        values = {pair: cell.daily if daily else cell.flow for pair, cell in self.matrix.cells.items()
                  if pair[0] in self.matrix.origins and pair[1] in self.matrix.destinations
                  and (cell.daily if daily else cell.flow)}
        shares = {pair: self.matrix.cell(pair).profile.shares() for pair in {*values, *scope}} if daily else None
        return values, shares

    def _scale(self, args: dict, check_only: bool) -> dict:
        """scale_demand: the demand shown, scaled (the window's scaler), typed into the grid; one undo step."""
        if self.scaler is None:
            raise ValueError("Demand can't be scaled here: there's no network to route on.")
        demand, scope = self.matrix.mode, self._pairs_from(args)
        values, shares = self._demand_now(demand, scope)
        plan, info = self.scaler(values, scope, {**args, "demand": demand}, shares)
        if check_only:
            return {"ok": True}
        self._type_in(plan, demand, args, info)
        return info

    def _calibrate(self, args: dict, check_only: bool) -> dict:
        """calibrate_demand: the hourly flows fitted to counts (the window's calibrator) typed into the grid."""
        if self.calibrator is None:
            raise ValueError("Demand can't be calibrated here: there's no network to route on.")
        flows = {pair: cell.flow for pair, cell in self.matrix.cells.items()
                 if cell.flow and pair[0] in self.matrix.origins and pair[1] in self.matrix.destinations}
        plan, info = self.calibrator(flows, args)
        if check_only:
            return {"ok": True}
        cleared = [pair for pair in flows if not plan.get(pair)]
        if cleared:
            info["pairs_cleared"] = len(cleared)  # fitted under 0.5 veh/h: their demand goes
        before = deepcopy(self.matrix.cells)
        groups: dict[int, list] = defaultdict(list)
        for pair in flows:
            groups[plan.get(pair, -1)].append(pair)
        self.blockSignals(True)
        try:
            for value, pairs in groups.items():
                self._set_demand(pairs, "hourly", value, None, None)
        finally:
            self.blockSignals(False)
        if self.matrix.cells != before:
            self.changed.emit(None)
        return info

    def _equilibrate(self, args: dict, check_only: bool) -> dict:
        """equilibrate_demand: hourly flows moved to their elastic equilibrium (the window's equilibrator, its runs),
        typed into the grid; one undo step."""
        if self.equilibrator is None:
            raise ValueError("Demand can't be equilibrated here: there's no network to run.")
        if self.matrix.mode != "hourly":
            raise ValueError("equilibrate_demand works on hourly demand: the matrix shows daily (set_od_demand or "
                             "generate_demand with demand 'hourly' switches it).")
        flows = {pair: cell.flow for pair, cell in self.matrix.cells.items()
                 if cell.flow and pair[0] in self.matrix.origins and pair[1] in self.matrix.destinations}
        if not flows:
            raise ValueError("There's no hourly demand to equilibrate.")
        plan, info = self.equilibrator(flows, args, check_only)
        if check_only:
            return {"ok": True}
        before = deepcopy(self.matrix.cells)
        groups: dict[int, list] = defaultdict(list)
        for pair in flows:
            groups[plan.get(pair, round(flows[pair]))].append(pair)
        self.blockSignals(True)
        try:
            for value, pairs in groups.items():
                self._set_demand(pairs, "hourly", value, None, None)
        finally:
            self.blockSignals(False)
        if self.matrix.cells != before:
            self.changed.emit(None)
        return info

    def _choose(self, args: dict, check_only: bool) -> dict:
        """choose_modes: the chooser's logit split per pair, typed into each pair's mode shares (pairs with the
        same whole-percent split selected together); one undo step."""
        if self.chooser is None:
            raise ValueError("Modes can't be chosen here: there's no network to route on.")
        demand, scope = self.matrix.mode, self._pairs_from(args)
        names = self.matrix.vehicle_modes.names()
        current = {pair: {name: share / 100 for name, share in zip(names, self.matrix.split_of(self.matrix.cell(pair)))}
                   for pair in scope}  # its people now ride the new split: its vehicles change with it
        plan, info = self.chooser(scope, args, self._demand_now(demand, scope)[0], current)
        if check_only:
            return {"ok": True}
        values = info.get("values", {})
        groups: dict[tuple, list] = defaultdict(list)
        for pair, shares in plan.items():
            value = round(values[pair]) if pair in values else None
            groups[(tuple(whole_percent([shares.get(name, 0.0) for name in names])), value)].append(pair)
        before = deepcopy(self.matrix.cells)
        self.blockSignals(True)
        try:
            for (split, value), pairs in groups.items():
                self._set_demand(pairs, demand, value, None, list(split))
        finally:
            self.blockSignals(False)
        if self.matrix.cells == before:
            raise ValueError("Nothing would change: those pairs already have that split. Nothing was changed.")
        self.changed.emit(None)
        return info

    def _pairs_from(self, args: dict) -> list[tuple[str, str]]:
        """The (origin, destination) pairs a tool names: `pairs` like "1W->1E", or every pair
        from `origins` and/or to `destinations` (one of them left out means all ends); none named
        means every pair."""
        origins, destinations = self.matrix.origins, self.matrix.destinations
        named = _parsed(args.get("pairs"))
        if named:
            pairs = []
            for item in named if isinstance(named, list) else [named]:
                # "1W->1E", and as small models write it: "1W-1E", "1W->>1E", "1W=>1E", "1W to 1E" (no end has a "-")
                parts = item if isinstance(item, (list, tuple)) else \
                    re.split(r"\s*(?:-+>*|=+>|→|>+|\bto\b|,)\s*", str(item).strip())
                if len(parts) != 2:
                    raise ValueError(f"Can't read the pair {item!r}; write it as \"origin->destination\", e.g. \"1W->1E\".")
                pairs.append((str(parts[0]).strip(), str(parts[1]).strip()))
        else:
            chosen_o = [str(end) for end in _listed(args.get("origins")) or origins]
            chosen_d = [str(end) for end in _listed(args.get("destinations")) or destinations]
            pairs = [(o, d) for o in chosen_o for d in chosen_d if o != d]
        unknown = sorted({o for o, _ in pairs if o not in origins} | {d for _, d in pairs if d not in destinations})
        if unknown:
            raise ValueError(f"No such origin or destination: {', '.join(unknown)}. Origins: {', '.join(origins)}; "
                             f"destinations: {', '.join(destinations)}.")
        pairs = [pair for pair in dict.fromkeys(pairs) if pair[0] != pair[1]]
        if not pairs:
            raise ValueError("A trip can't start and end at the same boundary end.")
        return pairs

    def _show_mode(self, demand: str) -> None:
        """Switch the matrix to hourly or daily demand, as the Demand drop-down does."""
        self.demand.setCurrentIndex(self.demand.findData(demand))

    def _select(self, pairs: list[tuple[str, str]]) -> None:
        """Select these pairs in the grid, as clicking the first and Ctrl+clicking the rest does."""
        table, spots = self.matrix.table, [(self.matrix.origins.index(o), self.matrix.destinations.index(d))
                                           for o, d in pairs]
        table.clearSelection()
        table.setCurrentCell(*spots[0])
        for row, column in spots[1:]:
            table.setRangeSelected(QTableWidgetSelectionRange(row, column, row, column), True)

    def _demand_plan(self, args: dict) -> tuple:
        """(demand, value, pattern, shares, quarters) from set_od_demand's arguments, checked; nothing changes."""
        demand = args.get("demand") or self.matrix.mode
        if demand not in UNITS:
            raise ValueError("demand must be 'hourly' (veh/h) or 'daily' (trips per day).")
        value, pattern, split = args.get("value"), args.get("arrival_pattern"), _parsed(args.get("mode_split"))
        split = split if split not in ([], {}) else None  # empty: llama3.2's filler, no split given
        phf, quarters = args.get("peak_hour_factor"), _parsed(args.get("quarters"))
        if value is None and not pattern and split is None and phf is None and quarters is None:
            raise ValueError("Give a value, an arrival_pattern, a mode_split and/or a peak_hour_factor or quarters.")
        if phf is not None or quarters is not None:
            if demand == "daily":
                raise ValueError("A peak-hour factor or 15-minute quarters apply to hourly demand only (the daily "
                                 "curve shapes daily demand).")
            if phf is not None and quarters is not None:
                raise ValueError("Give peak_hour_factor or quarters, not both.")
            if phf is not None and not isinstance(phf, (int, float)):
                raise ValueError("peak_hour_factor must be a number, 0.25 to 1.")
            quarters = phf_quarters(phf) if phf is not None else quarter_shares(quarters)
        if value is not None:
            value = round(float(value))
            top = 2_000_000 if demand == "daily" else 100_000
            if not 0 <= value <= top:
                raise ValueError(f"{demand} demand must be 0 to {top:,} {UNITS[demand]}.")
        if pattern:
            matches = [name for name in ARRIVAL_PATTERNS if name.lower() == str(pattern).lower().replace("_", " ")]
            if not matches:
                raise ValueError(f"arrival_pattern must be one of {', '.join(ARRIVAL_PATTERNS)}.")
            pattern = matches[0]
        if split is not None:
            shares = self._shares_from(split)
        return demand, value, pattern, None if split is None else shares, quarters

    def _set_demand(self, pairs: list[tuple[str, str]], demand: str, value, pattern, shares, quarters=None) -> dict:
        self._show_mode(demand)
        self._select(pairs)
        if value is not None:
            self.flow.setValue(value)  # typed into the value box...
            self._edited("flow")  # ...and set on every selected pair, even where it already shows
        if pattern:
            self.pattern.setCurrentText(pattern)
            self._edited("pattern")
        if shares is not None:
            for box, share in zip(self.mode_shares, shares[:-1]):  # typed into the boxes in turn: the
                box.setValue(share)  # slider moves, and the last type takes the rest
            self._edited("modes")
        if quarters is not None:  # straight onto the pairs: the PHF box shows them but types only its own shape
            for pair in pairs:
                cell = self.matrix.cell(pair)
                cell.quarters = quarters
                self.matrix.cells[pair] = cell
            self.show_current()
        cell = self.matrix.cell(pairs[0])
        total = sum(self.matrix.value(c) or 0 for c in self.matrix.cells.values())
        result = {"pairs": [f"{o}->{d}" for o, d in pairs][:20], "pairs_set": len(pairs), "demand": demand,
                  "value_each": self.matrix.value(cell), "unit": UNITS[demand],
                  "matrix_total": total,  # every pair's, so "each" isn't read as the total
                  "arrival_pattern": cell.pattern,
                  "mode_split": dict(zip(self.matrix.vehicle_modes.names(), self.matrix.split_of(cell))),
                  **(_quarters_record(cell) if demand == "hourly" else {})}
        timed = [f"{mode.name} every {mode.headway_min} min" for mode in self.matrix.vehicle_modes.modes
                 if mode.headway_min]
        if timed:
            result["note"] = (f"Timetabled ({', '.join(timed)}): their shares are fixed by each pair's flow, "
                              "and the other types shared the rest in the proportions given.")
        return result

    def _shares_from(self, split) -> list[int]:
        """Whole percent per vehicle type, in Mode Behavior's order, from a mode_split given as
        {name: percent}, [{"mode": name, "percent": n}, ...] or one number per type. Types left out get 0.
        Fractions adding up to 1 are read as percent (llama3.2 sent [0.4, 0.6])."""
        names = self.matrix.vehicle_modes.names()
        if isinstance(split, list) and split and all(isinstance(item, dict) for item in split):
            split = {str(item.get("mode", "")): item.get("percent", 0) for item in split}
        if isinstance(split, dict):
            given = {str(name).strip().lower(): share for name, share in split.items()}
            unknown = [name for name in given if name not in [known.lower() for known in names]]
            if unknown:
                raise ValueError(f"No vehicle type called {', '.join(unknown)}. The types are {', '.join(names)} "
                                 "(set in the Mode Behavior tab).")
            split = [given.get(name.lower(), 0) for name in names]
        try:
            given = [float(share) for share in split]
        except (TypeError, ValueError):
            raise ValueError(f"mode_split must give a percent for the vehicle types {', '.join(names)}.") from None
        scale = 100 if given and abs(sum(given) - 1) < 0.01 else 1
        shares = [round(share * scale) for share in given]
        if len(shares) != len(names) or min(shares) < 0 or sum(shares) != 100:
            raise ValueError(f"mode_split must give {len(names)} percentages, one each for {', '.join(names)} in that "
                             f"order, adding up to 100 (got {[f'{share:g}' for share in given]}).")
        return shares

    def _shape_curve(self, pairs: list[tuple[str, str]], profile: DailyProfile) -> dict:
        self._show_mode("daily")
        self._select(pairs)
        self._set_profile(profile)  # loads the editor, its equation dropdown and coefficients
        shares = profile.shares()
        peak = max(range(HOURS), key=shares.__getitem__)
        result = {"pairs": [f"{o}->{d}" for o, d in pairs], "curve": _curve_record(profile),
                  "busiest_hour": f"{peak:02d}:00-{peak + 1:02d}:00", "busiest_share": round(shares[peak], 3)}
        if not any(profile.hourly_levels()):
            result["note"] = "The curve is zero all day, so the trips are spread evenly."
        missing = [f"{o}->{d}" for o, d in pairs if self.matrix.cell((o, d)).daily is None]
        if missing:
            result["note"] = (result.get("note", "") + " These pairs have no trips per day yet: "
                              + ", ".join(missing[:10])).strip()
        return result

    def _read_for_assistant(self) -> dict:
        """read_od_matrix, compact for small models: every pair's values in one map per kind, what most pairs
        share (pattern, split, daily curve) once under "usual", and only the pairs that differ under "different"."""
        matrix, names = self.matrix, self.matrix.vehicle_modes.names()
        cells = {f"{o}->{d}": cell for (o, d), cell in matrix.cells.items()
                 if o in matrix.origins and d in matrix.destinations and (cell.flow is not None or cell.daily is not None)}
        options = {pair: {"arrival_pattern": cell.pattern, "mode_split": dict(zip(names, matrix.split_of(cell))),
                          **({"daily_curve": _curve_record(cell.profile)} if cell.daily is not None else {}),
                          **({"quarters": [0.25] * 4, **_quarters_record(cell)} if cell.flow is not None else {})}
                   for pair, cell in cells.items()}
        usual: dict = {}
        for key in ("arrival_pattern", "mode_split", "daily_curve", "peak_hour_factor", "quarters"):
            seen = [json.dumps(option[key]) for option in options.values() if key in option]
            if seen:
                usual[key] = json.loads(max(set(seen), key=seen.count))
        different = {pair: {key: value for key, value in option.items() if usual.get(key) != value}
                     for pair, option in options.items()}
        total = sum(1 for o in matrix.origins for d in matrix.destinations if o != d)
        return {"showing": matrix.mode, "vehicle_types": names, "origins": matrix.origins,
                "destinations": matrix.destinations, "pairs_total": total, "pairs_with_data_count": len(cells),
                # the whole matrix, so a "how much demand" question isn't a small model's sum of dozens of pairs
                "total_hourly_vph": round(sum(cell.flow or 0 for cell in cells.values())),
                "total_daily_trips": round(sum(cell.daily or 0 for cell in cells.values())),
                "hourly_vph": {pair: cell.flow for pair, cell in cells.items() if cell.flow is not None},
                "daily_trips": {pair: cell.daily for pair, cell in cells.items() if cell.daily is not None},
                "usual": usual, "different": {pair: values for pair, values in different.items() if values}}


def _quarters_record(cell: ODCell) -> dict:
    """An hourly pair's peak-hour factor, and its 15-minute shares of the hour when they aren't even."""
    return {"peak_hour_factor": round(phf_of(cell.quarters), 3),
            **({"quarters": [round(share, 4) for share in cell.quarters]} if cell.quarters else {})}


def _lead_with_what_was_kept(info: dict, share: float, kept: float) -> None:
    """A demand the check cut: the result starts with what the matrix now holds (qwen3.5 reported the 6,420 veh/h
    made as the demand when 47 were kept), the made total renamed; under half kept, the busiest junctions that
    only give way are named with what could let more hold (the model decides: signalise them, or main roads)."""
    made = info.pop("total_made", None)
    if made is None:  # scale_demand's: its total after scaling, and the factor that the check then cut too
        made = info.pop("total_after", None)
        if isinstance(info.get("factor"), (int, float)):
            info["factor"] = round(info["factor"] * share, 3)
    rest = dict(info)
    info.clear()
    info.update({"total_now": kept, "kept_after_check": f"{share:.0%} of {made} (the rest jammed in a run)",
                 **rest, "total_before_check": made})
    if share < 0.5:
        giving_way = [node for node, control in (info.get("junction_controls") or {}).items() if control == "uncontrolled"]
        if giving_way:
            info["to_hold_more"] = (f"The busiest junctions only give way: {', '.join(giving_way)}. Signalising them "
                                    "(set_junction_control, control 'signalised'), or main roads only "
                                    "(network_from_background, road_classes 'main'), may let more demand hold; then "
                                    "generate_demand again. Tell the user what was kept either way.")


def _parsed(value):
    """A tool argument as sent, or decoded when a small model sent a list or object as JSON text."""
    if isinstance(value, str) and value.strip()[:1] in ("[", "{"):
        try:
            return json.loads(value)
        except ValueError:
            pass
    return value


def _listed(value) -> list:
    """End names as a list: ["1W", "2W"], JSON text of one, or a single name."""
    value = _parsed(value)
    return [] if value in (None, "") else value if isinstance(value, list) else [value]


def _profile_from(args: dict) -> DailyProfile:
    """The curve a shape_daily_curve call describes: points, a polynomial or a preset."""
    points, polynomial, preset = _parsed(args.get("points")), _parsed(args.get("polynomial")), args.get("preset")
    if sum(item is not None and item != [] for item in (points, polynomial, preset)) != 1:
        raise ValueError("Give exactly one of points, polynomial or preset.")
    if preset:
        if preset not in ("weekday", "flat"):
            raise ValueError("preset must be 'weekday' or 'flat'.")
        return DailyProfile() if preset == "weekday" else DailyProfile("points", [(0.0, 1.0), (24.0, 1.0)])
    if polynomial is not None:
        try:
            coefficients = [float(a) for a in polynomial]
        except (TypeError, ValueError):
            raise ValueError("polynomial must be a list of numbers [a0, a1, ..., an].") from None
        if not 2 <= len(coefficients) <= max(DEGREES) + 1:
            raise ValueError(f"polynomial needs {min(DEGREES) + 1} to {max(DEGREES) + 1} coefficients (degree "
                             f"{min(DEGREES)} to {max(DEGREES)}).")
        return DailyProfile("poly", [], coefficients + [0.0] * (min(DEGREES) + 1 - len(coefficients)))
    try:
        pairs = sorted((float(p["hour"]), float(p["level"])) if isinstance(p, dict) else (float(p[0]), float(p[1]))
                       for p in points)
    except (TypeError, ValueError, KeyError, IndexError):
        raise ValueError("points must be [[hour, level], ...] with hours 0-24 and levels 0-1.") from None
    if len(pairs) < 2 or not all(0 <= hour <= HOURS for hour, _ in pairs) or min(level for _, level in pairs) < 0:
        raise ValueError("points need at least two [hour, level] pairs, hours 0-24, levels not below zero.")
    top = max(level for _, level in pairs)
    if top <= 0:
        raise ValueError("At least one point needs a level above zero.")
    pairs = [(hour, level / top) for hour, level in pairs] if top > 1 else pairs  # relative levels are fine
    if pairs[0][0] > 0:
        pairs.insert(0, (0.0, pairs[0][1]))  # the curve runs from midnight...
    if pairs[-1][0] < HOURS:
        pairs.append((float(HOURS), pairs[-1][1]))  # ...to midnight
    kept = [pairs[0]]
    for hour, level in pairs[1:]:
        if hour - kept[-1][0] >= MIN_GAP_H:
            kept.append((hour, level))
        elif hour == HOURS:
            kept[-1] = (hour, level)  # keep the midnight end
    return DailyProfile("points", kept)


def _curve_record(profile: DailyProfile) -> dict:
    if profile.kind == "poly":
        return {"kind": "polynomial", "coefficients": [round(a, 4) for a in profile.coefficients]}
    return {"kind": "points", "points": [[round(hour, 2), round(level, 3)] for hour, level in profile.points]}


def _clock(hours: float) -> str:
    return f"{int(hours):02d}:{round(hours % 1 * 60):02d}"


def _busiest(values: list[float]) -> str:
    if not any(values):
        return "no cars"
    hour = max(range(HOURS), key=values.__getitem__)
    return f"busiest {hour:02d}:00–{hour + 1:02d}:00 at {values[hour]:,.0f} veh/h"


class InstitutionPanel(QWidget):
    """Demand Generator → Institutions. Pick a building by its icon and set its figures; then, as in an
    RTS game, move it on the map (green: it can stand there; red: it can't), click to set it down,
    scroll to size its circle of influence and click again to finish (network_scene, institutions.py).
    Clicking a placed building shows its circle and what it loads onto each junction and end."""

    placing_changed = Signal(object)  # the footprint (m²) of the building to place now, or None: not placing
    delete_requested = Signal(str)  # the selected building's Delete
    split_requested = Signal(str)  # the selected building's Mode split
    height_requested = Signal(str, float)  # selected institution's total visual height, metres

    def __init__(self) -> None:
        super().__init__()
        self.kind: str | None = None  # the building being placed
        self.selected: Institution | None = None
        group = QGroupBox("Institutions")
        layout = QVBoxLayout(group)
        row = QHBoxLayout()
        row.setSpacing(4)
        self.kind_buttons: dict[str, object] = {}
        for name, kind in KINDS.items():
            button = icon_button(kind.glyph, name)  # the banner says each step
            button.setCheckable(True)
            button.toggled.connect(lambda on, name=name: self._pick(name, on))
            row.addWidget(button)
            self.kind_buttons[name] = button
        row.addStretch(1)
        layout.addLayout(row)
        self.empty_hint = hint_label("Pick a building to place it. Click one on the map to see its circle of influence.")
        layout.addWidget(self.empty_hint)

        self.form_box = QWidget()  # the figures of the building to place
        form = QFormLayout(self.form_box)
        form.setContentsMargins(0, 4, 0, 0)
        self.form_title = QLabel()
        self.form_title.setStyleSheet("font-weight: 600;")
        form.addRow(self.form_title)
        self.area = QSpinBox()
        self.area.setRange(100, int(MAX_AREA_M2))
        self.area.setSingleStep(500)
        self.area.setSuffix(" m²")
        self.area.setToolTip("Footprint area")
        self.height = QDoubleSpinBox()
        self.height.setRange(.5, MAX_BUILDING_HEIGHT_M)
        self.height.setDecimals(1)
        self.height.setSuffix(" m")
        self.height.setToolTip("Total building height")
        self.height_explicit = False
        self.height_note = hint_label("Assumed type default; adjust to measured height.")
        self.people = QSpinBox()
        self.people.setRange(0, 1_000_000)
        self.people.setSingleStep(50)
        self.people_label = QLabel("People")
        self.employees = QSpinBox()
        self.employees.setRange(0, 1_000_000)
        self.employees.setSingleStep(10)
        self.employees.setToolTip("Staff shift hours")
        self.shift_start, self.shift_end = QTimeEdit(), QTimeEdit()
        shift = QHBoxLayout()
        for box in (self.shift_start, self.shift_end):
            box.setDisplayFormat("HH:mm")
            shift.addWidget(box, 1)
        shift.insertWidget(1, QLabel("–"))
        self.car_share = QSpinBox()
        self.car_share.setRange(0, 100)
        self.car_share.setSuffix(" %")
        self.car_share.setToolTip("Share arriving by car")
        form.addRow("Area", self.area)
        form.addRow("Height", self.height)
        form.addRow(self.height_note)
        form.addRow(self.people_label, self.people)
        form.addRow("Employees", self.employees)
        form.addRow("Shift", shift)
        form.addRow("By car", self.car_share)
        self.trips = hint_label()
        form.addRow(self.trips)
        layout.addWidget(self.form_box)
        self.banner = hint_label()  # the next step while placing
        self.banner.setProperty("role", "banner")
        layout.addWidget(self.banner)

        self.details_box = QWidget()  # the building clicked on the map
        details = QVBoxLayout(self.details_box)
        details.setContentsMargins(0, 4, 0, 0)
        heading = QHBoxLayout()
        self.details_title = QLabel()
        self.details_title.setStyleSheet("font-weight: 600;")
        heading.addWidget(self.details_title, 1)
        self.split_button = icon_button(0xEB05, "Mode split")
        self.split_button.clicked.connect(lambda: self.split_requested.emit(self.selected.id))
        heading.addWidget(self.split_button)
        self.delete_button = icon_button(0xE74D, "Delete Institution", "danger")
        self.delete_button.clicked.connect(lambda: self.delete_requested.emit(self.selected.id))
        heading.addWidget(self.delete_button)
        details.addLayout(heading)
        self.details = hint_label()
        details.addWidget(self.details)
        height_form = QFormLayout()
        self.selected_height = QDoubleSpinBox()
        self.selected_height.setRange(.5, MAX_BUILDING_HEIGHT_M)
        self.selected_height.setDecimals(1)
        self.selected_height.setSuffix(" m")
        self.selected_height.setToolTip("Total building height")
        self.selected_height.valueChanged.connect(lambda value: self.selected and
                                                  self.height_requested.emit(self.selected.id, value))
        height_form.addRow("Height", self.selected_height)
        details.addLayout(height_form)
        layout.addWidget(self.details_box)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(group)

        self.area.valueChanged.connect(self._area_changed)
        self.height.valueChanged.connect(self._height_changed)
        for box in (self.people, self.employees, self.car_share):
            box.valueChanged.connect(self._preview)
        for box in (self.shift_start, self.shift_end):
            box.timeChanged.connect(self._preview)
        self.show_step(None)
        self._refresh()

    def figures(self) -> dict:
        """The form's figures, as institutions.place takes them."""
        start, end = self.shift_start.time(), self.shift_end.time()
        return {"people": self.people.value(), "employees": self.employees.value(),
                "shift_start_h": start.hour() + start.minute() / 60, "shift_end_h": end.hour() + end.minute() / 60,
                "car_share": float(self.car_share.value()),
                "height_m": self.height.value() if self.height_explicit else None}

    def _height_changed(self):
        self.height_explicit = True
        self.height_note.setText("Total height, including the roof.")

    def end_placing(self) -> None:
        """Stop placing (placed, Esc, or another tab): no building picked."""
        if self.kind is not None:
            self.kind_buttons[self.kind].setChecked(False)

    def _pick(self, name: str, on: bool) -> None:
        """A building's icon: its figures (its kind's defaults) to set, and the footprint on the map."""
        if on:
            for other, button in self.kind_buttons.items():
                if other != name and button.isChecked():
                    button.blockSignals(True)
                    button.setChecked(False)
                    button.blockSignals(False)
            self.kind = name
            kind = KINDS[name]
            area, people, employees, start, end, car = kind.defaults
            boxes = (self.area, self.height, self.people, self.employees, self.car_share, self.shift_start, self.shift_end)
            for box in boxes:
                box.blockSignals(True)
            self.area.setValue(area)
            self.height.setValue(BUILDING_HEIGHTS_M[name])
            self.height_explicit = False
            self.height_note.setText("Assumed type default; adjust to measured height.")
            self.people.setValue(people)
            self.employees.setValue(employees)
            self.car_share.setValue(car)
            self.shift_start.setTime(QTime(int(start), round(start % 1 * 60)))
            self.shift_end.setTime(QTime(int(end), round(end % 1 * 60)))
            for box in boxes:
                box.blockSignals(False)
            self.form_title.setText(f"New {name}")
            self.people_label.setText(kind.people)
            self.people.setToolTip("Leave and return by shift" if kind.home else
                                   "Arrive and leave by shift" if kind.together else "Come and go all shift")
        elif name == self.kind:
            self.kind = None
        self._refresh()
        self.placing_changed.emit(self.area.value() if self.kind else None)

    def _area_changed(self) -> None:
        self._preview()
        if self.kind:
            self.placing_changed.emit(self.area.value())  # the footprint on the map follows

    def _preview(self) -> None:
        """What the figures make: its car trips a day and its busiest hours."""
        if self.kind is None:
            return
        area = self.area.value()
        item = Institution("new", self.kind, 0.0, 0.0, float(area), sqrt(area) / 2, **self.figures())
        problem = institution_problem(item)
        if problem:
            self.trips.setText(problem.replace("Institution new's", "Its"))
            return
        arrive, leave = hourly_trips(item)
        self.trips.setText(f"≈ {sum(arrive):,.0f} cars come and {sum(leave):,.0f} go a day; arriving "
                           f"{_busiest(arrive)}, leaving {_busiest(leave)}.")

    def show_step(self, placing) -> None:
        """The next step while placing (institutions.Placement), in the banner."""
        if placing is None or self.kind is None:
            text = ""
        elif placing.x is None:
            text = "Move the building onto the map."
        elif placing.radius_m is None:
            text = (f"Red: it can't stand on {placing.problem}. Move it onto clear ground." if placing.problem else
                    "Green: clear ground. Click to set it down here.")
        else:
            reached = ", ".join(placing.links) if placing.links else "no junction or end yet"
            text = (f"Scroll to size its circle of influence: {placing.radius_m:.0f} m, reaching {reached}. "
                    "Click to finish; Esc cancels.")
        self.banner.setText(text)
        self.banner.setVisible(bool(text))

    def show_selected(self, network: Network, item: Institution | None) -> None:
        """A building clicked on the map: its figures, its mode split, its door and where its people come from."""
        self.selected = item
        if item is not None:
            self.selected_height.blockSignals(True)
            self.selected_height.setValue(building_height(item))
            self.selected_height.blockSignals(False)
            kind = KINDS[item.kind]
            arrive, _ = hourly_people(item)
            split = " · ".join(f"{name} {percent:.0f} %" for name, percent in shares(item).items())
            lines = [f"{item.people:,} {kind.people.lower()} · {item.employees:,} employees · shift "
                     f"{_clock(item.shift_start_h)}–{_clock(item.shift_end_h)} · {item.area_m2:,.0f} m²",
                     f"{'Mode split (logit)' if item.mode_shares else 'By its car share'}: {split}"]
            entrance, origins = door(network, item), catchment(network, item)
            if entrance and origins:
                lines.append(f"Catchment {item.radius_m:.0f} m: {sum(arrive):,.0f} people a day come from "
                             f"{', '.join(origins)} (an equal share each) to its door {entrance}, and go back.")
                lines.append(f"By car: {_busiest(hourly_trips(item)[0])} arriving.")
            else:
                lines.append(f"Catchment {item.radius_m:.0f} m reaches no junction or end but its door, so its "
                             f"{sum(arrive):,.0f} people a day have nowhere to come from: no trips in a Run.")
            self.details_title.setText(f"{item.id} {item.kind}")
            lines.append(f"Height {building_height(item):g} m" +
                         (" (assumed type default; adjust to measured height)." if item.height_m is None else " (including roof)."))
            self.details.setText("\n".join(lines))
        self._refresh()

    def _refresh(self) -> None:
        self.form_box.setVisible(self.kind is not None)
        self.details_box.setVisible(self.selected is not None)
        self.empty_hint.setVisible(self.kind is None and self.selected is None)
        self._preview()


class ModeSplitDialog(QDialog):
    """Institutions → Mode split: the statistics behind an institution's mode shares, by hand. Each mode's
    out-of-vehicle minutes and costs, the logit's coefficients, the walking speed and any observed shares to
    calibrate to. The shares update as they change (`compute`: network_tools.institution_split, which AI Mode's
    institution_mode_split calls too); Apply sets them on the building (`applied`, one undo step)."""

    applied = Signal(dict)  # the arguments, as institution_mode_split takes them

    def __init__(self, parent, title: str, compute, alternatives: list[dict], coefficients: dict,
                 walk_kmh: float) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Mode split · {title}")
        self.compute = compute
        layout = QVBoxLayout(self)
        layout.addWidget(hint_label("Its people choose by a logit of time and cost on the trips from its catchment "
                                    "to its door. Give an observed share to calibrate the constants to it."))
        grid = QGridLayout()
        for column, heading in enumerate(("Mode", "Use", "Out-of-vehicle min", "Cost ¢", "Cost ¢/km", "Observed %")):
            grid.addWidget(QLabel(heading), 0, column)
        self.rows: list[tuple] = []
        for row, alternative in enumerate(alternatives, 1):
            use = QCheckBox()
            use.setChecked(True)
            use.setToolTip("Use this mode")
            boxes = []
            for key, top in (("out_of_vehicle_min", 120), ("cost", 100_000), ("cost_per_km", 10_000)):
                box = QDoubleSpinBox()
                box.setRange(0, top)
                box.setDecimals(1)
                box.setValue(float(alternative.get(key) or 0))
                boxes.append(box)
            observed = QDoubleSpinBox()
            observed.setRange(-1, 100)
            observed.setDecimals(1)
            observed.setSpecialValueText("–")  # not observed
            observed.setValue(-1)
            observed.setToolTip("Observed share")
            grid.addWidget(QLabel(alternative["mode"]), row, 0)
            grid.addWidget(use, row, 1)
            for column, box in enumerate((*boxes, observed), 2):
                grid.addWidget(box, row, column)
            self.rows.append((alternative["mode"], use, boxes, observed))
        layout.addLayout(grid)
        form = QFormLayout()
        self.coefficients = {}
        for key, label in (("in_vehicle_per_min", "In-vehicle, a minute"), ("out_of_vehicle_per_min",
                                                                            "Out-of-vehicle, a minute"),
                           ("cost", "Cost, a cent")):
            box = QDoubleSpinBox()
            box.setRange(-1, 0)
            box.setDecimals(4)
            box.setSingleStep(0.001)
            box.setValue(coefficients[key])
            form.addRow(label, box)
            self.coefficients[key] = box
        self.walk_kmh = QDoubleSpinBox()
        self.walk_kmh.setRange(1, 10)
        self.walk_kmh.setDecimals(1)
        self.walk_kmh.setSuffix(" km/h")
        self.walk_kmh.setValue(walk_kmh)
        form.addRow("Walking speed", self.walk_kmh)
        layout.addLayout(form)
        self.result = hint_label()
        layout.addWidget(self.result)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Apply | QDialogButtonBox.StandardButton.Close)
        self.apply_button = buttons.button(QDialogButtonBox.StandardButton.Apply)
        self.apply_button.clicked.connect(lambda: self.applied.emit(self.arguments()))
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        for _, use, boxes, observed in self.rows:
            use.toggled.connect(self.update_result)
            for box in (*boxes, observed):
                box.valueChanged.connect(self.update_result)
        for box in (*self.coefficients.values(), self.walk_kmh):
            box.valueChanged.connect(self.update_result)
        self.update_result()

    def arguments(self) -> dict:
        """The dialog's figures, as institution_mode_split takes them."""
        alternatives, observed = [], {}
        for mode, use, boxes, seen in self.rows:
            if not use.isChecked():
                continue
            alternatives.append({"mode": mode, **{key: box.value() for key, box in
                                                  zip(("out_of_vehicle_min", "cost", "cost_per_km"), boxes)}})
            if seen.value() >= 0:
                observed[mode] = seen.value()
        return {"alternatives": alternatives, "observed_percent": observed,
                "coefficients": {key: box.value() for key, box in self.coefficients.items()},
                "walk_kmh": self.walk_kmh.value()}

    def update_result(self) -> None:
        try:
            percent, info = self.compute(self.arguments())
        except ValueError as exc:
            self.result.setText(str(exc))
            self.apply_button.setEnabled(False)
            return
        vehicles = ", ".join(f"{count:,} {mode}" for mode, count in info["vehicles_per_day_each_way"].items())
        lines = [" · ".join(f"{mode} {value:.1f} %" for mode, value in percent.items()),
                 f"{info['people_per_day_each_way']:,} people a day each way from {info['origins']} places, "
                 f"{info['mean_trip_km']:g} km on average to its door {info['door']}; vehicles a day each way: "
                 f"{vehicles or 'none'}.",
                 "Constants: " + ", ".join(f"{mode} {value:+.3f}" for mode, value in info["constants"].items())
                 + (f" ({info['calibration'].split(' (')[0]})" if info.get("calibration") else "")]
        if info.get("note"):
            lines.append(info["note"])
        self.result.setText("\n".join(lines))
        self.apply_button.setEnabled(True)

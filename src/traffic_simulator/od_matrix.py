"""Origin-destination matrix: a compact grid plus a side-panel editor for one pair, and the
Demand Generator's Institutions panel (institutions.py places the buildings).

Two kinds of demand: hourly (veh/h per pair) or daily (trips per day per pair, spread over
the 24 hours by a curve the user shapes; see daily_profile.py).

A Run spawns vehicles from the values shown (hourly or daily); a project file saves them
(main.MainWindow._project, network_io).

The cells' type and rules (ODCell, the split arithmetic, the demand tools' checks and read-out) are
demand.py's; this module shows and edits them.
"""

from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from dataclasses import replace
from itertools import accumulate
from math import sqrt

from PySide6.QtCore import QEvent, QPoint, QPointF, QRectF, Qt, QTime, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QKeySequence, QPainter, QPainterPath, QPen, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QButtonGroup,
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
    QPushButton,
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
from .daily_profile import (ARRIVAL_PATTERNS, DEGREES, HOURS, MIN_GAP_H, DailyProfile, from_record, phf_of,
                            phf_quarters, profile_record, through_hours)
from .institutions import KINDS, catchment, door, hourly_people, hourly_trips, shares, shift_people
from .demand import (MAX_DROP_PAIRS, UNITS, ODCell, drops_after, drops_from, drops_record, curve_record, demand_now,
                     demand_plan, lead_with_what_was_kept, listed, pairs_from, profile_from, quarters_record, read_od,
                     redistribute, split_of, whole_percent)
from .metro import lines_of, ride
from .results import clock_h
from .mode_behavior import VehicleModes
from .models import (BUILDING_HEIGHTS_M, MAX_AREA_M2, MAX_BUILDING_HEIGHT_M, MAX_INSTITUTION_PER_H, MAX_METRO_DEMAND,
                     MAX_WALK_DEMAND, Institution, Network, institution_problem)
from .building_assets import height as building_height
from .widgets import ChoiceButtons, hint_label, icon_button, styled_button

# Categorical slots 1-3 of the dataviz reference palette (its dark-surface steps), in
# fixed order. Validated on the grid grey: all checks pass, worst adjacent colour-blind
# dE 9.4. On the lighter panel grey bus/car dip under 3:1, so the slider always labels them.
TEXT = QColor(theme.TEXT)
SELECTED_FILL, SELECTED_EDGE = QColor(theme.BANNER), QColor(theme.ACCENT_EDGE)
HANDLE_EDGE, HANDLE_FILL = QColor(theme.HANDLE_EDGE), QColor(theme.HANDLE_FILL)
GRID = QColor(theme.CURVE_GRID)  # curve editor grid lines
BARS = QColor(theme.ACCENT)  # hourly share bars: the accent, subdued
BARS.setAlpha(110)
CELL_PX = (56, 40)  # a grid cell's width and height at 100 %
ZOOM_RANGE, FIT_MAX = (0.25, 4.0), 2.0  # Fit Grid stops at 200 % so a small grid doesn't balloon
PAN_BUTTONS = (Qt.MouseButton.MiddleButton, Qt.MouseButton.RightButton)  # as on the map


def _mode_text(names: list[str], split: tuple[int, ...]) -> str:
    return " · ".join(f"{name} {share}%" for name, share in zip(names, split))


class ModeSplitSlider(QWidget):
    """Handles cut 0-100 % into one zone per vehicle type (Mode Behavior's list, in its colours),
    so the shares always sum to 100: one handle between each pair of neighbouring types.

    Drag a handle; click a type's zone (away from the handles) to pick it (type_clicked: its
    drop-offs). With the slider focused, Left/Right move the active handle 1 % (Page Up/Down
    10 %) and Space moves to the next handle. set_share() takes a typed percentage for one type.
    """

    changed = Signal()  # the user moved a handle; set_split() stays quiet
    type_clicked = Signal(int)  # a type's zone was clicked: its index
    MARGIN, TRACK_TOP, TRACK_HEIGHT, HANDLE_RADIUS = 10, 8, 12, 8

    def __init__(self, vehicle_modes: VehicleModes) -> None:
        super().__init__()
        self.vehicle_modes = vehicle_modes
        self.cuts = self._cuts(vehicle_modes.default_split)  # where each zone ends, but the last
        self.fixed: set[int] = set()  # timetabled types: their share follows their headway
        self.active = 0
        self._tie: list[int] = []  # handles pressed where they overlap: the first move picks one
        self._dragging = False  # the press landed on a handle: moves drag it
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMinimumHeight(30)
        self.setToolTip("Drag splits; click type: drop-offs")

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
            painter.setBrush(HANDLE_EDGE if self._locked(index) else HANDLE_FILL)  # grey: fixed
            painter.drawEllipse(QRectF(self._x(cut) - self.HANDLE_RADIUS, top + height / 2 - self.HANDLE_RADIUS,
                                       2 * self.HANDLE_RADIUS, 2 * self.HANDLE_RADIUS))

    def mousePressEvent(self, event) -> None:  # type intentionally inherited from Qt
        x = event.position().x()
        self._dragging = False
        if all(abs(self._x(cut) - x) > self.HANDLE_RADIUS + 2 for cut in self.cuts):  # on a zone: that type
            bounds = [0, *self.cuts, 100]
            value = (x - self.MARGIN) / max(1, self.width() - 2 * self.MARGIN) * 100
            self.type_clicked.emit(next((i for i, end in enumerate(bounds[1:]) if value <= end), len(bounds) - 2))
            return
        free = self._free_handles()
        if not free:
            return
        value = self._value(x)
        nearest = min(abs(value - self.cuts[i]) for i in free)
        closest = [i for i in free if abs(value - self.cuts[i]) == nearest]
        stacked = [i for i in free if self.cuts[i] == self.cuts[closest[0]]]
        self._tie = stacked if len(stacked) > 1 and nearest <= 2 else []
        self._dragging = True
        if not self._tie:
            self.active = closest[0]
            self._move(self.active, value)
        self.update()

    def mouseMoveEvent(self, event) -> None:  # type intentionally inherited from Qt
        if not self._dragging or not self.cuts:
            return  # a click on a type's zone picks it; a slip after it moves nothing
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
        self.unit = "veh"  # what a trip is, in the hour tooltips
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
                painter.setBrush(SELECTED_EDGE if index == self._drag else HANDLE_FILL)
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
            trips = f" ≈ {self.daily_total * share:.0f} {self.unit}" if self.daily_total > 0 else ""
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


class DayCurve(QWidget):
    """A day's spread: the curve to drag, its busiest hour, the equation dropdown and a polynomial's coefficients. A
    vehicle pair's (ODCellPanel), a walker, spawn or metro pair's (MetroMatrix), an institution's arrivals or
    departures (InstitutionPanel). `changed` after each edit; show_profile loads one quietly."""

    changed = Signal()

    def __init__(self, title: str = "Spread over the day", unit: str = "veh") -> None:
        super().__init__()
        self.unit = unit
        self.total = -1  # trips a day, for the busiest hour's figure; -1: none
        daily = QVBoxLayout(self)
        daily.setContentsMargins(0, 4, 0, 4)
        self.heading = QLabel(title)
        self.heading.setStyleSheet("font-weight: 600;")
        daily.addWidget(self.heading)
        self.curve = CurveEditor()
        self.curve.unit = unit
        self.curve.setToolTip("Drag points to shape day")
        daily.addWidget(self.curve)
        self.peak = hint_label()
        daily.addWidget(self.peak)
        curve_form = QFormLayout()
        curve_form.setContentsMargins(0, 0, 0, 0)
        self.kind = QComboBox()
        self.kind.addItem("Free curve (drag points)", "points")
        for degree in DEGREES:
            self.kind.addItem(f"Polynomial, degree {degree}", degree)
        self.kind.setToolTip("Curve kind")
        self.kind.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.kind.setMinimumContentsLength(12)  # may shorten its text rather than widen the sidebar
        kind_row = QHBoxLayout()
        kind_row.addWidget(self.kind, 1)
        self.reset = icon_button(0xE787, "Weekday Shape")
        kind_row.addWidget(self.reset)
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
        self.curve.changed.connect(self._edited)
        self.kind.activated.connect(self._kind_chosen)
        self.reset.clicked.connect(lambda: self.set_profile(DailyProfile()))

    @property
    def profile(self) -> DailyProfile:
        return self.curve.profile

    def show_profile(self, profile: DailyProfile, total: float | None = None) -> None:
        """Load a curve into the editor, the equation dropdown and its coefficients (no `changed`); `total`, the
        day's trips, when given."""
        if total is not None:
            self.total = total
        self.curve.set_profile(profile)
        poly = profile.kind == "poly"
        self.kind.setCurrentIndex(self.kind.findData(len(profile.coefficients) - 1 if poly else "points"))
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
        self.show_peak()

    def set_profile(self, profile: DailyProfile) -> None:
        self.show_profile(profile)
        self._edited()

    def set_total(self, total: float) -> None:
        self.total = total
        self.show_peak()

    def _edited(self) -> None:
        self.show_peak()
        self.changed.emit()

    def _kind_chosen(self) -> None:
        """Change the curve's kind, keeping its shape as closely as the new kind allows."""
        kind, current = self.kind.currentData(), self.curve.profile
        if kind == "points":
            self.set_profile(current if current.kind == "points" else current.as_points())
        else:
            self.set_profile(current.as_polynomial(kind))

    def _coefficient_edited(self, power: int, value: float) -> None:
        profile = self.curve.profile
        if profile.kind == "poly" and power < len(profile.coefficients):
            profile.coefficients[power] = value
            self.curve.update()
            self._edited()

    def show_peak(self) -> None:
        """The busiest hour of the curve, and its trips when a daily total is entered."""
        self.curve.daily_total = max(self.total, 0)
        shares = self.curve.profile.shares()
        peak = max(range(HOURS), key=shares.__getitem__)
        trips = f" ≈ {self.total * shares[peak]:,.0f} {self.unit}/h" if self.total > 0 else ""
        self.peak.setText(f"Busiest hour {peak:02d}:00–{peak + 1:02d}:00: {shares[peak]:.1%} of the day{trips}.")


class DropSlider(QWidget):
    """One vehicle type's drop-offs along an OD pair's trip: the track is its fastest route from the entry to the
    exit, the junctions on the way ticked. Click the track to add a point (near a junction it snaps to it), drag
    one, double-click or right-click to remove it. Each point ends its % of the type's trips there; a point is
    a share of the trip's length, so it holds on any route the vehicles take."""

    changed = Signal()  # an edit finished: a point added, dragged or removed
    picked = Signal()  # the selected point changed
    MARGIN, TRACK_Y, RADIUS, SNAP_PX, MIN_GAP, NEW_SHARE = 16, 24, 6, 8, 0.01, 10

    def __init__(self) -> None:
        super().__init__()
        self.length = 0.0  # metres, entry to exit
        self.junctions: list[tuple[float, str]] = []  # (share of the length, node id) on the way
        self.ends = ("", "")
        self.colour = QColor(HANDLE_EDGE)
        self.points: list[list] = []  # [share of the length, %], in order along the trip
        self.selected: int | None = None
        self._drag = self._moved = False
        self.setMinimumHeight(58)

    def set_route(self, length: float, junctions: list, ends: tuple[str, str], colour: str) -> None:
        self.length, self.junctions, self.ends, self.colour = length, list(junctions), ends, QColor(colour)
        self.update()

    def set_points(self, points) -> None:
        self.points = sorted([at, share] for at, share in points)
        if self.selected is not None and self.selected >= len(self.points):
            self.selected = None
        self.update()

    def left_over(self) -> int:
        """The % of the type's trips no point takes: they reach the exit."""
        return 100 - sum(share for _, share in self.points)

    def _x(self, at: float) -> float:
        return self.MARGIN + (self.width() - 2 * self.MARGIN) * at

    def _at(self, x: float) -> float:
        """The share of the trip under x, kept off the ends; within SNAP_PX of a junction, that junction."""
        at = min(max((x - self.MARGIN) / max(1, self.width() - 2 * self.MARGIN), self.MIN_GAP), 1 - self.MIN_GAP)
        near = min(self.junctions, key=lambda junction: abs(self._x(junction[0]) - x), default=None)
        return near[0] if near and abs(self._x(near[0]) - x) <= self.SNAP_PX else at

    def _point_at(self, position: QPointF) -> int | None:
        for index, (at, _) in enumerate(self.points):
            if abs(self._x(at) - position.x()) <= self.RADIUS + 3 and abs(self.TRACK_Y - position.y()) <= 14:
                return index
        return None

    def paintEvent(self, event) -> None:  # type intentionally inherited from Qt
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        font = QFont(self.font())
        font.setPointSizeF(8)
        painter.setFont(font)
        left, right, y = self._x(0), self._x(1), self.TRACK_Y
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self.colour)
        painter.drawRoundedRect(QRectF(left, y - 3, right - left, 6), 3, 3)
        painter.setPen(QColor(theme.SECONDARY_TEXT))
        for at, name in [(0.0, self.ends[0]), *self.junctions, (1.0, self.ends[1])]:
            x = self._x(at)
            painter.drawLine(QPointF(x, y + 5), QPointF(x, y + 10))
            painter.drawText(QRectF(x - 30, y + 11, 60, 14), Qt.AlignmentFlag.AlignCenter, name)
        for index, (at, share) in enumerate(self.points):
            x = self._x(at)
            painter.setPen(QPen(SELECTED_EDGE, 2))
            painter.setBrush(SELECTED_EDGE if index == self.selected else HANDLE_FILL)
            painter.drawEllipse(QPointF(x, y), self.RADIUS, self.RADIUS)
            painter.setPen(TEXT)
            painter.drawText(QRectF(x - 30, y - 22, 60, 14), Qt.AlignmentFlag.AlignCenter, f"{share} %")

    def mousePressEvent(self, event) -> None:  # type intentionally inherited from Qt
        index = self._point_at(event.position())
        if event.button() == Qt.MouseButton.RightButton:
            self._remove(index)
            return
        if event.button() != Qt.MouseButton.LeftButton:
            return
        if index is None:  # a new point here, if some trips are left and it isn't on top of another
            at = self._at(event.position().x())
            if self.left_over() <= 0 or any(abs(at - other) < self.MIN_GAP for other, _ in self.points):
                return
            point = [at, min(self.NEW_SHARE, self.left_over())]
            self.points.append(point)
            self.points.sort()
            index = self.points.index(point)
            self.changed.emit()
        self.selected, self._drag, self._moved = index, True, False
        self.picked.emit()
        self.update()

    def mouseMoveEvent(self, event) -> None:  # type intentionally inherited from Qt
        if not self._drag or self.selected is None:
            return
        point = self.points[self.selected]
        at = self._at(event.position().x())
        if at == point[0] or any(abs(at - other[0]) < self.MIN_GAP for other in self.points if other is not point):
            return
        point[0], self._moved = at, True
        self.points.sort()
        self.selected = self.points.index(point)
        self.update()

    def mouseReleaseEvent(self, event) -> None:  # type intentionally inherited from Qt
        if self._drag:
            self._drag = False
            if self._moved:
                self.changed.emit()

    def mouseDoubleClickEvent(self, event) -> None:  # type intentionally inherited from Qt
        self._remove(self._point_at(event.position()))

    def _remove(self, index: int | None) -> None:
        if index is not None:
            del self.points[index]
            self.selected, self._drag = None, False
            self.picked.emit()
            self.update()
            self.changed.emit()


def _paint_blocked(painter: QPainter, rect) -> None:
    """A cell that takes no value (theme.BLOCKED_CELL hatched with BLOCKED_HATCH): it reads as shut."""
    painter.fillRect(rect, QColor(theme.BLOCKED_CELL))
    painter.fillRect(rect, QBrush(QColor(theme.BLOCKED_HATCH), Qt.BrushStyle.BDiagPattern))


class _BlockedDelegate(QStyledItemDelegate):
    """MetroMatrix's cells: as Qt paints them, but one that can't be typed in (no Qt.ItemIsEditable) blocked."""

    def paint(self, painter: QPainter, option, index) -> None:
        if index.flags() & Qt.ItemFlag.ItemIsEditable:
            super().paint(painter, option, index)
            return
        painter.save()
        _paint_blocked(painter, option.rect)
        painter.restore()


class _CellDelegate(QStyledItemDelegate):
    """Paints a cell: blank until it has a flow, then the value over a thin mode bar."""

    def __init__(self, matrix: ODMatrix) -> None:
        super().__init__(matrix)
        self.matrix = matrix

    def paint(self, painter: QPainter, option, index) -> None:
        rect = option.rect
        pair = self.matrix.pair_at(index.row(), index.column())
        painter.save()
        if pair is None:  # the diagonal: no trip from an end to itself
            _paint_blocked(painter, rect)
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


class MetroMatrix(QWidget):
    """Demand Generator → MRT: the metro's own OD matrix, passengers an hour between its stations (M1, M2...),
    rows from, columns to (Network.metro_demand, apart from the roads' demand). A number typed in a cell sets it, 0 or
    blank clears it, anything else is refused and the cell put back; the diagonal and pairs no ride joins (metro.ride:
    one line, or one change at an interchange) can't be typed in. Daily (`daily`, set by the window from the Demand
    switch): people a day (Network.metro_daily), the selected pair's curve in `side` (the sidebar's, placed by
    ODCellPanel.add_side, as Vehicle OD's pair editor is), the grid keeping the whole area. `changed` after each edit
    (an undo step; its merge key: None, or a curve's burst of edits)."""

    changed = Signal(object)

    TITLE = "MRT passengers {per}, station to station"
    EMPTY = "No MRT stations yet: deploy a line in Edit Network > MRT."
    LIMIT = MAX_METRO_DEMAND
    UNIT = "passengers"
    # The network's attributes it shows: the places (rows and columns), people an hour, each pair's day
    PLACES, DEMAND, DAILY = "metro_stops", "metro_demand", "metro_daily"
    TIP = ""  # a place's header tooltip; "": the station's lines
    ANY_PAIR = False  # any two different places can be typed in; False: only those a ride joins

    def __init__(self) -> None:
        super().__init__()
        self.network: Network | None = None
        self.daily = False
        self.stations: list[str] = []
        self.title = QLabel()
        self.title.setStyleSheet("font-weight: 600;")
        self.table = QTableWidget()
        self.table.setItemDelegate(_BlockedDelegate(self.table))  # the cells no one types in: shut
        self.table.itemChanged.connect(self._edited)
        self.table.currentCellChanged.connect(self._show_day)
        self.day = DayCurve(unit=self.UNIT)  # daily: the selected pair's day
        self.day.changed.connect(self._curve_edited)
        self.day_hint = hint_label("Click a pair with people a day to shape its day.")
        self.side = QWidget()  # for the sidebar (ODCellPanel.add_side)
        side = QVBoxLayout(self.side)
        side.setContentsMargins(0, 0, 0, 0)
        side.addWidget(self.day)
        side.addWidget(self.day_hint)
        layout = QVBoxLayout(self)
        layout.addWidget(self.title)
        layout.addWidget(self.table, 1)
        self.empty = hint_label(self.EMPTY)
        layout.addWidget(self.empty)
        layout.addStretch()  # no grid yet: the title and hint at the top
        self._show_day()  # no pair yet: no curve (it showed under Hourly until the grid was first drawn)

    def _places(self, network: Network) -> list[str]:
        """The rows' and columns' places, in number order (M2 before M10)."""
        return sorted(getattr(network, self.PLACES), key=lambda key: (int(key[1:]) if key[1:].isdigit() else 0, key))

    def _cells(self) -> dict:
        """The demand shown: people an hour, or each pair's day ({"people", "curve"})."""
        return getattr(self.network, self.DAILY if self.daily else self.DEMAND)

    def _people(self, key: str) -> float | None:
        value = self._cells().get(key)
        return value["people"] if self.daily and value is not None else value

    def _joined(self, network: Network, places: list[str]) -> set:
        """The pairs that can be typed in: any two different places, or those a ride joins."""
        return {(board, alight) for board in places for alight in places
                if board != alight and (self.ANY_PAIR or ride(network, board, alight))}

    def _tip(self, network: Network, place: str) -> str:
        if self.TIP:
            return self.TIP
        lines = [line.id for line in lines_of(network, place)]
        if len(lines) > 4:  # a tooltip is five words at most
            return f"{len(lines)} lines"
        return ("Lines " if len(lines) > 1 else "Line ") + ", ".join(lines)

    def show_network(self, network: Network) -> None:
        """Rows and columns for the network's places (_places), each cell its people an hour."""
        self.network = network
        self.title.setText(self.TITLE.format(per="a day" if self.daily else "an hour"))
        stations = self._places(network)
        self.stations = stations
        self.rides = self._joined(network, stations)
        table = self.table
        table.blockSignals(True)
        table.clear()
        table.setRowCount(len(stations))
        table.setColumnCount(len(stations))
        table.setHorizontalHeaderLabels(stations)
        table.setVerticalHeaderLabels(stations)
        for index, stop_id in enumerate(stations):
            for header in (table.horizontalHeaderItem(index), table.verticalHeaderItem(index)):
                header.setToolTip(self._tip(network, stop_id))
        for row, board in enumerate(stations):
            for column, alight in enumerate(stations):
                people = self._people(f"{board}->{alight}")
                item = QTableWidgetItem("" if people is None else f"{people:g}")
                if (board, alight) not in self.rides:
                    item.setFlags(Qt.ItemFlag.ItemIsEnabled)  # no ride: the same station, or no line joins them
                    item.setToolTip("No ride between them" if board != alight else "")
                table.setItem(row, column, item)
        table.blockSignals(False)
        self.table.setVisible(bool(stations))
        self.empty.setVisible(not stations)
        self._show_day()

    def _edited(self, item: QTableWidgetItem) -> None:
        board, alight = self.stations[item.row()], self.stations[item.column()]
        key, text = f"{board}->{alight}", item.text().strip()
        try:
            people = float(text) if text else 0.0
            if not (0 <= people <= self.LIMIT * (HOURS if self.daily else 1)) or (
                    people and (board, alight) not in self.rides):
                raise ValueError  # out of range, or no ride between them
        except ValueError:
            before = self._people(key)
            self.table.blockSignals(True)
            item.setText("" if before is None else f"{before:g}")
            self.table.blockSignals(False)
            return
        cells = self._cells()
        if not people:
            cells.pop(key, None)
        elif self.daily:  # a new pair's day starts as the weekday's
            cells[key] = {"people": people, "curve": cells[key]["curve"] if key in cells else profile_record(DailyProfile())}
        else:
            cells[key] = people
        self._show_day()
        self.changed.emit(None)

    def _current_key(self) -> str | None:
        row, column = self.table.currentRow(), self.table.currentColumn()
        if 0 <= row < len(self.stations) and 0 <= column < len(self.stations):
            return f"{self.stations[row]}->{self.stations[column]}"
        return None

    def _show_day(self, *_) -> None:
        """Daily: the selected pair's curve, once it has people a day; else how to get one."""
        key = self._current_key() if self.daily and self.network is not None else None
        day = self._cells().get(key) if key else None
        self.day.setVisible(day is not None)
        self.day_hint.setVisible(self.daily and day is None)
        if day is not None:
            self.day.heading.setText(f"{key.replace('->', ' to ')}: spread over the day")
            self.day.show_profile(from_record(day["curve"]), day["people"])

    def _curve_edited(self) -> None:
        key = self._current_key()
        cells = self._cells()
        if key in cells:
            cells[key]["curve"] = profile_record(self.day.profile)
            self.changed.emit(("day", type(self).__name__, key))  # a burst of edits to one curve: one undo step


class SpawnMatrix(MetroMatrix):
    """Demand Generator → Deploy Pedestrian: people walking an hour between the spawn points placed on the map
    (Network.spawn_demand), rows from, columns to, typed in as MetroMatrix's; any two different points."""

    TITLE = "Pedestrians {per}, deploy point to deploy point"
    EMPTY = "No deploy points yet: Place Deploy Points, click the map, right-click to finish."
    LIMIT = MAX_WALK_DEMAND
    UNIT = "people"
    PLACES, DEMAND, DAILY = "spawn_points", "spawn_demand", "spawn_daily"
    TIP, ANY_PAIR = "Deploy point", True


class PedestrianMatrix(MetroMatrix):
    """Demand Generator → Pedestrian OD: people walking an hour between boundary ends (Network.walk_demand), rows from,
    columns to, typed in as MetroMatrix's; any two different ends (a run notes the pairs no footpath or path joins).
    Institutions add their walkers on top."""

    TITLE = "Pedestrians {per}, boundary end to boundary end"
    EMPTY = "No boundary ends yet: add a road to the network's edge in Edit Network > Roads."
    LIMIT = MAX_WALK_DEMAND
    UNIT = "people"
    PLACES, DEMAND, DAILY = "approach_nodes", "walk_demand", "walk_daily"
    TIP, ANY_PAIR = "Boundary end", True


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
        """Show and edit hourly or daily demand (the Demand Generator's Hourly/Daily switch)."""
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
            cell.drops = drops_after(cell.drops, action, index)
        self.refresh()

    def split_of(self, cell: ODCell) -> tuple[int, ...]:
        """The split in effect (demand.split_of) for the demand shown, with Mode Behavior's timetabled types."""
        return split_of(cell, self.vehicle_modes.vehicles_per_hour(), self.mode)

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

    def prune(self, ends) -> None:
        """Pairs naming an end that's gone go with it (as Network._prune_walking does for pedestrians), so a
        new end given its name starts with none. Called before each undo step is taken: Undo brings them back."""
        for pair in [pair for pair in self.cells if not set(pair) <= set(ends)]:
            del self.cells[pair]

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


class DemandTabs(QWidget):
    """The Demand Generator's tabs, three to a row (five names don't fit one row of the sidebar), each with its key:
    `currentData` and `findData`, as a combo box has; `currentIndex`, `setCurrentIndex` and `currentChanged`, as a tab
    bar has."""

    currentChanged = Signal(int)
    COLUMNS = 3

    def __init__(self) -> None:
        super().__init__()
        self.grid = QGridLayout(self)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(6)
        self.group = QButtonGroup(self)  # exclusive: one tab open
        self.group.idToggled.connect(lambda index, on: on and self.currentChanged.emit(index))
        self.keys: list[str] = []

    def add(self, label: str, key: str, tip: str) -> None:
        index = len(self.keys)
        self.keys.append(key)
        button = QPushButton(label)
        button.setCheckable(True)
        button.setProperty("role", "tab")  # a tab's tile (theme)
        button.setToolTip(tip)
        self.group.addButton(button, index)
        self.grid.addWidget(button, index // self.COLUMNS, index % self.COLUMNS)
        if not index:
            button.setChecked(True)

    def count(self) -> int:
        return len(self.keys)

    def tabData(self, index: int) -> str:
        return self.keys[index]

    def currentIndex(self) -> int:
        return self.group.checkedId()

    def setCurrentIndex(self, index: int) -> None:
        if 0 <= index < len(self.keys):
            self.group.button(index).setChecked(True)

    def currentData(self) -> str:
        return self.keys[self.currentIndex()]

    def findData(self, key: str) -> int:
        return self.keys.index(key) if key in self.keys else -1


class ODCellPanel(QWidget):
    """Side-panel editor for the selected pair; stays open while you move through the grid."""

    changed = Signal(object)  # OD data changed; arg = undo merge key (typing into one cell)
    demand_chosen = Signal(str)  # a Demand tab: "vehicles", "walk", "spawn", "institutions" or "metro" (or the switch)

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
        # (origin, destination) -> (metres, [(share of the length, junction id)]) of the fastest route, or None when
        # there's none; set by the window; None: drop-offs can't be placed
        self.router = None
        # Hourly or daily, for every tab at once (a Run uses that one), then which demand to enter: each tab its editor.
        self.period = ChoiceButtons([("Hourly", "hourly"), ("Daily", "daily")])
        self.period.setToolTip("Per hour or per day")
        self.period.changed.connect(self._period_chosen)
        self.demand = DemandTabs()
        for label, key, tip in (("Vehicle OD", "vehicles", "Vehicles between boundary ends"),
                                ("Pedestrian OD", "walk", "Walkers between boundary ends"),  # PedestrianMatrix
                                ("Deploy Pedestrian", "spawn", "Walkers between deploy points"),  # points placed on the map
                                ("Institutions", "institutions", "Buildings that make trips"),
                                ("MRT", "metro", "Passengers between stations")):  # the metro's own (MetroMatrix)
            self.demand.add(label, key, tip)
        self.demand.currentChanged.connect(self._demand_chosen)
        demand_form = QFormLayout()
        demand_form.addRow("Demand", self.period)
        demand_form.addRow(self.demand)
        self.institutions = InstitutionPanel()
        self.institutions.hide()
        # The OD Pair editor shows only while a cell is selected; until then, one line says how.
        self.empty_hint = hint_label("Click a cell in the grid to edit its demand.")
        self.empty_hint.setToolTip("Ctrl+click to select several")
        self.metro_hint = hint_label()  # their text follows the switch (_period_chosen)
        self.metro_hint.hide()
        self.walk_hint = hint_label()
        self.walk_hint.hide()
        # Spawn points: placed on the map (each click one, a right-click finishes), then their own grid (SpawnMatrix)
        self.spawn_box = QWidget()
        spawn_layout = QVBoxLayout(self.spawn_box)
        spawn_layout.setContentsMargins(0, 0, 0, 0)
        self.place_spawn_button = styled_button("Place Deploy Points", tooltip="Click the map for points", checkable=True)
        spawn_layout.addWidget(self.place_spawn_button)
        spawn_layout.addWidget(hint_label(
            "Click anywhere near the network for each deploy point; right-click finishes and shows their grid. Type "
            "people walking between them there (an hour, or a day with its curve). A walker walks straight to the footpath nearest its point, "
            "the shortest way on, and straight from the footpath nearest its destination. Click a point on the map "
            "to select it; Delete removes it."))
        self.spawn_box.hide()
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
        # One type's drop-offs (its zone clicked in Modes): trips ending part way along the pair's route.
        self.drop_type: int | None = None
        self.drop_box = QWidget()
        drops = QVBoxLayout(self.drop_box)
        drops.setContentsMargins(0, 4, 0, 4)
        self.drop_heading = QLabel()
        self.drop_heading.setStyleSheet("font-weight: 600;")
        drops.addWidget(self.drop_heading)
        self.drop_slider = DropSlider()
        self.drop_slider.setToolTip("Click track to add drop-off")
        drops.addWidget(self.drop_slider)
        self.drop_share_row = QWidget()
        share_row = QHBoxLayout(self.drop_share_row)
        share_row.setContentsMargins(0, 0, 0, 0)
        share_row.addWidget(QLabel("Ends here"))
        self.drop_share = QSpinBox()
        self.drop_share.setRange(1, 100)
        self.drop_share.setSuffix(" %")
        self.drop_share.setToolTip("Trips ending at this point")
        share_row.addWidget(self.drop_share)
        share_row.addStretch(1)
        drops.addWidget(self.drop_share_row)
        self.drop_hint = hint_label()
        drops.addWidget(self.drop_hint)
        self.drop_box.hide()
        form.addRow(self.drop_box)
        fields.addLayout(form)
        self.day = DayCurve()  # Daily demand: how the day's trips spread over 24 hours
        fields.addWidget(self.day)
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
        self.sides: dict[str, QWidget] = {}  # each grid's own day curve (add_side), shown on its tab
        self.outer = outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addLayout(demand_form)
        outer.addWidget(self.empty_hint)
        outer.addWidget(self.metro_hint)
        outer.addWidget(self.walk_hint)
        outer.addWidget(self.spawn_box)
        outer.addWidget(group)
        outer.addWidget(self.institutions)

        self.flow.valueChanged.connect(lambda: self._edited("flow"))
        self.pattern.currentIndexChanged.connect(lambda: self._edited("pattern"))
        self.phf.valueChanged.connect(lambda: self._edited("quarters"))
        self.modes.changed.connect(lambda: self._edited("modes"))
        self.modes.changed.connect(self._sync_shares)
        self.modes.type_clicked.connect(self._pick_drop_type)
        self.drop_slider.changed.connect(lambda: self._edited("drops"))
        self.drop_slider.picked.connect(self._show_drop_point)
        self.drop_share.valueChanged.connect(self._drop_share_typed)
        self.day.changed.connect(lambda: self._edited("profile"))
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
        self._period_chosen()
        self.show_current()

    def add_side(self, kind: str, widget: QWidget) -> None:
        """A tab's grid's own editor (MetroMatrix.side), in the sidebar under its hint, as Vehicle OD's pair editor."""
        self.sides[kind] = widget
        self.outer.insertWidget(self.outer.indexOf(self.pair_group), widget)
        widget.setVisible(self.demand.currentData() == kind)

    def _period_chosen(self) -> None:
        """Hourly or daily: every tab's demand of that kind, as a Run takes it."""
        daily = self.period.value() == "daily"
        self.matrix.set_mode(self.period.value())
        per, shape = ("a day", " Click a pair to shape its day below.") if daily else ("an hour", "")
        self.metro_hint.setText(f"Type passengers {per} between stations in the grid.{shape} Stations no line joins, "
                                "directly or with one change, can't be typed in. Buildings near stations add their "
                                "own riders.")
        self.walk_hint.setText(f"Type people walking {per} between boundary ends in the grid.{shape} They walk the "
                               "footpaths and paths of Edit Network > Roads; buildings' walkers add on top.")
        self.institutions.set_daily(daily)
        self._demand_chosen()

    def _demand_chosen(self) -> None:
        """Vehicle OD: the OD pair editor over the grid. Institutions: its panel (the map). The others: their grid."""
        kind = self.demand.currentData()
        self.institutions.setVisible(kind == "institutions")
        self.metro_hint.setVisible(kind == "metro")
        self.walk_hint.setVisible(kind == "walk")
        self.spawn_box.setVisible(kind == "spawn")
        for key, side in self.sides.items():
            side.setVisible(key == kind)
        if kind != "spawn":
            self.place_spawn_button.setChecked(False)
        self._show_editor()
        self.demand_chosen.emit(kind)

    def _show_editor(self) -> None:
        """The OD Pair editor while a cell is selected (Vehicle OD); else the one-line hint."""
        pairs = self.demand.currentData() == "vehicles"
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
        self.day.setVisible(daily)
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
        self.day.show_profile(cell.profile, self.flow.value() if daily else -1)
        self._show_drops()
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
                cell.profile = deepcopy(self.day.profile)
            elif field == "pattern":
                cell.pattern = self.pattern.currentText()
            elif field == "quarters":
                cell.quarters = phf_quarters(self.phf.value())
            elif field == "drops":  # the picked type's points; the other types' stay
                cell.drops = tuple(sorted([*(point for point in cell.drops if point[0] != self.drop_type),
                                           *((self.drop_type, at, share) for at, share in self.drop_slider.points)]))
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
        self.day.set_total(self.flow.value() if self.matrix.mode == "daily" else -1)

    def _pick_drop_type(self, index: int) -> None:
        """A type's zone clicked in Modes: its drop-offs below the shares (clicked again: put away)."""
        self.drop_type = None if index == self.drop_type else index
        self.drop_slider.selected = None
        self._show_drops()

    def _show_drops(self) -> None:
        """The picked type's drop-offs on the current pair's fastest route: one pair at a time, as routes differ."""
        pair = self.matrix.current_pair()
        modes = self.matrix.vehicle_modes.modes
        self.drop_box.setVisible(pair is not None and self.drop_type is not None and self.drop_type < len(modes))
        if not self.drop_box.isVisible():
            return
        mode = modes[self.drop_type]
        self.drop_heading.setText(f"Drop-offs: {mode.name}")
        route = self.router(*pair) if self.router and len(self.matrix.selected_pairs()) == 1 else None
        self.drop_slider.setVisible(route is not None)
        if route is None:
            self.drop_share_row.hide()
            self.drop_hint.setText("Select one pair to place its drop-offs: each pair's route differs."
                                   if len(self.matrix.selected_pairs()) > 1 else
                                   f"No route from {pair[0]} to {pair[1]} yet, so nowhere to drop off.")
            return
        self.drop_slider.set_route(route[0], route[1], pair, mode.colour)
        self.drop_slider.set_points((at, share) for kind, at, share in self.matrix.cell(pair).drops
                                    if kind == self.drop_type)
        self._show_drop_point()

    def _show_drop_point(self) -> None:
        """The selected point's % in its box, at most what the other points leave; and what reaches the exit."""
        slider, pair = self.drop_slider, self.matrix.current_pair()
        index = slider.selected
        self.drop_share_row.setVisible(index is not None)
        if index is not None:
            share = slider.points[index][1]
            self.drop_share.blockSignals(True)
            self.drop_share.setMaximum(share + slider.left_over())
            self.drop_share.setValue(share)
            self.drop_share.blockSignals(False)
        self.drop_hint.setText(f"{slider.left_over()} % of these trips reach {pair[1] if pair else 'the exit'} "
                               f"({slider.length:.0f} m). Click the track to add a point where trips end, drag it, "
                               "double-click or right-click to remove it.")

    def _drop_share_typed(self, value: int) -> None:
        slider = self.drop_slider
        if slider.selected is not None:
            slider.points[slider.selected][1] = value
            slider.update()
            self._edited("drops")
            self._show_drop_point()

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
        self.drop_type = None  # its index may have moved
        self.drop_box.hide()
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
                cell.profile = deepcopy(self.day.profile)
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
        plan = self._demand_plan(args) if name == "set_od_demand" else profile_from(args)
        if name == "set_od_demand" and plan[1] is None:  # a pattern or split alone
            daily = plan[0] == "daily"
            if not any((self.matrix.cell(pair).daily if daily else self.matrix.cell(pair).flow) for pair in pairs):
                raise ValueError(f"Those pairs have no {plan[0]} demand yet: give a value too. Nothing was changed.")
        drops = None
        if name == "set_od_demand" and args.get("drop_offs") is not None:  # each pair's points on its own route
            if len(pairs) > MAX_DROP_PAIRS and listed(args["drop_offs"]):
                raise ValueError(f"Drop-offs go on {MAX_DROP_PAIRS} pairs at most at a time (each pair's route "
                                 "differs): name the pairs. Nothing was changed.")
            names = self.matrix.vehicle_modes.names()
            drops = {pair: drops_from(args["drop_offs"], names, self.router(*pair) if self.router else None, pair,
                                      self.matrix.cell(pair).drops) for pair in pairs}

        def apply() -> dict:
            return self._set_demand(pairs, *plan, drops=drops) if name == "set_od_demand" else \
                self._shape_curve(pairs, plan)

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
                    lead_with_what_was_kept(info, share, sum(round(value * share) for value in typed.values()))
        except Exception:
            self.matrix.cells = before
            self.matrix.refresh()
            raise
        finally:
            self.blockSignals(False)
        if self.matrix.cells != before:
            self.changed.emit(None)

    def _demand_now(self, demand: str, scope: list) -> tuple[dict, dict | None]:
        return demand_now(self.matrix.cells, self.matrix.origins, self.matrix.destinations, demand, scope)

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
        return pairs_from(args, self.matrix.origins, self.matrix.destinations)

    def _show_mode(self, demand: str) -> None:
        """Hourly or daily demand on the Vehicle OD tab, as the switch and the tab do."""
        self.period.set_value(demand)
        self.demand.setCurrentIndex(self.demand.findData("vehicles"))
        self._period_chosen()

    def _select(self, pairs: list[tuple[str, str]]) -> None:
        """Select these pairs in the grid, as clicking the first and Ctrl+clicking the rest does."""
        table, spots = self.matrix.table, [(self.matrix.origins.index(o), self.matrix.destinations.index(d))
                                           for o, d in pairs]
        table.clearSelection()
        table.setCurrentCell(*spots[0])
        for row, column in spots[1:]:
            table.setRangeSelected(QTableWidgetSelectionRange(row, column, row, column), True)

    def _demand_plan(self, args: dict) -> tuple:
        return demand_plan(args, self.matrix.mode, self.matrix.vehicle_modes.names())

    def _set_demand(self, pairs: list[tuple[str, str]], demand: str, value, pattern, shares, quarters=None,
                    drops=None) -> dict:
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
        if drops is not None:  # straight onto the pairs, each its own route's points, as the drop-off slider sets them
            for pair in pairs:
                cell = self.matrix.cell(pair)
                cell.drops = drops[pair]
                self.matrix.cells[pair] = cell
            self.show_current()
        cell = self.matrix.cell(pairs[0])
        total = sum(self.matrix.value(c) or 0 for c in self.matrix.cells.values())
        result = {"pairs": [f"{o}->{d}" for o, d in pairs][:20], "pairs_set": len(pairs), "demand": demand,
                  "value_each": self.matrix.value(cell), "unit": UNITS[demand],
                  "matrix_total": total,  # every pair's, so "each" isn't read as the total
                  "arrival_pattern": cell.pattern,
                  "mode_split": dict(zip(self.matrix.vehicle_modes.names(), self.matrix.split_of(cell))),
                  **(quarters_record(cell) if demand == "hourly" else {}),
                  **({"drop_offs": drops_record(cell.drops, self.matrix.vehicle_modes.names())} if cell.drops else {})}
        timed = [f"{mode.name} every {mode.headway_min} min" for mode in self.matrix.vehicle_modes.modes
                 if mode.headway_min]
        if timed:
            result["note"] = (f"Timetabled ({', '.join(timed)}): their shares are fixed by each pair's flow, "
                              "and the other types shared the rest in the proportions given.")
        return result

    def _shape_curve(self, pairs: list[tuple[str, str]], profile: DailyProfile) -> dict:
        self._show_mode("daily")
        self._select(pairs)
        self.day.set_profile(profile)  # loads the editor, its equation dropdown and coefficients
        shares = profile.shares()
        peak = max(range(HOURS), key=shares.__getitem__)
        result = {"pairs": [f"{o}->{d}" for o, d in pairs], "curve": curve_record(profile),
                  "busiest_hour": f"{peak:02d}:00-{peak + 1:02d}:00", "busiest_share": round(shares[peak], 3)}
        if not any(profile.hourly_levels()):
            result["note"] = "The curve is zero all day, so the trips are spread evenly."
        missing = [f"{o}->{d}" for o, d in pairs if self.matrix.cell((o, d)).daily is None]
        if missing:
            result["note"] = (result.get("note", "") + " These pairs have no trips per day yet: "
                              + ", ".join(missing[:10])).strip()
        return result

    def _read_for_assistant(self) -> dict:
        matrix = self.matrix
        return read_od(matrix.cells, matrix.origins, matrix.destinations, matrix.mode, matrix.vehicle_modes.names(),
                       matrix.vehicle_modes.vehicles_per_hour())


def _busiest(values: list[float]) -> str:
    if not any(values):
        return "no cars"
    hour = max(range(HOURS), key=values.__getitem__)
    return f"busiest {hour:02d}:00–{hour + 1:02d}:00 at {values[hour]:,.0f} veh/h"


class InstitutionPanel(QWidget):
    """Demand Generator → Institutions. Pick a building by its icon and set its figures; then, as in an
    RTS game, move it on the map (green: it can stand there; red: it can't), click to set it down,
    scroll to size its circle of influence and click again to finish (network_scene, institutions.py).
    Clicking a placed building shows its circle and what it loads onto each junction and end, and its demand as the
    Hourly/Daily switch shows it: people arriving and leaving an hour, or its day's arrive and leave curves."""

    placing_changed = Signal(object)  # the footprint (m²) of the building to place now, or None: not placing
    delete_requested = Signal(str)  # the selected building's Delete
    split_requested = Signal(str)  # the selected building's Mode split
    height_requested = Signal(str, float)  # selected institution's total visual height, metres
    # selected institution's "per_hour" or "curves", as a whole, and the box or curve edited ("arrive", "leave"; None:
    # all of them), so a burst of edits to one is one undo step
    demand_requested = Signal(str, str, object, object)

    def __init__(self) -> None:
        super().__init__()
        self.kind: str | None = None  # the building being placed
        self.selected: Institution | None = None
        self.network: Network | None = None
        self.daily = False  # the Demand switch (set_daily)
        group = QGroupBox("Institutions")
        layout = QVBoxLayout(group)
        row = QHBoxLayout()
        row.setSpacing(4)
        self.kind_buttons: dict[str, object] = {}
        for name, kind in KINDS.items():
            button = icon_button(kind.glyph, name, checkable=True)  # the banner says each step
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
        self.hourly_box = QWidget()  # Hourly: people an hour typed in
        hourly = QFormLayout(self.hourly_box)
        hourly.setContentsMargins(0, 4, 0, 0)
        self.per_hour: dict[str, QSpinBox] = {}
        for key, label, tip in (("arrive", "Arriving", "People arriving an hour"),
                                ("leave", "Leaving", "People leaving an hour")):
            box = QSpinBox()
            box.setRange(-1, MAX_INSTITUTION_PER_H)  # -1 shows "—": its day's busiest hour
            box.setSpecialValueText("—")
            box.setSingleStep(10)
            box.setSuffix(" people/h")
            box.setToolTip(tip)
            box.valueChanged.connect(lambda value, key=key: self._per_hour_typed(key, value))
            hourly.addRow(label, box)
            self.per_hour[key] = box
        hourly.addRow(hint_label("By any mode; its mode split shares them. —: its day's busiest hour."))
        details.addWidget(self.hourly_box)
        self.daily_box = QWidget()  # Daily: its arrivals' and departures' curves
        daily = QVBoxLayout(self.daily_box)
        daily.setContentsMargins(0, 0, 0, 0)
        self.days = {key: DayCurve(title, "people") for key, title in (("arrive", "Arriving over the day"),
                                                                      ("leave", "Leaving over the day"))}
        for key, day in self.days.items():
            day.changed.connect(lambda key=key: self._curve_edited(key))
            daily.addWidget(day)
        self.shift_button = styled_button("Use Shift Times", tooltip="Curves from its shift")
        self.shift_button.clicked.connect(lambda: self.demand_requested.emit(self.selected.id, "curves", {}, None))
        daily.addWidget(self.shift_button)
        details.addWidget(self.daily_box)
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

    def set_daily(self, daily: bool) -> None:
        """The Demand switch: the selected building's people an hour, or its day's curves."""
        self.daily = daily
        if self.selected is not None:
            self.show_selected(self.network, self.selected)
        self._refresh()

    def _per_hour_typed(self, key: str, value: int) -> None:
        per_hour = dict(self.selected.per_hour)
        if value < 0:
            per_hour.pop(key, None)
        else:
            per_hour[key] = float(value)
        self.demand_requested.emit(self.selected.id, "per_hour", per_hour, key)

    def _curve_edited(self, key: str) -> None:
        self.demand_requested.emit(self.selected.id, "curves",
                                   {**self.selected.curves, key: profile_record(self.days[key].profile)}, key)

    def show_selected(self, network: Network, item: Institution | None) -> None:
        """A building clicked on the map: its figures, its mode split, its door and where its people come from, and
        its demand as the switch shows it (its curves start from its shift until shaped)."""
        self.selected, self.network = item, network
        if item is not None:
            for key, box in self.per_hour.items():
                box.blockSignals(True)
                box.setValue(round(item.per_hour[key]) if key in item.per_hour else -1)
                box.blockSignals(False)
            day, shift = hourly_people(item), shift_people(item)
            for side, (key, curve) in enumerate(self.days.items()):
                curve.show_profile(from_record(item.curves[key]) if key in item.curves else through_hours(shift[side]),
                                   sum(day[side]))
            self.shift_button.setEnabled(bool(item.curves))
            self.selected_height.blockSignals(True)
            self.selected_height.setValue(building_height(item))
            self.selected_height.blockSignals(False)
            kind = KINDS[item.kind]
            arrive, _ = hourly_people(item)
            split = " · ".join(f"{name} {percent:.0f} %" for name, percent in shares(item).items())
            lines = [f"{item.people:,} {kind.people.lower()} · {item.employees:,} employees · shift "
                     f"{clock_h(item.shift_start_h)}–{clock_h(item.shift_end_h)} · {item.area_m2:,.0f} m²",
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
        self.hourly_box.setVisible(not self.daily)
        self.daily_box.setVisible(self.daily)
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

"""The Demand Generator's painted demand controls: the mode-split slider, the day curve and its Curves-style
editor, and the drop-off slider along a route. Moved out of od_matrix.py, whose pair editor (ODCellPanel) builds them.
"""

from __future__ import annotations

from copy import deepcopy
from itertools import accumulate

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from . import theme
from .daily_profile import DEGREES, HOURS, MIN_GAP_H, DailyProfile
from .mode_behavior import VehicleModes
from .widgets import hint_label, icon_button


# Categorical slots 1-3 of the dataviz reference palette (its dark-surface steps), in
# fixed order. Validated on the grid grey: all checks pass, worst adjacent colour-blind
# dE 9.4. On the lighter panel grey bus/car dip under 3:1, so the slider always labels them.
TEXT = QColor(theme.TEXT)
SELECTED_FILL, SELECTED_EDGE = QColor(theme.BANNER), QColor(theme.ACCENT_EDGE)
HANDLE_EDGE, HANDLE_FILL = QColor(theme.HANDLE_EDGE), QColor(theme.HANDLE_FILL)
GRID = QColor(theme.CURVE_GRID)  # curve editor grid lines
BARS = QColor(theme.ACCENT)  # hourly share bars: the accent, subdued
BARS.setAlpha(110)


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

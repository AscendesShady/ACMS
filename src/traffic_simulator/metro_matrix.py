"""The people-between-places grids of the Demand Generator: the MRT OD matrix and its Pedestrian OD and Deploy
Pedestrian variants, each with its day curve in the sidebar. Moved out of od_matrix.py (which re-exports them).
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush, QColor, QPainter
from PySide6.QtWidgets import (
    QLabel,
    QStyledItemDelegate,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import theme
from .daily_profile import HOURS, DailyProfile, from_record, profile_record
from .metro import lines_of, ride
from .models import MAX_METRO_DEMAND, MAX_WALK_DEMAND, Network
from .widgets import hint_label
from .demand_controls import DayCurve


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

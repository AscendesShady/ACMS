"""Editing institutions in the Demand Generator: the Institutions panel and the selected building's Mode split
dialog. od_matrix.py's pair editor (ODCellPanel) builds the panel.
"""

from __future__ import annotations

from math import sqrt

from PySide6.QtCore import QTime, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QSpinBox,
    QTimeEdit,
    QVBoxLayout,
    QWidget,
)

from .daily_profile import HOURS, from_record, profile_record, through_hours
from .institutions import KINDS, catchment, door, hourly_people, hourly_trips, shares, shift_people
from .results import clock_h
from .models import (BUILDING_HEIGHTS_M, MAX_AREA_M2, MAX_BUILDING_HEIGHT_M, MAX_INSTITUTION_PER_H, Institution,
                     Network, institution_problem)
from .building_assets import height as building_height
from .widgets import hint_label, icon_button, styled_button
from .demand_controls import DayCurve


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
        # Show Buildings: footprints and labels on the map; demand is the same either way (the window wires it)
        self.buildings_button = icon_button(0xEC06, "Show Buildings", checkable=True)  # native CityNext glyph
        row.addWidget(self.buildings_button)
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

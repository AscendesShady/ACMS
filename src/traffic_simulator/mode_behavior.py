"""Mode Behavior tab: the vehicle types (travel modes) the whole app uses.

VehicleModes holds them in order, a name and a colour each. The Demand Generator splits
trips between them (its slider and % boxes, and the bars in the matrix cells), and View
Summary and the assistant's demand tools read the same list. Renaming a type keeps every
OD pair's shares; a new type starts at 0 % everywhere; deleting one hands its share to
the others in proportion. A timetabled type (chosen when it's added; Bus, never Car or Bike) may have a headway; one with a headway (a bus every 10 min) has its share of each
pair fixed by its timetable (fixed_split); the others share the rest. Behaviour settings
(size, speeds, acceleration, car following, lane changing) come with the simulation.
"""

from __future__ import annotations

from copy import deepcopy
from itertools import count
import re

from PySide6.QtCore import QEvent, QObject, QTimer, Signal
from PySide6.QtWidgets import (QComboBox, QDoubleSpinBox, QFormLayout, QGridLayout, QGroupBox, QLabel, QLineEdit, QMessageBox,
                               QSpinBox, QVBoxLayout, QWidget)

from .demand import fixed_split, redistribute, whole_percent  # noqa: F401 - offered here too, as before
from .widgets import hint_label, styled_button
from .simulation import DT
from .theme import VEHICLE_PAINTS
from .vehicle import FILTERING_CLASSES, VEHICLE_CLASSES, VehicleMode

# The dataviz reference palette's categorical slots (dark-surface steps), in fixed order. Checked
# on the app's greys: every check passes; magenta and green sit under 3:1 against the grey, so a
# mode is never shown by colour alone (the % boxes and this tab name each one).
PALETTE = VEHICLE_PAINTS[:8]
MAX_MODES = len(PALETTE)  # past eight, colours stop being told apart reliably
MAX_HEADWAY_MIN = 120
PCU_RANGE = (0.1, 10.0)
OCCUPANCY_RANGE = (0.01, 500.0)


class VehicleModes(QObject):
    """The vehicle types, in order. `changed(action, index)` follows every change: "added",
    "renamed", "deleted" or "headway" (with the type's index), or "reset" (-1) when restored by undo."""

    changed = Signal(str, int)

    def __init__(self) -> None:
        super().__init__()
        self.modes = [VehicleMode("Bus", PALETTE[0], timetabled=True, length_m=12.0, width_m=2.5, max_speed_kmh=85.0,
                                  accel=1.2, decel=4.0, occupancy=20.0, pcu=2.0),
                      VehicleMode("Car", PALETTE[1], occupancy=1.5),
                      VehicleMode("Bike", PALETTE[2], length_m=1.6, width_m=0.65, max_speed_kmh=50.0,
                                  desired_max_kmh=20.0, accel=1.2, decel=3.0, min_gap_m=0.5, pcu=0.2,
                                  vehicle_class="bicycle")]
        self.default_split: tuple[int, ...] = (10, 85, 5)  # a new OD pair's shares

    def names(self) -> list[str]:
        return [mode.name for mode in self.modes]

    def problem_with_name(self, name: str, index: int | None = None) -> str | None:
        name = name.strip()
        if not name:
            return "A vehicle type needs a name."
        if any(mode.name.lower() == name.lower() for i, mode in enumerate(self.modes) if i != index):
            return f"There is already a vehicle type called {name}."
        return None

    def add(self, name: str = "", timetabled: bool = False) -> int:
        if len(self.modes) >= MAX_MODES:
            raise ValueError(f"Up to {MAX_MODES} vehicle types: past that their colours can't be told apart.")
        name = name.strip() or next(f"Type {n}" for n in count(len(self.modes) + 1)
                                    if not self.problem_with_name(f"Type {n}"))
        problem = self.problem_with_name(name)
        if problem:
            raise ValueError(problem)
        used = {mode.colour for mode in self.modes}
        self.modes.append(VehicleMode(name, next(colour for colour in PALETTE if colour not in used), timetabled=timetabled))
        self.default_split = (*self.default_split, 0)
        self.changed.emit("added", len(self.modes) - 1)
        return len(self.modes) - 1

    def create(self, specs: list, check_only: bool = False) -> list[int]:
        """Add vehicle types made as SUMO vehicle classes (vehicle.VEHICLE_CLASSES): each spec a dict of
        name, kind, and optionally headway_min and occupancy. All are checked first, so a refusal
        (ValueError, saying why) adds none. A motorcycle, moped or bicycle filters (VehicleMode.filters). Returns the new
        types' indexes."""
        if not isinstance(specs, list) or not specs or not all(isinstance(spec, dict) for spec in specs):
            raise ValueError("Give the types as a list, e.g. [{\"name\": \"Tram\", \"kind\": \"tram\"}].")
        if len(self.modes) + len(specs) > MAX_MODES:
            raise ValueError(f"Up to {MAX_MODES} vehicle types: there are {len(self.modes)}, so {MAX_MODES - len(self.modes)} "
                             "more at most.")
        names = []
        for spec in specs:
            name, kind = str(spec.get("name", "")).strip(), str(spec.get("kind", "")).strip().lower()
            problem = self.problem_with_name(name) or (f"Two new types are called {name}." if name.lower() in names else None)
            if problem:
                raise ValueError(problem)
            if kind not in VEHICLE_CLASSES:
                raise ValueError(f"{name}: kind must be one of {', '.join(VEHICLE_CLASSES)}.")
            headway, occupancy = spec.get("headway_min", 0), spec.get("occupancy")
            if not (isinstance(headway, (int, float)) and float(headway).is_integer() and 0 <= headway <= MAX_HEADWAY_MIN):
                raise ValueError(f"{name}: headway_min must be a whole 1 to {MAX_HEADWAY_MIN} minutes, or 0 for no timetable.")
            if occupancy is not None and not (isinstance(occupancy, (int, float)) and 0 < occupancy <= 500):
                raise ValueError(f"{name}: occupancy must be above 0 and at most 500 people.")
            names.append(name.lower())
        if check_only:
            return []
        made = []
        for spec in specs:
            index = self.add(str(spec["name"]), timetabled=bool(spec.get("headway_min")))
            mode = self.modes[index]
            mode.vehicle_class = str(spec["kind"]).strip().lower()
            for key, value in VEHICLE_CLASSES[mode.vehicle_class].items():
                setattr(mode, key, value)
            mode.filters = mode.vehicle_class in FILTERING_CLASSES
            if spec.get("occupancy") is not None:
                mode.occupancy = float(spec["occupancy"])
            if spec.get("headway_min"):
                self.set_headway(index, int(spec["headway_min"]))
            made.append(index)
        return made

    FIGURES = {"length_m": (0.3, 60), "width_m": (0.3, 5), "max_speed_kmh": (5, 400), "accel": (0.1, 10),
               "decel": (0.5, 10), "emergency_decel": (0.5, 15), "min_gap_m": (0, 20), "tau_s": (DT, 5), "sigma": (0, 1), "speed_dev": (0, 1),
               "occupancy": OCCUPANCY_RANGE, "pcu": PCU_RANGE}  # update()'s figures and their ranges
    # A reaction time shorter than the simulation's step can't be kept to: the driver reacts only once a step, so
    # following that close collides (SUMO warns of the same, sumo.dlr.de/docs/Simulation/Safety.html).
    WHY = {"tau_s": f" (no shorter than the {DT:g} s simulation step, or vehicles collide)"}

    def update(self, name: str, changes: dict, check_only: bool = False) -> int:
        """Change a type by name: new_name, timetabled, headway_min, vehicle_class (what it is: its size and speeds
        stay), filters (lateral runs: passes others within its lane) or FIGURES. All checked first (ValueError, saying why, changes nothing). Returns its index."""
        index = next((i for i, mode in enumerate(self.modes) if mode.name.lower() == str(name).strip().lower()), None)
        if index is None:
            raise ValueError(f"No vehicle type called {name}; there are {', '.join(self.names())}.")
        unknown = set(changes) - set(self.FIGURES) - {"new_name", "timetabled", "headway_min", "vehicle_class", "filters"}
        if unknown or not changes:
            raise ValueError(f"Give what changes: new_name, timetabled, headway_min, vehicle_class, filters or "
                             f"{', '.join(self.FIGURES)}.")
        if "vehicle_class" in changes and changes["vehicle_class"] not in VEHICLE_CLASSES:
            raise ValueError(f"vehicle_class must be one of {', '.join(VEHICLE_CLASSES)}.")
        if not isinstance(changes.get("timetabled", True), bool) or not isinstance(changes.get("filters", True), bool):
            raise ValueError("timetabled and filters must be true or false.")
        if "new_name" in changes and (problem := self.problem_with_name(str(changes["new_name"]), index)):
            raise ValueError(problem)
        headway = changes.get("headway_min", 0)
        if not (isinstance(headway, (int, float)) and float(headway).is_integer() and 0 <= headway <= MAX_HEADWAY_MIN):
            raise ValueError(f"headway_min must be a whole 1 to {MAX_HEADWAY_MIN} minutes, or 0 for no timetable.")
        if headway and not changes.get("timetabled", self.modes[index].timetabled):
            raise ValueError(f"{self.modes[index].name} has no headway: it isn't timetabled (give timetabled true in the "
                             "same call).")
        for key, (low, high) in self.FIGURES.items():
            if key in changes and not (isinstance(changes[key], (int, float)) and low <= changes[key] <= high):
                raise ValueError(f"{key} must be {low:g} to {high:g}{self.WHY.get(key, '')}.")
        decel = changes.get("decel", self.modes[index].decel)
        if decel > changes.get("emergency_decel", self.modes[index].emergency_decel):
            raise ValueError(f"decel ({decel:g}) must be no more than emergency_decel, the hardest it can brake "
                             f"({changes.get('emergency_decel', self.modes[index].emergency_decel):g}).")
        if check_only:
            return index
        mode = self.modes[index]
        for key in self.FIGURES:
            if key in changes:
                setattr(mode, key, float(changes[key]))
        if "new_name" in changes:
            mode.name = str(changes["new_name"]).strip()
        if "vehicle_class" in changes:
            mode.vehicle_class = changes["vehicle_class"]
        if "filters" in changes:
            mode.filters = changes["filters"]
        if "timetabled" in changes:
            mode.timetabled = changes["timetabled"]
            mode.headway_min = mode.headway_min if mode.timetabled else 0
        if "headway_min" in changes:
            mode.headway_min = int(changes["headway_min"])
        self.changed.emit("updated", index)
        return index

    def rename(self, index: int, name: str) -> None:
        problem = self.problem_with_name(name, index)
        if problem:
            raise ValueError(problem)
        self.modes[index].name = name.strip()
        self.changed.emit("renamed", index)

    def delete(self, index: int) -> None:
        if len(self.modes) == 1:
            raise ValueError("Keep at least one vehicle type: trips need a mode.")
        del self.modes[index]
        self.default_split = redistribute(self.default_split, index)
        self.changed.emit("deleted", index)

    def set_headway(self, index: int, minutes: int) -> None:
        """Minutes between the type's vehicles on a timetable (0: none). Each OD pair then carries
        60 ÷ headway of them an hour, a share the Demand Generator holds fixed."""
        if not 0 <= minutes <= MAX_HEADWAY_MIN:
            raise ValueError(f"A headway is 1 to {MAX_HEADWAY_MIN} minutes, or 0 for no timetable.")
        if minutes and not self.modes[index].timetabled:
            raise ValueError(f"{self.modes[index].name} has no headway: it isn't timetabled (give timetabled true in the "
                             "same call).")
        self.modes[index].headway_min = int(minutes)
        self.changed.emit("headway", index)

    def set_colour(self, index: int, colour: str | None) -> None:
        """None chooses stable random vehicle paint, retaining the type's categorical colour."""
        if not 0 <= index < len(self.modes) or (colour is not None and
                                               not re.fullmatch(r"#[0-9a-fA-F]{6}", str(colour))):
            raise ValueError("Choose a vehicle type and a colour like #3987e5, or Random.")
        mode = self.modes[index]
        if mode.random_colour == (colour is None) and (colour is None or mode.colour == colour):
            return
        mode.random_colour = colour is None
        if colour is not None:
            mode.colour = colour
        self.changed.emit("colour", index)

    def set_pcu(self, index: int, pcu: float) -> None:
        """The type's passenger car units (a car is 1)."""
        if not PCU_RANGE[0] <= pcu <= PCU_RANGE[1]:
            raise ValueError(f"A PCU value is {PCU_RANGE[0]:g} to {PCU_RANGE[1]:g} (a car is 1).")
        self.modes[index].pcu = round(float(pcu), 2)
        self.changed.emit("pcu", index)

    def set_occupancy(self, index: int, people: float) -> None:
        """People a vehicle of the type carries."""
        if not OCCUPANCY_RANGE[0] <= people <= OCCUPANCY_RANGE[1]:
            raise ValueError(f"An occupancy is {OCCUPANCY_RANGE[0]:g} to {OCCUPANCY_RANGE[1]:g} people.")
        self.modes[index].occupancy = round(float(people), 2)
        self.changed.emit("occupancy", index)

    def vehicles_per_hour(self) -> dict[int, float]:
        """Timetabled types (index -> vehicles per hour)."""
        return {i: 60 / mode.headway_min for i, mode in enumerate(self.modes) if mode.headway_min}

    def state(self) -> tuple[list[VehicleMode], tuple[int, ...]]:
        """A copy for the undo history."""
        return deepcopy(self.modes), self.default_split

    def restore(self, modes: list[VehicleMode], default_split: tuple[int, ...]) -> None:
        self.modes, self.default_split = deepcopy(modes), tuple(default_split)
        self.changed.emit("reset", -1)


class NumberBox(QSpinBox):
    """A whole number that's typed: clicking in it selects what's there, so the digits replace it."""

    def focusInEvent(self, event) -> None:  # type intentionally inherited from Qt
        super().focusInEvent(event)
        QTimer.singleShot(0, self.selectAll)


class ModeBehaviorPanel(QWidget):
    """The Mode Behavior tab: rename, add and delete vehicle types."""

    def __init__(self, vehicle_modes: VehicleModes) -> None:
        super().__init__()
        self.vehicle_modes = vehicle_modes
        self.names: list[QLineEdit] = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        types = QGroupBox("Vehicle Types")
        types_layout = QVBoxLayout(types)
        self.rows_widget = QWidget()
        self.rows = QGridLayout(self.rows_widget)
        self.rows.setContentsMargins(0, 0, 0, 0)
        self.rows.setColumnStretch(1, 1)
        types_layout.addWidget(self.rows_widget)
        # The clicked type's own values (its colour or name clicked): headway, PCU and occupancy.
        self.details = QWidget()
        details = self.details_form = QFormLayout(self.details)
        details.setContentsMargins(0, 4, 0, 4)
        details.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.FieldsStayAtSizeHint)
        self.details_title = QLabel()
        self.details_title.setStyleSheet("font-weight: 600;")
        details.addRow(self.details_title)
        self.colour = QComboBox()
        for name, colour in zip(("Blue", "Orange", "Teal", "Gold", "Pink", "Green", "Lavender", "Coral",
                                 "White", "Grey", "Sand", "Charcoal"), VEHICLE_PAINTS):
            self.colour.addItem(name, colour)
        self.colour.addItem("Random", None)
        self.colour.setToolTip("Vehicle paint colour")
        self.colour.currentIndexChanged.connect(self._set_colour)
        details.addRow("Colour", self.colour)
        self.headway = NumberBox()
        self.headway.setRange(0, MAX_HEADWAY_MIN)
        self.headway.setSuffix(" min")
        self.headway.setToolTip("Headway in minutes; 0: none")
        self.headway.valueChanged.connect(self._set_headway)
        details.addRow("Headway", self.headway)
        self.pcu = QDoubleSpinBox()
        self.pcu.setRange(*PCU_RANGE)
        self.pcu.setDecimals(2)
        self.pcu.setSingleStep(0.1)
        self.pcu.setToolTip("Passenger car units (car 1)")
        self.pcu.valueChanged.connect(self._set_pcu)
        details.addRow("PCU", self.pcu)
        self.occupancy = QDoubleSpinBox()
        self.occupancy.setRange(*OCCUPANCY_RANGE)
        self.occupancy.setDecimals(2)
        self.occupancy.setSuffix(" people")
        self.occupancy.setToolTip("People per vehicle")
        self.occupancy.valueChanged.connect(self._set_occupancy)
        details.addRow("Occupancy", self.occupancy)
        self.details.hide()
        self.picked = -1  # the type the details show
        types_layout.addWidget(self.details)
        self.add_button = styled_button("Add Vehicle Type", tooltip=f"Up to {MAX_MODES} types")
        self.add_button.clicked.connect(self._add)
        types_layout.addWidget(self.add_button)
        types_layout.addWidget(hint_label(
            "The Demand Generator splits trips between these types. Renaming keeps every OD pair's "
            "shares; a new type starts at 0 %; deleting one hands its share to the others in "
            "proportion. Click a type for its colour, headway, PCU and occupancy. Only a timetabled type (chosen "
            "when it's added) has a headway: e.g. a bus every 10 min fixes that type's share in each pair at "
            "60 ÷ headway vehicles an hour; the other types share the rest. Ctrl+Z undoes any of these."))
        layout.addWidget(types)
        layout.addWidget(hint_label(
            "Random gives each vehicle a stable paint colour; demand charts keep the type colour. "
            "Simple vehicle skins follow the vehicle class. Colour and styling leave physical dimensions "
            "and driving behaviour unchanged."))
        layout.addStretch(1)
        vehicle_modes.changed.connect(self._rebuild)
        self._rebuild()

    def _rebuild(self, action: str = "", index: int = -1) -> None:
        if action == "renamed":  # the name box already shows it, unless the rename came from elsewhere
            self.names[index].setText(self.vehicle_modes.modes[index].name)
            return
        if action in ("headway", "pcu", "occupancy"):  # the details show them, the box maybe mid-edit
            if index == self.picked:
                self._pick(index)
            return
        outgoing = [self.rows.takeAt(0).widget() for _ in range(self.rows.count())]
        for gone in outgoing:  # all silent before any hides: a half-typed name isn't a rename of the types an undo
            gone.blockSignals(True)  # restored, and the focus passing through them picks no type that may be gone
            gone.removeEventFilter(self)
        for gone in outgoing:
            gone.hide()
            gone.deleteLater()  # later: a name box may be mid-signal
        self.names = []
        modes = self.vehicle_modes.modes
        for row, mode in enumerate(modes):
            swatch = QLabel()
            swatch.setFixedSize(12, 12)
            swatch.setStyleSheet(f"background: {mode.colour}; border-radius: 2px;")
            name = QLineEdit(mode.name)
            name.setMaxLength(24)
            name.setToolTip("Rename, then press Enter")
            name.editingFinished.connect(lambda row=row, name=name: self._rename(row, name))
            for clickable in (swatch, name):  # clicking the type shows its values below
                clickable.setProperty("type_row", row)
                clickable.installEventFilter(self)
            delete = styled_button("Delete…", "danger", f"Delete {mode.name}")
            delete.setEnabled(len(modes) > 1)
            delete.clicked.connect(lambda _=False, row=row: self._delete(row))
            self.rows.addWidget(swatch, row, 0)
            self.rows.addWidget(name, row, 1)
            self.rows.addWidget(delete, row, 2)
            self.names.append(name)
        self.add_button.setEnabled(len(modes) < MAX_MODES)
        if 0 <= self.picked < len(modes):
            self._pick(self.picked)
        else:
            self.picked = -1
            self.details.hide()

    def eventFilter(self, watched, event) -> bool:  # type intentionally inherited from Qt
        if event.type() in (QEvent.Type.MouseButtonPress, QEvent.Type.FocusIn) and watched.property("type_row") is not None:
            self._pick(int(watched.property("type_row")))
        return False

    def _pick(self, row: int) -> None:
        """Show this type's own values below the list."""
        mode = self.vehicle_modes.modes[row]
        self.picked = row
        self.details_title.setText(mode.name)
        self.colour.blockSignals(True)
        custom = self.colour.findText("Custom")
        if custom >= 0:
            self.colour.removeItem(custom)
        colour_index = self.colour.findData(None if mode.random_colour else mode.colour)
        if colour_index < 0:
            self.colour.addItem("Custom", mode.colour)
            colour_index = self.colour.count() - 1
        self.colour.setCurrentIndex(colour_index)
        self.colour.blockSignals(False)
        for box, value in ((self.headway, mode.headway_min), (self.pcu, mode.pcu), (self.occupancy, mode.occupancy)):
            box.blockSignals(True)
            box.setValue(value)
            box.blockSignals(False)
        self.details_form.setRowVisible(self.headway, mode.timetabled)  # a car or bike has none
        self.details.show()

    def _set_colour(self, index: int) -> None:
        if index >= 0 and 0 <= self.picked < len(self.vehicle_modes.modes):
            self.vehicle_modes.set_colour(self.picked, self.colour.itemData(index))

    def _set_headway(self, minutes: int) -> None:
        if 0 <= self.picked < len(self.vehicle_modes.modes):
            self.vehicle_modes.set_headway(self.picked, minutes)

    def _set_pcu(self, value: float) -> None:
        if 0 <= self.picked < len(self.vehicle_modes.modes):
            self.vehicle_modes.set_pcu(self.picked, value)

    def _set_occupancy(self, value: float) -> None:
        if 0 <= self.picked < len(self.vehicle_modes.modes):
            self.vehicle_modes.set_occupancy(self.picked, value)

    def _rename(self, row: int, field: QLineEdit) -> None:
        modes = self.vehicle_modes.modes
        if row >= len(modes) or field.text().strip() == modes[row].name:
            return  # unchanged, or a box on its way out after a delete
        try:
            self.vehicle_modes.rename(row, field.text())
        except ValueError as exc:
            QMessageBox.warning(self, "Rename vehicle type", str(exc))
            field.setText(modes[row].name)

    def _delete(self, row: int) -> None:
        name = self.vehicle_modes.modes[row].name
        answer = QMessageBox.question(self, "Delete vehicle type",
                                      f"Delete {name}? Its share in every OD pair goes to the other types "
                                      "in proportion. Ctrl+Z undoes it.")
        if answer == QMessageBox.StandardButton.Yes:
            self.vehicle_modes.delete(row)

    def _add(self) -> None:
        Button = QMessageBox.StandardButton
        answer = QMessageBox.question(self, "Add vehicle type",
                                      "Keep a headway for the new type? Yes: it runs to a timetable (e.g. a bus every "
                                      "10 min). No: it never has one, like a car or bike.",
                                      Button.Yes | Button.No | Button.Cancel, Button.No)
        if answer == Button.Cancel:
            return
        row = self.vehicle_modes.add(timetabled=answer == Button.Yes)
        self._pick(row)
        QTimer.singleShot(0, lambda: row < len(self.names) and (self.names[row].setFocus(),  # name it next,
                                                                 self.names[row].selectAll()))  # unless it's gone

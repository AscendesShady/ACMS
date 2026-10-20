"""Dispatch Emergency Vehicle: send emergency vehicles through the run (see src/traffic_simulator/extensions.py for
the hooks).

Each schedule is one emergency vehicle, on one row: a tick to select it; From and To, a boundary end (an approach,
e.g. 1W) or a junction (e.g. 2,1), typed or picked from the list, or drawn at random (the small Random button before
each); Every (minutes between dispatches, 0: once now) or After (once, that many minutes from now). Above the rows,
+ adds a schedule and - deletes the ticked ones; beside them, a seed and Random Emergency: the seed's emergency
vehicle (`random_emergency`: a delay of up to MAX_DELAY_MIN minutes, from one place to another it can reach, all
drawn from that seed, so the same seed gives the same one) joins the live run that long after the click. Under the
rows, Dispatch Selected sends the ticked schedules and Dispatch Fleet every one into the live run
(app.dispatch_emergency, Simulation.dispatch), after checking them all (one that can't go sends none). Batch Run,
with Test DEVL ticked, dispatches the fleet at the start of each of its runs (fleet()), and each run's own random emergency vehicle, drawn
from the run's seed (the same for every agent on that seed), within the run.
Their vehicles are the project's type called Emergency, else SUMO's emergency class, drawn red.
"""

import random

from PySide6.QtCore import QSize
from PySide6.QtWidgets import QCheckBox, QComboBox, QHBoxLayout, QSpinBox, QVBoxLayout, QWidget

from traffic_simulator.widgets import MINI_ICON_PX, hint_label, icon_button, styled_button
from traffic_simulator.theme import STATUS

NAME = "Dispatch Emergency Vehicle"
ORDER = 4

MAX_EVERY_MIN = 120
MAX_DELAY_MIN = 10  # a random emergency vehicle comes within this many minutes of its button
MAX_SEED = 999_999
app = None  # the window's services (dispatch_emergency, places), set by the Extensions tab
_lines: list = []  # each schedule's (row, tick, From box, To box, Every/After box, minutes box)
_ui: dict = {}  # the panel's add_line, while it exists (set_fleet)


class Place(QComboBox):
    """A boundary end or junction: typed, or picked from the network's as they are when the list opens."""

    def __init__(self) -> None:
        super().__init__()
        self.setEditable(True)
        self.setMinimumContentsLength(4)

    def showPopup(self) -> None:
        text = self.currentText()
        self.clear()
        self.addItems(app.places() if app is not None else [])
        self.setCurrentText(text)
        super().showPopup()


def random_emergency(seed: int, places: list[str], can_go, max_delay_s: int = MAX_DELAY_MIN * 60) -> tuple[str, str, int]:
    """The seed's emergency vehicle: (from, to, seconds from now), all drawn from Random(seed): a delay of 0 to
    `max_delay_s` (MAX_DELAY_MIN minutes), then two places (ends or junctions) until `can_go(from, to)` says it can
    go. The same seed and network give the same one. ValueError: none can go (can_go's last reason)."""
    rng, reason = random.Random(seed), "There are fewer than two ends or junctions."
    delay = rng.randint(0, int(max_delay_s))
    for _ in range(100 if len(places) > 1 else 0):
        start, end = rng.sample(places, 2)
        try:
            can_go(start, end)
            return start, end, delay
        except ValueError as exc:
            reason = str(exc)
    raise ValueError(reason)


def fleet() -> list[tuple[str, str, int, int]]:
    """Every schedule: (from, to, every minutes, after minutes): Every repeats (0: once now), After sends one once."""
    return [_schedule(line) for line in _lines]


def set_fleet(schedules: list[tuple[str, str, int, int]]) -> None:
    """Replace every schedule row with these, (from, to, every min, after min), as + would add them: the assistant's
    schedule_emergency, which checks them first (a row is Every or After, not both)."""
    for line in list(_lines):
        _lines.remove(line)
        line[0].deleteLater()
    for start, end, every, after in schedules:
        _ui["add"]()
        _, _, start_box, end_box, mode, minutes = _lines[-1]
        start_box.setCurrentText(start)
        end_box.setCurrentText(end)
        mode.setCurrentIndex(1 if after and not every else 0)
        minutes.setValue(every or after)


def _schedule(line) -> tuple[str, str, int, int]:
    _, _, start, end, mode, minutes = line
    every = mode.currentIndex() == 0
    return start.currentText().strip(), end.currentText().strip(), minutes.value() if every else 0, 0 if every else minutes.value()


def panel():
    _lines.clear()
    widget = QWidget()
    layout = QVBoxLayout(widget)
    layout.setContentsMargins(0, 0, 0, 0)
    status = hint_label()
    status.hide()  # until there's something to say

    def say(text: str, failed: bool = False) -> None:
        status.setStyleSheet(f"color: {STATUS['failed']};" if failed else "")
        status.setText(text)
        status.show()

    def send(chosen: list[tuple[str, str, int, int]]) -> None:
        """Dispatch these, after checking them all: one that can't go sends none."""
        if app is None:
            say("Dispatching works in the ACMS window.", True)
            return
        if not chosen:
            say("Tick a schedule first.", True)
            return
        try:
            for start, end, every, after in chosen:
                app.dispatch_emergency(start, end, every, check_only=True)
            for start, end, every, after in chosen:
                app.dispatch_emergency(start, end, every, after_s=after * 60)
        except ValueError as exc:
            say(str(exc), True)
            return
        say("; ".join(f"Sent {start} → {end}" + (f", every {every} min" if every else "") + (f", in {after} min" if after else "")
                      for start, end, every, after in chosen) + ".")

    def add_line() -> None:
        """One schedule, one row: a tick, From and To (each after its Random), Every or After, minutes."""
        line = QWidget()
        row = QHBoxLayout(line)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(2)
        tick = QCheckBox()
        tick.setToolTip("Select")
        row.addWidget(tick)
        start, end = Place(), Place()
        for name, box, other in (("From", start, end), ("To", end, start)):
            box.lineEdit().setPlaceholderText(name)
            box.setToolTip(f"{name}: end or junction")
            pick = icon_button(0xE8B1, "Random", "mini")  # Shuffle
            pick.setIconSize(QSize(MINI_ICON_PX, MINI_ICON_PX))

            def draw(_=False, box=box, other=other) -> None:
                choices = [place for place in (app.places() if app is not None else []) if place != other.currentText()]
                if choices:
                    box.setCurrentText(random.choice(choices))

            pick.clicked.connect(draw)
            row.addWidget(pick)
            row.addWidget(box, 1)
        mode = QComboBox()
        mode.addItems(["Every", "After"])
        mode.setToolTip("Frequency or dispatch after")
        minutes = QSpinBox()
        minutes.setRange(0, MAX_EVERY_MIN)
        minutes.setSuffix(" min")
        minutes.setToolTip("Minutes; Every 0: once")
        row.addWidget(mode)
        row.addWidget(minutes)
        lines.addWidget(line)
        _lines.append((line, tick, start, end, mode, minutes))

    _ui["add"] = add_line

    def delete_lines() -> None:
        """Remove the ticked schedules."""
        ticked = [line for line in _lines if line[1].isChecked()]
        if not ticked:
            say("Tick a schedule first.", True)
        for line in ticked:
            _lines.remove(line)
            line[0].deleteLater()

    seed = QSpinBox()
    seed.setRange(0, MAX_SEED)
    seed.setValue(1)
    seed.setToolTip("Random emergency seed")
    surprise = icon_button(0xE945, "Random Emergency")  # LightningBolt

    def send_random() -> None:
        """The seed's emergency vehicle into the live run, at its random time from now."""
        if app is None:
            say("Dispatching works in the ACMS window.", True)
            return
        try:
            start, end, delay = random_emergency(seed.value(), app.places(),
                                                 lambda a, b: app.dispatch_emergency(a, b, 0, check_only=True))
            app.dispatch_emergency(start, end, 0, after_s=delay)
        except ValueError as exc:
            say(str(exc), True)
            return
        say(f"Seed {seed.value()}: an emergency vehicle from {start} to {end} in {delay // 60}:{delay % 60:02d}.")

    surprise.clicked.connect(send_random)
    add = icon_button(0xE710, "Add Schedule")  # Add
    add.clicked.connect(add_line)
    delete = icon_button(0xE738, "Delete Schedule")  # Remove
    delete.clicked.connect(delete_lines)
    top = QHBoxLayout()
    for button in (add, delete):
        top.addWidget(button)
    top.addStretch(1)
    top.addWidget(seed)
    top.addWidget(surprise)
    layout.addLayout(top)
    lines = QVBoxLayout()
    layout.addLayout(lines)
    dispatch_selected = styled_button("Dispatch Selected", "", "Dispatch ticked schedules")
    dispatch_selected.clicked.connect(lambda: send([_schedule(line) for line in _lines if line[1].isChecked()]))
    dispatch_fleet = styled_button("Dispatch Fleet", "primary", "Dispatch every vehicle")
    dispatch_fleet.clicked.connect(lambda: send(fleet()))
    buttons = QHBoxLayout()
    buttons.addWidget(dispatch_selected, 1)
    buttons.addWidget(dispatch_fleet, 1)
    layout.addLayout(buttons)
    layout.addWidget(status)
    add_line()
    return widget

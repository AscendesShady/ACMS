"""Extensions tab: features anyone can add from a source file, without editing the app.

An extension is one Python file in the project's extensions/ folder (names starting with _
are skipped). It needs at least one of these, and may name and order itself:

    NAME = "Bus Lane Signals"          # optional: the file name otherwise
    DESCRIPTION = "What it adds."      # optional
    ORDER = 1                          # optional: its place in the tab (then by name)

    def draw(network, t):
        return [...]                   # Qt graphics items, in metres like the map

    def panel():
        return QWidget(...)            # its settings: a collapsible section under its checkbox

    def signal_override(junction, arm, state, context, agent):
        return state                   # inside every signal, every step (see below)

    def algorithm(frame):
        ...                            # runs with the latest frame of a run (simulation.Frame)

    def report(agent):                 # optional, with one of those: sheets for the run's workbook
        return {"sheets": [(name, rows, what)], "measures": {...}}

    def log(event):
        ...                            # each AI Mode step, recorded (see below)

    app = None                         # optional: the tab sets it to the window's services (run_batch)

Nothing runs until its box is ticked, and unticking it makes it neutral again.
- draw() runs after every map redraw and up to 10 times a second while the clock runs; `t` is the
  simulation time. Its items are removed before each draw. `network` is a copy made for
  extensions, so changing it can't change the real network.
- panel(): the ▸ arrow beside its checkbox shows or hides it; it can be used while ticked.
- signal_override() runs in the simulation's physics thread for each arm of each signalised
  junction, every step (0.5 s), and returns the state that arm shows: "G", "Y" or "R". `state` is
  the plan's; `context` holds the time, the plan and the vehicles on the arm; `agent.decide(key,
  question)` gives an LLM's TRUE/FALSE answer without waiting (signal_agent.py). With no ticked
  extension defining it the simulation calls nothing: the signals run their plans untouched.
- algorithm(frame) runs up to 10 times a second during a run, with the run's latest frame.
- report(agent), for an extension whose signal_override a run used, when the run ends: sheets for its
  workbook and figures a batch compares; `agent` is the signal agent that run's overrides asked.
- log(event) runs on the UI thread once each AI Mode answer ends (a step): `event` is a plain dict of the
  prompt, what was understood (the model's Plan: line, the tools it was offered), every tool call with its arguments, plan, the user's
  decision, result and timings, the model's messages, tokens, the changes the step made (network, demand,
  vehicle types) and `map_png`, the map as it was then (PNG bytes). The window builds it only while an
  extension with log() is ticked (MainWindow._research_step).
A file that fails to load, or any of these that raises, switches that extension off with the
error and its line shown here; the editor and the run carry on.
"""

from __future__ import annotations

import importlib.util
import time
import traceback
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QGraphicsItem, QGroupBox, QHBoxLayout, QLabel, QToolButton, QVBoxLayout,
                               QWidget)
from shiboken6 import isValid

from .clock import SimulationClock
from .network_scene import NetworkGraphicsScene
from .properties_panel import hint_label
from .theme import STATUS

EXTENSIONS_DIR = Path(__file__).resolve().parents[2] / "extensions"
HOOKS = ("draw", "panel", "signal_override", "algorithm", "log")  # report() alone isn't enough
ARROW_PX = 16  # the collapse arrow beside an extension with settings


@dataclass
class Extension:
    path: Path
    module: ModuleType | None = None  # None: it failed to load
    error: str = ""
    on: bool = False
    items: list = field(default_factory=list)  # what it drew last, removed before the next draw
    check: QCheckBox | None = None
    status: QLabel | None = None
    controls: QWidget | None = None  # from its panel(): its settings
    arrow: QToolButton | None = None  # shows or hides them

    @property
    def name(self) -> str:
        return str(getattr(self.module, "NAME", "") or self.path.stem.replace("_", " ").title())

    def has(self, hook: str) -> bool:
        return callable(getattr(self.module, hook, None))


def describe_error(exc: BaseException, path: Path) -> str:
    """The error, with the line in the extension's own file where it happened."""
    line = exc.lineno if isinstance(exc, SyntaxError) else None
    for frame in traceback.extract_tb(exc.__traceback__):
        if frame.filename == str(path):
            line = frame.lineno
    message = traceback.format_exception_only(type(exc), exc)[-1].strip()
    return f"Line {line}: {message}" if line else message


def load(path: Path) -> Extension:
    extension = Extension(path)
    try:
        spec = importlib.util.spec_from_file_location(f"extensions.{path.stem}", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        if not any(callable(getattr(module, hook, None)) for hook in HOOKS):
            raise TypeError("an extension needs a draw(), panel(), signal_override() or algorithm() function")
        extension.module = module
    except (Exception, SystemExit) as exc:  # SystemExit: an exit() in the file mustn't close the app
        extension.error = describe_error(exc, path)
    return extension


class SignalOverrides:
    """The ticked extensions' signal_override()s, called in turn from the physics thread for each
    signalised arm each step, each with the state the one before it left. One returns the arm's state, "G",
    "Y" or "R", or (that, {lane index: the lane's own state}) to set lanes apart (DEVL's kerbside lane); a
    later one's lanes win. One that raises, or returns anything else, is dropped at once (the plan stands) and
    listed in `failed` for the tab to switch off and report. `agent` is the signal agent they ask, or a dict of
    one per extension name (a batch run's: TSP's and DEVL's models differ)."""

    def __init__(self, extensions: list[Extension], agent) -> None:
        self.extensions = list(extensions)  # every one the run uses, for their reports
        self.calls = [(extension, extension.module.signal_override) for extension in extensions]
        self.agent = agent
        self.failed: list[tuple[Extension, str]] = []

    def agent_of(self, extension: Extension):
        """The signal agent this extension asks."""
        return self.agent.get(extension.name) if isinstance(self.agent, dict) else self.agent

    def __call__(self, junction: str, arm: str, state: str, context: dict):
        lanes: dict = {}
        for entry in list(self.calls):
            extension, override = entry
            try:
                result = override(junction, arm, state, context, self.agent_of(extension))
                if isinstance(result, tuple) and len(result) == 2 and isinstance(result[1], dict):
                    result, own = result
                    if not all(isinstance(index, int) and lane in ("G", "Y", "R") for index, lane in own.items()):
                        raise ValueError(f"signal_override returned lanes {own!r}, not {{lane index: G, Y or R}}")
                    lanes.update(own)
                if result not in ("G", "Y", "R"):
                    raise ValueError(f'signal_override returned {result!r}, not "G", "Y" or "R"')
                state = result
            except (Exception, SystemExit) as exc:
                self.calls.remove(entry)
                self.failed.append((extension, describe_error(exc, extension.path)))
        return (state, lanes) if lanes else state


class ExtensionsPanel(QWidget):
    """The Extensions tab: switch each extension file on or off, reload after editing."""

    hooks_changed = Signal()  # an extension with a signal_override() was switched on or off

    def __init__(self, scene: NetworkGraphicsScene, clock: SimulationClock, folder: Path = EXTENSIONS_DIR) -> None:
        super().__init__()
        self.scene, self.clock, self.folder = scene, clock, folder
        self.extensions: list[Extension] = []
        self.agent = None  # the LLM agent signal overrides ask (set by the main window)
        self._app = None  # the window's services for extensions that ask (`app` in their file): run_batch
        self.latest_frame = lambda: None  # the run's latest frame, for algorithm() (set by the main window)
        self._network = None  # the copy extensions draw from; None: copy again
        # ponytail: extensions redraw all their items, up to 10 times a second while the clock runs
        # (not every frame); if big grids stutter, let them keep static items and redraw only what animates.
        self._drawn_at = 0.0
        clock.ticked.connect(self._tick)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        box = QGroupBox("Installed Extensions")
        box_layout = QVBoxLayout(box)
        self.rows = QVBoxLayout()
        box_layout.addLayout(self.rows)
        # Each extension is a tick (and its settings, if it has any); they are read from the folder at start.
        box.setToolTip(f"Loaded from {folder}")
        layout.addWidget(box)
        layout.addStretch(1)
        scene.redrawn.connect(self._map_redrawn)
        self.reload()

    @property
    def app(self):
        return self._app

    @app.setter
    def app(self, services) -> None:
        self._app = services
        for extension in self.extensions:
            if extension.module is not None and hasattr(extension.module, "app"):
                extension.module.app = services

    def settings(self) -> list[tuple[str, str]]:
        """Each extension, whether it is ticked, and (ticked) its settings as its panel shows them:
        "Seeds: 1-10; Duration: 30 min". For a run's record (the results workbook)."""
        from PySide6.QtWidgets import QAbstractSpinBox, QCheckBox as Box, QComboBox, QFormLayout, QLineEdit  # noqa: PLC0415

        from .properties_panel import ChoiceButtons  # noqa: PLC0415

        def shown_value(w):
            if hasattr(w, "summary"):  # a dropdown of ticks: what is ticked
                return w.summary()
            if isinstance(w, (QLineEdit, QAbstractSpinBox, QLabel)):  # a label: a slider's value beside it
                return w.text()
            if isinstance(w, QComboBox):
                return w.currentText()
            if isinstance(w, Box):
                return "yes" if w.isChecked() else "no"
            if isinstance(w, ChoiceButtons):
                return w.group.checkedButton().text()
            return None

        rows = []
        for extension in self.extensions:
            shown = []
            if extension.on and extension.controls is not None:
                for form in extension.controls.findChildren(QFormLayout) or []:
                    for row in range(form.rowCount()):
                        label = form.itemAt(row, QFormLayout.ItemRole.LabelRole)
                        field = form.itemAt(row, QFormLayout.ItemRole.FieldRole)
                        widgets = [field.widget()] if field and field.widget() else \
                            [field.layout().itemAt(i).widget() for i in range(field.layout().count())] if field and field.layout() else []
                        values = [shown_value(w) for w in widgets if w is not None and w.isVisibleTo(extension.controls)]
                        values = [value for value in values if value]
                        if values and label and label.widget():
                            shown.append(f"{label.widget().text()}: {' '.join(values)}")
            rows.append((extension.name, ("ticked" if extension.on else "not ticked") + (f" ({'; '.join(shown)})" if shown else "")))
        return rows

    def reload(self) -> None:
        """Read every extension file again; those that were on stay on if they still load."""
        was_on = {extension.path.name for extension in self.extensions if extension.on}
        for extension in self.extensions:
            self._clear(extension)
        paths = sorted(self.folder.glob("*.py")) if self.folder.is_dir() else []
        self.extensions = sorted((load(path) for path in paths if not path.name.startswith("_")),
                                 key=lambda extension: (getattr(extension.module, "ORDER", 100), extension.path.name))
        while self.rows.count():
            gone = self.rows.takeAt(0).widget()
            gone.hide()
            gone.deleteLater()
        for extension in self.extensions:
            self.rows.addWidget(self._row(extension))
        if not self.extensions:
            self.rows.addWidget(hint_label(f"No extension files in {self.folder}."))
        for extension in self.extensions:
            if extension.path.name in was_on and extension.module:
                extension.check.setChecked(True)
        self.app = self._app  # the reloaded files get the window's services again
        self.hooks_changed.emit()  # the reloaded files replace the old overrides

    def _row(self, extension: Extension) -> QWidget:
        row = QWidget()
        layout = QVBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 6)
        header = QHBoxLayout()
        header.setSpacing(2)
        extension.check = QCheckBox(extension.name)
        extension.check.setToolTip(extension.path.name)
        extension.check.setEnabled(extension.module is not None)
        extension.check.toggled.connect(lambda on, extension=extension: self._switch(extension, on))
        if extension.has("panel"):
            try:
                extension.controls = extension.module.panel()
                extension.controls.hide()
                extension.controls.setEnabled(False)  # usable while ticked
            except (Exception, SystemExit) as exc:
                extension.error = describe_error(exc, extension.path)
                extension.module, extension.controls = None, None
                extension.check.setEnabled(False)
        if extension.controls is not None:  # collapsible: the arrow shows or hides its settings
            extension.arrow = QToolButton()
            extension.arrow.setArrowType(Qt.ArrowType.RightArrow)
            extension.arrow.setCheckable(True)
            extension.arrow.setFixedSize(ARROW_PX, ARROW_PX)
            extension.arrow.setStyleSheet("QToolButton { border: none; background: transparent; }")  # flat, open or shut
            extension.arrow.setToolTip("Show or hide settings")
            extension.arrow.toggled.connect(lambda shown, extension=extension: self._expand(extension, shown))
            header.addWidget(extension.arrow)
        else:
            header.addSpacing(ARROW_PX + header.spacing())  # checkboxes line up, arrow or not
        header.addWidget(extension.check)
        header.addStretch(1)
        layout.addLayout(header)
        description = getattr(extension.module, "DESCRIPTION", "")
        if description:
            layout.addWidget(hint_label(str(description)))
        if extension.controls is not None:
            layout.addWidget(extension.controls)
            row.setMinimumWidth(extension.controls.minimumSizeHint().width())  # folded, still as wide as open: no clipping
        extension.status = QLabel(extension.error)
        extension.status.setWordWrap(True)
        extension.status.setStyleSheet(f"color: {STATUS['failed']};")
        extension.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        extension.status.setVisible(bool(extension.error))
        layout.addWidget(extension.status)
        return row

    @staticmethod
    def _expand(extension: Extension, shown: bool) -> None:
        extension.arrow.setArrowType(Qt.ArrowType.DownArrow if shown else Qt.ArrowType.RightArrow)
        extension.controls.setVisible(shown)

    def _switch(self, extension: Extension, on: bool) -> None:
        extension.on = on
        self._clear(extension)
        if extension.controls is not None:
            extension.controls.setEnabled(on)
            if on and not extension.arrow.isChecked():
                extension.arrow.setChecked(True)  # ticked: its settings open
        if on:
            extension.status.hide()
            self._draw(extension)
        if extension.has("signal_override"):
            self.hooks_changed.emit()

    def ticked(self) -> list[str]:
        """The names of the ticked extensions."""
        return [extension.name for extension in self.extensions if extension.on]

    def signal_override(self, agent=None) -> SignalOverrides | None:
        """The ticked extensions' signal overrides, for a run; None when none is ticked (neutral). `agent`:
        the signal agent they ask (a headless run's waits for answers); the window's live one by default."""
        ticked = [extension for extension in self.extensions if extension.on and extension.has("signal_override")]
        return SignalOverrides(ticked, agent or self.agent) if ticked else None

    def find(self, name: str) -> Extension:
        """An extension by its name (or file name), case aside; ValueError listing the names."""
        wanted = str(name).strip().lower()
        for extension in self.extensions:
            short = extension.name[extension.name.find("(") + 1:-1].lower() if extension.name.endswith(")") else ""
            if wanted in (extension.name.lower(), extension.path.stem.lower(), short) or \
                    extension.name.lower().startswith(wanted) and len(wanted) >= 3:  # "TSP", "transit signal"
                if extension.module is None:
                    raise ValueError(f"{extension.name} didn't load: {extension.error}")
                return extension
        raise ValueError(f"No extension called {name!r}. There are: {', '.join(e.name for e in self.extensions)}.")

    def overrides_for(self, names: list[str], agent=None) -> SignalOverrides | None:
        """The signal overrides of these extensions, ticked or not (an experiment's scenario); None for none.
        `agent`: the one signal agent they ask, or one per extension name (a dict)."""
        chosen = [self.find(name) for name in names]
        with_override = [extension for extension in chosen if extension.has("signal_override")]
        return SignalOverrides(with_override, agent or self.agent) if with_override else None

    @staticmethod
    def reports(overrides: SignalOverrides | None) -> list[dict]:
        """When a run ends: the report() of each extension its overrides came from (sheets for its workbook,
        figures to compare), asked with that run's agent. One that raises is left out, saying why."""
        found = []
        for extension in (overrides.extensions if overrides is not None else []):
            if extension.has("report"):
                try:
                    found.append(dict(extension.module.report(overrides.agent_of(extension))))
                except (Exception, SystemExit) as exc:
                    found.append({"sheets": [(f"{extension.name[:20]} report", [["Couldn't report"],
                                              [describe_error(exc, extension.path)]], "Its report() raised.")]})
        return found

    @staticmethod
    def fields(extension: Extension) -> dict:
        """An extension's settings by label: its panel's labelled form rows ({"Seeds": the box, ...}), a dropdown
        that lists afresh as it opens (TSP's Model, Models) listed now."""
        from PySide6.QtWidgets import QFormLayout  # noqa: PLC0415

        fields = {}
        for form in (extension.controls.findChildren(QFormLayout) if extension.controls else []):
            for row in range(form.rowCount()):
                label, field = form.itemAt(row, QFormLayout.ItemRole.LabelRole), form.itemAt(row, QFormLayout.ItemRole.FieldRole)
                if label and label.widget() and field:
                    widget = field.widget() or (field.layout().itemAt(0).widget() if field.layout() else None)
                    if callable(getattr(widget, "before_open", None)):
                        widget.before_open()
                    fields[label.widget().text().strip()] = widget
        return fields

    def describe(self) -> list[dict]:
        """Every extension for the assistant's read_extensions: its name, whether it is ticked, what it does (its
        docstring's first paragraph) and each setting by label with its value and, for a choice, what it can be."""
        from PySide6.QtWidgets import QAbstractSlider, QAbstractSpinBox, QComboBox, QLineEdit  # noqa: PLC0415

        from .properties_panel import ChoiceButtons  # noqa: PLC0415

        found = []
        for extension in self.extensions:
            settings = {}
            for label, widget in self.fields(extension).items():
                if hasattr(widget, "set_ticked"):  # a dropdown of ticks
                    settings[label] = {"ticked": widget.ticked(), "can_be": widget.labels()}
                elif isinstance(widget, QCheckBox):
                    settings[label] = {"value": "yes" if widget.isChecked() else "no"}
                elif isinstance(widget, ChoiceButtons):
                    settings[label] = {"value": widget.group.checkedButton().text(),
                                       "can_be": [button.text() for button in widget.group.buttons()]}
                elif isinstance(widget, QComboBox):
                    settings[label] = {"value": widget.currentText(),
                                       "can_be": [widget.itemText(i) for i in range(widget.count())]}
                elif isinstance(widget, (QAbstractSpinBox, QAbstractSlider)):
                    settings[label] = {"value": widget.value(), "from": widget.minimum(), "to": widget.maximum()}
                elif isinstance(widget, QLineEdit):
                    settings[label] = {"value": widget.text()}
            module = extension.module
            found.append({"name": extension.name, "ticked": extension.on, "loaded": module is not None,
                          "about": (getattr(module, "DESCRIPTION", "") or (module.__doc__ or "").split("\n\n")[0]
                                    if module else extension.error), "settings": settings})
        return found

    def set_settings(self, extension: Extension, settings: dict, check_only: bool = False) -> None:
        """Fill fields of an extension's panel by their labels (text boxes, dropdowns, number boxes)."""
        from PySide6.QtWidgets import QAbstractSlider, QAbstractSpinBox, QComboBox, QFormLayout, QLineEdit  # noqa: PLC0415

        fields = {label.lower(): widget for label, widget in self.fields(extension).items()}
        from .properties_panel import ChoiceButtons  # noqa: PLC0415

        for key, value in (settings or {}).items():
            widget = fields.get(str(key).strip().lower())
            if widget is None:
                raise ValueError(f"{extension.name} has no setting {key!r}; it has: {', '.join(fields) or 'none'}.")
            if hasattr(widget, "set_ticked"):  # a dropdown of ticks: by the labels to tick
                wanted = value if isinstance(value, list) else [part for part in str(value).split(",") if part.strip()]
                widget.set_ticked([str(label).strip() for label in wanted], check_only)  # ValueError for unknown labels
                continue
            if isinstance(widget, QCheckBox):  # a tick box: yes or no
                if not check_only:
                    widget.setChecked(str(value).strip().lower() in ("yes", "true", "1", "on"))
                continue
            if isinstance(widget, ChoiceButtons):  # radio buttons: by the label of one
                labels = [button.text() for button in widget.group.buttons()]
                wanted = next((i for i, label in enumerate(labels) if label.lower() == str(value).strip().lower()), None)
                if wanted is None:
                    raise ValueError(f"{extension.name}: {key} can be {', '.join(labels)}.")
                if not check_only:
                    widget.group.button(wanted).click()  # as a person would: its changed signal follows
                continue
            if isinstance(widget, QComboBox) and widget.findText(str(value)) < 0:
                raise ValueError(f"{extension.name}: {key} can be {', '.join(widget.itemText(i) for i in range(widget.count()))}.")
            if isinstance(widget, (QAbstractSpinBox, QAbstractSlider)):  # read and range-checked now, not clamped or failing later
                try:
                    number = type(widget.value())(float(value))
                except (TypeError, ValueError):
                    raise ValueError(f"{extension.name}: {key} must be a number, not {value!r}.") from None
                if not widget.minimum() <= number <= widget.maximum():
                    raise ValueError(f"{extension.name}: {key} must be {widget.minimum():g} to {widget.maximum():g}.")
            if check_only:
                continue
            if isinstance(widget, QLineEdit):
                widget.setText(str(value))
            elif isinstance(widget, QComboBox):
                widget.setCurrentText(str(value))
            elif isinstance(widget, (QAbstractSpinBox, QAbstractSlider)):
                widget.setValue(type(widget.value())(float(value)))

    def fail(self, extension: Extension, message: str) -> None:
        """Switch an extension off and say why (an error while the run used it)."""
        extension.error = "Switched off: " + message
        extension.status.setText(extension.error)
        extension.status.show()
        extension.check.setChecked(False)

    def _map_redrawn(self) -> None:
        self._network = None  # the network may have changed
        self.draw()  # the redraw cleared the extensions' items

    def _tick(self, _seconds: float) -> None:
        now = time.monotonic()
        if self.clock.running and now - self._drawn_at < 0.1:
            return
        self._drawn_at = now
        self.draw()
        frame = self.latest_frame()
        if frame is not None:
            self.run_algorithms(frame)

    def run_algorithms(self, frame) -> None:
        """Each ticked extension's algorithm(), with the run's latest frame."""
        for extension in self.extensions:
            if extension.on and extension.has("algorithm"):
                try:
                    extension.module.algorithm(frame)
                except (Exception, SystemExit) as exc:
                    self.fail(extension, describe_error(exc, extension.path))

    def wants(self, hook: str) -> bool:
        """Whether a ticked extension defines `hook` (the window builds a log event only then)."""
        return any(extension.on and extension.has(hook) for extension in self.extensions)

    def log(self, event: dict) -> None:
        """Each ticked extension's log(), with one AI Mode step (a failing one is switched off, as any hook)."""
        for extension in self.extensions:
            if extension.on and extension.has("log"):
                try:
                    extension.module.log(event)
                except (Exception, SystemExit) as exc:
                    self.fail(extension, describe_error(exc, extension.path))

    def draw(self, *_) -> None:
        for extension in self.extensions:
            if extension.on:
                self._draw(extension)

    def _draw(self, extension: Extension) -> None:
        if not extension.has("draw"):
            return
        if self._network is None:
            if self.scene.network is None:
                return
            # ponytail: a copy per map redraw, so per mouse move while dragging with an
            # extension on; copy once at the drag's end if huge grids make dragging lag.
            self._network = deepcopy(self.scene.network)
        self._clear(extension)
        try:
            extension.items = list(extension.module.draw(self._network, self.clock.time_s) or [])
            for item in extension.items:
                self.scene.addItem(item)
        except (Exception, SystemExit) as exc:
            self.fail(extension, describe_error(exc, extension.path))  # clears whatever it managed to add

    def _clear(self, extension: Extension) -> None:
        for item in extension.items:
            # A map redraw has already deleted them; anything that isn't an item never got added.
            if isinstance(item, QGraphicsItem) and isValid(item) and item.scene() is not None:
                self.scene.removeItem(item)
        extension.items = []

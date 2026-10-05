"""Who decides an extension's TRUE/FALSE questions (TSP, DEVL): form rows shared by their panels (Qt).

Decided by: System 1, System 2 or Multiple. With System 1 or 2, **Model** picks which of that system's models
answers (AI Mode's agent_options: System 1's Jev or Laya; System 2's Ollama models, Grok and Gemini), live runs and
a batch's alike; none listed: AI Mode's own choice. Multiple is for Batch Run: **Models** (a dropdown of ticks), each
tested against the baseline (No LLM), and the batch's **Decide within** (each answer's deadline, simulated s), **Pace**
(simulated seconds a real second), **Runs at once** and **Times each** (shown under Multiple, used by any batch). A
live run asks the System chosen last. `batch_settings()` gives Batch Run what to test.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QComboBox, QFormLayout, QHBoxLayout, QLabel, QSlider, QSpinBox

from .properties_panel import CheckList, ChoiceButtons

SYSTEMS = (("System 1", "1"), ("System 2", "2"), ("Multiple", "multiple"))
DEADLINE_S = (1, 280, 30)  # Decide within: least, most, at first (simulated s)
PACES = (1, 2, 5, 10)  # simulated seconds a real second; an answer's latency costs pace x its seconds
AT_ONCE = (1, 8, 4)  # Runs at once: least, most, at first
MAX_TIMES = 100


class ModelBox(QComboBox):
    """A system's models, listed afresh as it opens (`before_open`)."""

    def __init__(self, before_open) -> None:
        super().__init__()
        self.before_open = before_open

    def showPopup(self) -> None:
        self.before_open()
        super().showPopup()


class AgentChoice:
    """The rows, added to `form`. `options()` lists AI Mode's agent_options (each a dict: label, system, provider,
    model, name); `changed(system, model)` follows a change of system ("1" or "2") or model (an option, or None)."""

    def __init__(self, form: QFormLayout, options, system: str = "1", changed=None) -> None:
        self.options, self.changed, self.last_system = options, changed, system
        self._found: dict = {}  # label -> option, as last listed
        self.choice = ChoiceButtons(list(SYSTEMS))
        self.choice.set_value(system)
        self.model = ModelBox(self.refresh)
        self.model.setToolTip("Model deciding")
        self.agents = CheckList(before_open=self.refresh)
        self.agents.setToolTip("Models to test")
        self.deadline = QSlider(Qt.Orientation.Horizontal)
        self.deadline.setRange(*DEADLINE_S[:2])
        self.deadline.setValue(DEADLINE_S[2])
        self.deadline.setToolTip("Decision time limit")
        seconds = QLabel(f"{self.deadline.value()} s")
        seconds.setMinimumWidth(seconds.fontMetrics().horizontalAdvance("280 s"))
        self.deadline.valueChanged.connect(lambda value: seconds.setText(f"{value} s"))
        within = QHBoxLayout()
        within.addWidget(self.deadline, 1)
        within.addWidget(seconds)
        self.pace = ChoiceButtons([(f"{factor}×", factor) for factor in PACES])
        self.pace.setToolTip("Simulated seconds per second")
        self.at_once = QSpinBox()
        self.at_once.setRange(*AT_ONCE[:2])
        self.at_once.setValue(AT_ONCE[2])
        self.at_once.setToolTip("Runs going side by side")
        self.times = QSpinBox()
        self.times.setRange(1, MAX_TIMES)
        self.times.setToolTip("Runs per seed and model")
        self.form = form
        form.addRow("Decided by", self.choice)
        form.addRow("Model", self.model)
        self._model_row = form.rowCount() - 1
        self._rows = []
        for label, field in (("Models", self.agents), ("Decide within", within), ("Pace", self.pace),
                             ("Runs at once", self.at_once), ("Times each", self.times)):
            form.addRow(label, field)
            self._rows.append(form.rowCount() - 1)
        self.choice.changed.connect(self._show)
        self.model.currentIndexChanged.connect(lambda: self._tell())
        self._show()

    def refresh(self) -> None:
        """List AI Mode's models now (the baseline always runs in a batch, so isn't listed), keeping the picks."""
        options = [option for option in (self.options() or []) if option.get("system")]
        self._found = {option["label"]: option for option in options}
        self.agents.set_options(list(self._found), set(self.agents.ticked()))
        picked = self.model.currentData()
        self.model.blockSignals(True)
        self.model.clear()
        for option in options:
            if option["system"] == self.last_system:
                self.model.addItem(option["label"].split(": ", 1)[-1], option["label"])
        self.model.setCurrentIndex(max(0, self.model.findData(picked)))
        self.model.blockSignals(False)
        if self.model.currentData() != picked:
            self._tell()

    def system(self) -> str:
        """Who a live run asks: "1" or "2" (the System chosen last)."""
        return self.last_system

    def model_option(self) -> dict | None:
        """The model picked for System 1 or 2 (an agent_options entry), or None: AI Mode's own."""
        return self._found.get(self.model.currentData())

    def batch_settings(self) -> dict:
        """What Batch Run tests: deadline_s, speed, at_once, times, and agents (labels) with their servers (label ->
        model server); or, with System 1 or 2 and no model listed to pick, {"system": "1" or "2"} instead of agents
        (the window finds AI Mode's)."""
        settings = {"deadline_s": self.deadline.value(), "speed": self.pace.value(), "at_once": self.at_once.value(),
                    "times": self.times.value()}
        if self.choice.value() == "multiple":
            labels = self.agents.ticked()
        elif self.model_option() is not None:
            labels = [self.model_option()["label"]]
        else:
            return {**settings, "system": self.last_system}
        return {**settings, "agents": labels,
                "servers": {label: (self._found.get(label) or {}).get("provider", label) for label in labels}}

    def _show(self) -> None:
        multiple = self.choice.value() == "multiple"
        if not multiple:
            self.last_system = self.choice.value()
        self.refresh()
        self.form.setRowVisible(self._model_row, not multiple)
        for row in self._rows:
            self.form.setRowVisible(row, multiple)
        self._tell()

    def _tell(self) -> None:
        if self.changed is not None:
            self.changed(self.last_system, self.model_option())

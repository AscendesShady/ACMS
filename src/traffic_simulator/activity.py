"""Watch my edits (the eye in the menu bar): everything the user does in the window, logged as it happens.

While it is on, every button click (and whether a tick box is now on), menu choice and shortcut (the window's
actions), drop-down choice, slider position, text or number entered (its final value, when the field loses focus
or Enter is pressed, not every key), tab opened, map click (in metres) and map selection, and each undo step's
changes (the network, demand and vehicle types: MainWindow._state_changes) goes to `activity.csv` in a dated folder,
`ACMS activity <date time>`, in the results folder, a line at a time (a crash loses nothing), with the time, the seconds since it began, the
kind, where (the open tab, or a dialog's title), what (the control's name: its text, form label, placeholder or
tooltip) and the value. A click or a choice also logs everything it changed besides itself, from the window's
values noted when it was pressed (`_state`, `_changes`): a field or list the app filled ("set": a Random button's
pick), a box it ticked, a message it showed ("shown") and the status bar's ("message"). Lines also wait in `pending` for the Research Log's next step (`take`). Password fields log
"(hidden)", and a text field the app itself changed (the question box cleared after Ask) logs nothing. A logger
only: it never asks the assistant anything.
"""

from __future__ import annotations

import csv
import time
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, Qt, QTimer
from PySide6.QtWidgets import (QAbstractButton, QAbstractSlider, QAbstractSpinBox, QApplication, QComboBox,
                               QFormLayout, QGraphicsView, QLabel, QLineEdit, QMainWindow, QMenu, QPlainTextEdit,
                               QTextEdit, QToolButton, QWidget)

COLUMNS = ["Time", "Seconds", "Kind", "Where", "What", "Value"]
SAME_WITHIN_S = 0.4  # one gesture reaching the log two ways (a button and its action) is logged once
MAX_TEXT = 500
ENTRIES = (QLineEdit, QAbstractSpinBox, QTextEdit, QPlainTextEdit)


def name_of(widget: QWidget) -> str:
    """What the user would call a control: its text, its form label, placeholder, tooltip or accessible name."""
    if isinstance(widget, QAbstractButton) and widget.text().strip():
        return widget.text().replace("&", "").strip()
    parent = widget.parentWidget()
    layout = parent.layout() if parent is not None else None
    if isinstance(layout, QFormLayout):
        label = layout.labelForField(widget)
        if label is not None and hasattr(label, "text") and label.text().strip():
            return label.text().replace("&", "").strip()
    for text in (getattr(widget, "placeholderText", lambda: "")(), widget.toolTip(), widget.accessibleName(),
                 widget.objectName()):
        if text and text.strip():
            return text.strip()
    return type(widget).__name__


def value_of(widget: QWidget) -> str:
    """A control's value as text (a password field's hidden)."""
    if isinstance(widget, QLineEdit):
        return "(hidden)" if widget.echoMode() != QLineEdit.EchoMode.Normal else widget.text()
    if isinstance(widget, QAbstractSpinBox):
        return widget.text()
    if isinstance(widget, (QTextEdit, QPlainTextEdit)):
        return widget.toPlainText()[:MAX_TEXT]
    if isinstance(widget, QComboBox):
        return widget.currentText()
    if isinstance(widget, QAbstractSlider):
        return str(widget.value())
    if isinstance(widget, QAbstractButton) and widget.isCheckable():
        return "on" if widget.isChecked() else "off"
    return ""


class ActivityLog(QObject):
    """The window's activity log: `start(folder)`, `stop()`, `log(...)`; `where()` names the open tab."""

    def __init__(self, where=lambda: "", parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.where = where
        self.path: Path | None = None
        self.pending: list[list] = []  # lines not yet handed to a Research Log step
        self._file = None
        self._writer = None
        self._began = 0.0
        self._last: tuple = ((), 0.0)
        self._at_focus: dict = {}  # an entry field -> its value when it got focus
        self._actions: set = set()
        self._before: dict = {}  # the window's values when a button or list was pressed (_state)

    @property
    def on(self) -> bool:
        return self._file is not None

    def start(self, folder: Path, actions=()) -> Path:
        """Start logging to a new file in `folder`; `actions`: the window's QActions (menus, shortcuts)."""
        if self.on:
            return self.path
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        session = folder / f"ACMS activity {datetime.now():%Y-%m-%d %H-%M-%S}"  # a dated folder, as the Research
        count = 1                                                                # Log's and a batch's are
        while session.exists():
            count += 1
            session = session.with_name(f"{session.name.split(' (')[0]} ({count})")
        session.mkdir(parents=True)
        self.path = session / "activity.csv"
        self._file = open(self.path, "w", newline="", encoding="utf-8-sig")
        self._writer = csv.writer(self._file)
        self._writer.writerow(COLUMNS)
        self._began = time.monotonic()
        for action in actions:
            if id(action) not in self._actions and action.text():
                self._actions.add(id(action))
                action.triggered.connect(lambda checked=False, action=action: self.on and self.log(
                    "action", action.text().replace("&", ""), ("on" if action.isChecked() else "off")
                    if action.isCheckable() else ""))
        app = QApplication.instance()
        app.installEventFilter(self)
        app.focusChanged.connect(self._focus_changed)
        self.log("watch", "Watch my edits", "on")
        return self.path

    def stop(self) -> Path | None:
        """Stop and close the file; its path."""
        if not self.on:
            return None
        self.log("watch", "Watch my edits", "off")
        app = QApplication.instance()
        app.removeEventFilter(self)
        app.focusChanged.disconnect(self._focus_changed)
        self._file.close()
        self._file = self._writer = None
        self._at_focus.clear()
        return self.path

    def log(self, kind: str, what: str, value: str = "", where: str | None = None) -> None:
        """One line, unless the same thing was logged a moment ago (one gesture seen two ways)."""
        if not self.on:
            return
        where = self.where() if where is None else where
        key, now = (kind if kind != "action" else "click", what, value), time.monotonic()
        if key == self._last[0] and now - self._last[1] < SAME_WITHIN_S:
            return
        self._last = (key, now)
        line = [datetime.now().isoformat(timespec="milliseconds"), round(now - self._began, 2), kind, where, what,
                str(value)[:MAX_TEXT]]
        self._writer.writerow(line)
        self._file.flush()
        self.pending.append(line)

    def take(self) -> list[list]:
        """The lines since the last call (each Research Log step takes its own)."""
        lines, self.pending = self.pending, []
        return lines

    def _where_of(self, widget: QWidget) -> str:
        """The open tab for the main window's controls (its parent), else the dialog's title."""
        window = widget.window()
        return self.where() if window is None or window is self.parent() else (window.windowTitle() or self.where())

    def _state(self, window: QWidget) -> dict:
        """Every value the user could see change in `window`: fields, lists, tick boxes, messages (a label's text
        while shown) and the status bar's. Read-only text (the AI transcript) and controls marked activity_skip (the
        menu bar's clock) are left out."""
        found = {}
        for widget in window.findChildren(QWidget):
            if widget.property("activity_skip"):
                continue
            if isinstance(widget, (QTextEdit, QPlainTextEdit)) and widget.isReadOnly() or \
                    isinstance(widget, QLineEdit) and isinstance(widget.parentWidget(), QComboBox):
                continue  # read-only text; a typable list's own field (the list itself is logged)
            if isinstance(widget, (*ENTRIES, QComboBox)) or isinstance(widget, QAbstractButton) and widget.isCheckable():
                found[id(widget)] = (widget, "set", value_of(widget))
            elif isinstance(widget, QLabel):
                found[id(widget)] = (widget, "shown", widget.text() if widget.isVisible() else "")
        if isinstance(window, QMainWindow):
            found["status"] = (window.statusBar(), "message", window.statusBar().currentMessage())
        return found

    def _changes(self, source: QWidget) -> None:
        """What a click or choice changed besides itself (a Random pick, a message, a box ticked by the app), a line
        each, from the values noted when it was pressed."""
        before, self._before = self._before, {}
        if not before:
            return
        try:
            after = self._state(source.window())
        except RuntimeError:
            return
        for key, (widget, kind, old) in before.items():
            if widget is source or key not in after or after[key][2] == old:
                continue
            new = after[key][2]
            if kind == "shown":
                if new:
                    self.log("shown", "message", new, self._where_of(widget))
            elif kind == "message":
                if new:
                    self.log("message", "status bar", new)
            else:
                self.log("set", name_of(widget), new, self._where_of(widget))

    def eventFilter(self, watched, event) -> bool:  # noqa: N802 - Qt's name
        kind = event.type()
        if kind == QEvent.Type.MouseButtonPress and isinstance(watched, (QAbstractButton, QComboBox)):
            try:
                self._before = self._state(watched.window())  # to say what the click changes
            except RuntimeError:
                self._before = {}
        if kind == QEvent.Type.MouseButtonRelease and isinstance(watched, QWidget) and watched.isEnabled():
            if isinstance(watched, QMenu):
                action = watched.actionAt(event.position().toPoint())
                if action is not None and action.text() and id(action) not in self._actions:
                    self.log("menu", action.text().replace("&", ""))
            elif isinstance(watched, QAbstractButton) and not (isinstance(watched, QToolButton) and
                                                               watched.defaultAction() is not None):
                if watched.rect().contains(event.position().toPoint()):
                    QTimer.singleShot(0, lambda button=watched: self._clicked(button))  # after it toggles
            elif isinstance(watched, QAbstractSlider):
                self.log("slider", name_of(watched), value_of(watched), self._where_of(watched))
            elif isinstance(watched.parentWidget(), QGraphicsView) and event.button() == Qt.MouseButton.LeftButton:
                view = watched.parentWidget()
                spot = view.mapToScene(event.position().toPoint())
                self.log("map click", "map", f"x {spot.x():.1f} m, y {spot.y():.1f} m")
        elif kind == QEvent.Type.KeyPress and isinstance(watched, (QLineEdit, QAbstractSpinBox)) and \
                event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._entered(watched)
        elif kind == QEvent.Type.MouseButtonPress and isinstance(watched, QComboBox) and \
                not watched.property("activity_logged"):
            watched.setProperty("activity_logged", True)  # its choices, from now on
            watched.activated.connect(lambda _=0, box=watched: self.on and (self.log(
                "choice", name_of(box), box.currentText(), self._where_of(box)), self._changes(box)))
        return False

    def _clicked(self, button: QAbstractButton) -> None:
        try:
            self.log("click", name_of(button), value_of(button), self._where_of(button))
            self._changes(button)
        except RuntimeError:  # the button was deleted by its own click
            pass

    def _entered(self, widget: QWidget) -> None:
        raw = widget.text() if isinstance(widget, QLineEdit) else value_of(widget)  # a password's change counts too
        if isinstance(widget, QLineEdit) and not widget.isModified():  # set by the app (the question box cleared
            self._at_focus[id(widget)] = (widget, raw)                # after Ask), not typed: not the user's entry
            return
        if self._at_focus.get(id(widget), (None, None))[1] != raw:
            self.log("entry", name_of(widget), value_of(widget), self._where_of(widget))
            self._at_focus[id(widget)] = (widget, raw)

    def _focus_changed(self, old, new) -> None:
        if isinstance(old, ENTRIES):
            try:
                self._entered(old)
            except RuntimeError:  # deleted as focus left it
                pass
        if isinstance(new, ENTRIES):
            self._at_focus[id(new)] = (new, new.text() if isinstance(new, QLineEdit) else value_of(new))

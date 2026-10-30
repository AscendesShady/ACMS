"""Watch my edits (activity.py): the eye in the menu bar logging every click, entry, choice, tab, map selection,
action and edit to a CSV as it happens, and handing each Research Log step its lines.
Run with `python tests/test_activity.py` (or the test monitor)."""

import csv
import os
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QCheckBox, QComboBox, QLineEdit, QPushButton  # noqa: E402

from traffic_simulator.activity import name_of, value_of  # noqa: E402

APP = QApplication.instance() or QApplication([])


def rows(path: Path) -> list[dict]:
    with open(path, encoding="utf-8-sig") as file:
        return list(csv.DictReader(file))


def test_the_eye_in_the_menu_bar_logs_everything_the_user_does():
    from traffic_simulator.main import MainWindow

    window = MainWindow()
    window.results_folder = Path(tempfile.mkdtemp())
    window._ask_to_save = False
    window.show()
    APP.processEvents()
    assert window.watch_action in window.menuBar().cornerWidget().actions()  # in the menu bar, not AI Mode's row
    assert window.watch_action.toolTip() == "Watch my edits" and not hasattr(window.ai_mode, "watch")  # a logger only
    window.watch_action.trigger()  # as a click: toggled (it starts), then triggered
    log = window.activity
    assert log.on and log.path.parent.parent == window.results_folder and log.path.name == "activity.csv"
    assert log.path.parent.name.startswith("ACMS activity ")  # a dated folder, as the Research Log's and a batch's

    button = next(b for b in window.findChildren(QPushButton) if b.isVisible() and b.isEnabled() and b.text())
    QTest.mouseClick(button, Qt.MouseButton.LeftButton)  # a click, by the control's own name
    APP.processEvents()
    log._focus_changed(None, window.seed_box)  # a number typed: its final value, once
    window.seed_box.setValue(7)
    log._focus_changed(window.seed_box, None)
    log._focus_changed(window.seed_box, None)
    window.tabs.setCurrentIndex((window.tabs.currentIndex() + 1) % window.tabs.count())  # a tab opened
    window.select("road", next(iter(window.network.roads)))  # a map selection
    window.ai_mode._execute("update_roads", {"roads": [next(iter(window.network.roads))], "speed_limit_kmh": 30})
    window.undo_action.trigger()  # a shortcut's action
    password = QLineEdit(window)
    password.setEchoMode(QLineEdit.EchoMode.Password)
    password.setText("secret")
    log._focus_changed(None, password)
    QTest.keyClicks(password, "2")  # typed by the user
    log._focus_changed(password, None)
    question = QLineEdit(window)  # typed, then cleared by the app (as the question box is after Ask): one entry
    question.setPlaceholderText("Ask something")
    log._focus_changed(None, question)
    QTest.keyClicks(question, "how busy is 2,2?")
    log._focus_changed(question, None)
    question.clear()
    log._focus_changed(None, question)
    log._focus_changed(question, None)
    pending = log.take()
    assert pending and not log.take()  # each Research Log step gets the lines since the one before
    window.watch_action.setChecked(False)
    path = log.path
    found = rows(path)
    kinds = [row["Kind"] for row in found]
    assert kinds[0] == "watch" and found[0]["Value"] == "on" and kinds[-1] == "watch" and found[-1]["Value"] == "off"
    assert not any("Watch my edits" in str(row.values()) for row in found[1:-1])  # the eye logs itself once each way
    assert any(row["Kind"] == "click" and row["What"] == button.text().replace("&", "") for row in found)
    assert [row["Value"] for row in found if row["Kind"] == "entry" and row["What"] == "Random seed for runs"] == ["7"]
    assert any(row["Kind"] == "tab" for row in found) and any(row["Kind"] == "select" for row in found)
    assert any(row["Kind"] == "edit" and "speed 50.0 → 30.0" in row["What"] for row in found)
    assert any(row["Kind"] == "action" and row["What"].startswith("Undo") for row in found)
    assert any(row["Value"] == "(hidden)" for row in found) and not any("secret" in row["Value"] for row in found)
    asked = [row["Value"] for row in found if row["Kind"] == "entry" and row["What"] == name_of(question)]
    assert asked == ["how busy is 2,2?"]  # no empty line for the box the app cleared
    assert all(row["Where"] for row in found if row["Kind"] in ("click", "entry"))  # the tab it happened in
    assert not log.on and window.watch_action.isChecked() is False
    window.close()


def test_what_a_click_fills_in_is_logged_too():
    from traffic_simulator.main import MainWindow

    window = MainWindow()
    window.results_folder = Path(tempfile.mkdtemp())
    window._ask_to_save = False
    window.show()
    window.tabs.setCurrentWidget(window.extensions)
    dispatch = window._extension("dispatch_emergency")
    dispatch.check.setChecked(True)
    APP.processEvents()
    window.watch_action.setChecked(True)
    log = window.activity
    shuffle = next(b for b in dispatch.controls.findChildren(QPushButton) if b.toolTip() == "Random" and b.isVisible())
    QTest.mouseClick(shuffle, Qt.MouseButton.LeftButton)  # a random From: the click, and the place it picked
    APP.processEvents()
    picked = dispatch.module._lines[0][2].currentText()
    surprise = next(b for b in dispatch.controls.findChildren(QPushButton) if b.toolTip() == "Random Emergency")
    QTest.mouseClick(surprise, Qt.MouseButton.LeftButton)  # no run: its message says why
    APP.processEvents()
    window.watch_action.setChecked(False)
    found = rows(log.path)
    clicks = [i for i, row in enumerate(found) if row["Kind"] == "click" and row["What"] == "Random"]
    assert clicks and picked
    sets = [row for row in found[clicks[0]:] if row["Kind"] == "set" and row["Value"] == picked]
    assert [row["What"] for row in sets] == ["From: end or junction"]  # the random pick, once
    assert any(row["Kind"] in ("shown", "message") and row["Value"] for row in found
               if found.index(row) > next(i for i, r in enumerate(found) if r["What"] == "Random Emergency"))
    assert not any(row["Kind"] == "shown" and row["Value"] and row["Value"][:2].isdigit() and row["Value"][2] == ":"
                   for row in found)  # the menu bar's clock isn't logged
    window.close()


def test_controls_are_named_and_read_as_the_user_sees_them():
    box = QCheckBox("Lane changing")
    box.setChecked(True)
    assert name_of(box) == "Lane changing" and value_of(box) == "on"
    combo = QComboBox()
    combo.addItems(["Webster", "SCOOT"])
    combo.setCurrentIndex(1)
    combo.setToolTip("Signal timing method")
    assert name_of(combo) == "Signal timing method" and value_of(combo) == "SCOOT"
    field = QLineEdit()
    field.setPlaceholderText("Seeds, e.g. 1-5")
    assert name_of(field) == "Seeds, e.g. 1-5"


def test_lines_no_step_takes_keep_only_the_latest_waiting():
    """With Watch my edits on and no Research Log step taking its lines, `pending` keeps the last KEEP_PENDING
    (it grew without end); activity.csv still has every line."""
    from traffic_simulator import activity

    log, keep = activity.ActivityLog(), activity.KEEP_PENDING
    activity.KEEP_PENDING = 10
    try:
        log.start(Path(tempfile.mkdtemp()))
        for n in range(25):
            log.log("entry", f"field {n}", str(n))
        assert len(log.pending) == 10 and log.pending[-1][4] == "field 24"
        assert len(rows(log.path)) == 26  # "Watch my edits on", then every entry
    finally:
        log.stop()
        activity.KEEP_PENDING = keep


def test_a_full_disk_turns_the_eye_off_and_says_so_once():
    from traffic_simulator.main import MainWindow

    class Full:
        def write(self, _):
            raise OSError(28, "No space left on device")

        flush = close = lambda self: None

    window = MainWindow()
    window.results_folder = Path(tempfile.mkdtemp())
    window.watch_action.setChecked(True)
    real = window.activity._file
    window.activity._file, window.activity._writer = Full(), csv.writer(Full())
    real.close()
    window.activity.log("click", "Run")  # the eventFilter's and focusChanged's way in: no error raised
    assert not window.activity.on and not window.watch_action.isChecked()
    assert "No space left" in window.statusBar().currentMessage()
    window.activity.log("click", "Stop")  # off now: nothing tried, nothing said again
    window.close()


if __name__ == "__main__":
    for name, check in list(globals().items()):
        if name.startswith("test_"):
            check()
    print("ok")

"""Two things never overlap: while a test runs (a batch, an experiment, the assistant's checks) Run and Play
wait with their tooltips naming it, any other way to start one is refused saying what is going on, Batch Run's
Start waits, closing the window stops the test and then closes, and Stop during an applied edit leaves the
assistant ready, not answering."""

import os
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication, QCheckBox, QLabel, QLineEdit, QPushButton  # noqa: E402

from traffic_simulator.main import MainWindow  # noqa: E402

APP = QApplication.instance() or QApplication([])


def window():
    made = MainWindow()
    made._ask_to_save = False
    return made


def test_a_test_going_on_holds_back_every_other_way_to_start_one():
    main, release, seen = window(), threading.Event(), {}
    batch = main._extension("batch_run").controls
    start = next(button for button in batch.findChildren(QPushButton) if button.text() == "Start Batch")
    seeds = next(box for box in batch.findChildren(QLineEdit) if box.toolTip().startswith("Seeds"))

    def probe():
        seen["busy"] = main.busy()
        seen["menu"] = main.run_action.isEnabled(), main.play_action.isEnabled(), main.run_action.toolTip()
        main._play()  # Play with no run would start one
        seen["runner"] = main.runner
        try:
            main._in_background(lambda progress, cancelled: None, "Decision sensitivity")
            seen["nested"] = ""
        except ValueError as exc:
            seen["nested"] = str(exc)
        next(box for box in batch.findChildren(QCheckBox) if box.toolTip() == "TSP tab's models").setChecked(True)
        seeds.setText("1-2")  # the panel looks again
        seen["start"] = start.isEnabled()
        seen["hint"] = any("Batch run is going on" in label.text() for label in batch.findChildren(QLabel))
        release.set()

    QTimer.singleShot(50, probe)
    assert main._in_background(lambda progress, cancelled: release.wait(10) and "done", "Batch run") == "done"
    assert seen["busy"] == "Batch run" and seen["menu"] == (False, False, "Busy: Batch run")
    assert seen["runner"] is None and seen["nested"].startswith("Batch run is going on")
    assert not seen["start"] and seen["hint"]
    assert main.busy() == "" and main.run_action.isEnabled() and main.run_action.toolTip() == "Run from 00:00:00"

    main._run()  # and a live run holds a test back
    assert main.busy() == "A live run"
    try:
        main._in_background(lambda progress, cancelled: None, "Batch run")
        raise AssertionError("a test started beside a live run")
    except ValueError as exc:
        assert str(exc).startswith("A live run is going on")
    main._stop_run()
    main.close()


def test_closing_mid_test_stops_it_then_closes():
    main, seen = window(), {}
    main.show()

    def close_now():
        main.close()
        seen["open"], seen["cancel"] = main.isVisible(), main._cancel

    def work(progress, cancelled):
        while not cancelled():
            time.sleep(0.01)
        return "stopped cleanly"

    QTimer.singleShot(50, close_now)
    assert main._in_background(work, "Batch run") == "stopped cleanly"  # its caller still gets the result
    assert seen == {"open": True, "cancel": True}  # the close waited for the test
    APP.processEvents()
    assert not main.isVisible()


def test_stop_during_an_applied_edit_leaves_the_assistant_ready():
    from traffic_simulator.ai_mode import _ToolRequest

    main = window()
    panel = main.ai_mode
    request, later = _ToolRequest({"id": "a", "name": "describe", "arguments": {}}), _ToolRequest(
        {"id": "b", "name": "describe", "arguments": {}})
    request.record = {"decision": "waiting"}
    panel._pending = request

    def execute(name, args):  # a long edit (an experiment): Stop pressed, then a new question's card, meanwhile
        panel.stop()
        panel._pending = later
        return {"ok": True}

    panel._execute = execute
    panel._decide(True)
    assert request.done.is_set() and request.result == {"error": "Stopped by the user."}
    assert request.record["decision"] == "applied"  # it was applied, and the record says so
    assert panel._pending is later and not later.done.is_set()  # the new question's card isn't answered with it
    assert "thinking" not in (panel._busy_text or "")
    main.close()


if __name__ == "__main__":
    for name, check in list(globals().items()):
        if name.startswith("test_"):
            check()
    print("ok")

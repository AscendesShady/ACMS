"""Test monitor: python run_monitor.py

Runs every test_* function in tests/test_*.py in a fresh Python process (so
Run Again always tests your latest code) and shows live progress: a bar,
each test's status and time, and the traceback of the selected failure.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
import traceback
from pathlib import Path

from traffic_simulator.theme import STATUS, apply_theme
from PySide6.QtCore import QProcess, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSplitter,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

TESTS_DIR = Path(__file__).resolve().parent / "tests"
COLORS = {state: QColor(colour) for state, colour in STATUS.items()}  # readable on the grey theme


def run_worker() -> None:
    """Child process: run the tests, one JSON line per event on stdout.

    Anything the tests print goes to stderr so it can't break the protocol.
    """
    report, sys.stdout = sys.stdout, sys.stderr
    os.environ["QT_QPA_PLATFORM"] = "offscreen"  # the tests' platform, however the monitor was started
    sys.path.insert(0, str(TESTS_DIR))  # tests import helpers from each other, as when run on their own
    tests = []
    for path in sorted(TESTS_DIR.glob("test_*.py")):
        try:
            spec = importlib.util.spec_from_file_location(path.stem, path)
            module = importlib.util.module_from_spec(spec)
            sys.modules[path.stem] = module  # one copy, whether loaded here or imported by another test
            spec.loader.exec_module(module)  # type: ignore[union-attr]
        except Exception as exc:  # a file that won't load fails on its own; the others still run
            message = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
            tests.append((f"{path.stem} (loading)", _failing(message)))
            continue
        tests += [(f"{path.stem}.{name}", check) for name, check in vars(module).items()
                  if name.startswith("test_") and callable(check)]
    print(json.dumps({"total": len(tests)}), file=report, flush=True)
    for name, check in tests:
        print(json.dumps({"start": name}), file=report, flush=True)
        started = time.perf_counter()
        try:
            check()
            error = ""
        except (Exception, SystemExit) as exc:  # traceback from the test itself, minus this runner's frame
            error = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__.tb_next))
        print(json.dumps({"name": name, "error": error, "seconds": time.perf_counter() - started}),
              file=report, flush=True)


def _failing(message: str):
    """A test that fails with a test file's loading error, so it shows in the list like any failure."""
    def check() -> None:
        raise ImportError(message)
    return check


def _duration(seconds: float) -> str:
    if seconds < 0.0005:
        return "<1 ms"
    return f"{seconds * 1000:.0f} ms" if seconds < 1 else f"{seconds:.2f} s"


class Monitor(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Test Monitor")
        self.resize(760, 520)
        self.headline = QLabel()
        self.headline.setStyleSheet("font-size: 15px; font-weight: 600;")
        self.bar = QProgressBar()
        self.run_button = QPushButton("Run Again")
        self.run_button.clicked.connect(self.run)
        top = QHBoxLayout()
        top.addWidget(self.headline, 1)
        top.addWidget(self.run_button)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Test", "Status", "Time"])
        self.tree.setRootIsDecorated(False)
        self.tree.setColumnWidth(0, 480)
        self.tree.currentItemChanged.connect(self._show_details)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setPlaceholderText("Select a test to see its traceback.")
        self.details.setStyleSheet("font-family: Consolas, monospace;")
        split = QSplitter(Qt.Orientation.Vertical)
        split.addWidget(self.tree)
        split.addWidget(self.details)
        split.setSizes([300, 200])

        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(self.bar)
        layout.addWidget(split, 1)

        self.process = QProcess(self)
        self.process.readyReadStandardOutput.connect(self._read)
        self.process.finished.connect(self._finished)
        self.run()

    def run(self) -> None:
        if self.process.state() != QProcess.ProcessState.NotRunning:
            return
        self.tree.clear()
        self.details.clear()
        self.items: dict[str, QTreeWidgetItem] = {}
        self.errors: dict[str, str] = {}
        self.total = self.passed = self.failed = 0
        self.buffer = b""
        self.bar.setRange(0, 0)  # busy until the worker reports the test count
        self._set_headline("Starting…", "running")
        self.run_button.setEnabled(False)
        self.started = time.perf_counter()
        self.process.start(sys.executable, [str(Path(__file__).resolve()), "--worker"])

    def _set_headline(self, text: str, state: str) -> None:
        self.headline.setText(text)
        self.headline.setStyleSheet(f"font-size: 15px; font-weight: 600; color: {COLORS[state].name()};")

    def _read(self) -> None:
        self.buffer += bytes(self.process.readAllStandardOutput())
        *lines, self.buffer = self.buffer.split(b"\n")
        for line in filter(bytes.strip, lines):
            self._handle(json.loads(line))

    def _handle(self, event: dict) -> None:
        if "total" in event:
            self.total = event["total"]
            self.bar.setRange(0, max(self.total, 1))
            self.bar.setValue(0)
        elif "start" in event:
            item = QTreeWidgetItem([event["start"], "running…", ""])
            item.setForeground(1, COLORS["running"])
            self.items[event["start"]] = item
            self.tree.addTopLevelItem(item)
            self.tree.scrollToItem(item)
        else:
            name, error = event["name"], event["error"]
            state = "failed" if error else "passed"
            item = self.items[name]
            item.setText(1, state)
            item.setText(2, _duration(event["seconds"]))
            item.setForeground(1, COLORS[state])
            if error:
                self.errors[name] = error
                self.failed += 1
            else:
                self.passed += 1
            self.bar.setValue(self.passed + self.failed)
            failed = f" · {self.failed} failed" if self.failed else ""
            self._set_headline(f"{self.passed + self.failed} / {self.total} tests{failed}",
                               "failed" if self.failed else "running")

    def _finished(self, exit_code: int, _status) -> None:
        self._read()
        self.run_button.setEnabled(True)
        elapsed = _duration(time.perf_counter() - self.started)
        done = self.passed + self.failed
        if exit_code != 0 or done < self.total:
            # The worker itself broke: an import error in a test file, or a crash.
            self.bar.setRange(0, max(self.total, 1))
            self._set_headline(f"Test run crashed after {done} / {self.total or '?'} tests", "failed")
            self.details.setPlainText(bytes(self.process.readAllStandardError()).decode(errors="replace"))
            return
        if self.failed:
            self._set_headline(f"{self.failed} failed, {self.passed} passed in {elapsed}", "failed")
            self.tree.setCurrentItem(self.items[next(iter(self.errors))])
        else:
            self._set_headline(f"All {self.total} tests passed in {elapsed}", "passed")

    def _show_details(self, item: QTreeWidgetItem | None, _previous=None) -> None:
        if item is not None:
            self.details.setPlainText(self.errors.get(item.text(0), "Passed."))

    def closeEvent(self, event) -> None:  # type intentionally inherited from Qt
        self.process.kill()
        self.process.waitForFinished(1000)
        super().closeEvent(event)


if __name__ == "__main__":
    if "--worker" in sys.argv:
        run_worker()
    else:
        app = QApplication(sys.argv)
        apply_theme(app)
        monitor = Monitor()
        monitor.show()
        raise SystemExit(app.exec())

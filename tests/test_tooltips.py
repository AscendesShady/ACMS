"""Hover text stays short: every tooltip in the window, five words at most."""

import os
import re

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QAction  # noqa: E402
from PySide6.QtWidgets import QApplication, QTabBar, QWidget  # noqa: E402

MAX_WORDS = 5


def _words(text: str) -> int:
    return len(re.sub(r"<[^>]+>", " ", text).split())


def _long_tooltips(window) -> list[str]:
    found = []
    for widget in window.findChildren(QWidget):
        if _words(widget.toolTip()) > MAX_WORDS:
            found.append(f"{type(widget).__name__}: {widget.toolTip()!r}")
        if isinstance(widget, QTabBar):
            found += [f"tab: {widget.tabToolTip(i)!r}" for i in range(widget.count())
                      if _words(widget.tabToolTip(i)) > MAX_WORDS]
    found += [f"action {action.text()!r}: {action.toolTip()!r}" for action in window.findChildren(QAction)
              if _words(action.toolTip()) > MAX_WORDS]
    found += [f"map item: {item.toolTip()!r}" for item in window.scene.items() if _words(item.toolTip()) > MAX_WORDS]
    return found


def test_every_tooltip_is_five_words_at_most():
    from traffic_simulator.main import MainWindow
    from traffic_simulator.institutions import KINDS
    from traffic_simulator.models import Institution

    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    problems = _long_tooltips(window)
    network = window.network
    for kind, ids in (("node", network.nodes), ("road", network.roads), ("end", network.approach_nodes)):
        window.select(kind, sorted(ids)[0])  # each editor's own controls
        problems += _long_tooltips(window)
    for number, kind in enumerate(KINDS, 1):  # each kind's tooltip (the hospital's people label is long)
        network.institutions[f"IN{number}"] = Institution(f"IN{number}", kind, 300.0 * number, -400.0, 2500.0, 300.0, 120, 8)
    window._commit()
    window.od_matrix.set_ends(*window._od_ends())
    for mode in ("hourly", "daily"):
        window.od_panel._show_mode(mode)
        window.od_panel._select([(window.od_matrix.origins[0], window.od_matrix.destinations[1])])
        problems += _long_tooltips(window)
        window.od_panel.demand.setCurrentIndex(window.od_panel.demand.findData("institutions"))
        window.od_panel.institutions.show_selected(network, network.institutions["IN1"])  # its hourly or daily demand
        problems += _long_tooltips(window)
    shown = []
    window.ai_mode.indicator.connect(lambda text, tip: shown.append(tip))  # the status bar's System 2 line
    window.ai_mode.provider.addItem("Local (Ollama)", "local")
    window.ai_mode.provider.setCurrentIndex(window.ai_mode.provider.count() - 1)
    window.ai_mode._wanted = "llama3.1:8b"
    window.ai_mode._indicate("ready")
    problems += _long_tooltips(window) + [f"System 2 status: {tip!r}" for tip in shown if _words(tip) > MAX_WORDS]
    window.close()
    assert not problems, "\n".join(sorted(set(problems)))


def test_tooltips_set_while_a_test_runs_are_five_words_at_most():
    from traffic_simulator.main import MainWindow

    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    window._testing, window._busy_what = True, "Never preempt (baseline) batch run of every seed"
    window._headless.update({"Never preempt (baseline), seed 1": (754.0, 1800.0), "Testing the network": (60.0, 1800.0)})
    window._show_busy()  # Run and Play name what holds them back
    window._show_headless()  # the menu bar's clock shows the runs going
    problems = _long_tooltips(window)
    window._testing = False
    window._headless.clear()
    window.close()
    assert not problems, "\n".join(sorted(set(problems)))


if __name__ == "__main__":
    for name, check in list(globals().items()):
        if name.startswith("test_"):
            check()
    print("ok")

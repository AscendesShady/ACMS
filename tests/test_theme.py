"""Theme readability check, and a click seen on every button. Run with `python tests/test_theme.py` (or the test
monitor)."""

import os
import subprocess
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"))  # real fonts

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtCore import QPoint  # noqa: E402
from PySide6.QtWidgets import (QApplication, QCheckBox, QLabel, QPushButton, QRadioButton, QStyle,
                               QStyleOptionButton)  # noqa: E402

from traffic_simulator.theme import FLASH_MS, PRESSED, READABLE_PAIRS, apply_theme  # noqa: E402

APP = QApplication.instance() or QApplication([])


def contrast(first: str, second: str) -> float:
    """WCAG 2 contrast ratio between two #rrggbb colours."""
    def luminance(colour: str) -> float:
        channels = [int(colour[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]
    high, low = sorted((luminance(first), luminance(second)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def test_text_is_readable_on_every_background():
    assert abs(contrast("#ffffff", "#000000") - 21) < 1e-9
    weak = [(text, background, round(contrast(text, background), 2))
            for text, background in READABLE_PAIRS if contrast(text, background) < 4.5]
    assert not weak, weak


def test_a_click_lights_the_button_for_a_moment():
    apply_theme(APP)
    apply_theme(APP)  # a second window: still one flash filter
    assert APP._click_flash is not None
    for role in ("", "primary", "mini"):
        button = QPushButton("Go")
        button.setProperty("role", role)
        button.show()
        APP.processEvents()
        resting = button.grab().toImage().pixelColor(button.width() // 2, 3).name()
        QTest.mouseClick(button, Qt.MouseButton.LeftButton)
        APP.processEvents()
        lit = button.grab().toImage().pixelColor(button.width() // 2, 3).name()
        assert button.property("flash") and lit != resting, (role, resting, lit)  # seen at once
        assert role == "primary" or lit == PRESSED
        until = time.time() + FLASH_MS / 1000 + 0.3
        while time.time() < until:
            APP.processEvents()
        assert not button.property("flash")  # then back as it was
        button.close()


def test_checked_controls_have_a_visible_mark_inside_the_indicator():
    """A checkmark/dot distinguishes checked controls without relying only on a blue fill."""
    apply_theme(APP)
    for kind, element in ((QCheckBox, QStyle.SubElement.SE_CheckBoxIndicator),
                          (QRadioButton, QStyle.SubElement.SE_RadioButtonIndicator)):
        button = kind("Allowed")
        button.setChecked(True)
        button.show()
        APP.processEvents()
        option = QStyleOptionButton()
        button.initStyleOption(option)
        rect = button.style().subElementRect(element, option, button).adjusted(4, 4, -4, -4)
        image = button.grab().toImage()
        colours = [image.pixelColor(x, y) for x in range(rect.left(), rect.right() + 1)
                   for y in range(rect.top(), rect.bottom() + 1)]
        # The old indicator was a uniform blue square/disc, with no checkmark or dot at all.
        darkest = min(colours, key=lambda c: c.lightnessF())
        lightest = max(colours, key=lambda c: c.lightnessF())
        assert contrast(darkest.name(), lightest.name()) >= 3, kind.__name__
        button.close()


def test_keyboard_focus_keeps_button_content_and_size_stable():
    apply_theme(APP)
    button = QPushButton("Generate Network")
    button.show()
    APP.processEvents()
    button.clearFocus()
    APP.processEvents()

    def bounds():
        option = QStyleOptionButton()
        button.initStyleOption(option)
        return button.sizeHint(), button.style().subElementRect(QStyle.SubElement.SE_PushButtonContents,
                                                                option, button)

    resting = bounds()
    button.setFocus(Qt.FocusReason.TabFocusReason)
    APP.processEvents()
    assert button.hasFocus()
    assert bounds() == resting  # a thicker focus border used to move the text and change the size hint
    button.close()


def test_shared_widgets_need_no_properties_panel():
    """The shared widgets (widgets.py) load without the selection editor or the model, and only main uses the editor."""
    loaded = subprocess.run([sys.executable, "-c", "import sys, traffic_simulator.widgets; print(sorted(sys.modules))"],
                            capture_output=True, text=True, timeout=60, check=True).stdout
    assert "traffic_simulator.properties_panel" not in loaded and "traffic_simulator.models" not in loaded
    root = Path(__file__).resolve().parents[1]
    users = [path.relative_to(root).as_posix() for folder in ("src", "extensions", "tools")
             for path in (root / folder).rglob("*.py") if "properties_panel import" in path.read_text(encoding="utf-8")]
    assert users == ["src/traffic_simulator/main.py"], users


def test_the_window_lines_up():
    """The window's frame, measured: the menu bar's text centred on the run bar, which sits as far below the window's
    top as above the panels; "ACMS" and the open tab's name on one baseline; the icon tabs filling the row edge to
    edge with no base line through them; AI Mode's pages on the sidebar's column, as the others."""
    from traffic_simulator.main import MainWindow
    window = MainWindow()
    window.resize(1280, 800)
    window.show()
    for _ in range(12):
        APP.processEvents()
    try:
        top = lambda widget: widget.mapTo(window, QPoint(0, 0)).y()
        menu, glass = window.menuBar(), window.menuBar().cornerWidget()
        below = top(window.centralWidget()) - (top(glass) + glass.height())  # the panels start there
        assert abs(top(glass) - below) <= 1, (top(glass), below)  # evenly padded above and below
        item = menu.actionGeometry(menu.actions()[0])
        # File's box on the bar's middle (Segoe UI's text sits ~1.5 px below its box's centre: on the bar's own)
        assert abs(item.center().y() - (top(glass) - top(menu) + glass.height() / 2)) <= 2
        brand = next(label for label in window.findChildren(QLabel) if label.property("role") == "brand")
        baseline = lambda label: top(label) + label.contentsRect().top() + label.fontMetrics().ascent()
        assert abs(baseline(brand) - baseline(window.page_title)) <= 1, (baseline(brand), baseline(window.page_title))
        bar = window.tabs.tabBar()
        assert not bar.drawBase()  # the line that ran into the tiles
        assert bar.tabRect(0).left() == 0 and bar.tabRect(bar.count() - 1).right() >= bar.width() - 2
        assert bar.width() - 2 * bar.count() < bar.sizeHint().width() <= bar.width()  # padded to fill, never wider
        for index in range(window.ai_mode.systems.count()):
            assert window.ai_mode.systems.widget(index).layout().contentsMargins().left() == 0
    finally:
        window.close()


if __name__ == "__main__":
    for name, check in list(globals().items()):
        if name.startswith("test_"):
            check()
    print("ok")

"""Theme readability check, and a click seen on every button. Run with `python tests/test_theme.py` (or the test
monitor)."""

import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QPushButton  # noqa: E402

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


if __name__ == "__main__":
    for name, check in list(globals().items()):
        if name.startswith("test_"):
            check()
    print("ok")

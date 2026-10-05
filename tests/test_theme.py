"""Theme readability check. Run with `python tests/test_theme.py` (or the test monitor)."""

from traffic_simulator.theme import READABLE_PAIRS


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


if __name__ == "__main__":
    for name, check in list(globals().items()):
        if name.startswith("test_"):
            check()
    print("ok")

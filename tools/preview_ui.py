"""Capture the real Qt interface without starting traffic or changing a saved project.

    .venv\Scripts\python.exe tools\preview_ui.py --output reports/ui-preview --width 1280 --height 800

Uses the CPU/offscreen renderer. Windows fonts are explicitly located for Qt's offscreen plugin.
This checks desktop layout and rendering, not the native Windows window frame or OpenGL path.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if os.name == "nt":
    os.environ.setdefault("QT_QPA_FONTDIR", str(Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"))

from PySide6.QtWidgets import QApplication, QScrollArea  # noqa: E402

from traffic_simulator.main import MainWindow  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=800)
    parser.add_argument("--isometric", action="store_true", help="Capture the orthographic 2.5D camera")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    window.resize(args.width, args.height)
    window.show()
    window.view.set_isometric(args.isometric)
    sidebar = window.findChild(QScrollArea, "sidebar")
    records = []
    try:
        for index, name in enumerate(window.tab_names):
            window.tabs.setCurrentIndex(index)
            pages = range(window.ai_mode.systems.count()) if window.tabs.currentWidget() is window.ai_mode else [None]
            for page in pages:
                if page is not None:
                    window.ai_mode.systems.setCurrentIndex(page)
                for _ in range(12):
                    app.processEvents()
                # Flush floating toolbar effects before grab reuses their offscreen paint cache.
                window.repaint()
                app.processEvents()
                slug = name.lower().replace(" ", "-") + (f"-{page + 1}" if page is not None else "")
                path = args.output / f"{index + 1:02d}-{slug}.png"
                if not window.grab().save(str(path)):
                    raise RuntimeError(f"Could not save {path}")
                records.append({"tab": name, "page": page, "image": path.name,
                                "window": list(window.size().toTuple()),
                                "sidebar_width": sidebar.width(),
                                "sidebar_scroll": sidebar.verticalScrollBar().isVisible(),
                                "workspace_width": window.workspace.width(),
                                "isometric": window.view.isometric,
                                "horizontal_scroll": sidebar.horizontalScrollBar().isVisible()})
    finally:
        window.close()
        app.processEvents()
    (args.output / "layout.json").write_text(json.dumps(records, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(records, indent=2))


if __name__ == "__main__":
    main()

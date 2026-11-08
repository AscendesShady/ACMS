"""Writing a file whole or not at all (no Qt): write_whole, a .part beside the file then os.replace, the app's one
file writer. Moved out of results.py (which still imports it, results.write_whole), so saving a project, the map
tiles and the OSM cache don't depend on the run-results module."""

from __future__ import annotations

import os
import threading
from pathlib import Path


def write_whole(path, data: str | bytes) -> None:
    """`data` (text as UTF-8) to `path` whole or not at all: to a .part beside it, then os.replace (a write torn by a
    crash or a full disk leaves the file as it was, and no .part behind). The app's one way to write a file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(f"{path.name}.{threading.get_ident()}.part")  # a thread's own
    try:
        if isinstance(data, str):
            part.write_text(data, encoding="utf-8")
        else:
            part.write_bytes(data)
        os.replace(part, path)
    except OSError:
        part.unlink(missing_ok=True)
        raise

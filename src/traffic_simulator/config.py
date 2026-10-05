"""Application defaults loaded from the repository configuration file."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "defaults.json"


def _load_defaults() -> dict[str, Any]:
    try:
        return json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        # Keep the editor usable if it is copied without the optional config file.
        return {}


_DEFAULTS = _load_defaults()
DEFAULT_HORIZONTAL_CORRIDORS = int(_DEFAULTS.get("horizontal_corridors", 3))
DEFAULT_VERTICAL_CORRIDORS = int(_DEFAULTS.get("vertical_corridors", 4))
DEFAULT_DRIVING_SIDE = str(_DEFAULTS.get("driving_side", "left"))
DEFAULT_SPEED_LIMIT_KMH = float(_DEFAULTS.get("speed_limit_kmh", 50))
DEFAULT_LANES_PER_DIRECTION = int(_DEFAULTS.get("lanes_per_direction", 2))
DEFAULT_LANE_CHANGING = bool(_DEFAULTS.get("lane_changing", True))

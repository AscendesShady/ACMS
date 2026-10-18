"""Display-only vehicle appearance choices. Geometry lives in asset_mesh; no physics or Qt.

The front is +x; x/y run from -1 to 1 inside the vehicle's actual half length/width.
Both renderers use asset_mesh models; colours are supplied by theme.py. Style zero keeps
old, manually constructed frames plain. Vehicle classes and emergency dispatch choose the skin.
"""

from hashlib import blake2s



def identity_number(identifier: str) -> int:
    """Stable across processes and frames; never draws from a simulation random stream."""
    return int.from_bytes(blake2s(identifier.encode("utf-8"), digest_size=4).digest(), "big")


def style(vehicle) -> int:
    kind = getattr(vehicle, "vehicle_class", "")
    if getattr(vehicle, "emergency", False) or kind == "emergency":
        return 5
    if kind in ("bus", "coach", "tram"):
        return 4
    if kind == "bicycle":
        return 6
    if kind in ("motorcycle", "moped"):
        return 7
    if kind in ("delivery", "truck", "trailer"):
        return 8
    return 1 + identity_number(vehicle.id) % 3 if kind else 0


def paint_colour(vehicle, palette: tuple[str, ...]) -> str:
    return palette[identity_number(vehicle.id) % len(palette)] if getattr(vehicle, "random_colour", False) else vehicle.colour


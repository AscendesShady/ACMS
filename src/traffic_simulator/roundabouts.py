"""Roundabout sizing from the approach speed and lanes.

The HCM's roundabout chapter covers capacity; the geometric guidance it relies
on is NCHRP Report 672 (FHWA, "Roundabouts: An Informational Guide", 2nd ed.):

- a category by entry lanes, each with a recommended maximum entry design
  speed and a typical inscribed circle diameter (ICD) range;
- a circulatory roadway 100-120 % as wide as the widest entry (120 % here);
- the fastest-path speed-radius relation V = 3.4614 R^0.3673 (mph, ft) for the
  circulatory roadway's -2 % cross slope, used to report the circulating speed.

The design speed is the fastest approach limit held inside the category's
entry-speed range; it places the ICD within the category's typical range, so
faster approaches get a larger roundabout.
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import LANE_WIDTH_M, Network

# Category: (name, recommended max entry design speed range km/h, typical ICD range m).
CATEGORIES = {
    "mini": ("Mini-roundabout", (25.0, 30.0), (14.0, 27.0)),
    1: ("Single-lane roundabout", (30.0, 40.0), (27.0, 55.0)),
    2: ("Two-lane roundabout", (40.0, 50.0), (46.0, 67.0)),
    3: ("Three-lane roundabout", (40.0, 50.0), (61.0, 91.0)),
}
# Single-lane approaches already this slow need no more deflection than a mini gives.
MINI_MAX_APPROACH_KMH = 30.0
CIRCULATORY_SHARE_OF_ENTRY = 1.2
PATH_OFFSET_FROM_ISLAND_M = 1.5  # fastest-path vehicle centre line, from the island edge


@dataclass(frozen=True)
class RoundaboutDesign:
    category: str
    entry_lanes: int
    approach_speed_kmh: float
    design_speed_kmh: float
    inscribed_diameter_m: float
    circulatory_width_m: float
    island_diameter_m: float
    circulating_speed_kmh: float
    mini: bool


def fastest_path_speed_kmh(radius_m: float) -> float:
    """NCHRP 672 speed on a path of this radius with a -2 % cross slope."""
    return 3.4614 * (radius_m / 0.3048) ** 0.3673 * 1.609344


def design_roundabout(network: Network, node_id: str) -> RoundaboutDesign:
    roads = [road for road in network.all_roads() if node_id in (road.from_node, road.to_node)]
    # Lanes entering the roundabout: one direction of a two-way road, all of a one-way one.
    entering = [road.lanes_each_way for road in roads if road.direction == "two-way" or road.to_node == node_id]
    entry_lanes = max(entering or [1])
    approach_speed = max([road.speed_limit_kmh for road in roads] or [network.default_speed_limit_kmh])
    mini = entry_lanes == 1 and approach_speed <= MINI_MAX_APPROACH_KMH
    name, (low_speed, high_speed), (low_icd, high_icd) = CATEGORIES["mini" if mini else min(entry_lanes, 3)]
    design_speed = min(max(approach_speed, low_speed), high_speed)
    share = (design_speed - low_speed) / (high_speed - low_speed)
    inscribed = low_icd + share * (high_icd - low_icd)
    width = CIRCULATORY_SHARE_OF_ENTRY * entry_lanes * LANE_WIDTH_M
    island = max(inscribed - 2 * width, 2.0)
    circulating = fastest_path_speed_kmh(island / 2 + PATH_OFFSET_FROM_ISLAND_M)
    return RoundaboutDesign(name, entry_lanes, approach_speed, design_speed, inscribed, width,
                            island, circulating, mini)

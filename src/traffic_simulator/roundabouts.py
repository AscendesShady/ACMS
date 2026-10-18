"""Roundabout sizing from the approach speed and lanes.

The HCM's roundabout chapter covers capacity; the geometric guidance it relies
on is NCHRP Report 672 (FHWA, "Roundabouts: An Informational Guide", 2nd ed.):

- a category by entry lanes, each with a recommended maximum entry design
  speed and a typical inscribed circle diameter (ICD) range; it covers up to three
  entry lanes, so more are refused;
- a circulatory roadway 100-120 % as wide as the widest entry (120 % here);
- the fastest-path speed-radius relation V = 3.4614 R^0.3673 (mph, ft) for the
  circulatory roadway's -2 % cross slope, used to report the circulating speed.

`hcm_entry_capacity` is the HCM 6th edition's entry capacity against the circulating
flow, the analytical check analysis.junction_los reports beside the simulated one.

The design speed is the fastest approach limit held inside the category's
entry-speed range; it places the ICD within the category's typical range, so
faster approaches get a larger roundabout.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import exp

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
MAX_ENTRY_LANES = 3  # the guide's largest category


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


def hcm_entry_capacity(circulating_vph: float, lanes: int = 1) -> float | None:
    """HCM 6th ed. (Ch 22) entry capacity, veh/h, of an entry of `lanes` lanes
    against `circulating_vph` (cars as passenger cars), with as many circulating lanes as entry lanes as NCHRP 672
    designs them: one lane 1380·e^(−0.00102·v_c) (Eq 22-1); two lanes 1420·e^(−0.00085·v_c) (right) +
    1350·e^(−0.00092·v_c) (left) (Eq 22-4). None for three: beyond the HCM's roundabout equations."""
    if lanes == 1:
        return 1380 * exp(-0.00102 * circulating_vph)
    if lanes == 2:
        return 1420 * exp(-0.00085 * circulating_vph) + 1350 * exp(-0.00092 * circulating_vph)
    return None


def tanner_entry_capacity(circulating_vph: float, headway_s: float = 2.0, critical_s: float = 4.98,
                          follow_up_s: float = 2.61) -> float:
    """Tanner's (1962) entry capacity, veh/h, against a bunched circulating stream: no gap within `headway_s` (Δ,
    the shortest headway in it: 2.0 s for one circulating lane, Akçelik / SIDRA; Troutbeck's bunched exponential),
    C = q(1 − Δq)·e^(−q(t_c − Δ)) / (1 − e^(−q·t_f)), q in veh/s. The critical and follow-up headways are those
    HCM 6 Eq 22-1 implies (t_f = 3600/1380, t_c − t_f/2 = 1/0.00102 s per 3600). Below about 800 veh/h it is near
    the HCM's curve; above, lower, as a one-lane ring's bunched traffic leaves fewer gaps than the HCM's random
    stream (the simulation's single-lane entries fall the same way)."""
    q = circulating_vph / 3600
    if q <= 0:
        return 3600 / follow_up_s
    return max(0.0, 3600 * q * (1 - headway_s * q) * exp(-q * (critical_s - headway_s)) / (1 - exp(-q * follow_up_s)))


def fastest_path_speed_kmh(radius_m: float) -> float:
    """NCHRP 672 speed on a path of this radius with a -2 % cross slope."""
    return 3.4614 * (radius_m / 0.3048) ** 0.3673 * 1.609344


def design_roundabout(network: Network, node_id: str) -> RoundaboutDesign:
    roads = [road for road in network.all_roads() if node_id in (road.from_node, road.to_node)]
    # Lanes entering the roundabout: one direction of a two-way road, all of a one-way one.
    entering = [road.lanes_each_way for road in roads if road.direction == "two-way" or road.to_node == node_id]
    entry_lanes = max(entering or [1])
    if entry_lanes > MAX_ENTRY_LANES:
        raise ValueError(f"Roundabout {node_id}: {entry_lanes} lanes enter from one road; NCHRP Report 672 designs "
                         f"roundabouts of up to {MAX_ENTRY_LANES} entry lanes. Use fewer lanes, or a signal.")
    approach_speed = max([road.speed_limit_kmh for road in roads] or [network.default_speed_limit_kmh])
    mini = entry_lanes == 1 and approach_speed <= MINI_MAX_APPROACH_KMH
    name, (low_speed, high_speed), (low_icd, high_icd) = CATEGORIES["mini" if mini else entry_lanes]
    design_speed = min(max(approach_speed, low_speed), high_speed)
    share = (design_speed - low_speed) / (high_speed - low_speed)
    inscribed = low_icd + share * (high_icd - low_icd)
    width = CIRCULATORY_SHARE_OF_ENTRY * entry_lanes * LANE_WIDTH_M
    island = inscribed - 2 * width  # at least 5.6 m: a mini's smallest ICD less its circulatory roadway
    circulating = fastest_path_speed_kmh(island / 2 + PATH_OFFSET_FROM_ISLAND_M)
    return RoundaboutDesign(name, entry_lanes, approach_speed, design_speed, inscribed, width,
                            island, circulating, mini)

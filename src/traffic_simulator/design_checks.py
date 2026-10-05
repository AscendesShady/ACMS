"""Design checks on the network as drawn (no Qt), each rule cited, for the assistant's design_audit tool.

- Sight on curves (AASHTO Green Book 2018 §3.2.2 and §3.3.12): stopping sight distance SSD = 0.278·V·t + 0.039·V²/a
  (Eq 3-2, metric; t = 2.5 s, a = 3.4 m/s²) and the clear offset it needs from the inside lane's centre on a curve of
  radius R, HSO = R·(1 − cos(28.65·SSD/R degrees)) (Eq 3-37); against the placed buildings.
- Intersection sight (§9.5.3, Case B1): along the major road b = 0.278·V·t_g (Eq 9-1) with t_g 7.5 s for a car,
  9.5 a single-unit truck, 11.5 a combination truck, 0.5 s (car) or 0.7 s (truck) more for each lane crossed beyond
  one (Table 9-6); the triangle from a decision point 4.4 m back from the major road's edge; at junctions without
  signals, against the placed buildings.
- Turning paths (§2.8, Table 2-5b): each junction turn's tightest radius (on the lane centres it follows) against the
  centreline turning radius of the largest design vehicle among the vehicle types: passenger car 6.40 m, city bus
  11.52 m, single-unit truck 11.58 m, semitrailer 12.36 m (its 13.66 m design radius less half its width, the
  table's footnote).
- Skew (§9.5.2): adjacent legs meeting at under 75 degrees.
- Junction spacing (Towards Safer Roads in Developing Countries, TRL 1991, Ch 7 checklist B item 12): junctions on an
  arterial (2 or more lanes each way, or 60 km/h or more) at least 250 m apart.
- Lane balance (§10.9.5.9): past a merge of two roads at least their lanes less one; before a diverge into two, at
  least their lanes less one.
Not checked: acceleration and deceleration lane lengths (Tables 10-4 and 10-6 aren't readable in the copy used),
grades (the network has none), access near junctions (a building's door is a junction here).
"""

from __future__ import annotations

from math import atan2, cos, degrees, hypot, pi, radians, sin, sqrt

from .geometry import polyline_meets_box
from .models import LANE_WIDTH_M, Network

REACTION_S, DECELERATION = 2.5, 3.4  # Green Book §3.2.2: brake reaction time, deceleration rate
GAPS_S = {"car": (7.5, 0.5), "single-unit truck": (9.5, 0.7), "combination truck": (11.5, 0.7)}  # Table 9-6: t_g, per lane
DECISION_BACK_M = 4.4  # the minor-road driver's eye back from the major road's edge (§9.5.3)
# Table 2-5b centreline turning radii (m) of the design vehicles, and which vehicle types they stand for
DESIGN_RADII = (("passenger car", 6.40), ("city bus", 11.52), ("single-unit truck", 11.58), ("semitrailer", 12.36))
SKEW_DEG, ARTERIAL_SPACING_M, ARTERIAL_KMH = 75.0, 250.0, 60.0


def stopping_sight_m(speed_kmh: float) -> float:
    """Green Book Eq 3-2, metric."""
    return 0.278 * speed_kmh * REACTION_S + 0.039 * speed_kmh ** 2 / DECELERATION


def sight_offset_m(sight_m: float, radius_m: float) -> float:
    """Green Book Eq 3-37, metric: the clear offset from the inside lane's centre a curve of `radius_m` needs."""
    return radius_m * (1 - cos(radians(min(28.65 * sight_m / radius_m, 180.0))))


def design_vehicle(mode) -> tuple[str, float]:
    """The design vehicle a vehicle type turns like (Table 2-5b), by its name and size."""
    name, length = mode.name.lower(), mode.length_m
    if "bus" in name or "coach" in name:
        return DESIGN_RADII[1]
    if length > 12.5:
        return DESIGN_RADII[3]
    if length > 7.5 or "truck" in name or "lorry" in name:
        return DESIGN_RADII[2]
    return DESIGN_RADII[0]


def _boxes(network: Network) -> list[tuple[str, float, float, float, float]]:
    found = []
    for item in network.institutions.values():
        half = sqrt(item.area_m2) / 2
        found.append((item.id, item.x - half, item.y - half, item.x + half, item.y + half))
    return found


def _bearing(network: Network, road, node_id: str) -> float:
    """Which way a road leaves a node, radians."""
    points = network.road_points(road)
    if road.to_node == node_id:
        points = points[::-1]
    (ax, ay), (bx, by) = points[0], points[1]
    return atan2(by - ay, bx - ax)


def _min_radius(points: list) -> float | None:
    """The tightest radius along a polyline: the circle through each three points in a row."""
    best = None
    for (ax, ay), (bx, by), (cx, cy) in zip(points, points[1:], points[2:]):
        area = abs((bx - ax) * (cy - ay) - (by - ay) * (cx - ax)) / 2
        if area < 1e-6:
            continue
        radius = hypot(bx - ax, by - ay) * hypot(cx - bx, cy - by) * hypot(cx - ax, cy - ay) / (4 * area)
        best = radius if best is None else min(best, radius)
    return best


def _inside(px: float, py: float, triangle: list) -> bool:
    (ax, ay), (bx, by), (cx, cy) = triangle
    d1 = (px - bx) * (ay - by) - (ax - bx) * (py - by)
    d2 = (px - cx) * (by - cy) - (bx - cx) * (py - cy)
    d3 = (px - ax) * (cy - ay) - (cx - ax) * (py - ay)
    return not ((d1 < 0 or d2 < 0 or d3 < 0) and (d1 > 0 or d2 > 0 or d3 > 0))


def audit(network: Network, modes: list) -> dict:
    """Every check in the module's docstring; each finding with the object, what the rule asks and what is there."""
    from .simulation import Graph  # noqa: PLC0415 - the lane graph only for the turns

    boxes = _boxes(network)
    findings: dict = {"sight_on_curves": [], "intersection_sight": [], "turning_paths": [], "skew": [],
                      "junction_spacing": [], "lane_balance": []}
    for road in network.all_roads():  # sight on curves
        bends = [abs(k) for k in road.curvature if k]
        if not bends:
            continue
        radius = 1 / max(bends)
        sight = stopping_sight_m(road.speed_limit_kmh)
        needed = sight_offset_m(sight, radius)
        points, inner = network.road_points(road), road.width_m / 2 - LANE_WIDTH_M / 2
        blocking = []
        for name, left, top, right, bottom in boxes:
            grown = needed + inner  # the clear band, from the road's centre line
            if polyline_meets_box(points, left - grown, top - grown, right + grown, bottom + grown):
                blocking.append(name)
        row = {"road": road.id, "speed_kmh": road.speed_limit_kmh, "radius_m": round(radius, 1),
               "stopping_sight_m": round(sight, 1), "clear_offset_needed_m": round(needed, 1)}
        if blocking:
            findings["sight_on_curves"].append({**row, "blocked_by": blocking})
        elif not boxes:
            findings["sight_on_curves"].append({**row, "note": "keep this clear of buildings and planting"})
    heaviest = max((design_vehicle(mode) for mode in modes), key=lambda pair: pair[1], default=DESIGN_RADII[0])
    truck = heaviest[0] in ("single-unit truck", "semitrailer")
    for node_id, node in network.nodes.items():
        roads = [road for road in network.all_roads() if node_id in (road.from_node, road.to_node)]
        if len(roads) < 2:
            continue
        bearings = sorted((_bearing(network, road, node_id), road) for road in roads)
        for (a, first), (b, second) in zip(bearings, bearings[1:] + [(bearings[0][0] + 2 * pi, bearings[0][1])]):
            angle = degrees(b - a)
            if len(roads) >= 3 and angle < SKEW_DEG:
                findings["skew"].append({"junction": node_id, "legs": [first.id, second.id], "angle_deg": round(angle, 1)})
        if node.control == "uncontrolled" and len(roads) >= 3:  # intersection sight, every approach as the minor one
            major = max(roads, key=lambda road: (road.speed_limit_kmh, road.lanes))
            kind = "combination truck" if heaviest[0] == "semitrailer" else "single-unit truck" if truck else "car"
            base, per_lane = GAPS_S[kind]
            gap = base + per_lane * max(major.lanes - 2, 0)
            along = 0.278 * major.speed_limit_kmh * gap
            if boxes:
                for minor in [road for road in roads if road is not major]:
                    angle = _bearing(network, minor, node_id)
                    back = major.width_m / 2 + DECISION_BACK_M
                    eye = (node.x + back * cos(angle), node.y + back * sin(angle))
                    for side in (_bearing(network, major, node_id), _bearing(network, major, node_id) + pi):
                        far = (node.x + along * cos(side), node.y + along * sin(side))
                        triangle = [(node.x, node.y), eye, far]
                        closed = triangle + [triangle[0]]
                        hit = [name for name, left, top, right, bottom in boxes
                               if polyline_meets_box(closed, left, top, right, bottom)
                               or _inside((left + right) / 2, (top + bottom) / 2, triangle)]
                        if hit:
                            findings["intersection_sight"].append({
                                "junction": node_id, "minor_road": minor.id, "major_road": major.id, "design_vehicle": kind,
                                "time_gap_s": gap, "sight_along_major_m": round(along, 1), "blocked_by": hit})
            else:
                findings["intersection_sight"].append({"junction": node_id, "major_road": major.id, "design_vehicle": kind,
                                                       "time_gap_s": gap, "sight_along_major_m": round(along, 1),
                                                       "note": "keep each sight triangle clear this far along the major road"})
    graph = Graph(network)  # turning paths: the lane-centre curves the simulation drives, per junction
    tight: dict = {}
    for link in graph.links.values():
        for turn in link.turns.values():
            if turn.ring or turn.kind == "straight" or turn.node not in network.nodes:
                continue
            radius = _min_radius(turn.points)
            if radius is not None and radius < heaviest[1]:
                row = tight.setdefault(turn.node, {"junction": turn.node, "turns_too_tight": 0, "tightest_m": radius,
                                                   "tightest": f"{link.road} to {turn.lane_out.link.road}"})
                row["turns_too_tight"] += 1
                if radius < row["tightest_m"]:
                    row.update(tightest_m=radius, tightest=f"{link.road} to {turn.lane_out.link.road}")
    findings["turning_paths"] = [{**row, "tightest_m": round(row["tightest_m"], 1), "design_vehicle": heaviest[0],
                                  "needs_m": heaviest[1]} for row in sorted(tight.values(), key=lambda r: r["tightest_m"])]
    for road in network.roads.values():  # junction spacing on arterials
        arterial = road.lanes_each_way >= 2 or road.speed_limit_kmh >= ARTERIAL_KMH
        if arterial and road.from_node in network.nodes and road.to_node in network.nodes and road.length_m < ARTERIAL_SPACING_M:
            findings["junction_spacing"].append({"road": road.id, "length_m": round(road.length_m, 1),
                                                 "needs_m": ARTERIAL_SPACING_M})
    for node_id, node in network.nodes.items():  # lane balance at merges and diverges without signals
        if node.control != "uncontrolled":
            continue
        arriving = [link for link in graph.links.values() if link.end == node_id]
        leaving = [link for link in graph.links.values() if link.start == node_id]
        if len(arriving) == 2 and len(leaving) == 1:
            needed = len(arriving[0].lanes) + len(arriving[1].lanes) - 1
            if len(leaving[0].lanes) < needed:
                findings["lane_balance"].append({"junction": node_id, "kind": "merge", "lanes_in": [len(l.lanes) for l in arriving],
                                                 "lanes_out": len(leaving[0].lanes), "needs_at_least": needed})
        if len(arriving) == 1 and len(leaving) == 2:
            needed = len(leaving[0].lanes) + len(leaving[1].lanes) - 1
            if len(arriving[0].lanes) < needed:
                findings["lane_balance"].append({"junction": node_id, "kind": "diverge", "lanes_in": len(arriving[0].lanes),
                                                 "lanes_out": [len(l.lanes) for l in leaving], "needs_at_least": needed})
    counts = {name: len(rows) for name, rows in findings.items()}
    return {"findings": {name: rows[:20] for name, rows in findings.items()}, "counts": counts,
            "design_vehicle": heaviest[0], "buildings_checked": len(boxes),
            "not_checked": "speed-change lane lengths, grades, access within 50 m of junctions (doors are junctions here)",
            "rules": "AASHTO Green Book 2018 (Eqs 3-2, 3-37, 9-1; Tables 2-5b, 9-6; §9.5.2, §10.9.5.9); Towards Safer "
                     "Roads in Developing Countries (TRL 1991) Ch 7 checklist B12."}

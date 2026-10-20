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
- Weaving (HCM 2000 Chapter 24, weaving.py): each merge followed within 750 m by a diverge, its type (Exhibit 24-5)
  and what that type can take (p24-9); one with no auxiliary lane between is two ramp junctions that close; a layout
  Exhibit 24-5 calls infeasible (one movement needing 1 lane change and the other 2 or more) is flagged as such.
Not checked: acceleration and deceleration lane lengths (Tables 10-4 and 10-6 aren't readable in the copy used),
grades (the network has none), access near junctions (a building's door is a junction here).

The network's shape, for the assistant's tools of the same names, View Summary and the run workbook (none of them
needing network_tools): `analyse_network` (connectivity, reachability from the ends, dead ends, short roads, curves
posted too fast, crowded roundabouts) and `network_indices` (graph indices, Rodrigue p613-639).
"""

from __future__ import annotations

import heapq
from collections import Counter, defaultdict
from math import atan2, cos, degrees, hypot, pi, radians, sin, sqrt

from . import weaving
from .geometry import polyline_meets_box
from .interchanges import curve_speed_kmh
from .models import LANE_WIDTH_M, Network, Road
from .roundabouts import design_roundabout

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
    from .lane_graph import Graph  # noqa: PLC0415 - the lane graph only for the turns

    boxes = _boxes(network)
    findings: dict = {"sight_on_curves": [], "intersection_sight": [], "turning_paths": [], "skew": [],
                      "junction_spacing": [], "lane_balance": [], "weaving": []}
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
        roads = roads_at(network, node_id)
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
    for segment in weaving.segments(graph):  # a merge closely followed by a diverge (HCM 2000 Ch 24)
        kind = segment["type"]
        note = (f"Type {kind} weave: weaving flow at most {weaving.WEAVING_FLOW_MAX[kind]:.0f} pc/h; weaving_los "
                "grades it with the demand" if kind in weaving.CONSTANTS else
                "on-ramp and off-ramp under 750 m apart with no auxiliary lane: the merge and diverge interact; "
                "an auxiliary lane between them makes it a Type A weave" if kind == "ramp junctions" else
                "not a feasible weave (Exhibit 24-5): one movement needs 1 lane change and the other 2 or more")
        findings["weaving"].append({**segment, **weaving.limits(segment), "note": note})
    counts = {name: len(rows) for name, rows in findings.items()}
    return {"findings": {name: rows[:20] for name, rows in findings.items()}, "counts": counts,
            "design_vehicle": heaviest[0], "buildings_checked": len(boxes),
            "not_checked": "speed-change lane lengths, grades, access within 50 m of junctions (doors are junctions here)",
            "rules": "AASHTO Green Book 2018 (Eqs 3-2, 3-37, 9-1; Tables 2-5b, 9-6; §9.5.2, §10.9.5.9); Towards Safer "
                     "Roads in Developing Countries (TRL 1991) Ch 7 checklist B12; HCM 2000 Ch 24 (weaving)."}


def tightest(road: Road) -> float:
    return max((abs(kappa) for kappa in road.curvature), default=0.0)


def roads_at(network: Network, node_id: str) -> list[Road]:
    return [road for road in network.all_roads() if node_id in (road.from_node, road.to_node)]


def analyse_network(network: Network) -> dict:
    findings: dict = {}
    try:
        network.validate()
        findings["valid"] = True
    except ValueError as exc:
        findings["valid"] = str(exc)
    ahead: dict[str, set[str]] = {}
    back: dict[str, set[str]] = {}
    joined: dict[str, set[str]] = {}
    for road in network.all_roads():
        pairs = [(road.from_node, road.to_node)]
        if road.direction == "two-way":
            pairs.append((road.to_node, road.from_node))
        for a, b in pairs:
            ahead.setdefault(a, set()).add(b)
            back.setdefault(b, set()).add(a)
        joined.setdefault(road.from_node, set()).add(road.to_node)
        joined.setdefault(road.to_node, set()).add(road.from_node)

    def spread(starts, links) -> set[str]:
        seen, todo = set(starts), list(starts)
        while todo:
            for nxt in links.get(todo.pop(), ()):
                if nxt not in seen:
                    seen.add(nxt)
                    todo.append(nxt)
        return seen

    every = [node.id for node in network.all_nodes()]
    parts, left = 0, set(every)
    while left:
        parts += 1
        left -= spread([next(iter(left))], joined)
    findings["separate_parts"] = parts
    entries = [end for end in network.approach_nodes if ahead.get(end)]
    exits = [end for end in network.approach_nodes if back.get(end)]
    if entries and exits:
        reached, leaving = spread(entries, ahead), spread(exits, back)
        findings["junctions_traffic_cannot_reach"] = sorted(j for j in network.nodes if j not in reached)
        findings["junctions_traffic_cannot_leave"] = sorted(j for j in network.nodes if j not in leaving)
    else:
        findings["note"] = "No boundary ends carry traffic both in and out, so reachability wasn't checked."
    findings["dead_end_junctions"] = sorted(j for j in network.nodes if len(roads_at(network, j)) == 1)
    findings["unconnected_junctions"] = sorted(j for j in network.nodes if not roads_at(network, j))
    findings["roads_under_15_m"] = sorted(road.id for road in network.all_roads() if road.length_m < 15)
    findings["curves_posted_too_fast"] = [
        {"road": road.id, "speed_limit_kmh": road.speed_limit_kmh,
         "safe_curve_speed_kmh": curve_speed_kmh(1 / tightest(road))}
        for road in network.all_roads()
        if tightest(road) and road.speed_limit_kmh > curve_speed_kmh(1 / tightest(road))]
    crowded = []
    for node in network.nodes.values():
        if node.control == "roundabout":
            radius = design_roundabout(network, node.id).inscribed_diameter_m / 2
            crowded += [{"roundabout": node.id, "road": road.id, "radius_m": round(radius, 1),
                         "road_length_m": round(road.length_m, 1)}
                        for road in roads_at(network, node.id) if road.length_m <= radius * 1.5]
    findings["roundabouts_crowding_their_roads"] = crowded
    return findings


def route_lengths(network: Network, sources) -> dict:
    """Metres of the shortest route from each source to every node it can reach, one-way roads kept."""
    ahead = defaultdict(list)
    for road in network.all_roads():
        ahead[road.from_node].append((road.to_node, road.length_m))
        if road.direction == "two-way":
            ahead[road.to_node].append((road.from_node, road.length_m))
    found = {}
    for source in sources:
        best, todo = {source: 0.0}, [(0.0, source)]
        while todo:
            metres, here = heapq.heappop(todo)
            if metres > best.get(here, float("inf")):
                continue
            for there, step in ahead[here]:
                if metres + step < best.get(there, float("inf")):
                    best[there] = metres + step
                    heapq.heappush(todo, (metres + step, there))
        found[source] = best
    return found


def network_indices(network: Network) -> dict:
    """Graph indices of the road network (Rodrigue, The Geography of Transport Systems, p613–639): v vertices
    (junctions and ends), e edges (roads), p components; cyclomatic μ = e − v + p, α = μ / (2v − 5), β = e / v,
    γ = e / (3(v − 2)) (planar forms); the diameter in links; degrees; the detour index (route km over
    straight-line km between boundary ends, one-way roads kept); Shimbel accessibility (the sum of shortest km
    from a junction to every other)."""
    nodes = {node.id: node for node in network.all_nodes()}
    roads = list(network.all_roads())
    v, e = len(nodes), len(roads)
    parent = {node: node for node in nodes}

    def root(node):
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    around = defaultdict(list)
    for road in roads:
        parent[root(road.from_node)] = root(road.to_node)
        around[road.from_node].append((road.to_node, road.length_m))
        around[road.to_node].append((road.from_node, road.length_m))
    components = len({root(node) for node in nodes})
    mu = e - v + components

    def hops(source):
        """Links to the junction farthest from source (breadth first)."""
        seen, frontier, depth = {source}, [source], -1
        while frontier:
            depth += 1
            nearer = []
            for here in frontier:
                for there, _ in around[here]:
                    if there not in seen:
                        seen.add(there)
                        nearer.append(there)
            frontier = nearer
        return depth

    def metres(source):
        best, todo = {source: 0.0}, [(0.0, source)]
        while todo:
            spent, here = heapq.heappop(todo)
            if spent > best[here]:
                continue
            for there, step in around[here]:
                if spent + step < best.get(there, float("inf")):
                    best[there] = spent + step
                    heapq.heappush(todo, (spent + step, there))
        return best

    junctions = list(network.nodes)
    shimbel = {node: sum(metres(node).get(other, 0.0) for other in junctions) / 1000 for node in junctions}
    ends = list(network.approach_nodes)
    routed = route_lengths(network, ends)
    detours = [routed[o][d] / hypot(nodes[o].x - nodes[d].x, nodes[o].y - nodes[d].y)
               for o in ends for d in ends if o != d and d in routed[o] and hypot(nodes[o].x - nodes[d].x, nodes[o].y - nodes[d].y)]
    ranked = sorted(shimbel, key=shimbel.get)
    return {"vertices": v, "edges": e, "components": components, "cyclomatic": mu,
            "alpha": round(mu / (2 * v - 5), 3) if v > 2 else None, "beta": round(e / v, 3) if v else None,
            "gamma": round(e / (3 * (v - 2)), 3) if v > 2 else None,
            "diameter_links": max((hops(node) for node in nodes), default=0),
            "degrees": dict(sorted(Counter(len(around[node]) for node in junctions).items())),
            "detour_index": round(sum(detours) / len(detours), 3) if detours else None,
            "most_accessible": {node: round(shimbel[node], 1) for node in ranked[:5]},
            "least_accessible": {node: round(shimbel[node], 1) for node in ranked[-5:][::-1]},
            "notes": ["alpha: share of the most circuits a planar graph could have (0 a tree, 1 complete); beta: roads a "
                      "node; gamma: share of the most roads possible. Shimbel: km to every other junction (lower is "
                      "more accessible)."]}

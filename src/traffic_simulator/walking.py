"""The walking network (no Qt): where pedestrians can go, the way they take, the roads they cross, and who walks.

Built from the network's footpaths (`Road.footpaths`, beside its roads: `Network.footpath_line`), its paths
(`Network.walkways`) and its crossings (`Network.pedestrian_crossings`), each walked both ways, SUMO's NonInteracting model
(references/reverse_engineering/simulation/pedestrian_system.md) for the walking itself. A footpath runs between
the corners of the junctions at its road's ends (`corner_m` from each node); at a junction the corners, the path
ends and the roads go round in angle order, and two neighbouring corners are joined free (the walking area) where no
road lies between them, or by crossing it where one does: a gap crossing (HCM 2000 Ch 18, p18-13: the walker waits
for a gap of tc = L / Sp + ts, Eq 18-17; traffic doesn't yield). A crossing splits its road's footpaths and joins
them: a zebra (right of way: traffic stops for walkers on it) or an overbridge (no conflict, its stairs climbed).
`WalkGraph.route` is the cheapest walk between two nodes (Dijkstra; from any corner of the first to any of the last,
never through a node's middle) and the crossings on it; `walk_pairs` the people walking in a run: the pedestrian OD
matrix (`Network.walk_demand`) and the institutions' walkers (`institutions.walk_demand`).
"""

from __future__ import annotations

import heapq
from collections import defaultdict
from dataclasses import dataclass, field
from math import atan2, hypot, pi
from typing import NamedTuple

from .geometry import nearest_along, point_along, polyline_length, sub_polyline
from .metro import WALK_KMH
from .models import CYCLE_LANE_M, FOOTPATH_M, Network

WALK_SPEED_DEV = 0.1  # the spread of walkers' own speeds about WALK_KMH (a speed factor, as vehicles'): an assumption
START_UP_S = 3.0  # HCM 2000 Eq 18-17's ts, a pedestrian's start-up and end clearance time (its Example Problem 4)
OVERBRIDGE_CLIMB_S = 20.0  # an overbridge's stairs, each side: about 6 m up or down at 0.3 m/s (an assumption)


class Crossed(NamedTuple):
    """A road a walk crosses: from `s0` to `s1` metres along the walk, `kind` "zebra", "overbridge" or "gap" (at a
    junction's arm, in a gap in its traffic), across `road` `distance_m` from its from_node, `length_m` across;
    `crossing` the Network.pedestrian_crossings id (None for a gap); `slow`, the share of its speed a walker keeps on it (an
    overbridge's stairs: below 1)."""
    s0: float
    s1: float
    kind: str
    road: str
    distance_m: float
    length_m: float
    crossing: str | None = None
    slow: float = 1.0


def corner_m(network: Network, node_id: str) -> float:
    """How far from a junction its footpaths end at its corners: beyond its widest road's half, a cycle lane and a
    footpath. 0 at a boundary end: its road's footpaths run to the map's edge."""
    if node_id in network.approach_nodes:
        return 0.0
    return max((road.width_m / 2 + CYCLE_LANE_M * bool(road.cycle_lanes) + FOOTPATH_M for road in network.all_roads()
                if node_id in (road.from_node, road.to_node)), default=0.0)


@dataclass
class WalkDemand:
    """People walking between two road nodes (a pedestrian OD pair, or an institution's walkers): `hourly` people an
    hour, or `daily` a day spread in the 24 hourly `shares` (Simulation.rate's fields); `start` / `finish`, a place
    before the first node or beyond the last that they walk from or on to, straight (a building and its door)."""
    origin: str
    destination: str
    hourly: float | None
    daily: float | None = None
    shares: list = field(default_factory=list)
    start: tuple | None = None
    finish: tuple | None = None
    quarters: tuple = ()
    attach: bool = False  # between two points of their own (spawn points): routed from `start` to `finish`, each
    # joining the walking network at its nearest footpath (WalkGraph.route), not from node to node  # no 15-minute profile: an even hour (Simulation.rate_now)


class WalkGraph:
    """Every walkable stretch between two points of the walking network (footpaths between corners and crossings,
    paths, corners joined across a junction's walking area, roads crossed), both ways, with each node's corners."""

    def __init__(self, network: Network) -> None:
        self.network = network
        self.ways: dict[str, list] = defaultdict(list)  # point -> [(next point, line, metres, cost, Crossed or None)]
        self.corners: dict[str, list] = defaultdict(list)  # node -> [(point, (x, y))] its footpath and path ends
        self._routes: dict[tuple, tuple | None] = {}
        self.position: dict[str, tuple] = {}  # each point of the network, where it is
        # Footpath and path pieces, the stretches a point off the network joins at: [first point, last point, line],
        # and the points joined to each so far, (metres along it, point), in order
        self.pieces: list[list] = []
        self._joined: dict[int, list] = {}
        speed = WALK_KMH / 3.6
        roads = list(network.all_roads())
        corner = {node.id: corner_m(network, node.id) for node in network.all_nodes()}
        on_road = defaultdict(list)
        for crossing in network.pedestrian_crossings.values():
            if network.crossing_problem(crossing) is None:
                on_road[crossing.road].append(crossing)
        for road in roads:
            crossings = sorted(on_road[road.id], key=lambda crossing: crossing.distance_m)
            sides = {}
            for side in road.footpaths:
                line = network.footpath_line(road, side)
                cuts = [corner[road.from_node], *(c.distance_m for c in crossings),
                        polyline_length(line) - corner[road.to_node]]
                keys = [f"{road.id}|{side}|{road.from_node}", *(f"{c.id}|{side}" for c in crossings),
                        f"{road.id}|{side}|{road.to_node}"]
                pieces = [sub_polyline(line, a, b) for a, b in zip(cuts, cuts[1:])]
                for (a, b), piece in zip(zip(keys, keys[1:]), pieces):
                    self._join(a, b, piece)
                    self.pieces.append([a, b, piece])
                self.corners[road.from_node].append((keys[0], pieces[0][0]))
                self.corners[road.to_node].append((keys[-1], pieces[-1][-1]))
                sides[side] = line
            for crossing in crossings if len(sides) == 2 else ():
                across = [point_along(sides["left"], crossing.distance_m)[:2],
                          point_along(sides["right"], crossing.distance_m)[:2]]
                metres = polyline_length(across)
                cost = metres + (2 * OVERBRIDGE_CLIMB_S * speed if crossing.kind == "overbridge" else 0.0)
                self._join(f"{crossing.id}|left", f"{crossing.id}|right", across, cost,
                           (crossing.kind, road.id, crossing.distance_m, metres, crossing.id, metres / cost))
        for way in network.walkways.values():
            line = network.walkway_points(way)
            piece = sub_polyline(line, corner[way.start], polyline_length(line) - corner[way.end])
            self._join(f"{way.id}|start", f"{way.id}|end", piece)
            self.pieces.append([f"{way.id}|start", f"{way.id}|end", piece])
            self.corners[way.start].append((f"{way.id}|start", piece[0]))
            self.corners[way.end].append((f"{way.id}|end", piece[-1]))
        for node_id, points in self.corners.items():
            self._round(node_id, points, [road for road in roads if node_id in (road.from_node, road.to_node)],
                        corner[node_id], speed)

    def _join(self, a: str, b: str, line: list, cost: float | None = None, crossed: tuple | None = None) -> None:
        metres = polyline_length(line)
        cost = metres if cost is None else cost
        self.position.setdefault(a, tuple(line[0]))
        self.position.setdefault(b, tuple(line[-1]))
        self.ways[a].append((b, line, metres, cost, crossed))
        self.ways[b].append((a, line[::-1], metres, cost, crossed))

    def _round(self, node_id: str, points: list, roads: list, corner: float, speed: float) -> None:
        """Join a node's corners round it: neighbours (in angle order) with no road between them free, with one
        a gap crossing of that road (its cost: the walk and a critical gap's wait, Eq 18-17, as a walk); at a
        boundary end the road's way on off the map counts as a road no one crosses."""
        node = self.network.node(node_id)

        def angle(x: float, y: float) -> float:
            return atan2(y - node.y, x - node.x)

        arms = []
        for road in roads:
            line = self.network.road_points(road)
            line = line if road.from_node == node_id else line[::-1]
            x, y = point_along(line, min(corner + 5.0, polyline_length(line) / 2))[:2]
            arms.append((angle(x, y), road))
            if node_id in self.network.approach_nodes:
                arms.append((angle(x, y) + pi, None))  # on off the map: never crossed or walked round
        around = sorted(points, key=lambda point: angle(*point[1]))
        if len(around) < 2:
            return
        for (key, here), (after, there) in zip(around, around[1:] + around[:1]):
            start = angle(*here)
            sweep = (angle(*there) - start) % (2 * pi) or 2 * pi
            between = [road for at, road in arms if 0 < (at - start) % (2 * pi) < sweep]
            line = [here, there]
            if not between:
                self._join(key, after, line)
            elif len(between) == 1 and between[0] is not None:
                road = between[0]
                metres = polyline_length(line)
                distance = corner if road.from_node == node_id else \
                    self.network.road_geometry_length(road) - corner
                self._join(key, after, line, 2 * metres + START_UP_S * speed,
                           ("gap", road.id, distance, metres, None, 1.0))

    def attach(self, x: float, y: float) -> str | None:
        """A point off the network (a spawn point) joined to it: a straight walk to the nearest place on a footpath or
        path, that piece split there (next to any point already joined to it). Its key; None with no footpath."""
        key = f"@{x:.3f},{y:.3f}"
        if key in self.ways:
            return key
        best = None
        for index, (a, b, line) in enumerate(self.pieces):
            if polyline_length(line) < 1e-6:
                continue
            along = nearest_along(line, (x, y))
            px, py = point_along(line, along)[:2]
            gap = hypot(px - x, py - y)
            if best is None or gap < best[0]:
                best = (gap, index, along, (px, py))
        if best is None:
            return None
        _, index, along, where = best
        a, b, line = self.pieces[index]
        joined = self._joined.setdefault(index, [(0.0, a), (polyline_length(line), b)])
        at = f"{key}|joins"
        before = max((item for item in joined if item[0] <= along), key=lambda item: item[0])
        after = min((item for item in joined if item[0] >= along and item[1] != before[1]), key=lambda item: item[0])
        self._join(before[1], at, sub_polyline(line, before[0], along))
        self._join(at, after[1], sub_polyline(line, along, after[0]))
        joined.append((along, at))
        self._join(key, at, [(x, y), where])  # the walk to it
        return key

    def _ends(self, end) -> list[str]:
        """The points a walk may start or finish at: a node's corners, or a point's join (attach)."""
        if isinstance(end, tuple):
            joined = self.attach(*end)
            return [joined] if joined else []
        return [point for point, _ in self.corners.get(end, ())]

    def route(self, origin, destination) -> tuple[list, list] | None:
        """The cheapest walk from `origin` to `destination`, each a node (from a corner of the first to a corner of
        the last) or an (x, y) point (straight to and from its nearest footpath, attach): its line and the roads it
        crosses (Crossed, in order); None if nothing joins them."""
        key = (origin, destination)
        if key not in self._routes:
            ends = set(self._ends(destination))
            best = {point: 0.0 for point in self._ends(origin)}
            came: dict = {}
            queue = [(0.0, point) for point in best]
            heapq.heapify(queue)
            found = None
            while queue:
                cost, point = heapq.heappop(queue)
                if point in ends:
                    found = point
                    break
                if cost > best[point]:
                    continue  # a dearer way to a point already reached
                for after, line, metres, step, crossed in self.ways.get(point, ()):
                    if cost + step < best.get(after, float("inf")):
                        best[after], came[after] = cost + step, (point, line, metres, crossed)
                        heapq.heappush(queue, (cost + step, after))
            if found is None:
                self._routes[key] = None
            else:
                legs, point = [], found
                while point in came:
                    point, *leg = came[point]
                    legs.append(leg)
                start = self.position[point]
                points, events, walked = [start], [], 0.0
                for line, metres, crossed in reversed(legs):
                    if crossed is not None:
                        events.append(Crossed(walked, walked + metres, *crossed))
                    points += line[1:]
                    walked += metres
                self._routes[key] = (points, events)
        return self._routes[key]


def footpath_join(network: Network, x: float, y: float) -> tuple | None:
    """The nearest place on any footpath or path to (x, y): where a spawn point's walkers join the network (as
    WalkGraph.attach finds it); None with no footpath or path."""
    lines = [network.footpath_line(road, side) for road in network.all_roads() for side in road.footpaths]
    lines += [network.walkway_points(way) for way in network.walkways.values()]
    joins = [point_along(line, nearest_along(line, (x, y)))[:2] for line in lines]
    return min(joins, key=lambda at: hypot(at[0] - x, at[1] - y), default=None)


def walk_pairs(network: Network, modes: list, daily: bool) -> list[WalkDemand]:
    """The people walking in a run: each pedestrian OD pair (people an hour, all day long in a daily run) and each
    institution's walkers (institutions.walk_demand)."""
    pairs = [WalkDemand(*key.split("->"), people) for key, people in network.walk_demand.items() if people > 0]
    points = network.spawn_points
    pairs += [WalkDemand(*key.split("->"), people, start=(points[key.split("->")[0]].x, points[key.split("->")[0]].y),
                         finish=(points[key.split("->")[1]].x, points[key.split("->")[1]].y), attach=True)
              for key, people in network.spawn_demand.items() if people > 0]
    if network.institutions:
        from .institutions import walk_demand  # noqa: PLC0415 - institutions builds on this module
        pairs += walk_demand(network, modes, daily)
    return pairs

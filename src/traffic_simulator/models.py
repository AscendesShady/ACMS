"""Core, GUI-independent data model for the urban network editor.

All geometry is vector: continuous coordinates in metres, roads as polylines
(see geometry.py). Grid row/column numbers are labels, never spatial cells.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from math import floor, hypot, isfinite, sqrt
from typing import Any

from .geometry import (curvature_at, distance_to_segment, polyline_length, polyline_meets_box, segment_hit,
                       sub_polyline)


LANE_WIDTH_M = 3.5
# A bus stop's bay (lay-by) beside the kerb: an entry taper, the stopping length (a 12 m bus with
# room), an exit taper. The app's choices, in the lay-by form of UK bus stop design guidance.
BAY_ENTRY_M, BAY_STOP_M, BAY_EXIT_M = 15.0, 15.0, 10.0
BAY_LENGTH_M = BAY_ENTRY_M + BAY_STOP_M + BAY_EXIT_M
BAY_WIDTH_M = 3.0
BUS_STOP_DIRECTIONS = ("forward", "backward", "both")  # forward: traffic from_node → to_node
MAX_DWELL_S = 600.0  # a bus stop's longest dwell time: a ten-minute layover
MAX_BOARDING_PER_H = 5000.0  # passengers arriving at one stop to board, an hour
SIGNAL_METHODS = ("webster", "transyt", "synchro", "scats", "scoot", "actuated", "max_pressure", "fixed")
BACKGROUND_KINDS = ("colour", "image", "map")
INSTITUTION_KINDS = ("Residential", "School", "Hospital", "Office", "Shop")  # institutions.KINDS details each
MAX_AREA_M2, MAX_INFLUENCE_M = 1_000_000.0, 3000.0  # a building's footprint (1 km²) and its circle's radius


def stop_line_inset(road_width: float, road_length: float) -> float:
    """How far back from a controlled junction's centre its stop line sits (and where the
    simulation stops vehicles): clear of the junction box, never past a fifth of a short road."""
    return min(road_width * 0.78, road_length * 0.22)


def lane_end_inset(control: str, road_width: float, road_length: float, widest_m: float,
                   diameter_m: float = 0.0) -> float:
    """How far back from a junction's centre the lanes of a road meeting it end: the stop line
    (signalised), the give-way line just outside the circle (roundabout, `diameter_m` its inscribed
    diameter) or the junction's edge (uncontrolled: half the widest road there). The simulation
    cuts its lanes there, and bus stop bays keep clear of it."""
    if control == "roundabout":
        return diameter_m / 2 + 0.5
    if control == "signalised":
        return stop_line_inset(road_width, road_length)
    return widest_m / 2


def bay_depth(t: float) -> float:
    """How far out of the carriageway a bus bay reaches, 0 to 1 of its width, `t` metres along it
    in the direction of travel: smooth tapers either side of the stopping length (smoothstep, so
    the path in has no kink). The map draws this shape and buses drive it."""
    if BAY_ENTRY_M <= t <= BAY_LENGTH_M - BAY_EXIT_M:
        return 1.0
    x = t / BAY_ENTRY_M if t < BAY_ENTRY_M else (BAY_LENGTH_M - t) / BAY_EXIT_M
    x = min(max(x, 0.0), 1.0)
    return x * x * (3 - 2 * x)


@dataclass(slots=True)
class Node:
    id: str
    x: float
    y: float
    row: int | None  # grid position; None for nodes added off the grid
    column: int | None
    control: str = "signalised"  # "signalised", "roundabout" or "uncontrolled" (merges, bends)
    supply_vph: float = 0.0  # boundary ends: flow entering the network
    demand_vph: float = 0.0  # boundary ends: flow leaving the network
    # Signal timing for signalised junctions (simulation.py, signal_timing.py).
    signal_phases: int = 2  # 2 or 4
    signal_method: str = "webster"  # one of SIGNAL_METHODS
    green_times_s: list[float] = field(default_factory=lambda: [30.0, 30.0])  # one per phase, for "fixed"
    # Coordinated methods (TRANSYT, Synchro, SCATS, SCOOT): the cycle's limits; the degree of saturation
    # SCATS and SCOOT aim for; the delay a stop counts as in TRANSYT's and SCOOT's performance index.
    cycle_min_s: float = 40.0
    cycle_max_s: float = 120.0
    target_saturation: float = 0.9
    stop_weight_s: float = 10.0


@dataclass(slots=True)
class Road:
    id: str
    from_node: str
    to_node: str
    length_m: float
    lanes_per_direction: int = 2
    lane_changing: bool = True
    speed_limit_kmh: float = 50.0
    direction: str = "two-way"
    road_type: str = "urban"
    # Interior points of a curved road, from_node to to_node; empty = straight.
    shape: list[tuple[float, float]] = field(default_factory=list)
    # Passes over the roads it crosses. Any road crossing another without a shared
    # node is grade-separated; this marks which one is on top (drawn as a bridge).
    overpass: bool = False
    # Exact signed curvature (1/m) of each segment of the road's points, from the curve
    # maths that built it; empty = straight. Positive bends to the (-uy, ux) side.
    curvature: list[float] = field(default_factory=list)
    # A one-way road of one lane (a single-lane slip road); lanes_per_direction is then 1.
    single_lane: bool = False

    @property
    def lanes(self) -> int:
        """Lanes across the whole road: both directions', or a one-way road's carriageway."""
        return 1 if self.single_lane else 2 * self.lanes_per_direction

    @property
    def lanes_each_way(self) -> int:
        """Lanes its traffic has in each direction it runs."""
        return self.lanes if self.direction == "one-way" else self.lanes_per_direction

    @property
    def width_m(self) -> float:
        return self.lanes * LANE_WIDTH_M


@dataclass(slots=True)
class BusStop:
    """A bus stop: a bay beside the kerb where buses pull in, stop and merge back."""
    id: str  # BS1, BS2... in the order added
    road: str
    distance_m: float  # the bay's middle, metres along the road's centre line from its from_node
    direction: str = "both"  # the traffic it serves: "forward" (from_node → to_node), "backward" or "both"
    dwell_s: float = 20.0  # how long each bus stands at it while passengers get off and on (with no passengers set)
    # Passengers (simulation.py, HCM 2000 Eq 27-2): those arriving to board, per hour in each direction it serves, and
    # the share of each bus's load getting off. Both 0: no passengers, every bus stands dwell_s.
    boarding_per_h: float = 0.0
    alighting_percent: float = 0.0


@dataclass(slots=True)
class Institution:
    """A building that makes trips (institutions.py): a square footprint clear of the roads, and a circle
    of influence whose junctions and boundary ends take its trips."""
    id: str  # IN1, IN2... in the order placed
    kind: str  # one of INSTITUTION_KINDS
    x: float  # the footprint's centre, metres
    y: float
    area_m2: float  # the square footprint's area
    radius_m: float  # the circle of influence, from the centre
    people: int = 0  # residents, students, patients and visitors, visitors or customers, by kind
    employees: int = 0
    shift_start_h: float = 8.0  # hours after midnight, 0-24
    shift_end_h: float = 17.0
    car_share: float = 60.0  # percent of people and employees who come by car
    # percent of people and employees by each mode ("Walk" and vehicle type names), from a mode split
    # (network_tools.institution_split); empty: only car_share is known (by car, the rest not on the roads)
    mode_shares: dict = field(default_factory=dict)


def institution_problem(item: Institution) -> str | None:
    """What is wrong with an institution's figures, or None."""
    numbers = (item.x, item.y, item.area_m2, item.radius_m, item.shift_start_h, item.shift_end_h, item.car_share)
    if item.kind not in INSTITUTION_KINDS:
        return f"Institution {item.id} must be one of {', '.join(INSTITUTION_KINDS)}."
    if not all(isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(value) for value in numbers):
        return f"Institution {item.id} needs numbers for its place, size, circle, shift and car share."
    if not all(isinstance(count, int) and not isinstance(count, bool) and count >= 0
               for count in (item.people, item.employees)):
        return f"Institution {item.id} needs whole numbers of people and employees, not below zero."
    if not 1 <= item.area_m2 <= MAX_AREA_M2:
        return f"Institution {item.id}'s area must be 1 to {MAX_AREA_M2:,.0f} m²."
    if not sqrt(item.area_m2) / 2 <= item.radius_m <= MAX_INFLUENCE_M:
        return (f"Institution {item.id}'s circle of influence must reach at least its own walls and at most "
                f"{MAX_INFLUENCE_M:g} m.")
    if not (0 <= item.shift_start_h < 24 and 0 <= item.shift_end_h < 24) or item.shift_start_h == item.shift_end_h:
        return f"Institution {item.id}'s shift must start and end at different times of day (00:00-23:59)."
    if not 0 <= item.car_share <= 100:
        return f"Institution {item.id}'s car share must be 0-100 %."
    shares = item.mode_shares
    if not isinstance(shares, dict) or not all(
            isinstance(name, str) and name and isinstance(value, (int, float)) and not isinstance(value, bool)
            and isfinite(value) and 0 <= value <= 100 for name, value in shares.items())             or (shares and abs(sum(shares.values()) - 100) > 0.5):
        return f"Institution {item.id}'s mode shares must be 0-100 % by mode name, adding up to 100 %."
    return None


@dataclass
class Background:
    """What the map shows behind the network: a solid colour, an image (PNG, JPG or a PDF page) laid
    at a place and width in metres, or OpenStreetMap georeferenced at a WGS84 point (map_tiles.py)."""
    kind: str = "colour"  # one of BACKGROUND_KINDS
    colour: str = "#075322"  # "#rrggbb"; also under an image or the map
    image: str = ""  # the PNG, JPG or PDF file
    page: int = 0  # a PDF's page, from 0
    x_m: float = 0.0  # the image's top-left corner, metres
    y_m: float = 0.0
    width_m: float = 1000.0  # the image's width; its height keeps its proportions
    rotation_deg: float = 0.0  # the image turned clockwise about its top-left corner
    opacity: float = 1.0  # an image's or the map's, 0-1
    lat: float = 51.5072  # WGS84 degrees of the scene point (anchor_x, anchor_y)
    lon: float = -0.1276
    anchor_x: float = 0.0
    anchor_y: float = 0.0
    map_rotation_deg: float = 0.0  # the map turned clockwise about the anchor (0: north up)
    locked: bool = False  # pinned where it is: nothing moves, turns or resizes it but Set Scale
    # Set Scale's reference line on the image: [x1, y1, x2, y2] in image pixels, then its true length in
    # metres. Empty: none. It moves with the image.
    reference: list[float] = field(default_factory=list)


def route_choice_problem(choice) -> str | None:
    """Why a route choice setting can't be used, or None."""
    if not isinstance(choice, dict):
        return "Route choice must be an object."
    if not choice:
        return None
    if set(choice) - {"theta_per_min", "routes", "link_times_s"}:
        return "Route choice takes theta_per_min, routes and link_times_s."
    theta, routes, times = choice.get("theta_per_min", 0.5), choice.get("routes", 3), choice.get("link_times_s", {})
    if not isinstance(theta, (int, float)) or not 0.01 <= theta <= 10:
        return "Route choice's theta_per_min must be 0.01 to 10."
    if not isinstance(routes, int) or isinstance(routes, bool) or not 1 <= routes <= 6:
        return "Route choice's routes must be 1 to 6."
    if not isinstance(times, dict) or any(not isinstance(v, (int, float)) or v <= 0 for v in times.values()):
        return "Route choice's link_times_s are positive seconds by link id."
    return None


@dataclass
class Network:
    """The single source of truth for world-space network geometry."""

    nodes: dict[str, Node] = field(default_factory=dict)
    roads: dict[str, Road] = field(default_factory=dict)
    approach_nodes: dict[str, Node] = field(default_factory=dict)
    approach_roads: dict[str, Road] = field(default_factory=dict)
    bus_stops: dict[str, BusStop] = field(default_factory=dict)
    institutions: dict[str, Institution] = field(default_factory=dict)  # placed in Demand Generator → Institutions
    # Each interchange (its nodes are named "DI1.1", "DI1.2", ...): [x, y, ux, uy], where it was built
    # and the freeway's direction there. A change of driving side mirrors it across the freeway's normal.
    interchanges: dict[str, list[float]] = field(default_factory=dict)
    background: Background = field(default_factory=Background)
    horizontal_corridors: int = 0
    vertical_corridors: int = 0
    default_spacing: float = 500.0
    driving_side: str = "left"
    default_speed_limit_kmh: float = 50.0
    default_lanes_per_direction: int = 2
    default_lane_changing: bool = True
    default_signal_method: str = "webster"  # what new signalised junctions are timed by (SIGNAL_METHODS)
    # Route choice (simulation.Graph.route_shares): {} = every vehicle the fastest route; else
    # {"theta_per_min": θ, "routes": k, "link_times_s": {link id ("road>" / "road<"): seconds}} (assign_routes)
    route_choice: dict = field(default_factory=dict)

    def node(self, node_id: str) -> Node:
        if node_id in self.nodes:
            return self.nodes[node_id]
        return self.approach_nodes[node_id]

    def road(self, road_id: str) -> Road:
        if road_id in self.roads:
            return self.roads[road_id]
        return self.approach_roads[road_id]

    def node_label(self, node: Node) -> str:
        """Graph coordinates "(x,y)" with bottom-left 1,1; off-grid nodes show their ID."""
        if node.row is None or node.column is None:
            return node.id
        return f"({node.column + 1},{self.horizontal_corridors - node.row})"

    def all_nodes(self):
        yield from self.nodes.values()
        yield from self.approach_nodes.values()

    def all_roads(self):
        yield from self.roads.values()
        yield from self.approach_roads.values()

    def road_points(self, road: Road) -> list[tuple[float, float]]:
        """The road's centre line from from_node to to_node, through any curve points."""
        start, end = self.node(road.from_node), self.node(road.to_node)
        return [(start.x, start.y), *((x, y) for x, y in road.shape), (end.x, end.y)]

    def curvature_at(self, road: Road, distance: float, offset: float = 0.0) -> float:
        """Signed curvature (1/m) `distance` metres along the road, `offset` metres to
        its (-uy, ux) side (a lane centre line); see geometry.curvature_at."""
        return curvature_at(self.road_points(road), road.curvature, distance, offset)

    def set_driving_side(self, side: str) -> list[str]:
        """Traffic keeps to `side` from now on, over the whole network at once. What is drawn and
        simulated from the side (lanes, signal heads, stop lines, bus bays, turns, roundabouts) follows
        it. An interchange is laid out for a side when it is built, so each one is mirrored across the
        freeway's normal where it was built: a mirror image swaps keep-left and keep-right, which makes
        it exactly the interchange built for the other side (its ramps, curves and bridges; the roads
        tying it to the rest of the network stay where they are). Other roads look the same either way.
        Returns the interchanges mirrored."""
        if side not in {"left", "right"}:
            raise ValueError("The driving side must be left or right.")
        if side == self.driving_side:
            return []
        mirrored = [name for name, axis in self.interchanges.items() if self._mirror_interchange(name, *axis)]
        self.driving_side = side
        return mirrored

    def _mirror_interchange(self, name: str, x0: float, y0: float, ux: float, uy: float) -> bool:
        """Mirror an interchange's nodes and inner roads across the line through (x0, y0) normal to
        (ux, uy). A road tying it to the rest of the network stays where it was, re-tied to the
        interchange node now at its old end. False if none of its nodes are left."""
        def mirror(x: float, y: float) -> tuple[float, float]:
            along = (x - x0) * ux + (y - y0) * uy
            return x - 2 * along * ux, y - 2 * along * uy

        members = {node.id for node in self.nodes.values() if node.id.startswith(name + ".")}
        if not members:
            return False
        before = {node_id: (self.nodes[node_id].x, self.nodes[node_id].y) for node_id in members}
        for node_id in members:
            self.nodes[node_id].x, self.nodes[node_id].y = mirror(*before[node_id])
        for road in list(self.all_roads()):
            inside = {road.from_node, road.to_node} & members
            if len(inside) == 2:
                road.shape = [mirror(x, y) for x, y in road.shape]
                road.curvature = [-kappa for kappa in road.curvature]  # a mirror image bends the other way
            elif inside:
                (end,) = inside
                there = next((node_id for node_id in members if hypot(self.nodes[node_id].x - before[end][0],
                                                                       self.nodes[node_id].y - before[end][1]) < 1e-3),
                             None)
                if there is not None and there != end:
                    self._retie(road, end, there)
        self.synchronize_road_lengths()
        return True

    def _retie(self, road: Road, old: str, new: str) -> None:
        """Move one end of a road from node `old` to node `new`, renaming it "from-to"."""
        road.from_node, road.to_node = (new if road.from_node == old else road.from_node,
                                        new if road.to_node == old else road.to_node)
        roads = self.roads if road.id in self.roads else self.approach_roads
        new_id = f"{road.from_node}-{road.to_node}"
        if new_id != road.id and new_id not in self.roads and new_id not in self.approach_roads:
            del roads[road.id]
            for stop in self.bus_stops.values():
                if stop.road == road.id:
                    stop.road = new_id
            road.id = new_id
            roads[new_id] = road

    def reverse_road(self, road_id: str) -> None:
        """Swap a road's ends. A curve's points run the other way, so its curvature
        list is reversed and changes sign."""
        road = self.road(road_id)
        for stop in self.bus_stops.values():  # measured from the other end now, serving the same traffic
            if stop.road == road_id:
                stop.distance_m = self.road_geometry_length(road) - stop.distance_m
                stop.direction = {"forward": "backward", "backward": "forward"}.get(stop.direction, stop.direction)
        road.from_node, road.to_node = road.to_node, road.from_node
        road.shape = road.shape[::-1]
        road.curvature = [-kappa for kappa in reversed(road.curvature)]

    def road_geometry_length(self, road: Road) -> float:
        return polyline_length(self.road_points(road))

    def _carry_shapes(self, before: dict[str, tuple[float, float]]) -> None:
        """Keep curves attached when their end nodes move (`before` = old positions).

        Each curve follows the similarity (move, turn, scale) that takes its old
        chord to the new one, so it keeps its form relative to its ends.
        """
        for road in self.all_roads():
            if not road.shape or not ({road.from_node, road.to_node} & before.keys()):
                continue
            start, end = self.node(road.from_node), self.node(road.to_node)
            old_a = complex(*before.get(start.id, (start.x, start.y)))
            old_b = complex(*before.get(end.id, (end.x, end.y)))
            if old_a == old_b:
                continue
            new_a = complex(start.x, start.y)
            turn = (complex(end.x, end.y) - new_a) / (old_b - old_a)
            moved = [new_a + (complex(x, y) - old_a) * turn for x, y in road.shape]
            road.shape = [(point.real, point.imag) for point in moved]
            road.curvature = [kappa / abs(turn) for kappa in road.curvature]  # scaled curve

    def synchronize_road_lengths(self) -> None:
        """Make stored physical lengths reflect the authoritative node geometry."""
        for road in self.all_roads():
            road.length_m = self.road_geometry_length(road)

    def set_road_length(self, road_id: str, new_length_m: float) -> None:
        """Change one road's length by sliding its far end along the road.

        An internal road carries the rest of its own corridor (same row or
        column) with the far node, so that corridor keeps its other lengths
        while crossing roads angle instead: a 100 m top edge over a 500 m
        bottom edge forms a trapezoid. An approach road moves only its
        exterior end.
        """
        if new_length_m <= 0:
            raise ValueError("Road length must be greater than zero.")
        road = self.road(road_id)
        start, end = self.node(road.from_node), self.node(road.to_node)
        if start.id in self.approach_nodes:
            start, end = end, start  # always slide the exterior end
        dx, dy = end.x - start.x, end.y - start.y
        old_length = self.road_geometry_length(road)
        if old_length <= 0 or hypot(dx, dy) <= 0:
            raise ValueError(f"Road {road_id} has zero-length geometry.")

        if end.id in self.approach_nodes or None in (start.row, end.row) or road.shape:
            # Boundary end, off-grid node or curve: no corridor to carry. Sliding
            # the end scales a curve by new/old, so its length comes out exact.
            moving = {end.id}
        elif start.row == end.row:
            step = end.column - start.column
            moving = {node.id for node in self.nodes.values()
                      if node.row == end.row and (node.column - start.column) * step > 0}
        elif start.column == end.column:
            step = end.row - start.row
            moving = {node.id for node in self.nodes.values()
                      if node.column == end.column and (node.row - start.row) * step > 0}
        else:
            moving = {end.id}
        # Exterior ends travel with the intersection they hang off.
        for approach in self.approach_roads.values():
            if moving & {approach.from_node, approach.to_node}:
                moving.update(node_id for node_id in (approach.from_node, approach.to_node)
                              if node_id in self.approach_nodes)

        before = {node_id: (self.node(node_id).x, self.node(node_id).y) for node_id in moving}
        scale = (float(new_length_m) - old_length) / old_length
        for node_id in moving:
            node = self.node(node_id)
            node.x += dx * scale
            node.y += dy * scale
        self._carry_shapes(before)
        self.synchronize_road_lengths()

    def move_nodes(self, node_ids: list[str], dx: float, dy: float) -> bool:
        """Translate only these nodes; roads attached to them stretch like rubber bands.

        Refuses (returns False, nothing changed) a move that would shrink any
        road below 1 m, since zero-length roads have no direction.
        """
        nodes = [self.node(node_id) for node_id in set(node_ids)]
        before = {node.id: (node.x, node.y) for node in nodes}
        for node in nodes:
            node.x += dx
            node.y += dy
        self._carry_shapes(before)
        if any(self.road_geometry_length(road) < 1.0 for road in self.all_roads()):
            after = {node.id: (node.x, node.y) for node in nodes}
            for node in nodes:
                node.x, node.y = before[node.id]
            self._carry_shapes(after)
            return False
        self.synchronize_road_lengths()
        return True

    def add_node(self, x: float, y: float) -> Node:
        """Add an off-grid intersection. Grid nodes are named by position ("2,3")
        and boundary ends by side ("1E"); added nodes can't be, so they are
        numbered N1, N2, ... in the order added and never renamed."""
        number = 1
        while f"N{number}" in self.nodes or f"N{number}" in self.approach_nodes:
            number += 1
        node = Node(id=f"N{number}", x=x, y=y, row=None, column=None, signal_method=self.default_signal_method)
        self.nodes[node.id] = node
        return node

    def connection_problem(self, from_node: str, to_node: str) -> str | None:
        """Why add_road(from_node, to_node) would be refused, or None if it's fine."""
        if from_node not in self.nodes or to_node not in self.nodes:
            return "Edges can only connect two intersections."
        if from_node == to_node:
            return "Choose two different intersections."
        if any({road.from_node, road.to_node} == {from_node, to_node} for road in self.roads.values()):
            return f"{from_node} and {to_node} are already connected."
        return None

    def crossings(self, points: list[tuple[float, float]],
                  touching: set[str] = frozenset()) -> list[tuple[float, float, float, str]]:
        """Where a planned centre line would cross existing roads, nearest first.

        Returns (distance along the line, x, y, road id). Roads that meet the line's
        own end nodes (`touching`) and touches within 2 m of either road's ends are
        not crossings: they are junctions, or too close to split.
        """
        line_length = polyline_length(points)
        found = []
        for road in self.all_roads():
            if touching & {road.from_node, road.to_node}:
                continue
            other = self.road_points(road)
            other_length = self.road_geometry_length(road)
            walked = 0.0
            for (ax, ay), (bx, by) in zip(points, points[1:]):
                segment = hypot(bx - ax, by - ay)
                other_walked = 0.0
                for (cx, cy), (dx, dy) in zip(other, other[1:]):
                    other_segment = hypot(dx - cx, dy - cy)
                    hit = segment_hit(ax, ay, bx, by, cx, cy, dx, dy)
                    if hit is not None:
                        t, u = hit
                        along, along_other = walked + t * segment, other_walked + u * other_segment
                        if 2 < along < line_length - 2 and 2 < along_other < other_length - 2:
                            found.append((along, ax + t * (bx - ax), ay + t * (by - ay), road.id))
                    other_walked += other_segment
                walked += segment
        return sorted(found)

    def split_road(self, road_id: str, node_id: str) -> list[str]:
        """Cut a road where node_id sits on it, e.g. a new junction at a crossing.

        Both parts keep the road's settings and direction, and a curve splits at its
        nearest segment. A boundary road keeps its name on the part that reaches its
        end; the inner part becomes an ordinary road.
        Returns the IDs of the two parts, from_node side first.
        """
        road = self.road(road_id)
        node = self.nodes[node_id]
        points = self.road_points(road)
        cut = min(range(len(points) - 1), key=lambda i: distance_to_segment((node.x, node.y), points[i], points[i + 1]))
        first_shape, second_shape = points[1:cut + 1], points[cut + 1:-1]
        bends = road.curvature
        first_bends, second_bends = (bends[:cut + 1], bends[cut:]) if bends else ([], [])
        settings = {key: getattr(road, key) for key in ("lanes_per_direction", "lane_changing",
                                                        "speed_limit_kmh", "direction", "road_type", "overpass", "single_lane")}
        if road_id in self.approach_roads:
            if road.from_node in self.approach_nodes:  # end -> inner: the boundary road ends at the cut
                inner = self.add_road(node_id, road.to_node)
                road.to_node, road.shape, inner.shape = node_id, first_shape, second_shape
                road.curvature, inner.curvature = first_bends, second_bends
                parts = [road, inner]
            else:  # inner -> end: the boundary road starts at the cut
                inner = self.add_road(road.from_node, node_id)
                road.from_node, road.shape, inner.shape = node_id, second_shape, first_shape
                road.curvature, inner.curvature = second_bends, first_bends
                parts = [inner, road]
        else:
            del self.roads[road_id]
            parts = [self.add_road(road.from_node, node_id), self.add_road(node_id, road.to_node)]
            parts[0].shape, parts[1].shape = first_shape, second_shape
            parts[0].curvature, parts[1].curvature = first_bends, second_bends
        for part in parts:
            for key, value in settings.items():
                setattr(part, key, value)
            part.length_m = self.road_geometry_length(part)
        for stop in self.bus_stops.values():  # a bus stop stays where it was, on the part it is on
            if stop.road == road_id:
                first = stop.distance_m <= parts[0].length_m
                stop.road = parts[0 if first else 1].id
                stop.distance_m -= 0.0 if first else parts[0].length_m
        return [part.id for part in parts]

    def add_road(self, from_node: str, to_node: str) -> Road:
        """Connect two intersections with a new two-way road."""
        problem = self.connection_problem(from_node, to_node)
        if problem:
            raise ValueError(problem)
        road = Road(id=f"{from_node}-{to_node}", from_node=from_node, to_node=to_node,
                    length_m=0.0, lanes_per_direction=self.default_lanes_per_direction,
                    lane_changing=self.default_lane_changing,
                    speed_limit_kmh=self.default_speed_limit_kmh)
        road.length_m = self.road_geometry_length(road)
        self.roads[road.id] = road
        return road

    def add_end(self, inner_id: str, x: float, y: float) -> Road:
        """Add a boundary end at (x, y) joined to an intersection by a new road.

        The side (E/W/N/S) is the direction from the intersection. The number
        keeps the side's order (E/W count up from the bottom, N/S from the
        left): between 1E and 2E it is 1.5E, past the last end 4E, before
        the first 0.5E. The end and its road share the name, fixed from then on.
        """
        if inner_id not in self.nodes:
            raise ValueError("An end must start from an intersection.")
        inner = self.nodes[inner_id]
        dx, dy = x - inner.x, y - inner.y
        if hypot(dx, dy) < 1.0:
            raise ValueError("Place the end away from its intersection.")
        if abs(dx) >= abs(dy):
            side, key = ("E" if dx > 0 else "W"), (lambda px, py: -py)  # screen y points down
        else:
            side, key = ("S" if dy > 0 else "N"), (lambda px, py: px)
        placed = []  # (position along the side, number) of this side's numbered ends
        for end in self.approach_nodes.values():
            if end.id[-1:] == side:
                try:
                    placed.append((key(end.x, end.y), float(end.id[:-1])))
                except ValueError:
                    continue  # ends from older files, e.g. "A_W_1"
        here = key(x, y)
        low = max((number for where, number in placed if where < here), default=0.0)
        high = min((number for where, number in placed if where > here), default=None)
        top = floor(max((number for _, number in placed), default=0.0)) + 1
        number = (low + high) / 2 if high is not None and low < high else top
        if f"{number:g}{side}" in self.approach_nodes:  # possible after ends were dragged out of order
            number = top
        name = f"{number:g}{side}"
        self.approach_nodes[name] = Node(id=name, x=x, y=y, row=None, column=None)
        road = Road(id=name, from_node=inner_id, to_node=name, length_m=hypot(dx, dy),
                    lanes_per_direction=self.default_lanes_per_direction,
                    lane_changing=self.default_lane_changing,
                    speed_limit_kmh=self.default_speed_limit_kmh)
        self.approach_roads[name] = road
        return road

    def delete_node(self, node_id: str) -> None:
        """Remove a node with every road touching it; ends left dangling go too."""
        self.node(node_id)  # KeyError for an unknown ID
        for road in [road for road in self.all_roads() if node_id in (road.from_node, road.to_node)]:
            self.delete_road(road.id)
        self.nodes.pop(node_id, None)
        self.approach_nodes.pop(node_id, None)

    def delete_road(self, road_id: str) -> None:
        """Remove a road with its bus stops; an approach road takes its exterior end with it."""
        road = self.road(road_id)
        if self.roads.pop(road_id, None) is None:
            del self.approach_roads[road_id]
            for node_id in (road.from_node, road.to_node):
                self.approach_nodes.pop(node_id, None)
        for stop_id in [stop.id for stop in self.bus_stops.values() if stop.road == road_id]:
            del self.bus_stops[stop_id]

    def objects_in_box(self, left: float, top: float, right: float, bottom: float,
                       crossing: bool) -> list[tuple[str, str]]:
        """What a selection box takes, AutoCAD's way: a window (`crossing` False) takes what lies
        wholly inside it, a crossing box whatever it touches. Junctions ("node") and boundary ends
        ("end") by their point, roads ("road") by their whole centre line, bus stops ("bus_stop") by
        their bay's stretch of road. In metres, y down."""
        def inside(x: float, y: float) -> bool:
            return left <= x <= right and top <= y <= bottom

        def takes(points: list[tuple[float, float]]) -> bool:
            return polyline_meets_box(points, left, top, right, bottom) if crossing else all(inside(*p) for p in points)

        found = [("node", node.id) for node in self.nodes.values() if inside(node.x, node.y)]
        found += [("end", end.id) for end in self.approach_nodes.values() if inside(end.x, end.y)]
        found += [("road", road.id) for road in self.all_roads() if takes(self.road_points(road))]
        for stop in self.bus_stops.values():
            middle = self.bus_stop_distance(stop)
            bay = sub_polyline(self.road_points(self.road(stop.road)), middle - BAY_LENGTH_M / 2, middle + BAY_LENGTH_M / 2)
            if takes(bay):
                found.append(("bus_stop", stop.id))
        return found

    def bus_stop_range(self, road: Road) -> tuple[float, float]:
        """Where a bus stop's middle may sit on this road, metres from its from_node: its whole bay
        clear of the junction at each end (where their lanes end, lane_end_inset). Empty (low above
        high) when the road is too short for a bay."""
        length = self.road_geometry_length(road)
        return (self.lane_inset(road.from_node, road) + BAY_LENGTH_M / 2,
                length - self.lane_inset(road.to_node, road) - BAY_LENGTH_M / 2)

    def lane_inset(self, node_id: str, road: Road) -> float:
        """How far back from the node the road's lanes end (lane_end_inset; 0 at a boundary end)."""
        from .roundabouts import design_roundabout  # it imports this module

        if node_id in self.approach_nodes:
            return 0.0
        node = self.nodes[node_id]
        widest = max(other.width_m for other in self.all_roads() if node_id in (other.from_node, other.to_node))
        diameter = design_roundabout(self, node_id).inscribed_diameter_m if node.control == "roundabout" else 0.0
        return lane_end_inset(node.control, road.width_m, self.road_geometry_length(road), widest, diameter)

    def bus_stop_distance(self, stop: BusStop) -> float:
        """Where the stop's bay has its middle, metres from its road's from_node: as placed, kept
        clear of the junctions if the road has changed since (mid-way between them if the bay no
        longer fits; the simulation then leaves it out)."""
        low, high = self.bus_stop_range(self.road(stop.road))
        return (low + high) / 2 if low > high else min(max(stop.distance_m, low), high)

    def bus_stop_sides(self, stop: BusStop) -> list[bool]:
        """The traffic the stop serves: True for from_node → to_node. A one-way road has only that."""
        if self.road(stop.road).direction == "one-way":
            return [True]
        return {"forward": [True], "backward": [False], "both": [True, False]}[stop.direction]

    def add_bus_stop(self, road_id: str, direction: str = "both") -> BusStop:
        """A bus stop on the road, named BS1, BS2... in the order added. The first goes mid-way
        between the junctions; each one after it in the middle of the longest free stretch between
        the stops already there, or failing that at the road end farthest from them, so no bay lands
        on another. Refused, changing nothing, where the road is too short for a bay or has no room
        left (each bay needs its whole length clear of the other stops)."""
        road = self.road(road_id)
        if direction not in BUS_STOP_DIRECTIONS:
            raise ValueError(f"A bus stop serves {', '.join(BUS_STOP_DIRECTIONS)} traffic, not {direction!r}.")
        low, high = self.bus_stop_range(road)
        if low > high:
            raise ValueError(f"Road {road_id} is too short for a bus stop: its bay needs {BAY_LENGTH_M:g} m "
                             "clear of the junctions.")
        taken = sorted(self.bus_stop_distance(stop) for stop in self.bus_stops.values() if stop.road == road_id)

        def clearance(d: float) -> float:
            return min((abs(d - other) for other in taken), default=float("inf"))

        edges = [low, *taken, high]
        spot = max(((a + b) / 2 for a, b in zip(edges, edges[1:])), key=clearance)
        if clearance(spot) < BAY_LENGTH_M:
            spot = max((low, high), key=clearance)
        if clearance(spot) < BAY_LENGTH_M:
            raise ValueError(f"Road {road_id} has no room for another bus stop: each bay needs {BAY_LENGTH_M:g} m "
                             "of the road clear of the other stops. Move one, or let one stop serve both directions.")
        number = 1
        while f"BS{number}" in self.bus_stops:
            number += 1
        stop = BusStop(f"BS{number}", road_id, spot, direction)
        self.bus_stops[stop.id] = stop
        return stop

    def move_bus_stop(self, stop_id: str, distance_m: float) -> None:
        """Slide a bus stop along its road: its middle `distance_m` from the from_node, kept clear
        of the junctions."""
        stop = self.bus_stops[stop_id]
        low, high = self.bus_stop_range(self.road(stop.road))
        if low <= high:
            stop.distance_m = min(max(float(distance_m), low), high)

    def validate(self) -> None:
        problem = route_choice_problem(self.route_choice)
        if problem:
            raise ValueError(problem)
        if not 1 <= self.horizontal_corridors <= 20:
            raise ValueError("Horizontal corridor count must be between 1 and 20.")
        if not 1 <= self.vertical_corridors <= 20:
            raise ValueError("Vertical corridor count must be between 1 and 20.")
        if self.default_spacing <= 0:
            raise ValueError("Default spacing must be positive.")
        if self.driving_side not in {"left", "right"}:
            raise ValueError("Driving side must be 'left' or 'right'.")
        if self.default_speed_limit_kmh <= 0:
            raise ValueError("Default speed limit must be positive.")
        if self.default_lanes_per_direction <= 0:
            raise ValueError("Default lanes per direction must be at least one.")
        if self.default_signal_method not in SIGNAL_METHODS:
            raise ValueError(f"Default signal timing must be one of {', '.join(SIGNAL_METHODS)}.")
        combined_nodes = {**self.nodes, **self.approach_nodes}
        combined_roads = {**self.roads, **self.approach_roads}
        if len(combined_nodes) != len(self.nodes) + len(self.approach_nodes):
            raise ValueError("Node IDs must be unique across intersections and approaches.")
        if len(combined_roads) != len(self.roads) + len(self.approach_roads):
            raise ValueError("Road IDs must be unique across internal and approach roads.")
        for key, node in combined_nodes.items():
            if key != node.id:
                raise ValueError(f"Node dictionary key does not match ID {node.id!r}.")
            if node.control not in {"signalised", "roundabout", "uncontrolled"}:
                raise ValueError(f"Node {node.id} has an invalid control type.")
            if node.supply_vph < 0 or node.demand_vph < 0:
                raise ValueError(f"Flows at {node.id} cannot be negative.")
            if node.signal_phases not in (2, 4) or len(node.green_times_s) != node.signal_phases \
                    or any(green <= 0 for green in node.green_times_s):
                raise ValueError(f"Node {node.id} needs 2 or 4 phases, each with a positive green time.")
            if node.signal_method not in SIGNAL_METHODS:
                raise ValueError(f"Node {node.id}: signal timing must be one of {', '.join(SIGNAL_METHODS)}.")
            if not 20 <= node.cycle_min_s <= node.cycle_max_s <= 240:
                raise ValueError(f"Node {node.id}: cycle limits must run from at least 20 s to at most 240 s.")
            if not 0.5 <= node.target_saturation <= 1.1 or not 0 <= node.stop_weight_s <= 120:
                raise ValueError(f"Node {node.id}: target saturation 50-110 %, stop weight 0-120 s.")
        for key, road in combined_roads.items():
            if key != road.id:
                raise ValueError(f"Road dictionary key does not match ID {road.id!r}.")
            if road.from_node not in combined_nodes or road.to_node not in combined_nodes:
                raise ValueError(f"Road {road.id} references an unknown node.")
            if road.from_node == road.to_node:
                raise ValueError(f"Road {road.id} must connect two different nodes.")
            if road.length_m <= 0 or self.road_geometry_length(road) <= 0:
                raise ValueError(f"Road {road.id} must have a positive length.")
            if road.lanes_per_direction <= 0:
                raise ValueError(f"Road {road.id} must have at least one lane per direction.")
            if road.speed_limit_kmh <= 0:
                raise ValueError(f"Road {road.id} must have a positive speed limit.")
            if road.curvature and len(road.curvature) != len(road.shape) + 1:
                raise ValueError(f"Road {road.id} needs one curvature value per segment.")
            if road.direction not in {"two-way", "one-way"}:
                raise ValueError(f"Road {road.id} has an invalid direction.")
            if road.single_lane and (road.direction != "one-way" or road.lanes_per_direction != 1):
                raise ValueError(f"Road {road.id}: only a one-way road of 1 lane per direction can be single-lane.")
        bg = self.background
        if bg.kind not in BACKGROUND_KINDS or not re.fullmatch(r"#[0-9a-fA-F]{6}", str(bg.colour)):
            raise ValueError(f"The background must be one of {', '.join(BACKGROUND_KINDS)}, with a colour like #075322.")
        if not (bg.width_m > 0 and 0 <= bg.opacity <= 1 and bg.page >= 0 and -85.06 <= bg.lat <= 85.06
                and -180 <= bg.lon <= 180 and -360 <= bg.rotation_deg <= 360 and -360 <= bg.map_rotation_deg <= 360):
            raise ValueError("The background needs a positive width, an opacity of 0-1, a page from 0, a "
                             "latitude within ±85° (Web Mercator's range) and longitude within ±180°, and "
                             "rotations within ±360°.")
        if not isinstance(bg.locked, bool) or bg.reference and not (
                len(bg.reference) == 5 and all(isinstance(value, (int, float)) for value in bg.reference)
                and bg.reference[4] > 0 and bg.reference[:2] != bg.reference[2:4]):
            raise ValueError("The background's lock must be true or false, and its reference line two different "
                             "image points and a positive length.")
        for name, axis in self.interchanges.items():
            if not (isinstance(axis, list) and len(axis) == 4 and all(isinstance(value, (int, float)) for value in axis)
                    and abs(hypot(axis[2], axis[3]) - 1) < 1e-6):
                raise ValueError(f"Interchange {name} needs where it was built and the freeway's unit direction.")
        for key, stop in self.bus_stops.items():
            if key != stop.id:
                raise ValueError(f"Bus stop dictionary key does not match ID {stop.id!r}.")
            if stop.road not in combined_roads:
                raise ValueError(f"Bus stop {stop.id} is on an unknown road.")
            if stop.direction not in BUS_STOP_DIRECTIONS or not isinstance(stop.distance_m, (int, float)):
                raise ValueError(f"Bus stop {stop.id} needs a distance in metres and a direction "
                                 f"({', '.join(BUS_STOP_DIRECTIONS)}).")
            if not isinstance(stop.dwell_s, (int, float)) or not 0 <= stop.dwell_s <= MAX_DWELL_S:
                raise ValueError(f"Bus stop {stop.id} needs a dwell time of 0 to {MAX_DWELL_S:g} s.")
            if not isinstance(stop.boarding_per_h, (int, float)) or not 0 <= stop.boarding_per_h <= MAX_BOARDING_PER_H \
                    or not isinstance(stop.alighting_percent, (int, float)) or not 0 <= stop.alighting_percent <= 100:
                raise ValueError(f"Bus stop {stop.id}: boarding 0 to {MAX_BOARDING_PER_H:g} passengers an hour, "
                                 "alighting 0 to 100 %.")
        for key, item in self.institutions.items():
            if key != item.id:
                raise ValueError(f"Institution dictionary key does not match ID {item.id!r}.")
            if (problem := institution_problem(item)) is not None:
                raise ValueError(problem)

    def configuration(self) -> dict[str, Any]:
        return {
            "horizontal_corridors": self.horizontal_corridors,
            "vertical_corridors": self.vertical_corridors,
            "default_spacing": self.default_spacing,
            "driving_side": self.driving_side,
            "default_speed_limit_kmh": self.default_speed_limit_kmh,
            "default_lanes_per_direction": self.default_lanes_per_direction,
            "default_lane_changing": self.default_lane_changing,
            "default_signal_method": self.default_signal_method,
        }

    def as_records(self) -> dict[str, list[dict[str, Any]]]:
        return (
            {
                "nodes": [asdict(item) for item in self.nodes.values()],
                "roads": [asdict(item) for item in self.roads.values()],
                "approach_nodes": [asdict(item) for item in self.approach_nodes.values()],
                "approach_roads": [asdict(item) for item in self.approach_roads.values()],
                "bus_stops": [asdict(item) for item in self.bus_stops.values()],
                "institutions": [asdict(item) for item in self.institutions.values()],
            }
        )

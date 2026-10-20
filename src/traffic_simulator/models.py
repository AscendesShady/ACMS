"""Core, GUI-independent data model for the urban network editor.

All geometry is vector: continuous coordinates in metres, roads as polylines
(see geometry.py). Grid row/column numbers are labels, never spatial cells.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field, fields
from math import floor, hypot, isfinite, radians, sqrt
from typing import Any

from .geometry import (curvature_at, distance_to_segment, nearest_along, offset_polyline, polyline_length,
                       polyline_meets_box, segment_hit, sub_polyline, rail_leg)


LANE_WIDTH_M = 3.5
# A bus stop's bay (lay-by) beside the kerb: an entry taper, the stopping length (a 12 m bus with
# room), an exit taper. The app's choices, in the lay-by form of UK bus stop design guidance.
BAY_ENTRY_M, BAY_STOP_M, BAY_EXIT_M = 15.0, 15.0, 10.0
BAY_LENGTH_M = BAY_ENTRY_M + BAY_STOP_M + BAY_EXIT_M
BAY_WIDTH_M = 3.0
CYCLE_LANE_M = 1.5  # a cycle lane's width (Road.cycle_lanes): an assumption, a common desirable minimum
FOOTPATH_M = 2.0  # a footpath's width (Road.footpaths): an assumption, room for two people to pass
MAX_WALK_DEMAND = 100_000  # people an hour between two boundary ends (Network.walk_demand)
CROSSING_KINDS = ("zebra", "overbridge")  # Edit Network → Pedestrian (Network.pedestrian_crossings)
ZEBRA_CLEAR_M = 20.0  # a crossing keeps this far from either end of its road: off the junction (an assumption)
CROSSING_APART_M = 10.0  # and this far from another crossing on the road (an assumption)
# A turn pocket (Road.pockets): 1 or 2 short lanes for the turns on one side, opening this far back from the stop
# line; the carriageway widens on its traffic's kerb side over POCKET_TAPER_M before it.
POCKET_SIDES = ("kerb", "offside")
POCKET_LENGTH_M = (15.0, 150.0)
POCKET_TAPER_M = 20.0
BUS_STOP_DIRECTIONS = ("forward", "backward", "both")  # forward: traffic from_node → to_node
MAX_DWELL_S = 600.0  # a bus stop's longest dwell time: a ten-minute layover
MAX_BOARDING_PER_H = 5000.0  # passengers arriving at one stop to board, an hour
SIGNAL_METHODS = ("webster", "transyt", "synchro", "scats", "scoot", "actuated", "max_pressure", "fixed")
BACKGROUND_KINDS = ("colour", "image", "map")
INSTITUTION_KINDS = ("Residential", "School", "Hospital", "Office", "Shop")  # institutions.KINDS details each
MAX_AREA_M2, MAX_INFLUENCE_M = 1_000_000.0, 3000.0  # a building's footprint (1 km²) and its circle's radius
BUILDING_HEIGHTS_M = {"Residential": 9.0, "School": 7.2, "Hospital": 18.0, "Office": 18.0, "Shop": 4.5}
# Metro rail (Edit Network → Metro): stops placed on the map, each line's track running stop to stop. Two stops of a
# line closer than this would have overlapping platforms (a six-car metro train is about 140 m): the app's floor.
METRO_MIN_SPACING_M = 150.0
METRO_MIN_RADIUS_M = 300.0  # the tightest curve a line's track takes: a desirable minimum for urban metro (assumption)
METRO_MAX_TURN_DEG = 89.0  # the most a leg turns from a station: trains don't take right-angle or acute bends
METRO_PLATFORM_M = 120.0  # a station's platform, on straight track: a 5-carriage train and room (an assumption)
# A curve's transitions (clothoids), each: L = v^3 / (R C) at 80 km/h, R 300 m and C 0.6 m/s^3 of lateral jerk (an
# assumption; the references hold no rail alignment standard)
METRO_TRANSITION_M = 60.0
MAX_METRO_DEMAND = 100_000.0  # passengers an hour between two stations
MAX_BUILDING_HEIGHT_M = 300.0  # visual height in metres; no traffic or demand calculation uses it


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
    # Turn pockets on the approach to the ends its traffic arrives at: end node id -> {"lanes": 1 or 2, "length_m":
    # back from the stop line, "side": "kerb" (for kerb turns) or "offside" (offside turns and U-turns)}. Relative to
    # the driving side, so a change of side keeps each pocket serving the same turns.
    pockets: dict = field(default_factory=dict)
    # Cycle lanes, bikes only, beyond the kerb lane of the traffic arriving at these ends (node ids), the whole road
    # long (Network.cycle_lane_line).
    cycle_lanes: list = field(default_factory=list)  # node ids (a bare list: number_problem skips it)
    # Footpaths along it, on its "left" and/or "right" as seen from from_node to to_node (Network.footpath_line):
    # walked by pedestrians (walking.py). reverse_road swaps the sides.
    footpaths: list = field(default_factory=list)

    def arrival_ends(self) -> tuple[str, ...]:
        """The nodes its traffic arrives at: a one-way road's to_node, a two-way road's both ends."""
        return (self.to_node,) if self.direction == "one-way" else (self.to_node, self.from_node)

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
class Walkway:
    """A path of its own for pedestrians, off the roads (from OpenStreetMap's footways, or the assistant's add_path:
    through a park, a square): from one road node (a junction or a boundary end) to another through its bends, walked as the
    footpaths beside roads are (walking.py)."""
    id: str  # P1, P2... in the order drawn
    start: str
    end: str
    bends: list = field(default_factory=list)  # [x, y] points between its ends, in metres


@dataclass(slots=True)
class SpawnPoint:
    """Where pedestrians appear and go (Demand Generator → Pedestrian spawn points): a point anywhere near the
    network; a walker walks straight from it to the nearest footpath, and from the footpath nearest its destination
    straight to that (walking.WalkGraph). Its own OD matrix: Network.spawn_demand."""
    id: str  # S1, S2... in the order placed
    x: float
    y: float


@dataclass(slots=True)
class Crossing:
    """A place to cross a road on foot, `distance_m` from its from_node, joining its two footpaths (Edit Network →
    Pedestrian): a "zebra" (pedestrians have right of way: traffic stops for anyone on it) or an "overbridge" (over
    the traffic: no conflict, but its stairs to climb)."""
    id: str  # X1, X2... in the order placed
    road: str
    distance_m: float
    kind: str = "zebra"


@dataclass(slots=True)
class MetroStop:
    """A metro station: a point on the map (metres), apart from the roads (the line runs underground or raised)."""
    id: str  # M1, M2... in the order placed: the metro's own network and OD matrix are labelled by them
    x: float
    y: float


@dataclass(slots=True)
class MetroLine:
    """A metro line: its stops in order, its track through them (each leg curving from its station at
    METRO_MIN_RADIUS_M at the least, with transitions, its stations on straight track: geometry.rail_leg) and its service (set when it is accepted;
    the defaults are assumptions, METRO_SERVICE)."""
    id: str  # ML1, ML2... in the order deployed
    stops: list[str] = field(default_factory=list)  # a stop on two lines is an interchange: riders change there
    track: list = field(default_factory=list)  # [x, y] points through its stops; [] (older files): stop to stop
    headway_min: float = 5.0  # minutes between trains each way
    speed_kmh: float = 35.0  # commercial speed, stops' dwell included
    cars_per_train: int = 5  # carriages a train (HCM 2000 Eq 27-28's N_c)
    car_capacity: int = 200  # passengers a carriage carries, standing included (Eq 27-28's P_c)
    fare_cents: float = 100.0  # a trip's fare

    @property
    def train_capacity(self) -> int:
        """Passengers a train carries: its carriages times a carriage's passengers."""
        return self.cars_per_train * self.car_capacity


# The service a line gets unless set (Edit Network → Metro's Accept dialog): assumptions, each with its range.
METRO_SERVICE = {"headway_min": (5.0, 1.0, 60.0), "speed_kmh": (35.0, 10.0, 120.0), "cars_per_train": (5, 1, 12),
                 "car_capacity": (200, 20, 400), "fare_cents": (100.0, 0.0, 5000.0)}


class MetroCrossing(ValueError):
    """A metro leg crosses another line: merge them at an interchange there (add_metro_stop(merge=True)), or not."""

    def __init__(self, line_id: str) -> None:
        super().__init__(f"This leg crosses line {line_id}: merge them at an interchange station there, or discard "
                         "this line.")
        self.line_id = line_id


def metro_line_problem(line: MetroLine) -> str | None:
    """What is wrong with a metro line's service, or None."""
    for name, (_, low, high) in METRO_SERVICE.items():
        value = getattr(line, name)
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not isfinite(value) or not low <= value <= high:
            return f"Metro line {line.id}'s {name.replace('_', ' ')} must be {low:g} to {high:g}."
    return None


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
    height_m: float | None = None  # None: explicitly assumed kind default, not a surveyed height


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
    if item.height_m is not None and (not finite(item.height_m) or not .5 <= item.height_m <= MAX_BUILDING_HEIGHT_M):
        return f"Institution {item.id}'s height must be 0.5 to {MAX_BUILDING_HEIGHT_M:g} m."
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


def finite(value) -> bool:
    """A real number: not a bool (True is no length), text, NaN or ±infinity."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(value)


def number_problem(item) -> str | None:
    """The first of a dataclass's number fields (float, int, a list of them or of points) holding anything but finite
    numbers, or None. Every range check after it then holds: NaN passes every comparison's `not`."""
    for spec in fields(item):
        if spec.type in ("float", "int", "int | None") or spec.type.startswith("list["):
            value = getattr(item, spec.name)
            if value is None and spec.type == "int | None":
                continue
            values = value if isinstance(value, list) else [value]
            if not all(finite(x) for v in values for x in (v if isinstance(v, (list, tuple)) else [v])):
                return spec.name
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
    if set(choice) - {"theta_per_min", "routes", "link_times_s", "turn_times_s"}:
        return "Route choice takes theta_per_min, routes, link_times_s and turn_times_s."
    turns = choice.get("turn_times_s", {})
    if not isinstance(turns, dict) or any(not isinstance(moves, dict) or any(
            not isinstance(v, (int, float)) or v <= 0 for v in moves.values()) for moves in turns.values()):
        return "Route choice's turn_times_s are positive seconds by link id, then next link id."
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
    metro_stops: dict[str, MetroStop] = field(default_factory=dict)  # Edit Network → Metro
    metro_lines: dict[str, MetroLine] = field(default_factory=dict)
    # The metro's own OD matrix (Demand Generator → Metro): passengers an hour, "M1->M2": people. Apart from the roads'
    # demand; institutions add their metro riders on top (metro.md).
    metro_demand: dict[str, float] = field(default_factory=dict)
    walkways: dict[str, Walkway] = field(default_factory=dict)  # paths of their own (the map's, the assistant's)
    pedestrian_crossings: dict[str, Crossing] = field(default_factory=dict)  # Edit Network → Pedestrian
    # Pedestrians an hour between boundary ends (Demand Generator → Pedestrians), "1W->1E": people. Institutions add
    # their walkers on top (institutions.walk_demand).
    walk_demand: dict[str, float] = field(default_factory=dict)
    spawn_points: dict[str, SpawnPoint] = field(default_factory=dict)  # Demand Generator → Pedestrian spawn points
    spawn_demand: dict[str, float] = field(default_factory=dict)  # people an hour between them, "S1->S2": people
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
    # Route choice (lane_graph.Graph.route_shares): {} = every vehicle the fastest route; else
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

    def kerb_side(self, road: Road, end: str) -> str:
        """Which side of the road ("left" or "right", from from_node to to_node) the kerb of the traffic arriving at
        `end` is on, for the driving side."""
        return "left" if (self.driving_side == "left") == (end == road.to_node) else "right"

    def beside_line(self, road: Road, side: str, metres: float) -> list[tuple[float, float]]:
        """A line `metres` beyond the carriageway's edge on its `side` ("left" or "right", from from_node to
        to_node), the road's whole length; where a turn pocket widens the road on that side, it moves out with the
        kerb along the pocket's taper. Cycle lanes and footpaths run along these lines."""
        line = self.road_points(road)
        sign = 1.0 if side == "right" else -1.0  # geometry.offset_polyline: positive is the (-uy, ux) side
        base = offset_polyline(line, sign * (road.width_m / 2 + metres))
        for end, pocket in road.pockets.items():
            if end not in (road.from_node, road.to_node) or self.kerb_side(road, end) != side:
                continue
            moved = offset_polyline(line, sign * (road.width_m / 2 + metres + pocket["lanes"] * LANE_WIDTH_M))
            total, length = polyline_length(base), pocket["length_m"]
            if end == road.to_node:
                base = sub_polyline(base, 0.0, total - length - POCKET_TAPER_M) + \
                    sub_polyline(moved, polyline_length(moved) - length, polyline_length(moved))
            else:
                base = sub_polyline(moved, 0.0, length) + sub_polyline(base, length + POCKET_TAPER_M, total)
        return base

    def cycle_lane_line(self, road: Road, end: str) -> list[tuple[float, float]]:
        """The centre line of the cycle lane arriving at `end` (Road.cycle_lanes), the way its traffic runs: just
        beyond the kerb lane (beside_line). The lane graph's cycle lanes and the map's are both this line."""
        line = self.beside_line(road, self.kerb_side(road, end), CYCLE_LANE_M / 2)
        return line if end == road.to_node else line[::-1]

    def footpath_line(self, road: Road, side: str) -> list[tuple[float, float]]:
        """The centre line of the road's footpath on `side`, from from_node to to_node: beyond the carriageway and
        any cycle lane on that side."""
        cycle = any(self.kerb_side(road, end) == side for end in road.cycle_lanes)
        return self.beside_line(road, side, CYCLE_LANE_M * cycle + FOOTPATH_M / 2)

    def walkway_points(self, walkway: Walkway) -> list[tuple[float, float]]:
        """A path's line: its start node, its bends, its end node."""
        a, b = self.node(walkway.start), self.node(walkway.end)
        return [(a.x, a.y), *((x, y) for x, y in walkway.bends), (b.x, b.y)]

    def add_walkway(self, start: str, end: str, bends=()) -> Walkway:
        """A path P1, P2... from node `start` to node `end` (junctions or boundary ends) through `bends`. Refused
        (ValueError), changing nothing: an unknown node, the same node twice, or a bend off the map's numbers."""
        for node_id in (start, end):
            self.node(node_id)  # KeyError for an unknown ID
        bends = [[float(x), float(y)] for x, y in bends]
        if start == end or not all(finite(v) for point in bends for v in point):
            raise ValueError("A path runs from one node to another, through bends on the map.")
        number = 1
        while f"P{number}" in self.walkways:
            number += 1
        walkway = Walkway(f"P{number}", start, end, bends)
        self.walkways[walkway.id] = walkway
        return walkway

    def crossing_problem(self, crossing: Crossing) -> str | None:
        """Why a crossing can't be where it is, or None: a kind of CROSSING_KINDS, on a known road with footpaths on
        both sides, at least ZEBRA_CLEAR_M from either end, CROSSING_APART_M from another crossing and clear of the
        road's bus bays."""
        if crossing.kind not in CROSSING_KINDS:
            return f"Crossing {crossing.id} must be a {' or an '.join(CROSSING_KINDS)}."
        try:
            road = self.road(crossing.road)
        except KeyError:
            return f"Crossing {crossing.id} is on a road that isn't there."
        if sorted(road.footpaths) != ["left", "right"]:
            return f"A crossing joins the footpaths on both sides of {road.id}: give it both first."
        length = self.road_geometry_length(road)
        if not finite(crossing.distance_m) or not ZEBRA_CLEAR_M <= crossing.distance_m <= length - ZEBRA_CLEAR_M:
            return (f"A crossing goes at least {ZEBRA_CLEAR_M:g} m from the junctions at either end of {road.id} "
                    f"({ZEBRA_CLEAR_M:g} to {max(length - ZEBRA_CLEAR_M, 0):.0f} m along it).")
        for other in self.pedestrian_crossings.values():
            if other.id != crossing.id and other.road == road.id \
                    and abs(other.distance_m - crossing.distance_m) < CROSSING_APART_M:
                return f"Too near crossing {other.id}: crossings on a road are {CROSSING_APART_M:g} m apart at least."
        for stop in self.bus_stops.values():
            if stop.road == road.id and abs(self.bus_stop_distance(stop) - crossing.distance_m) < BAY_LENGTH_M / 2 + 2:
                return f"Bus stop {stop.id}'s bay is there: place the crossing clear of it."
        return None

    def add_crossing(self, road_id: str, distance_m: float, kind: str = "zebra") -> Crossing:
        """A crossing X1, X2... on the road (crossing_problem first: a refusal, ValueError, changes nothing)."""
        number = 1
        while f"X{number}" in self.pedestrian_crossings:
            number += 1
        crossing = Crossing(f"X{number}", road_id, float(distance_m), kind)
        problem = self.crossing_problem(crossing)
        if problem:
            raise ValueError(problem)
        self.pedestrian_crossings[crossing.id] = crossing
        return crossing

    def add_spawn_point(self, x: float, y: float) -> SpawnPoint:
        """A spawn point S1, S2... at (x, y); refused (ValueError), changing nothing, off the map's numbers."""
        if not (finite(x) and finite(y)):
            raise ValueError("A spawn point needs a place on the map.")
        number = 1
        while f"S{number}" in self.spawn_points:
            number += 1
        point = SpawnPoint(f"S{number}", float(x), float(y))
        self.spawn_points[point.id] = point
        return point

    def delete_spawn_point(self, point_id: str) -> None:
        """Remove a spawn point and the people walking to or from it."""
        del self.spawn_points[point_id]
        self.spawn_demand = {pair: people for pair, people in self.spawn_demand.items()
                             if point_id not in pair.split("->")}

    def _prune_walking(self) -> None:
        """Paths and pedestrian demand whose nodes or ends are gone go with them."""
        known = set(self.nodes) | set(self.approach_nodes)
        self.walkways = {key: way for key, way in self.walkways.items() if {way.start, way.end} <= known}
        self.walk_demand = {pair: people for pair, people in self.walk_demand.items()
                            if set(pair.split("->")) <= set(self.approach_nodes)}

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
        road.pockets = {new if end == old else end: pocket for end, pocket in road.pockets.items()}
        road.cycle_lanes = [new if end == old else end for end in road.cycle_lanes]
        roads = self.roads if road.id in self.roads else self.approach_roads
        new_id = f"{road.from_node}-{road.to_node}"
        if new_id != road.id and new_id not in self.roads and new_id not in self.approach_roads:
            del roads[road.id]
            for stop in [*self.bus_stops.values(), *self.pedestrian_crossings.values()]:
                if stop.road == road.id:
                    stop.road = new_id
            road.id = new_id
            roads[new_id] = road

    def reverse_road(self, road_id: str) -> None:
        """Swap a road's ends. A curve's points run the other way, so its curvature
        list is reversed and changes sign."""
        road = self.road(road_id)
        for crossing in self.pedestrian_crossings.values():  # where it was, measured from the other end
            if crossing.road == road_id:
                crossing.distance_m = self.road_geometry_length(road) - crossing.distance_m
        for stop in self.bus_stops.values():  # measured from the other end now, serving the same traffic
            if stop.road == road_id:
                stop.distance_m = self.road_geometry_length(road) - stop.distance_m
                stop.direction = {"forward": "backward", "backward": "forward"}.get(stop.direction, stop.direction)
        road.from_node, road.to_node = road.to_node, road.from_node
        road.footpaths = sorted({"left": "right", "right": "left"}[side] for side in road.footpaths)
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
        pockets = road.pockets  # each stays at its end, on the part that reaches it
        # A cycle lane runs on along both parts: to its end on the part reaching it, to the cut on the other
        cycles, (start, finish) = list(road.cycle_lanes), (road.from_node, road.to_node)
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
            part.pockets = {end: dict(pocket) for end, pocket in pockets.items() if end in (part.from_node, part.to_node)}
        for part in parts:
            part.footpaths = list(road.footpaths)  # the same sides: both parts run the road's way
        parts[0].cycle_lanes = [start if end == start else node_id for end in cycles]
        parts[1].cycle_lanes = [finish if end == finish else node_id for end in cycles]
        for part in parts:
            part.length_m = self.road_geometry_length(part)
        for stop in [*self.bus_stops.values(), *self.pedestrian_crossings.values()]:  # where it was, on its part
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
        self._prune_walking()

    def delete_road(self, road_id: str) -> None:
        """Remove a road with its bus stops; an approach road takes its exterior end with it."""
        road = self.road(road_id)
        if self.roads.pop(road_id, None) is None:
            del self.approach_roads[road_id]
            for node_id in (road.from_node, road.to_node):
                self.approach_nodes.pop(node_id, None)
        for stop_id in [stop.id for stop in self.bus_stops.values() if stop.road == road_id]:
            del self.bus_stops[stop_id]
        self.pedestrian_crossings = {key: crossing for key, crossing in self.pedestrian_crossings.items()
                                     if crossing.road != road_id}
        self._prune_walking()

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

    def add_metro_line(self, first: tuple[float, float], second: tuple[float, float], merge: bool = False) -> MetroLine:
        """A metro line from its first two stops, named ML1, ML2... in the order deployed, the track joining them.
        Checked as add_metro_stop: a refusal (ValueError, MetroCrossing) changes nothing."""
        self._metro_stop_problem(None, *first)
        self._metro_stop_problem(first, *second)
        number = 1
        while f"ML{number}" in self.metro_lines:
            number += 1
        line = MetroLine(f"ML{number}")
        self.metro_lines[line.id] = line
        try:
            for index, (x, y) in enumerate((first, second)):
                self.add_metro_stop(line.id, x, y, merge and index == 1)
        except ValueError:
            self.delete_metro_line(line.id)
            raise
        return line

    def metro_track(self, line_id: str) -> list:
        """The line's track: its saved points, or (an older file) straight from stop to stop."""
        line = self.metro_lines[line_id]
        return [tuple(point) for point in line.track] or [(self.metro_stops[stop_id].x, self.metro_stops[stop_id].y)
                                                          for stop_id in line.stops]

    def metro_leg(self, line_id: str, x: float, y: float, heading: tuple | None = None) -> tuple[list, tuple | None]:
        """The track a stop clicked at (x, y) would add to the line (geometry.rail_leg: on from the last stop's
        platform, through transitions and a curve, straight into this one's; drawn in if the turn is too sharp; aimed
        through (x, y) along `heading` when given; its last point is where the stop goes) and the first crossing of
        another line on it, (line id, x, y, the leg's points before it), or None. ValueError, changing nothing: off the
        map's numbers, an aim that can't be met, under METRO_MIN_SPACING_M from the last stop, or crossing its own
        track."""
        line = self.metro_lines[line_id]
        if not line.stops:
            self._metro_stop_problem(None, x, y)
            return [(float(x), float(y))], None
        self._metro_stop_problem(None, x, y)
        track = self.metro_track(line_id)
        leg = rail_leg(track, x, y, METRO_MIN_RADIUS_M, radians(METRO_MAX_TURN_DEG), METRO_PLATFORM_M,
                       METRO_TRANSITION_M, heading)
        self._metro_stop_problem(track[-1], *leg[-1])
        way = [track[-1], *leg]
        if any(segment_hit(*a, *b, *c, *d) for a, b in zip(way, way[1:]) for c, d in zip(track, track[1:])):
            raise ValueError("A line can't cross itself: place the stop where the track stays clear of its own.")
        for index, (a, b) in enumerate(zip(way, way[1:])):  # the first crossing along the leg
            hits = [(hit[0], other.id) for other in self.metro_lines.values() if other.id != line_id
                    for c, d in zip(self.metro_track(other.id), self.metro_track(other.id)[1:])
                    if (hit := segment_hit(*a, *b, *c, *d))]
            if hits:
                t, other_id = min(hits)
                return leg, (other_id, a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1]), leg[:index])
        return leg, None

    def add_metro_stop(self, line_id: str, x: float, y: float, merge: bool = False,
                       heading: tuple | None = None) -> MetroStop:
        """A stop at the end of the line, named M1, M2..., where its leg from the last stop ends (metro_leg: at
        (x, y), or drawn in from a turn too sharp). A leg crossing another line raises MetroCrossing; with `merge`
        it ends at the crossing instead, in a stop on both lines (an interchange, in the other line's order), at
        least METRO_MIN_SPACING_M from the other line's stops. Refused, changing nothing, as metro_leg."""
        line = self.metro_lines[line_id]
        leg, crossing = self.metro_leg(line_id, x, y, heading)
        other = None
        if crossing is not None:
            if not merge:
                raise MetroCrossing(crossing[0])
            other_id, cx, cy, before = crossing
            other = self.metro_lines[other_id]
            if line.stops:
                last = self.metro_stops[line.stops[-1]]
                self._metro_stop_problem((last.x, last.y), cx, cy)
            near = [stop_id for stop_id in other.stops
                    if hypot(self.metro_stops[stop_id].x - cx, self.metro_stops[stop_id].y - cy) < METRO_MIN_SPACING_M]
            if near:
                raise ValueError(f"Too near {near[0]} for an interchange: lines must cross at least "
                                 f"{METRO_MIN_SPACING_M:g} m from {other_id}'s stations.")
            leg = [*before, (cx, cy)]
        track = self.metro_track(line_id) if line.stops else []
        number = 1
        while f"M{number}" in self.metro_stops:
            number += 1
        x, y = leg[-1]
        stop = MetroStop(f"M{number}", float(x), float(y))
        self.metro_stops[stop.id] = stop
        line.stops.append(stop.id)
        line.track = [[float(px), float(py)] for px, py in [*track, *leg]]
        if other is not None:  # into the other line's order, by where it is along its track
            along = self.metro_track(other.id)
            place = {stop_id: nearest_along(along, (self.metro_stops[stop_id].x, self.metro_stops[stop_id].y))
                     for stop_id in other.stops}
            at = nearest_along(along, (x, y))
            other.stops.insert(sum(metres < at for metres in place.values()), stop.id)
        return stop

    @staticmethod
    def _metro_stop_problem(after: tuple[float, float] | None, x: float, y: float) -> None:
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and isfinite(v) for v in (x, y)):
            raise ValueError("A metro stop needs a place on the map.")
        if after is not None and hypot(x - after[0], y - after[1]) < METRO_MIN_SPACING_M:
            raise ValueError(f"Too near the line's last stop: stops on a line are at least {METRO_MIN_SPACING_M:g} m "
                             "apart (their platforms would overlap).")

    def delete_metro_line(self, line_id: str) -> None:
        """Remove the line and its stops, interchanges too (off the other line, unless that would leave it under two
        stops), and the metro demand to or from the stops gone."""
        gone = set(self.metro_lines.pop(line_id).stops)
        for other in self.metro_lines.values():
            if len([stop_id for stop_id in other.stops if stop_id not in gone]) >= 2:
                other.stops = [stop_id for stop_id in other.stops if stop_id not in gone]
        gone -= {stop_id for other in self.metro_lines.values() for stop_id in other.stops}
        for stop_id in gone:
            del self.metro_stops[stop_id]
        self.metro_demand = {pair: people for pair, people in self.metro_demand.items()
                             if not set(pair.split("->")) & gone}

    def metro_length_m(self, line_id: str) -> float:
        """The line's track length, curves included."""
        return polyline_length(self.metro_track(line_id))

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
        if found := number_problem(self):
            raise ValueError(f"The network's {found} must be a finite number.")
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
            if found := number_problem(node):
                raise ValueError(f"Node {node.id}: {found} must be finite numbers.")
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
            if found := number_problem(road):
                raise ValueError(f"Road {road.id}: {found} must be finite numbers.")
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
        from .roundabouts import design_roundabout  # it imports this module

        for node in self.nodes.values():
            if node.control == "roundabout":
                design_roundabout(self, node.id)  # refuses more entry lanes than NCHRP Report 672 designs for
        bg = self.background
        if found := number_problem(bg):
            raise ValueError(f"The background's {found} must be finite numbers.")
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
            if not (isinstance(axis, list) and len(axis) == 4 and all(finite(value) for value in axis)
                    and abs(hypot(axis[2], axis[3]) - 1) < 1e-6):
                raise ValueError(f"Interchange {name} needs where it was built and the freeway's unit direction.")
        for key, stop in self.bus_stops.items():
            if key != stop.id:
                raise ValueError(f"Bus stop dictionary key does not match ID {stop.id!r}.")
            if found := number_problem(stop):
                raise ValueError(f"Bus stop {stop.id}: {found} must be a finite number.")
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
        for key, stop in self.metro_stops.items():
            if key != stop.id or not all(isinstance(v, (int, float)) and not isinstance(v, bool) and isfinite(v)
                                         for v in (stop.x, stop.y)):
                raise ValueError(f"Metro stop {stop.id} needs its own ID and a place on the map in metres.")
        on_lines = [stop_id for line in self.metro_lines.values() for stop_id in line.stops]
        for key, line in self.metro_lines.items():
            if key != line.id or not isinstance(line.stops, list) or len(line.stops) < 2:
                raise ValueError(f"Metro line {line.id} needs its own ID and two stops or more.")
            if any(stop_id not in self.metro_stops for stop_id in line.stops) or len(set(line.stops)) < len(line.stops):
                raise ValueError(f"Metro line {line.id} has a stop that isn't on the map, or one twice.")
            if not isinstance(line.track, list) or (line.track and (len(line.track) < 2 or not all(
                    isinstance(point, (list, tuple)) and len(point) == 2 and all(
                        isinstance(v, (int, float)) and not isinstance(v, bool) and isfinite(v) for v in point)
                    for point in line.track))):
                raise ValueError(f"Metro line {line.id}'s track needs two points or more, each [x, y] in metres.")
            if (problem := metro_line_problem(line)) is not None:
                raise ValueError(problem)
        if set(on_lines) != set(self.metro_stops):
            raise ValueError("Each metro stop is on a line (on two or more: an interchange).")
        for key, way in self.walkways.items():
            if key != way.id or way.start == way.end or not {way.start, way.end} <= combined_nodes.keys() \
                    or not isinstance(way.bends, list) or not all(
                        isinstance(point, (list, tuple)) and len(point) == 2 and all(finite(v) for v in point)
                        for point in way.bends):
                raise ValueError(f"Path {way.id} needs its own ID and two different nodes, its bends [x, y] metres.")
        for key, crossing in self.pedestrian_crossings.items():
            if key != crossing.id:
                raise ValueError(f"Crossing dictionary key does not match ID {crossing.id!r}.")
            if (problem := self.crossing_problem(crossing)) is not None:
                raise ValueError(problem)
        for key, point in self.spawn_points.items():
            if key != point.id or not (finite(point.x) and finite(point.y)):
                raise ValueError(f"Spawn point {point.id} needs its own ID and a place on the map in metres.")
        for pair, people in self.spawn_demand.items():
            ends = pair.split("->") if isinstance(pair, str) else []
            if len(ends) != 2 or ends[0] == ends[1] or not all(end in self.spawn_points for end in ends):
                raise ValueError(f"Spawn demand {pair!r} needs two different spawn points, as \"S1->S2\".")
            if not finite(people) or not 0 <= people <= MAX_WALK_DEMAND:
                raise ValueError(f"Spawn demand {pair} must be 0 to {MAX_WALK_DEMAND:,} people an hour.")
        for pair, people in self.walk_demand.items():
            ends = pair.split("->") if isinstance(pair, str) else []
            if len(ends) != 2 or ends[0] == ends[1] or not all(end in self.approach_nodes for end in ends):
                raise ValueError(f"Pedestrian demand {pair!r} needs two different boundary ends, as \"1W->1E\".")
            if not finite(people) or not 0 <= people <= MAX_WALK_DEMAND:
                raise ValueError(f"Pedestrian demand {pair} must be 0 to {MAX_WALK_DEMAND:,} people an hour.")
        for pair, people in self.metro_demand.items():
            ends = pair.split("->") if isinstance(pair, str) else []
            if len(ends) != 2 or ends[0] == ends[1] or not all(end in self.metro_stops for end in ends):
                raise ValueError(f"Metro demand {pair!r} needs two different stations, as \"M1->M2\".")
            if not isinstance(people, (int, float)) or isinstance(people, bool) or not isfinite(people) \
                    or not 0 <= people <= MAX_METRO_DEMAND:
                raise ValueError(f"Metro demand {pair} must be 0 to {MAX_METRO_DEMAND:,.0f} passengers an hour.")
        for road in self.all_roads():
            if not isinstance(road.footpaths, list) or len(set(road.footpaths)) < len(road.footpaths) \
                    or not set(road.footpaths) <= {"left", "right"}:
                raise ValueError(f"Road {road.id}: footpaths go on its left and/or right, once each.")
            if not isinstance(road.cycle_lanes, list) or len(set(road.cycle_lanes)) < len(road.cycle_lanes) \
                    or not set(road.cycle_lanes) <= set(road.arrival_ends()):
                raise ValueError(f"Road {road.id}: a cycle lane goes on the traffic arriving at one of its ends "
                                 f"({', '.join(road.arrival_ends())}), once each.")
            if (problem := self.pocket_problem(road)) is not None:
                raise ValueError(problem)

    def pocket_problem(self, road: Road) -> str | None:
        """Why a road's turn pockets (Road.pockets) can't be built, or None. Each is at a signalised or uncontrolled
        junction its traffic arrives at (a roundabout's entries are sized by NCHRP 672, without flares), on a road
        that allows lane changing (vehicles change into it), clear of the bus bays beside its traffic; the pockets
        and their tapers fit between the stop lines."""
        pockets = road.pockets
        if not isinstance(pockets, dict):
            return f"Road {road.id}'s turn pockets must be an object by end junction."
        if not pockets:
            return None
        if not road.lane_changing or road.single_lane:
            return f"Road {road.id} needs lane changing for a turn pocket: vehicles change lanes into it."
        length = self.road_geometry_length(road)
        room = length - self.lane_inset(road.from_node, road) - self.lane_inset(road.to_node, road)
        low, high = POCKET_LENGTH_M
        for end, pocket in pockets.items():
            if end not in road.arrival_ends():
                return f"Road {road.id}: its traffic doesn't arrive at {end}, so it can't have a turn pocket there."
            if end not in self.nodes or self.nodes[end].control == "roundabout":
                return f"Road {road.id}: turn pockets go at signalised or uncontrolled junctions, not at {end}."
            if not (isinstance(pocket, dict) and set(pocket) == {"lanes", "length_m", "side"}
                    and type(pocket["lanes"]) is int and pocket["lanes"] in (1, 2) and finite(pocket["length_m"])
                    and pocket["side"] in POCKET_SIDES):
                return f"Road {road.id}: its turn pocket at {end} needs lanes (1 or 2), length_m and side (kerb or offside)."
            if not low <= pocket["length_m"] <= high:
                return f"Road {road.id}: its turn pocket at {end} must be {low:g} to {high:g} m long."
            room -= pocket["length_m"] + POCKET_TAPER_M
            # where the carriageway is widened, metres from from_node
            zone = (length - self.lane_inset(end, road) - pocket["length_m"] - POCKET_TAPER_M, length) \
                if end == road.to_node else (0.0, self.lane_inset(end, road) + pocket["length_m"] + POCKET_TAPER_M)
            for stop in self.bus_stops.values():
                middle = self.bus_stop_distance(stop) if stop.road == road.id else None
                if middle is not None and (end == road.to_node) in self.bus_stop_sides(stop) \
                        and middle - BAY_LENGTH_M / 2 < zone[1] and middle + BAY_LENGTH_M / 2 > zone[0]:
                    return f"Road {road.id}: bus stop {stop.id}'s bay is beside its turn pocket at {end}; move one."
        if room < 0:
            return (f"Road {road.id} is too short for its turn pockets: each needs its length and {POCKET_TAPER_M:g} m "
                    "of taper between the stop lines.")
        return None

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
                "metro_stops": [asdict(item) for item in self.metro_stops.values()],
                "metro_lines": [asdict(item) for item in self.metro_lines.values()],
                "walkways": [asdict(item) for item in self.walkways.values()],
                "spawn_points": [asdict(item) for item in self.spawn_points.values()],
                "crossings": [asdict(item) for item in self.pedestrian_crossings.values()],
            }
        )

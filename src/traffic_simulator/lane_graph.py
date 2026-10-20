"""The lane graph (no Qt): a network snapshot as links, lanes, turns through junctions, bus bays and signals, with
routes and the capacities and delays routes are chosen by. simulation.Simulation drives vehicles over it; anything
that only needs routes or turns (route checks, design audits, the assistant's tools) builds a Graph(network) alone,
without a Simulation's random streams or signal plans.
"""

from __future__ import annotations

import heapq
import random
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from itertools import count
from math import atan2, cos, hypot, log, pi, sin, sqrt

from .geometry import bezier, nearest_along, offset_polyline, point_along, polyline_length, segment_hit, sub_polyline
from .interchanges import curve_speed_kmh
from .models import (BAY_ENTRY_M, BAY_LENGTH_M, BAY_STOP_M, BAY_WIDTH_M, LANE_WIDTH_M, POCKET_TAPER_M, Network,
                     bay_depth, lane_end_inset)
from .roundabouts import design_roundabout
from .signal_timing import (ALL_RED_S, AMBER_S, KERB_TURN_PER_LANE_VPH, OFFSIDE_TURN_PER_LANE_VPH,
                            SATURATION_PER_LANE_VPH, UTURN_PER_LANE_VPH, clearance, critical_saturation, effective,
                            webster)
from .stats import logit

# A give-way movement at an uncontrolled junction is driven at most this fast through it: drivers slow to look at
# the line, so a queue follows on at a yield's follow-up headway, not the open road's. Measured: the minor arm's
# capacity with nothing to give way to, about HCM 6's yield entry (3600 / t_f with t_f 2.61 s: 1,380 veh/h, as
# Eq 22-1); kept at the line's 1,974 veh/h it was twice HCM's at light major flows (audit 2026-10-13, M3).
GIVE_WAY_KMH = 17.0
STORAGE_M_PER_VEHICLE = 7.5  # a queued car and its gap (5 m and 2.5 m): a lane of L metres holds L / 7.5 (storage_ratio)
# Side friction for a turn at a junction (AASHTO Green Book, low-speed turning roadways and intersection curves:
# about 0.32-0.38 at 20-30 km/h): v = sqrt(127 f R) km/h, no superelevation
JUNCTION_TURN_FRICTION = 0.35
# A schematic path is tighter than a car steers: a turn is driven at least this fast, the speed of the path a car
# takes (kerb about 20 m, crossing the junction about 28 m at that friction). Calibrated so turning lanes discharge as
# the HCM has it (2026-10-13): kerb 1,773 veh/h a lane, 0.87 of straight-on (HCM 2000 f_RT 0.85); offside 1,862,
# 0.91 (HCM f_LT 0.95 protected, RR67 0.87-0.91); at one 25 km/h floor they were 0.78 and 0.76
JUNCTION_TURN_MIN_KMH = {"kerb": 30.0, "offside": 35.0, "uturn": 25.0, "straight": 25.0}
# A road's class decides an uncontrolled junction's major road, as SUMO's netconvert ranks OSM highway types
# (data/typemap/osmNetconvert.typ.xml: motorway 13 ... living_street 3; a *_link one below its road). Roads the
# app makes ("urban", "ramp") are 0, so a grid's major road is still the pair most nearly in line.
ROAD_PRIORITY = {"motorway": 13, "trunk": 12, "primary": 11, "secondary": 10, "tertiary": 9, "unclassified": 5,
                 "residential": 4, "living_street": 3, "service": 2, "motorway_link": 12, "trunk_link": 11,
                 "primary_link": 10, "secondary_link": 9, "tertiary_link": 8}
TURN_SATURATION_VPH = {"straight": SATURATION_PER_LANE_VPH, "kerb": KERB_TURN_PER_LANE_VPH,
                       "offside": OFFSIDE_TURN_PER_LANE_VPH, "uturn": UTURN_PER_LANE_VPH}
# Give-way capacity, measured on the simulated drivers (one approach held saturated against random priority
# traffic, 3 seeds of 50 min a point; test_simulation re-measures some): (veh/h it gives way to, veh/h).
# Priority: re-measured 2026-10-13 with drivers slowing to GIVE_WAY_KMH at the line (3 seeds of 20 min a point,
# one and two lanes averaged per lane): within about 10 % of HCM 2000 Eq 17-3 for a minor right turn (t_c 6.2 s,
# t_f 3.3 s: 1091, 654, 388, 228 at 0, 400, 800, 1200), where before (kept at speed to the line) it was 1.8-2.2
# times HCM's at light priority flows and sized grids of give-way junctions to demand they jammed under. With every
# arm of the crossing loaded (turns too) the minor arm carries as much or more. A single-lane roundabout entry follows HCM 6 Eq 22-1 (roundabouts.hcm_entry_capacity)
# within 10 % up to about 800 veh/h circulating (1458, 1235, 1007, 762, 546 at 0–800); beyond, it falls faster
# (358 at 1000, 168 at 1200: −28 %, −59 %), and a two-lane entry more (1449, 1191, 936, 684, 461, 256, 100). Not
# the ring filling (Krauss allows 1,600–1,700 veh/h at its speed; it carries 1,350–1,450): its traffic bunches at
# headways of 2.25–2.5 s, and the HCM's Siegloch curve assumes a random stream. Tanner's formula with that bunching
# (roundabouts.tanner_entry_capacity) gives the same fall (341 and 208 at Δ 2.25 s; measured 2026-10-13). No speed
# or RING_TIMEGAP_S lifts 1000–1200 without breaking 10 % at 0–600 (the follow-up and Δ both come from Krauss).
PRIORITY_CAPACITY_VPH = ((0, 1265), (200, 950), (400, 670), (600, 500), (800, 370), (1000, 260), (1200, 185),
                         (1400, 155), (1600, 105))  # per lane of a minor arm (one and two lanes alike)
ROUNDABOUT_ENTRY_VPH = ((0, 1455), (200, 1215), (400, 970), (600, 725), (800, 505), (1000, 305),
                        (1200, 135))  # per entry, one and two lanes averaged: the ring is one path round
ROUTE_PENALTY = 1.5  # route choice's alternatives: a found route's links cost this much more in the next search
# At a ramp junction, no U-turn on a road this fast or faster: a freeway has no median opening (FHWA's median U-turn,
# FHWA-SA-14-069, is an arterial's); slower, a slip road's junction keeps it for two-phase signals' detours
RAMP_UTURN_MAX_KMH = 80.0
# A bus drives this fast in its bay, the bay's middle this far from its kerbside lane's (half a lane
# plus half the bay).
BAY_SPEED = 20 / 3.6
BAY_OFFSET_M = LANE_WIDTH_M / 2 + BAY_WIDTH_M / 2


def measured_capacity(table: tuple, conflicting_vph: float) -> float:
    """Capacity against this much priority traffic: straight lines between the measured points, held
    at the last point beyond them."""
    for (x0, c0), (x1, c1) in zip(table, table[1:]):
        if conflicting_vph <= x1:
            return c0 + (c1 - c0) * max(conflicting_vph - x0, 0.0) / (x1 - x0)
    return table[-1][1]


@dataclass(eq=False)
class Lane:
    link: Link
    index: int  # 0: kerbside
    points: list
    length: float
    start: float = 0.0  # a turn pocket's lane: where the pocket opens, metres along it (vehicles change in past it)
    vehicles: list = field(default_factory=list)  # ordered by position, rearmost first

    @property
    def speed(self) -> float:
        return self.link.speed

    @property
    def name(self) -> str:
        return f"{self.link.id} lane {self.index}"


@dataclass(eq=False)
class Link:
    id: str
    road: str
    start: str  # node ids
    end: str
    speed: float  # m/s, the road's limit, capped for its tightest bend (Graph._add_links)
    lane_changing: bool
    length: float = 0.0
    lanes: list = field(default_factory=list)
    turns: dict = field(default_factory=dict)  # (lane index, next link id) -> Turn
    cut_start: float = 0.0  # how far from each end node its lanes are cut back (stop or give-way line)
    cut_end: float = 0.0
    pockets: tuple = (0, 0)  # turn pocket lanes at its end (Road.pockets): (kerbside, offside), first and last in lanes
    # Its cycle lane (Road.cycle_lanes), bikes only: a Lane of index -1 beyond the kerb lane, not among `lanes`, so
    # nothing that picks or changes a vehicle's lane ever offers it; and the turns bikes take into or out of a cycle
    # lane, (lane index, next link id) -> Turn, apart from `turns` for the same reason.
    cycle: object = None
    bike_turns: dict = field(default_factory=dict)

    def all_turns(self) -> list:
        """Every turn out of it, bikes' too: what a vehicle crossing or merging with another has to look out for."""
        return [*self.turns.values(), *self.bike_turns.values()]



@dataclass(eq=False)
class Turn:
    node: str
    lane_in: Lane
    lane_out: Lane
    kind: str  # "kerb", "straight", "offside" or "uturn"
    permitted: bool  # by the lane rules; otherwise only after impatience
    points: list
    length: float
    speed: float  # m/s through the curve
    vehicles: list = field(default_factory=list)
    conflicts: list = field(default_factory=list)  # (other turn, metres along here, along there, give way?)
    ring: tuple | None = None  # roundabout: (metres on, metres off, angle on, angle off, direction, radius)

    @property
    def name(self) -> str:
        return f"junction {self.node}: {self.lane_in.name} to {self.lane_out.name}"


@dataclass(eq=False)
class Bay:
    """A bus stop's bay beside a link's kerbside lane: a bus leaves the lane `entry_s` metres along
    it, stops with its front `stop_s` metres along the bay for the stop's `dwell_s`, and rejoins the
    lane at `exit_s`."""
    stop: str  # the bus stop's id
    lane: Lane
    entry_s: float
    exit_s: float
    points: list
    length: float
    stop_s: float
    dwell_s: float
    boarding_per_h: float = 0.0  # passengers arriving to board (the stop's), and the share of a bus's load alighting
    alighting_share: float = 0.0
    vehicles: list = field(default_factory=list)

    @property
    def link(self) -> Link:
        return self.lane.link

    @property
    def speed(self) -> float:
        return min(self.lane.speed, BAY_SPEED)

    @property
    def name(self) -> str:
        return f"bus stop {self.stop} beside {self.lane.name}"



class Signal:
    """A signalised junction's phases in turn, each green, then amber, then all-red. Two phases:
    east-west arms, then north-south. Four: each arm on its own.

    Fixed-time (Webster, TRANSYT, Synchro, the greens entered): the same greens every cycle, the first
    phase's green starting `offset` seconds into each. Adaptive (SCATS, SCOOT): its `controller` is
    told as each cycle and each phase starts, and may change the greens (and so the cycle) from then.
    Its amber and all-red are the junction's own (signal_timing.clearance)."""

    def __init__(self, node, arms: dict[str, tuple[float, float]], amber: float = AMBER_S,
                 all_red: float = ALL_RED_S) -> None:
        self.amber, self.all_red, self.span = amber, all_red, amber + all_red
        # arms: arriving link id -> unit vector from the junction out along that arm
        if node.signal_phases == 2:
            groups = [[link for link, (ux, uy) in arms.items() if abs(ux) >= abs(uy)],
                      [link for link, (ux, uy) in arms.items() if abs(ux) < abs(uy)]]
        else:
            groups = [[link] for link in sorted(arms, key=lambda link: atan2(arms[link][1], arms[link][0]))]
        groups = [group for group in groups if group]
        greens = node.green_times_s or [30.0]
        self.phases = [(set(group), max(1.0, greens[i % len(greens)])) for i, group in enumerate(groups)]
        self.phase_of = {link: i for i, (group, _) in enumerate(self.phases) for link in group}
        self.cycle = sum(green + self.span for _, green in self.phases)
        self.offset = 0.0  # when the first phase's green starts in the cycle
        self.controller = None  # an adaptive method's junction (signal_timing), or None: fixed-time
        self._index = 0  # adaptive: the phase running, when its green began, when its cycle began
        self._start = self._cycle_start = 0.0

    def set_greens(self, greens: list[float]) -> None:
        """Use these greens, one per phase (a timing method's result)."""
        self.phases = [(group, green) for (group, _), green in zip(self.phases, greens)]
        self.cycle = sum(green + self.span for _, green in self.phases)

    def set_green(self, index: int, green: float) -> None:
        """One phase's green, as a controller decides it while it runs (actuated, max-pressure)."""
        self.phases[index] = (self.phases[index][0], green)
        self.cycle = sum(each + self.span for _, each in self.phases)

    def adapt(self, controller, t: float = 0.0) -> None:
        """Hand the signal to an adaptive controller from time t, where its offset puts it."""
        self.controller = None
        self._index, into = self._position(t)
        self._start, self._cycle_start = t - into, self.cycle_start(t)
        self.controller = controller

    def _position(self, t: float) -> tuple[int, float]:
        """(phase running at time t, seconds since its green began)."""
        if self.controller is None:
            into = (t - self.offset) % self.cycle
            for index, (_, green) in enumerate(self.phases):
                if into < green + self.span:
                    return index, into
                into -= green + self.span
            return len(self.phases) - 1, into
        while t >= self._start + self.phases[self._index][1] + self.span - 1e-9:  # times only move on
            self._start += self.phases[self._index][1] + self.span
            finished = self._index
            choose = getattr(self.controller, "next_phase", None)  # max-pressure, actuated: not always the next in turn
            self._index = choose(self, finished, self._start) if choose else (finished + 1) % len(self.phases)
            if self._index == 0:
                self._cycle_start = self._start
                self.controller.cycle_starts(self, self._start)
            self.controller.phase_starts(self, self._index, self._start)
        return self._index, t - self._start

    def cycle_start(self, t: float) -> float:
        """When the cycle running at time t began (its first phase's green)."""
        if self.controller is None:
            return t - (t - self.offset) % self.cycle
        self._position(t)
        return self._cycle_start

    def state(self, link_id: str, t: float) -> str:
        """"G", "Y" or "R" for the arm arriving on this link."""
        index, into = self._position(t)
        group, green = self.phases[index]
        if link_id not in group:
            return "R"
        return "G" if into < green else "Y" if into < green + self.amber else "R"



def _unit(points: list, at_end: bool) -> tuple[float, float]:
    """Direction of travel at a polyline's start or end."""
    (ax, ay), (bx, by) = (points[-2], points[-1]) if at_end else (points[0], points[1])
    length = hypot(bx - ax, by - ay) or 1.0
    return (bx - ax) / length, (by - ay) / length


def path_size_logit(paths: list, times: list[float], theta_per_min: float) -> list[float]:
    """Each route's share by path-size logit (Ben-Akiva & Bierlaire): P ∝ exp(−θ·T/60 + ln PS),
    PS = Σ_a (l_a / L) / (routes using a), so routes sharing links aren't counted as separate options."""
    users = Counter(link.id for path in paths for link in {link.id: link for link in path}.values())
    utilities = {}
    for index, (path, time) in enumerate(zip(paths, times)):
        length = sum(link.length for link in path)
        size = sum(link.length / length / users[link.id] for link in {link.id: link for link in path}.values())
        utilities[index] = -theta_per_min * time / 60 + log(size)
    shares = logit(utilities)
    return [shares[index] for index in range(len(paths))]


class Graph:
    """Links, lanes, turns, signals and routes, built from a network snapshot."""

    def __init__(self, network: Network) -> None:
        self.network = network
        self.keep = 1.0 if network.driving_side == "left" else -1.0
        self.links: dict[str, Link] = {}
        self.leaving: dict[str, list[Link]] = defaultdict(list)  # node -> links starting there
        self.arriving: dict[str, list[Link]] = defaultdict(list)
        self.roundabouts = {node.id: design_roundabout(network, node.id)
                            for node in network.nodes.values() if node.control == "roundabout"}
        roads_at: dict[str, list] = defaultdict(list)
        for road in network.all_roads():
            roads_at[road.from_node].append(road)
            roads_at[road.to_node].append(road)
        self.widest = {node: max(r.width_m for r in roads) for node, roads in roads_at.items()}
        for road in network.all_roads():
            self._add_links(road)
        self.bays = self._add_bays()
        self.signals: dict[str, Signal] = {}
        for node in network.nodes.values():
            self._add_turns(node.id)
            if node.control == "signalised" and self.arriving[node.id]:
                arms = {}
                for link in self.arriving[node.id]:
                    ux, uy = _unit(link.lanes[0].points, at_end=True)
                    arms[link.id] = (-ux, -uy)
                # Its amber and all-red: from its fastest approach, across it stop line to far stop line
                arriving = self.arriving[node.id]
                self.signals[node.id] = Signal(node, arms, *clearance(
                    max(link.speed for link in arriving),
                    2 * max(self._trim(node.id, network.road(link.road)) for link in arriving)))
            self._add_conflicts(node.id)
        self._add_detours()
        self.control_delay = self._control_delays()
        self._routes: dict[tuple[str, str], list[Link] | None] = {}
        self.choice = dict(getattr(network, "route_choice", None) or {})  # empty: every vehicle the fastest route
        self._shares: dict[tuple[str, str], list] = {}

    def _control_delays(self) -> dict[tuple[str, str], float]:
        """(link id, next link id) -> the move's expected control delay with no traffic, seconds: free-flow routing's
        junction cost (route), at the greens entered. A signal: the uniform delay at X → 0, r²/2C (HCM 2000 Eq
        16-11), r = C − its phase's effective green g (shown green + amber used − start-up lost). A movement that gives way to another arm's (a minor arm, an offside turn
        across the major road, a U-turn, a merge; not a diverge from its own road) or a roundabout entry: the HCM's
        control delay at no conflicting flow, 3600/c + 5 s (HCM 2000 Eq 17-38: the 5 s to slow down and pull away),
        c the measured capacity at no priority traffic (PRIORITY_CAPACITY_VPH, ROUNDABOUT_ENTRY_VPH). A turn's own length is not counted: a wide offside curve
        would look cheaper than a tight kerb one."""
        delays = {}
        for link in self.links.values():
            signal = self.signals.get(link.end)
            red = max(signal.cycle - effective(signal.phases[signal.phase_of[link.id]][1]), 0.0) \
                if signal is not None and link.id in signal.phase_of else 0.0
            for (_, after), turn in link.turns.items():
                delay = red * red / (2 * signal.cycle) if red else 0.0
                if link.end in self.roundabouts:
                    delay += 3600 / ROUNDABOUT_ENTRY_VPH[0][1] + 5
                elif any(give_way and other.lane_in.link is not link for other, *_, give_way in turn.conflicts):
                    delay += 3600 / PRIORITY_CAPACITY_VPH[0][1] + 5
                delays[(link.id, after)] = max(delays.get((link.id, after), 0.0), delay)
        return delays

    def _two_phase(self, node_id: str) -> bool:
        """A two-phase signal: no phase protects a turn across oncoming traffic, so there is none."""
        node = self.network.nodes.get(node_id)  # junctions only: boundary ends have no signal
        return node is not None and node.control == "signalised" and node.signal_phases == 2

    def _kind(self, arrive: Link, leave: Link) -> str:
        """How the movement from one link onto the next turns: "uturn" (back along its road),
        "straight", "kerb" or "offside" (across oncoming traffic), by the angle between them."""
        if leave.road == arrive.road:
            return "uturn"
        ux, uy = _unit(arrive.lanes[0].points, at_end=True)
        vx, vy = _unit(leave.lanes[0].points, at_end=False)
        cross, dot = ux * vy - uy * vx, ux * vx + uy * vy
        if dot > cos(pi * 35 / 180):
            ramp = [self.network.road(link.road).road_type == "ramp" for link in (arrive, leave)]
            if ramp[0] == ramp[1]:
                return "straight"
            # A ramp leaving or joining a road along it (a slip road's taper): kerb-side if its first or last
            # 10 m bend that way, judged on the chord since the two meet tangentially.
            if ramp[1]:
                points = leave.lanes[0].points
                x, y, *_ = point_along(points, 10.0)
                cross = ux * (y - points[0][1]) - uy * (x - points[0][0])
            else:
                points = arrive.lanes[0].points
                x, y, *_ = point_along(points, max(polyline_length(points) - 10.0, 0.0))
                cross = (points[-1][0] - x) * vy - (points[-1][1] - y) * vx
            return "kerb" if cross * self.keep < 0 else "straight"
        return "kerb" if cross * self.keep < 0 else "offside"

    def landmarks(self, route: list[Link]) -> list[tuple[int, str, str, float]]:
        """What a vehicle on this route heads for, in order: each roundabout, signalised junction and
        slip road (a ramp, from a road that isn't one) as (the leg whose end it is at, kind, id,
        metres from the route's start to that leg's end)."""
        found, walked = [], 0.0
        for leg, (link, after) in enumerate(zip(route, route[1:])):
            walked += link.length
            node = self.network.nodes.get(link.end)
            if node is not None and node.control == "roundabout":
                found.append((leg, "roundabout", node.id, walked))
            elif link.end in self.signals:
                found.append((leg, "signal", link.end, walked))
            elif self.network.road(after.road).road_type == "ramp" and self.network.road(link.road).road_type != "ramp":
                found.append((leg, "slip road", after.road, walked))
        return found

    @staticmethod
    def _joins(link: Link, after: Link) -> bool:
        return any(next_id == after.id for _, next_id in link.turns)

    def _add_detours(self) -> None:
        """A two-phase signal has no offside turns. Instead a vehicle goes straight on to the next
        junction, U-turns there, comes back and turns kerb-side (a median U-turn). Where the road
        ahead can't take it (a boundary end, a one-way road), it turns kerb-side, U-turns at that
        junction and comes back straight on. `route` uses these in place of the missing turn."""
        self.detours: dict[str, list] = defaultdict(list)  # arriving link id -> [(leaving link, (out, back))]
        for node_id in self.network.nodes:
            if not self._two_phase(node_id):
                continue
            for arrive in self.arriving[node_id]:
                for leave in self.leaving[node_id]:
                    if self._kind(arrive, leave) != "offside":
                        continue
                    ways = ((out, back) for first in ("straight", "kerb") for out in self.leaving[node_id]
                            if self._kind(arrive, out) == first for back in self.leaving[out.end]
                            if back.road == out.road and self._joins(out, back) and self._joins(back, leave))
                    way = next(ways, None)
                    if way:
                        self.detours[arrive.id].append((leave, way))

    def _trim(self, node_id: str, road) -> float:
        """How far back from the node its lanes stop: the stop or give-way line."""
        if node_id in self.network.approach_nodes:
            return 0.0
        node = self.network.node(node_id)
        width = road.width_m
        diameter = self.roundabouts[node_id].inscribed_diameter_m if node.control == "roundabout" else 0.0
        return lane_end_inset(node.control, width, self.network.road_geometry_length(road),
                              self.widest.get(node_id, width), diameter)

    def _add_bays(self) -> dict[str, list[Bay]]:
        """Each bus stop's bay beside the kerbside lane of each link it serves, shaped as the map
        draws it (bay_depth): it leaves the lane half a bay before the stop's middle and rejoins it
        half a bay after. A lane too short for the whole bay gets none. Link id -> bays in order."""
        bays: dict[str, list[Bay]] = defaultdict(list)
        network, steps = self.network, 40
        stop_at = round((BAY_ENTRY_M + BAY_STOP_M - 1.0) / BAY_LENGTH_M * steps)  # a bus's front, near the end
        for stop in network.bus_stops.values():
            road = network.road(stop.road)
            middle = point_along(network.road_points(road), network.bus_stop_distance(stop))[:2]
            for forward in network.bus_stop_sides(stop):
                link = self.links.get(f"{road.id}{'>' if forward else '<'}")
                if link is None or link.lanes[0].length < BAY_LENGTH_M + 1.0:
                    continue
                lane = link.lanes[link.pockets[0]]  # the kerbside lane (a kerb pocket's is beyond it, clear of the bay)
                entry = min(max(nearest_along(lane.points, middle) - BAY_LENGTH_M / 2, 0.5),
                            lane.length - 0.5 - BAY_LENGTH_M)
                points = []
                for i in range(steps + 1):  # out from the lane towards the kerb side, and back
                    t = BAY_LENGTH_M * i / steps
                    x, y, ux, uy = point_along(lane.points, entry + t)
                    out = self.keep * BAY_OFFSET_M * bay_depth(t)
                    points.append((x + uy * out, y - ux * out))
                bays[link.id].append(Bay(stop.id, lane, entry, entry + BAY_LENGTH_M, points, polyline_length(points),
                                         polyline_length(points[:stop_at + 1]), stop.dwell_s, stop.boarding_per_h,
                                         stop.alighting_percent / 100))
        for link_bays in bays.values():
            link_bays.sort(key=lambda bay: bay.entry_s)
        return bays

    def _add_links(self, road) -> None:
        points = self.network.road_points(road)
        length = polyline_length(points)
        count = road.lanes_each_way
        half = count / 2 if road.direction == "one-way" else count  # lanes between the centre line and the kerb
        directions = [True] if road.direction == "one-way" else [True, False]
        for forward in directions:
            start, end = (road.from_node, road.to_node) if forward else (road.to_node, road.from_node)
            line = points if forward else points[::-1]
            cut_start, cut_end = self._trim(start, road), self._trim(end, road)
            if cut_start + cut_end > length - 2.0:  # a short road: share what there is
                scale = max(length - 2.0, 0.0) / (cut_start + cut_end)
                cut_start, cut_end = cut_start * scale, cut_end * scale
            # The limit, capped for the road's tightest bend on its innermost lane (a lane offset towards a bend's
            # centre is tighter, geometry.curvature_at), however high the limit is edited.
            offsets = [-self.keep * (half - 0.5 - index) * LANE_WIDTH_M for index in range(count)]
            kappas = road.curvature if forward else [-kappa for kappa in road.curvature]  # run the other way
            bend = max((abs(kappa / (1 - kappa * offset)) for kappa in kappas for offset in offsets
                        if kappa * offset < 1), default=0.0)
            speed = min(road.speed_limit_kmh, curve_speed_kmh(1 / bend)) if bend else road.speed_limit_kmh
            link = Link(f"{road.id}{'>' if forward else '<'}", road.id, start, end, speed / 3.6,
                        road.lane_changing, length, cut_start=cut_start, cut_end=cut_end)
            pocket = road.pockets.get(end)
            extra = pocket["lanes"] if pocket else 0
            kerb = extra if pocket and pocket["side"] == "kerb" else 0
            link.pockets = (kerb, extra - kerb)
            for index, offset in enumerate(offsets):
                lane_line = offset_polyline(line, offset)
                if extra and not kerb:  # an offside pocket: the lanes move over to the widened kerb side along a taper
                    moved = offset_polyline(line, offset - self.keep * extra * LANE_WIDTH_M)
                    lane_line = sub_polyline(lane_line, 0.0, polyline_length(lane_line) - cut_end - pocket["length_m"]
                                             - POCKET_TAPER_M) + \
                        sub_polyline(moved, polyline_length(moved) - cut_end - pocket["length_m"], polyline_length(moved))
                piece = sub_polyline(lane_line, cut_start, polyline_length(lane_line) - cut_end)
                link.lanes.append(Lane(link, index + kerb, piece, polyline_length(piece)))
            # The pocket's lanes, kerbside or offside of those: lanes of the whole link (positions along them match
            # the others') that vehicles may use only from `start`, where the pocket opens.
            for index in range(0, kerb) if kerb else range(count, count + extra):
                lane_line = offset_polyline(line, -self.keep * (half + extra - 0.5 - index) * LANE_WIDTH_M)
                piece = sub_polyline(lane_line, cut_start, polyline_length(lane_line) - cut_end)
                lane_length = polyline_length(piece)
                link.lanes.append(Lane(link, index, piece, lane_length, max(lane_length - pocket["length_m"], 0.0)))
            link.lanes.sort(key=lambda lane: lane.index)
            if end in road.cycle_lanes:
                cycle = self.network.cycle_lane_line(road, end)
                piece = sub_polyline(cycle, cut_start, polyline_length(cycle) - cut_end)
                link.cycle = Lane(link, -1, piece, polyline_length(piece))
            self.links[link.id] = link
            self.leaving[start].append(link)
            self.arriving[end].append(link)

    def _add_turns(self, node_id: str) -> None:
        node = self.network.node(node_id)
        ring = self.roundabouts.get(node_id)
        # Where a ramp joins or leaves a road without a signal (an interchange's or a slip road's), the road is a divided
        # one: nothing crosses its other carriageway or turns back on it (a diamond's on-ramp had joined the opposite
        # carriageway across the freeway, and the freeway's traffic crossed it into the other side's off-ramp)
        ramp_junction = node.control == "uncontrolled" and any(
            self.network.road(link.road).road_type == "ramp" for link in (*self.arriving[node_id], *self.leaving[node_id]))
        ramp_lanes = self._ramp_lanes(node_id) if ramp_junction else {}
        for arrive in self.arriving[node_id]:
            for leave in self.leaving[node_id]:
                kind = self._kind(arrive, leave)
                if ramp_junction and (kind == "offside" or kind == "uturn" and arrive.speed >= RAMP_UTURN_MAX_KMH / 3.6):
                    continue  # an arterial's slip road keeps the median U-turn a two-phase signal's detour needs
                if kind == "uturn" and not self._two_phase(arrive.start):
                    continue  # U-turns only where a detour needs one (_add_detours)
                if kind == "offside" and self._two_phase(node_id):
                    continue  # nothing protects it from oncoming traffic: vehicles detour instead
                across = kind in ("offside", "uturn")  # both leave from the offside lane
                count_in, count_out = len(arrive.lanes), len(leave.lanes)
                kerb_in, off_in = arrive.pockets
                first, last = leave.pockets[0], count_out - leave.pockets[1] - 1  # into its whole lanes, not a pocket
                for lane in arrive.lanes:
                    pocket = "kerb" if lane.index < kerb_in else "offside" if lane.index >= count_in - off_in else ""
                    if kerb_in or off_in:  # a pocket's lanes take its side's turns; straight on from the others
                        permitted = kind == "kerb" if pocket == "kerb" else across if pocket else (
                            kind == "straight" or (kind == "kerb" and not kerb_in and lane.index == 0)
                            or (across and not off_in and lane.index == count_in - 1))
                    else:  # where the road forbids lane changes, drivers can't pick a lane: any lane may turn
                        permitted = (count_in == 1 or not arrive.lane_changing or kind == "straight"
                                     or (kind == "kerb" and lane.index == 0) or (across and lane.index == count_in - 1))
                    if kind == "kerb":  # two pocket lanes turn side by side
                        target = first + (min(lane.index, last - first) if pocket == "kerb" else 0)
                    elif across:
                        target = last - (min(count_in - 1 - lane.index, last - first) if pocket == "offside" else 0)
                    else:
                        target = first + min(max(lane.index - kerb_in, 0), last - first)
                    if (fixed := ramp_lanes.get((arrive.id, leave.id))) is not None:
                        target, permitted = fixed[lane.index][0], fixed[lane.index][1] or not arrive.lane_changing
                    out = leave.lanes[target]
                    if ring:
                        turn = self._ring_turn(node, ring, lane, out, kind, permitted)
                    else:
                        turn = self._curve_turn(node_id, lane, out, kind, permitted)
                    arrive.turns[(lane.index, leave.id)] = turn
                if arrive.cycle is not None or leave.cycle is not None:
                    # Bikes: out of a cycle lane any way its road's traffic may go, into the next cycle lane, else the
                    # kerbside whole lane; into a cycle lane from each lane, as that lane's own turn is permitted.
                    out = leave.cycle if leave.cycle is not None else leave.lanes[first]
                    starts = [(arrive.cycle, True)] if arrive.cycle is not None else \
                        [(lane, arrive.turns[(lane.index, leave.id)].permitted) for lane in arrive.lanes]
                    for lane, permitted in starts:
                        arrive.bike_turns[(lane.index, leave.id)] = (
                            self._ring_turn(node, ring, lane, out, kind, permitted) if ring
                            else self._curve_turn(node_id, lane, out, kind, permitted))

    def _ramp_lanes(self, node_id: str) -> dict:
        """(arriving link, leaving link) -> [(lane out, permitted) for each lane in], where a ramp joins or leaves a road
        along it on the kerb side (lane-to-lane connections, as SUMO's NBEdge::Connection, laid out as the
        HCM 2000 Ch 24 draws ramps): joining, the ramp takes
        the kerbside lanes and the road the offside ones, so a lane added between them is the auxiliary lane a weave
        uses; leaving, the ramp is reached from as many kerbside lanes as it has and the road on from as many offside
        ones. At most one lane is shared (lane balance, Green Book §10.9.5.9): a ramp wider than that uses its kerbside
        lanes only. Other lanes' turns are kept, not permitted (only after impatience), so the strategic lane changes
        (`reach`) do the weaving. Off-ramps had been reached from every lane, and the road's lanes had landed on the
        kerb side with the on-ramp's: nothing changed lanes."""
        ramp = {link.id: self.network.road(link.road).road_type == "ramp"
                for link in (*self.arriving[node_id], *self.leaving[node_id])}
        found = {}

        def split(pairs: list) -> tuple | None:  # (the road, the ramp) of two movements, by kind and road type
            kinds = {pair: self._kind(*pair) for pair in pairs}
            mains = [pair for pair, kind in kinds.items() if kind == "straight" and not (ramp[pair[0].id] or ramp[pair[1].id])]
            sides = [pair for pair, kind in kinds.items() if kind == "kerb"]
            return (mains[0], sides[0]) if len(pairs) == 2 and len(mains) == 1 and len(sides) == 1 else None

        def through(pair) -> bool:
            return self._kind(*pair) not in ("uturn", "offside") and not any(link.pockets != (0, 0) for link in pair)

        for leave in self.leaving[node_id]:  # merges: two links into this one
            pairs = split([(arrive, leave) for arrive in self.arriving[node_id] if through((arrive, leave))])
            if pairs and ramp[pairs[1][0].id]:
                n = len(leave.lanes)
                (main, _), (side, _) = pairs
                usable = max(1, min(len(side.lanes), n + 1 - len(main.lanes)))  # lane balance: one lane shared at most
                found[(side.id, leave.id)] = [(min(i, n - 1), i < usable) for i in range(len(side.lanes))]
                found[(main.id, leave.id)] = [(min(max(n - len(main.lanes) + i, 0), n - 1), True)
                                              for i in range(len(main.lanes))]
        for arrive in self.arriving[node_id]:  # diverges: this link into two
            pairs = split([(arrive, leave) for leave in self.leaving[node_id] if through((arrive, leave))])
            if pairs and ramp[pairs[1][1].id]:
                n = len(arrive.lanes)
                (_, main), (_, side) = pairs
                ramp_n, main_n = len(side.lanes), len(main.lanes)
                usable = max(1, min(ramp_n, n + 1 - main_n))
                found[(arrive.id, side.id)] = [(min(j, ramp_n - 1), j < usable) for j in range(n)]
                found[(arrive.id, main.id)] = [(min(max(j - (n - main_n), 0), main_n - 1), j >= n - main_n)
                                               for j in range(n)]
        return found

    def _curve_turn(self, node_id: str, lane: Lane, out: Lane, kind: str, permitted: bool) -> Turn:
        p0, p3 = lane.points[-1], out.points[0]
        u0, u3 = _unit(lane.points, at_end=True), _unit(out.points, at_end=False)
        chord = hypot(p3[0] - p0[0], p3[1] - p0[1])
        pull = chord * 0.4
        if kind == "uturn":  # swing out into the junction to turn round, not on the stop lines
            node = self.network.node(node_id)
            pull = max(pull, (node.x - p0[0]) * u0[0] + (node.y - p0[1]) * u0[1])
        points = bezier(p0, (p0[0] + u0[0] * pull, p0[1] + u0[1] * pull),
                        (p3[0] - u3[0] * pull, p3[1] - u3[1] * pull), p3)
        angle = abs(atan2(u0[0] * u3[1] - u0[1] * u3[0], u0[0] * u3[0] + u0[1] * u3[1]))
        radius = chord / (2 * sin(angle / 2)) if angle > 0.05 else float("inf")
        # A junction turn's speed from AASHTO's low-speed turning friction (JUNCTION_TURN_FRICTION, no superelevation),
        # not the open road's 0.21: at that, turning lanes discharged at 53-56 % of straight-on, where the HCM has
        # 85-95 % (audit 2026-10-13, M4). The path is schematic, often tighter than a car steers: at least 15 km/h.
        bend = max(sqrt(127 * JUNCTION_TURN_FRICTION * radius), JUNCTION_TURN_MIN_KMH.get(kind, 25.0)) / 3.6             if radius < 1e6 else lane.speed
        speed = min(lane.speed, out.speed, bend)
        return Turn(node_id, lane, out, kind, permitted, points, polyline_length(points), speed)

    def _ring_turn(self, node, design, lane: Lane, out: Lane, kind: str, permitted: bool) -> Turn:
        cx, cy = node.x, node.y
        radius = max(design.inscribed_diameter_m / 2 - design.circulatory_width_m / 2, 2.0)
        spin = 1.0 if self.keep > 0 else -1.0  # angle increases clockwise on screen (y down)
        p0, p3 = lane.points[-1], out.points[0]
        on = atan2(p0[1] - cy, p0[0] - cx) + spin * 0.25
        off = atan2(p3[1] - cy, p3[0] - cx) - spin * 0.25
        sweep = ((off - on) * spin) % (2 * pi)
        if sweep < 0.2:
            sweep += 2 * pi
        pieces = max(4, int(sweep * radius / 3))
        arc = [(cx + radius * cos(on + spin * sweep * i / pieces), cy + radius * sin(on + spin * sweep * i / pieces))
               for i in range(pieces + 1)]
        tangent_on = (-sin(on) * spin, cos(on) * spin)
        tangent_off = (-sin(on + spin * sweep) * spin, cos(on + spin * sweep) * spin)
        u0, u3 = _unit(lane.points, at_end=True), _unit(out.points, at_end=False)
        k = design.circulatory_width_m
        entry = bezier(p0, (p0[0] + u0[0] * k, p0[1] + u0[1] * k),
                       (arc[0][0] - tangent_on[0] * k, arc[0][1] - tangent_on[1] * k), arc[0], 8)
        exit_ = bezier(arc[-1], (arc[-1][0] + tangent_off[0] * k, arc[-1][1] + tangent_off[1] * k),
                       (p3[0] - u3[0] * k, p3[1] - u3[1] * k), p3, 8)
        points = entry + arc[1:] + exit_[1:]
        on_m = polyline_length(entry)
        speed = min(lane.speed, out.speed, curve_speed_kmh(radius) / 3.6)
        turn = Turn(node.id, lane, out, kind, permitted, points, polyline_length(points), speed)
        turn.ring = (on_m, on_m + sweep * radius, on, on + spin * sweep, spin, radius)
        return turn

    def _add_conflicts(self, node_id: str) -> None:
        """Where turns through this junction cross or merge, and who gives way to whom."""
        if node_id in self.roundabouts:
            return  # the ring decides (Simulation._ring_clear)
        node = self.network.node(node_id)
        turns = [turn for link in self.arriving[node_id] for turn in link.all_turns()]
        signal = self.signals.get(node_id)
        major = self._major_arms(node_id) if node.control == "uncontrolled" else set()
        for first in turns:
            for second in turns:
                if first.lane_in is second.lane_in:
                    continue  # the same lane splitting to different exits: no conflict
                spot = self._meeting(first, second)
                if spot is None:
                    continue
                same_green = signal is not None and \
                    signal.phase_of.get(first.lane_in.link.id) == signal.phase_of.get(second.lane_in.link.id)
                if "uturn" in (first.kind, second.kind):  # a U-turn gives way to all (on its green)
                    give_way = first.kind == "uturn" and second.kind != "uturn" and (signal is None or same_green)
                elif signal:
                    give_way = same_green and first.kind == "offside" and second.kind != "offside"
                else:
                    first_major = first.lane_in.link.id in major and first.lane_out.link.id in major
                    second_major = second.lane_in.link.id in major and second.lane_out.link.id in major
                    # HCM 2000's movement ranks (Ch 17): the major road first; among the minor movements an offside
                    # turn (rank 4) gives way to the other minor arm's straight and kerb movements (rank 3), which
                    # first-come had let it cross, so neither the run nor the capacity model counted that flow
                    give_way = (not first_major and second_major) or \
                        (not first_major and not second_major and first.kind == "offside"
                         and second.kind in ("straight", "kerb"))
                first.conflicts.append((second, spot[0], spot[1], give_way))
        if signal is None:  # a give-way movement: drivers slow to look at the line (GIVE_WAY_KMH)
            for turn in turns:
                if any(give_way for *_, give_way in turn.conflicts):
                    turn.speed = min(turn.speed, GIVE_WAY_KMH / 3.6)

    def _major_arms(self, node_id: str) -> set[str]:
        """An uncontrolled junction's major road: the two arms of the highest road class (ROAD_PRIORITY, as SUMO's
        netconvert ranks OSM types), the most nearly in line among them. A grid's roads are one class: in line."""
        arms: dict[str, tuple[float, float]] = {}
        for link in self.arriving[node_id]:
            ux, uy = _unit(link.lanes[0].points, at_end=True)
            arms[link.road] = (-ux, -uy)
        for link in self.leaving[node_id]:
            arms.setdefault(link.road, _unit(link.lanes[0].points, at_end=False))
        roads = list(arms)
        if len(roads) < 3:
            pair = set(roads)
        else:
            rank = {road: ROAD_PRIORITY.get(self.network.road(road).road_type, 0) for road in roads}
            pair = set(min(((a, b) for i, a in enumerate(roads) for b in roads[i + 1:]),
                           key=lambda ab: (-rank[ab[0]] - rank[ab[1]],  # a residential lane never outranks the trunk
                                           arms[ab[0]][0] * arms[ab[1]][0] + arms[ab[0]][1] * arms[ab[1]][1])))
        return {link.id for link in self.links.values() if link.road in pair and node_id in (link.start, link.end)}

    @staticmethod
    def _meeting(first: Turn, second: Turn) -> tuple[float, float] | None:
        """(metres along each) where two turns cross, or merge into the same lane."""
        if first.lane_out is second.lane_out:
            return first.length, second.length
        walked_a = 0.0
        for (ax, ay), (bx, by) in zip(first.points, first.points[1:]):
            walked_b = 0.0
            for (cx, cy), (dx, dy) in zip(second.points, second.points[1:]):
                hit = segment_hit(ax, ay, bx, by, cx, cy, dx, dy)
                if hit:
                    return (walked_a + hit[0] * hypot(bx - ax, by - ay),
                            walked_b + hit[1] * hypot(dx - cx, dy - cy))
                walked_b += hypot(dx - cx, dy - cy)
            walked_a += hypot(bx - ax, by - ay)
        return None

    def assign(self, rates: dict) -> tuple[dict, dict]:
        """Every pair's veh/h ({(origin, destination): veh/h}) on its fastest route: (veh/h arriving at each
        (junction, link), veh/h of each movement (link, next link))."""
        flows: dict[tuple[str, str], float] = defaultdict(float)
        moves: dict[tuple[str, str], float] = defaultdict(float)
        for (origin, destination), rate in rates.items():
            for share, route in self.route_shares(origin, destination):
                for link, after in zip(route, route[1:]):
                    flows[(link.end, link.id)] += rate * share
                    moves[(link.id, after.id)] += rate * share
        return flows, moves

    def circulating(self, link: Link) -> set[tuple[str, str]] | None:
        """The movements (link, next link) whose ring passes in front of this roundabout entry; None if the
        link enters no ring."""
        rings = [(into, turn) for into in self.arriving[link.end] for turn in into.turns.values() if turn.ring]
        entry = next((turn for into, turn in rings if into is link), None)
        if entry is None:
            return None
        angle, spin = entry.ring[2], entry.ring[4]
        return {(into.id, turn.lane_out.link.id) for into, turn in rings if into is not link
                and ((angle - turn.ring[2]) * spin) % (2 * pi) < (turn.ring[3] - turn.ring[2]) * spin}

    def saturation(self, node_id: str, flows: dict, moves: dict) -> float | None:
        """The junction's degree of saturation (flow over capacity) under these flows (see assign), at its
        most loaded entry; None where no one gives way or waits (a bend, a merge, nothing arriving).
        - Signalised: the critical v/c as Webster times it (signal_timing.critical_saturation).
        - Roundabout: each entry's flow over its measured capacity (ROUNDABOUT_ENTRY_VPH) against the
          circulating flow in front of it, the movements whose ring passes that entry.
        - Priority (uncontrolled): each give-way movement's flow over its measured capacity per lane
          (PRIORITY_CAPACITY_VPH) against the movements it gives way to, the opposite minor arm's straight and
          kerb movements among them for a minor offside turn (HCM ranks, _add_conflicts); a movement giving way to
          none at its lane's saturation flow (TURN_SATURATION_VPH); lane by lane as at a signal (_lane_load). On a
          2 x 2 grid of give-way junctions, demand sized to X 0.85 and 1.0 holds on 3 of 3 seeds and 1.2 starts to
          jam (2026-10-13; before, X 0.45 jammed: about 2.3 times optimistic)."""
        if node_id in self.signals:
            return critical_saturation(self.flow_ratios(node_id, moves), self.signals[node_id].span)
        arriving = self.arriving[node_id]
        if node_id in self.roundabouts:
            worst = None
            for link in arriving:
                entering = sum(flow for (into, _), flow in moves.items() if into == link.id)
                passing = self.circulating(link)
                if not entering or passing is None:
                    continue
                circulating = sum(moves.get(move, 0.0) for move in passing)
                x = entering / measured_capacity(ROUNDABOUT_ENTRY_VPH, circulating)
                worst = x if worst is None else max(worst, x)
            return worst
        worst = None
        for link in arriving:
            foes = {after: {(other.lane_in.link.id, other.lane_out.link.id) for (_, out), turn in link.turns.items()
                            if out == after for other, _, _, give_way in turn.conflicts if give_way}
                    for (_, after) in link.turns}
            if any(foes.values()):  # a movement giving way to none (the major road) at its lane's saturation flow
                x = self._lane_load(link, lambda after, flow, kind: flow / (measured_capacity(
                    PRIORITY_CAPACITY_VPH, sum(moves.get(foe, 0.0) for foe in foes[after])) if foes[after]
                    else TURN_SATURATION_VPH[kind]), moves)
                worst = x if worst is None else max(worst, x)
        return worst

    def storage_ratio(self, node_id: str, moves: dict) -> float | None:
        """The fullest arriving link's queue over the vehicles its lanes hold (HCM's queue storage ratio), under these
        movements: a signal's arm the deterministic most queued per lane, λr/(1 − λ/s) (arrivals over its effective red
        at Webster's timing and while it clears); a give-way movement or roundabout entry HCM 2000 Eq 17-37's 95th
        percentile queue, 900T[(x − 1) + √((x − 1)² + (3600/c)x/(150T))]c/3600, T 0.25 h. Over 1: the queue reaches
        the junction behind (spillback), which judging each junction alone can't see (Graph.saturation). None where no
        one waits."""
        def holds(link) -> float:
            return max(link.length / STORAGE_M_PER_VEHICLE, 1.0)

        def q95(x: float, c: float) -> float:
            t, x = 0.25, min(x, 3.0)
            return 900 * t * ((x - 1) + sqrt((x - 1) ** 2 + (3600 / max(c, 1.0)) * x / (150 * t))) * c / 3600

        worst = None
        if node_id in self.signals:
            signal = self.signals[node_id]
            ratios = self.flow_ratios(node_id, moves)
            if not any(ratios):
                return None
            cycle, greens = webster(ratios, signal.span)
            for (group, _), green in zip(signal.phases, greens):
                red = cycle - effective(green)
                for link_id in group:
                    link = self.links[link_id]
                    y = self.arm_ratio(link_id, moves)  # its busiest lane's flow over saturation flow
                    if y > 0:
                        queue = y * SATURATION_PER_LANE_VPH / 3600 * red / max(1 - y, 0.05)
                        worst = max(worst or 0.0, queue / holds(link))
                    # A movement giving way on its green (a U-turn, an offside turn's detour at two phases) waits
                    # for gaps in its lane: its own queue, at the give-way capacity against the flows it yields to
                    # at their rate in its green (on 80 m links waiting U-turners filled the offside lanes)
                    for (_, after), turn in link.turns.items():
                        foes = [(o.lane_in.link.id, o.lane_out.link.id) for o, _, _, give_way in turn.conflicts if give_way]
                        flow = moves.get((link_id, after), 0.0)
                        if foes and flow:
                            green_share = max(effective(green) / cycle, 0.1)
                            capacity = measured_capacity(PRIORITY_CAPACITY_VPH,
                                                         sum(moves.get(foe, 0.0) for foe in foes) / green_share)
                            worst = max(worst or 0.0, q95(flow / (capacity * green_share), capacity * green_share)
                                        / holds(link))
            return worst
        for link in self.arriving[node_id]:
            flow = sum(rate for (into, _), rate in moves.items() if into == link.id)
            if not flow:
                continue
            if node_id in self.roundabouts:
                passing = self.circulating(link)
                if passing is None:
                    continue
                capacity = measured_capacity(ROUNDABOUT_ENTRY_VPH, sum(moves.get(move, 0.0) for move in passing))
                queue = q95(flow / capacity, capacity) / max(len(link.lanes), 1)
            else:
                foes = {after: {(other.lane_in.link.id, other.lane_out.link.id) for (_, out), turn in link.turns.items()
                                if out == after for other, _, _, give_way in turn.conflicts if give_way}
                        for (_, after) in link.turns}
                if not any(foes.values()):
                    continue
                x = self._lane_load(link, lambda after, rate, kind: rate / (measured_capacity(
                    PRIORITY_CAPACITY_VPH, sum(moves.get(foe, 0.0) for foe in foes[after])) if foes[after]
                    else TURN_SATURATION_VPH[kind]), moves)
                lane_flow = flow / len(link.lanes)
                queue = q95(x, lane_flow / x) if x > 0 else 0.0
            worst = max(worst or 0.0, queue / holds(link))
        return worst

    def arm_ratio(self, link_id: str, moves: dict) -> float:
        """An arriving link's critical flow ratio at its signal, lane by lane: kerb turns load the kerbside
        lane and offside turns and U-turns the offside one, each at its own saturation flow (they slow for
        the curve), and straight-on traffic takes whichever lanes have room, as drivers balance them. Where
        any lane may turn (one lane, or no lane changing) every movement shares all the lanes."""
        return self._lane_load(self.links[link_id], lambda after, flow, kind: flow / TURN_SATURATION_VPH[kind], moves)

    @staticmethod
    def _lane_load(link: Link, ratio, moves: dict) -> float:
        """The busiest lane's load, Σ flow/capacity, with each movement's `ratio(next link, veh/h, kind)` put
        where drivers put it: kerb turns in the kerbside lane, offside turns and U-turns in the offside one,
        straight-on traffic in whichever lanes have room; where any lane may turn (one lane, or no lane
        changing), every movement shares all the lanes. A turn pocket's lanes (Link.pockets) share its side's
        turns and nothing else; straight-on traffic then has the other lanes."""
        load, shared = [0.0] * len(link.lanes), 0.0
        kerb, offside = link.pockets
        lanes = len(link.lanes)
        for after, turn in {after: turn for (_, after), turn in link.turns.items()}.items():
            flow = moves.get((link.id, after), 0.0)
            if not flow:
                continue
            share = ratio(after, flow, turn.kind)
            if turn.kind == "straight" or len(link.lanes) == 1 or not link.lane_changing:
                shared += share
            elif (turn.kind == "kerb" and kerb) or (turn.kind != "kerb" and offside):
                for index in range(kerb) if turn.kind == "kerb" else range(lanes - offside, lanes):
                    load[index] += share / (kerb if turn.kind == "kerb" else offside)
            else:
                load[kerb if turn.kind == "kerb" else lanes - offside - 1] += share
        pockets = max(load[:kerb] + load[lanes - offside:], default=0.0)
        ordered = sorted(load[kerb:lanes - offside])  # the shared flow fills the emptiest lanes first, to a common level
        for count in range(1, len(ordered) + 1):
            level = (shared + sum(ordered[:count])) / count
            if count == len(ordered) or level <= ordered[count]:
                return max(level, ordered[-1], pockets)
        return max(ordered[-1], pockets)

    def saturation_vph(self, link_id: str, moves: dict) -> float:
        """The arm's saturation flow for the movements it carries: its flow over its critical flow ratio
        (straight-on traffic in every lane when nothing is on it yet)."""
        flow = sum(rate for (into, _), rate in moves.items() if into == link_id)
        ratio = self.arm_ratio(link_id, moves)
        return flow / ratio if ratio > 0 else SATURATION_PER_LANE_VPH * len(self.links[link_id].lanes)

    def flow_ratios(self, node_id: str, moves: dict) -> list[float]:
        """Each phase's critical flow ratio at a signalised junction: its busiest arm's (arm_ratio)."""
        return [max(self.arm_ratio(link, moves) for link in group) for group, _ in self.signals[node_id].phases]

    def route(self, origin: str, destination: str) -> list[Link] | None:
        """Fastest links from the origin end to the destination end, or None if unreachable. At a
        two-phase signal an offside turn is a detour (_add_detours): its out-and-back links are
        part of the route, and U-turns are made only there."""
        key = (origin, destination)
        if key not in self._routes:
            self._routes[key] = self._fastest(origin, destination, self.free_move_time)
        return self._routes[key]

    def _fastest(self, origin: str, destination: str, cost) -> list[Link] | None:
        """The least-cost links from the origin end to the destination (Dijkstra). cost(link, next link or None at
        the destination), seconds: driving the link and making that move at its end."""
        best: dict[str, float] = {}
        came: dict[str, tuple] = {}  # link id -> (link before it, the detour's links between them)
        order = count()  # breaks ties, so links are never compared
        heap = [(0.0, next(order), link, None, (), False) for link in self.leaving[origin]]
        found = None
        while heap:
            spent, _, link, previous, via, there = heapq.heappop(heap)
            if there:  # the cheapest arrival, its last link's own cost included
                found = link
                break
            if link.id in best:
                continue
            best[link.id], came[link.id] = spent, (previous, via)
            if link.end == destination:
                heapq.heappush(heap, (spent + cost(link, None), next(order), link, None, (), True))
                continue
            moves = [(self.links[next_id], ()) for (_, next_id), turn in link.turns.items() if turn.kind != "uturn"]
            for after, via in moves + self.detours.get(link.id, []):
                if after.id not in best:
                    chain = [link, *via, after]
                    extra = sum(cost(a, b) for a, b in zip(chain, chain[1:]))
                    heapq.heappush(heap, (spent + extra, next(order), after, link, via, False))
        if not found:
            return None
        path, link = [], found
        while link is not None:
            path.append(link)
            link, via = came[link.id]
            path.extend(reversed(via))
        path.reverse()
        return path

    def free_move_time(self, link: Link, after: Link | None) -> float:
        """Free-flow routing's cost of driving a link and moving onto `after`, seconds: the link at its limit plus the
        move's control delay with no traffic (control_delay; none at the destination)."""
        return link.length / link.speed + (self.control_delay.get((link.id, after.id), 0.0) if after else 0.0)

    def move_time(self, link: Link, after: Link | None) -> float:
        """Route choice's cost of driving a link and moving onto `after`, seconds, the junction's delay counted once:
        the move's measured time (turn_times_s: entering the link to entering `after`, that move's own delay at the
        junction included); else the link's measured time over all its moves (link_times_s: an average junction
        delay already in it); else free flow with the control-delay prior (free_move_time). Never two of them."""
        moves = self.choice.get("turn_times_s", {}).get(link.id, {})
        return (after is not None and moves.get(after.id)) or self.choice.get("link_times_s", {}).get(link.id) \
            or self.free_move_time(link, after)

    def path_time(self, path: list[Link]) -> float:
        """A route's cost by move_time, seconds."""
        return sum(self.move_time(link, after) for link, after in zip(path, [*path[1:], None]))

    def route_shares(self, origin: str, destination: str) -> list[tuple[float, list[Link]]]:
        """[(probability, route)]: the fastest route alone without route choice; with it, up to `routes`
        alternatives by link penalty, chosen by path-size logit, P ∝ exp(−θ·T/60 + ln PS),
        PS = Σ_a (l_a / L) / (routes using a)."""
        key = (origin, destination)
        if key not in self._shares:
            best = self.route(origin, destination)
            if best is None or not self.choice:
                self._shares[key] = [] if best is None else [(1.0, best)]
            else:
                wanted, penalty, found = int(self.choice.get("routes", 3)), defaultdict(lambda: 1.0), {}
                for _ in range(2 * wanted):
                    path = self._fastest(origin, destination,
                                         lambda link, after: self.move_time(link, after) * penalty[link.id])
                    found.setdefault(tuple(link.id for link in path), path)
                    if len(found) >= wanted:
                        break
                    for link in path:
                        penalty[link.id] *= ROUTE_PENALTY
                paths = list(found.values())
                shares = path_size_logit(paths, [self.path_time(path) for path in paths],
                                         float(self.choice.get("theta_per_min", 0.5)))
                self._shares[key] = list(zip(shares, paths))
        return self._shares[key]

    def pick_route(self, origin: str, destination: str, rng: random.Random) -> list[Link] | None:
        """One vehicle's route: drawn by route_shares (no draw without route choice, so runs repeat as before)."""
        shares = self.route_shares(origin, destination)
        if len(shares) <= 1:
            return shares[0][1] if shares else None
        draw = rng.random()
        for share, path in shares:
            draw -= share
            if draw <= 0:
                return path
        return shares[-1][1]

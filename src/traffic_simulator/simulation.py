"""The traffic simulation: vehicles spawned from the OD matrix drive the network (no Qt).

Following SUMO's structure (reverse_engineering/): each step plans every vehicle's speed from
its leaders, signals and junctions (car following, simulation/car_following.md), moves them
along their paths, lets them change lanes (simulation/lane_changing.md), then inserts new
vehicles where there is room (algorithms/vehicle_insertion.md). All positions are vector
geometry in metres (geometry.py): nothing is cell-based.

The network becomes a lane graph once per Run, from a snapshot:
- Link: one road in one travel direction. Its lanes are the road's centre line offset sideways
  (lane 0 kerbside), cut back at each junction to the stop or give-way line (an uncontrolled
  node: its edge; a boundary end: not at all).
- Turn: a path through a junction from an arriving lane to a leaving lane. A cubic Bézier, or
  at a roundabout an entry curve, an arc round the ring in the driving direction (clockwise
  driving on the left) and an exit curve. A kerb-side turn leaves from lane 0, an offside turn
  (across oncoming traffic) or a U-turn from the offside lane, straight on from any lane.
- Two-phase signals have no offside turns: nothing protects them from oncoming traffic. A vehicle
  goes straight on to the next junction, U-turns there, comes back and turns kerb-side (or, with
  a boundary end ahead, turns kerb-side, U-turns and comes back straight on).
- Routes: the fastest path at the speed limits (Dijkstra) from the origin end to the destination,
  with those detours in place of the missing turns. With route choice set on the network
  (`Network.route_choice`), each pair has up to `routes` alternatives (link penalty: each found route's
  links cost ROUTE_PENALTY times more for the next search), and each vehicle draws one by path-size
  logit on the link times given (Ben-Akiva and Bierlaire; Handbook of Transportation Science p35–36);
  signal timing and demand sizing load the alternatives by the same shares.
- Lane changing (LC2013): each lane is scored by how far it leads along the route, and vehicles
  head for the best ones in good time; urgent changes get speed advice and are let in.

Junctions (algorithms/junction_behaviour.md). A vehicle enters a turn only when:
- its signal is green, or amber and it can't stop comfortably (fixed-time plans from each
  junction's phases and green times, with amber and all-red; algorithms/traffic_light_control.md);
- no vehicle on a crossing or merging turn is still short of the conflict point;
- gap acceptance: where it gives way (an offside turn against oncoming traffic on the same green,
  a minor arm of an uncontrolled junction, a roundabout entry against circulating traffic), no
  vehicle it gives way to would reach the conflict within the critical gap;
- its exit lane has room, so it never blocks the junction.
Otherwise it stops at the line, as behind a standing leader. After waiting in the wrong lane
for a while it takes the turn from where it is (impatience), rather than blocking for ever.

Demand (algorithms/demand_generation.md): every OD pair spawns vehicles at its flow, veh/h, or
its daily trips spread over the hours by its daily curve. Each minute's count follows the pair's
arrival pattern: Random (the flow's count, at random times), Poisson, Binomial (more regular)
or Negative binomial (bunched); each vehicle's type is drawn from the pair's mode split, and
timetabled types (a bus every n minutes) run to their headway instead. A vehicle waits at its
origin until its lane has room.

Bus stops (algorithms/public_transport.md): a bay beside the kerbside lane, off the road like SUMO's
parking stops. A bus heads for the kerbside lane in good time, slows, pulls into the bay, stands at
the stop for that stop's dwell time (boarding and alighting), then waits at the end of the bay for a gap
safe for it and for the traffic behind (insertion safety) and merges back. Traffic drives past it.
"""

from __future__ import annotations

import heapq
import random
import sys
import threading
import time as clock
from bisect import bisect_right
from collections import Counter, defaultdict, deque
from copy import deepcopy
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import NamedTuple
from itertools import accumulate, count
from math import atan2, ceil, cos, floor, hypot, inf, log, pi, sin, sqrt

from .geometry import bezier, nearest_along, offset_polyline, point_along, polyline_length, segment_hit, sub_polyline
from .interchanges import curve_speed_kmh
from .models import (BAY_ENTRY_M, BAY_LENGTH_M, BAY_STOP_M, BAY_WIDTH_M, LANE_WIDTH_M, Network, bay_depth,
                     lane_end_inset)
from .roundabouts import design_roundabout
from .stats import logit
from .signal_timing import (ACTUATED, ADAPTIVE, ALL_RED_S, AMBER_S, COORDINATED, DETECTOR_GAP_S, MIN_GREEN_S,
                            OPTIMISE_BUDGET_S, ActuatedJunction, MaxPressureJunction,
                            SATURATION_PER_LANE_VPH, SPAN_S, STOP_LINE_LOOP_M, UPSTREAM_LOOP_AT_M, UPSTREAM_LOOP_M,
                            Approach, ScatsJunction, ScatsRegion, ScootJunction, ScootLink, ScootRegion, TimingModel,
                            common_cycle, critical_saturation, fit_greens, KERB_TURN_PER_LANE_VPH, OFFSIDE_TURN_PER_LANE_VPH, percentile_greens, synchro, synchro_cycle,
                            transyt, webster)
from .emissions import fuel_rate_ml_s, light_motor
from .vehicle import EMERGENCY_MODE, Vehicle, create_vehicle, free_speed, next_speed, safe_speed, secure_gap, start_up

DT = 0.5  # simulated seconds per step (SUMO's is 1 s by default; drawing interpolates between steps)
COUNT_INTERVAL_S = 60.0  # arrivals are counted, per the pattern, in one-minute intervals
NEG_BINOMIAL_K = 2.0  # dispersion: variance = mean + mean²/k
CRITICAL_GAP_S = {"offside": 4.5, "minor": 5.0, "roundabout": 4.0}
TURN_SATURATION_VPH = {"straight": SATURATION_PER_LANE_VPH, "kerb": KERB_TURN_PER_LANE_VPH,
                       "offside": OFFSIDE_TURN_PER_LANE_VPH, "uturn": OFFSIDE_TURN_PER_LANE_VPH}
# Give-way capacity, measured on the simulated drivers (one approach held saturated against random priority
# traffic, 3 seeds of 50 min a point; test_simulation re-measures some): (veh/h it gives way to, veh/h).
# The HCM's gap-acceptance and roundabout formulas don't fit them: these drivers take short gaps at low
# priority flows and few at high ones.
PRIORITY_CAPACITY_VPH = ((0, 1990), (200, 1420), (400, 1050), (600, 710), (800, 510), (1000, 320), (1200, 225),
                         (1400, 185), (1600, 120))  # per lane of a minor arm (one and two lanes alike)
ROUNDABOUT_ENTRY_VPH = ((0, 1450), (200, 1150), (400, 880), (600, 615), (800, 410), (1000, 215),
                        (1200, 78))  # per entry, whatever its lanes: the ring is one path round


def measured_capacity(table: tuple, conflicting_vph: float) -> float:
    """Capacity against this much priority traffic: straight lines between the measured points, held
    at the last point beyond them."""
    for (x0, c0), (x1, c1) in zip(table, table[1:]):
        if conflicting_vph <= x1:
            return c0 + (c1 - c0) * max(conflicting_vph - x0, 0.0) / (x1 - x0)
    return table[-1][1]
IMPATIENCE_S = 8.0  # waiting this long in the wrong lane, it turns from where it is
# An emergency vehicle keeps to the kerbside lane (lane 0, whichever side traffic drives on) over the last
# EMERGENCY_LANE_M before each signalised junction on its route: the Dynamic Emergency Vehicle Lane, which that
# junction's DEVL controller may clear for it (a strategic lane choice, as a bus's for its bay; it turns from there).
EMERGENCY_LANE_M = 150.0
ROUTE_PENALTY = 1.5  # route choice's alternatives: a found route's links cost this much more in the next search
# Adverse weather (HCM 2000 Ch 22, p22-9 to 22-11, from freeway studies near 100 km/h): (the share of each speed
# limit drivers keep, the factor on their reaction time τ). Free-flow speed 10 km/h lower in light rain or snow, 20
# in heavy rain, 50 in heavy snow, taken as a share of 100 km/h; τ set so the queue discharge rate (saturation flow,
# the HCM's field method, 2 seeds of 40 min at a fixed-time signal) falls by the HCM's capacity loss. Measured: light
# rain 4 % lower from the slower speeds alone (HCM: little if any), light snow 7 % (HCM 5-10 %), heavy rain 14 % (HCM
# 14-15 %), heavy snow 31 % (HCM 30 %); test_simulation's weather test re-measures them.
WEATHER = {"clear": (1.0, 1.0), "light rain": (0.9, 1.0), "light snow": (0.9, 1.07), "heavy rain": (0.8, 1.12),
           "heavy snow": (0.5, 1.05)}
# Ramp metering by ALINEA (Papageorgiou, Hadj-Salem and Blosseville 1991, TRR 1320; Handbook of Transportation Science
# p187-190): every METER_PERIOD_S the rate moves by K_R × (target − measured occupancy, %) of the main road just past
# the merge, between the least and most rates; the meter shows green for METER_GREEN_S once every 3600/rate seconds
# (one vehicle a green).
METER_K_R, METER_PERIOD_S, METER_GREEN_S = 70.0, 60.0, 2.0
METER_RATES_VPH = (240.0, 1800.0)
METER_DETECTOR_M = 60.0  # the main road's detector this far past the merge (at most the middle of its link)
METER_SETBACK_M = 30.0  # the meter's stop line this far before the merge (a released vehicle then finds its gap)
# A fork (Simulation.fork, for rollouts): copied on a thread of its own with a deep stack, the lane graph's references
# nesting deeper than Python's default recursion limit allows.
FORK_STACK_BYTES, FORK_RECURSION = 128 * 1024 * 1024, 100_000
_fork_lock = threading.Lock()
CLOSURE_CLEAR_M = 30.0  # a closed section starts at least this far along its lanes (vehicles need room to change out)
HELD_MPS = 5 / 3.6  # max-pressure: a vehicle downstream slower than this counts as queued there
ARRIVAL_ZONE_M = 5.0  # observed arrivals at a signal: within this of the stop line (or stopped in its queue)
CONFLICT_TTC_S = 3.0  # observed rear-end conflicts: time to collision under this (analyses then pick 1.5 s, SSAM's default)
LOOKAHEAD_M = 150.0
STOP_MARGIN_M = 0.3
LANE_CHANGE_COOLDOWN_S = 3.0
# Strategic lane changing (LC2013): lanes are compared this far along the route (SUMO's bestLanes),
# and each lane change still needed wants this many seconds of driving before the lane runs out.
REACH_HORIZON_M = 3000.0
STRATEGIC_LOOKAHEAD_S = 10.0
# Bus stops: the vehicle types that use them (Mode Behavior names, any case), how fast a bus drives in
# its bay, and how far the bay's middle is from the kerbside lane's (half a lane plus half the bay).
BUS_TYPES = ("bus",)
# Passengers at stops (Bay.boarding_per_h): dwell = door time + alighting × ALIGHT_S + boarding × BOARD_S (HCM 2000
# Eq 27-2), with Exhibit 27-9's times for a conventional bus with one front door and a single coin fare (boarding
# 2.6-3.0 s, alighting 1.7-2.0 s: the low ends) and 2-5 s to open and close the doors (3); 0.5 s more a boarder once
# passengers stand. A bus holds CAPACITY_PER_M passengers a metre of its length (about 78 for a 12 m bus), half of them
# seated (both assumptions); boarders who don't fit wait for the next bus.
DOOR_S, BOARD_S, ALIGHT_S, STANDING_BOARD_S = 3.0, 2.6, 1.7, 0.5
CAPACITY_PER_M, SEATED_SHARE = 6.5, 0.5
BAY_SPEED = 20 / 3.6
BAY_OFFSET_M = LANE_WIDTH_M / 2 + BAY_WIDTH_M / 2


@dataclass(eq=False)
class Lane:
    link: Link
    index: int  # 0: kerbside
    points: list
    length: float
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
    speed: float  # m/s, the road's limit
    lane_changing: bool
    length: float = 0.0
    lanes: list = field(default_factory=list)
    turns: dict = field(default_factory=dict)  # (lane index, next link id) -> Turn
    cut_start: float = 0.0  # how far from each end node its lanes are cut back (stop or give-way line)
    cut_end: float = 0.0


@dataclass(frozen=True)
class Closure:
    """Lanes closed and/or a speed limit on part of one link for a while: an incident or a work zone."""
    link: str
    lanes: tuple  # lane indices closed, kerbside (0) first; () closes none (a speed limit only)
    from_m: float  # along the link's lanes
    to_m: float
    start_s: float
    end_s: float
    speed: float | None = None  # m/s on every lane over the section (a work zone); None: the road's limit


class _Held:
    """An adaptive or actuated signal's controller in a fork: its current greens kept, nothing more decided (the
    real controller's detectors and plans belong to the run it was forked from)."""

    def phase_starts(self, signal, index: int, t: float) -> None:
        pass

    def cycle_starts(self, signal, t: float) -> None:
        pass


@dataclass(eq=False)
class Meter:
    """A ramp meter at the end of an on-ramp's link: ALINEA (target occupancy) or a fixed rate."""
    link: str
    rate: float  # veh/h now
    target: float | None  # ALINEA's target occupancy, %; None: the fixed rate
    loops: list  # the main road's detector, one loop a lane past the merge
    at: float = 0.0  # its stop line, metres along the ramp's lanes
    occupied: float = 0.0  # loop-steps occupied this period, over all loops
    steps: int = 0
    history: list = field(default_factory=list)  # (time, rate, occupancy %) each period

    def green(self, t: float) -> bool:
        return t % (3600.0 / self.rate) < METER_GREEN_S


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


def _closer(best: tuple | None, found: tuple | None) -> tuple | None:
    """Whichever leader (free gap, speed, braking) is nearer; None counts as none."""
    return found if best is None or (found is not None and found[0] < best[0]) else best


@dataclass
class PairDemand:
    """One OD pair's demand, as the Demand Generator shows it (the split with timetabled
    types already fixed by their headways)."""
    origin: str
    destination: str
    hourly: float | None  # veh/h, when the matrix shows hourly demand
    daily: float | None  # trips per day, when it shows daily demand...
    shares: list  # ...spread over the 24 hours in these shares
    pattern: str
    split: tuple
    timetabled: bool = True  # timetabled types (buses) run on it; False for an institution's trips (their riders
    # take the network's buses)


class VehicleState(NamedTuple):
    """One vehicle in a Frame."""
    id: str
    mode: str  # its vehicle type
    colour: str
    length_m: float
    width_m: float
    x: float  # centre, metres
    y: float
    ux: float  # heading, unit vector
    uy: float
    prev: tuple  # (x, y, ux, uy) one step earlier, to draw smooth motion between steps
    speed: float  # m/s
    road: str  # the road it is on, or the junction it is crossing
    place: str  # its lane, its turn through the junction, or the bay of the bus stop it is at
    s: float  # metres along that lane or turn (its front)
    origin: str
    destination: str
    # What it is heading for next on its route ("roundabout", "signal" or "slip road"; "" for
    # none), that junction's or slip road's id, and roughly how far ahead it is (metres).
    heading_to: str = ""
    heading_to_id: str = ""
    heading_to_m: float = 0.0


class Frame(NamedTuple):
    """The run frozen at one moment: what viewers draw and what an AI reads. Never changes once
    made, so it can be read from any thread while the physics carries on."""
    time: float  # simulated seconds
    vehicles: tuple  # VehicleState, one per vehicle on the road
    signals: dict  # (junction, arriving road) -> "G", "Y" or "R"
    spawned: int
    arrived: int
    plans: dict = {}  # junction -> this run's signal plan (method, cycle, greens; Webster's flow ratios)

    def records(self, road: str | None = None, limit: int | None = 100) -> dict:
        """Plain data for an AI (the assistant's read_traffic): the time, counts, signals and each
        vehicle's position (metres), heading (degrees, 0 = east, clockwise on screen), speed and place."""
        vehicles = [v for v in self.vehicles if road is None or v.road == road]
        shown = vehicles if limit is None else vehicles[:max(0, int(limit))]
        signals: dict[str, dict[str, str]] = defaultdict(dict)
        for (junction, arm), state in self.signals.items():
            signals[junction][arm] = {"G": "green", "Y": "amber", "R": "red"}[state]
        return {
            "time_s": round(self.time, 1), "on_road": len(self.vehicles), "spawned": self.spawned,
            "arrived": self.arrived, "matching": len(vehicles), "shown": len(shown),
            "vehicles": [{"id": v.id, "type": v.mode, "x_m": round(v.x, 1), "y_m": round(v.y, 1),
                          "heading_deg": round(atan2(v.uy, v.ux) * 180 / pi, 1),
                          "speed_kmh": round(v.speed * 3.6, 1), "road": v.road, "place": v.place,
                          "along_m": round(v.s, 1), "from": v.origin, "to": v.destination,
                          **({"heading_to": {"kind": v.heading_to, "id": v.heading_to_id,
                                             "distance_m": round(v.heading_to_m, 1)}} if v.heading_to else {})}
                         for v in shown],
            "signals": dict(signals), "signal_plans": self.plans,
        }


class Signal:
    """A signalised junction's phases in turn, each green, then amber, then all-red. Two phases:
    east-west arms, then north-south. Four: each arm on its own.

    Fixed-time (Webster, TRANSYT, Synchro, the greens entered): the same greens every cycle, the first
    phase's green starting `offset` seconds into each. Adaptive (SCATS, SCOOT): its `controller` is
    told as each cycle and each phase starts, and may change the greens (and so the cycle) from then."""

    def __init__(self, node, arms: dict[str, tuple[float, float]]) -> None:
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
        self.cycle = sum(green + SPAN_S for _, green in self.phases)
        self.offset = 0.0  # when the first phase's green starts in the cycle
        self.controller = None  # an adaptive method's junction (signal_timing), or None: fixed-time
        self._index = 0  # adaptive: the phase running, when its green began, when its cycle began
        self._start = self._cycle_start = 0.0

    def set_greens(self, greens: list[float]) -> None:
        """Use these greens, one per phase (a timing method's result)."""
        self.phases = [(group, green) for (group, _), green in zip(self.phases, greens)]
        self.cycle = sum(green + SPAN_S for _, green in self.phases)

    def set_green(self, index: int, green: float) -> None:
        """One phase's green, as a controller decides it while it runs (actuated, max-pressure)."""
        self.phases[index] = (self.phases[index][0], green)
        self.cycle = sum(each + SPAN_S for _, each in self.phases)

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
                if into < green + SPAN_S:
                    return index, into
                into -= green + SPAN_S
            return len(self.phases) - 1, into
        while t >= self._start + self.phases[self._index][1] + SPAN_S - 1e-9:  # times only move on
            self._start += self.phases[self._index][1] + SPAN_S
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
        return "G" if into < green else "Y" if into < green + AMBER_S else "R"


class Loop:
    """A loop detector on a lane, `start` to `end` metres along it (lane-relative, like vehicles):
    whether a vehicle is over it, and how many have driven off its far end since the last reading
    (at a stop line: gone on into the junction)."""

    def __init__(self, lane: Lane, start: float, end: float, turns: list) -> None:
        self.lane, self.start, self.end = lane, max(start, 0.0), min(end, lane.length)
        self.turns = turns  # the turns leaving the lane: a vehicle gone on into one may still cover the loop
        self._before: list = []

    def read(self) -> tuple[bool, int]:
        lane, start, end = self.lane, self.start, self.end
        passed = sum(1 for vehicle, s in self._before if s < end and (  # past it, even if it changed lane since
            (isinstance(vehicle.segment, Lane) and vehicle.segment.link is lane.link and vehicle.s >= end)
            or (isinstance(vehicle.segment, Turn) and vehicle.segment.lane_in is lane)))
        self._before = [(vehicle, vehicle.s) for vehicle in lane.vehicles]
        index = bisect_right(lane.vehicles, start, key=_position)  # the first front past its start
        occupied = index < len(lane.vehicles) and lane.vehicles[index].s - lane.vehicles[index].length_m < self.end
        if not occupied and self.end >= lane.length - 1e-6:  # a tail still on the lane's end
            occupied = any(foe.s < foe.length_m for turn in self.turns for foe in turn.vehicles)
        return occupied, passed


def _unit(points: list, at_end: bool) -> tuple[float, float]:
    """Direction of travel at a polyline's start or end."""
    (ax, ay), (bx, by) = (points[-2], points[-1]) if at_end else (points[0], points[1])
    length = hypot(bx - ax, by - ay) or 1.0
    return (bx - ax) / length, (by - ay) / length


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
                self.signals[node.id] = Signal(node, arms)
            self._add_conflicts(node.id)
        self._add_detours()
        self._routes: dict[tuple[str, str], list[Link] | None] = {}
        self.choice = dict(getattr(network, "route_choice", None) or {})  # empty: every vehicle the fastest route
        self._shares: dict[tuple[str, str], list] = {}

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
        return "straight" if dot > cos(pi * 35 / 180) else "kerb" if cross * self.keep < 0 else "offside"

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
                lane = link.lanes[0]
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
            link = Link(f"{road.id}{'>' if forward else '<'}", road.id, start, end, road.speed_limit_kmh / 3.6,
                        road.lane_changing, length, cut_start=cut_start, cut_end=cut_end)
            for index in range(count):
                offset = -self.keep * (half - 0.5 - index) * LANE_WIDTH_M
                lane_line = offset_polyline(line, offset)
                piece = sub_polyline(lane_line, cut_start, polyline_length(lane_line) - cut_end)
                link.lanes.append(Lane(link, index, piece, polyline_length(piece)))
            self.links[link.id] = link
            self.leaving[start].append(link)
            self.arriving[end].append(link)

    def _add_turns(self, node_id: str) -> None:
        node = self.network.node(node_id)
        ring = self.roundabouts.get(node_id)
        for arrive in self.arriving[node_id]:
            for leave in self.leaving[node_id]:
                kind = self._kind(arrive, leave)
                if kind == "uturn" and not self._two_phase(arrive.start):
                    continue  # U-turns only where a detour needs one (_add_detours)
                if kind == "offside" and self._two_phase(node_id):
                    continue  # nothing protects it from oncoming traffic: vehicles detour instead
                across = kind in ("offside", "uturn")  # both leave from the offside lane
                count_in, count_out = len(arrive.lanes), len(leave.lanes)
                for lane in arrive.lanes:
                    # Where the road forbids lane changes, drivers can't pick a lane: any lane may turn.
                    permitted = (count_in == 1 or not arrive.lane_changing or kind == "straight"
                                 or (kind == "kerb" and lane.index == 0) or (across and lane.index == count_in - 1))
                    target = 0 if kind == "kerb" else count_out - 1 if across else min(lane.index, count_out - 1)
                    out = leave.lanes[target]
                    if ring:
                        turn = self._ring_turn(node, ring, lane, out, kind, permitted)
                    else:
                        turn = self._curve_turn(node_id, lane, out, kind, permitted)
                    arrive.turns[(lane.index, leave.id)] = turn

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
        speed = min(lane.speed, out.speed, curve_speed_kmh(radius) / 3.6 if radius < 1e6 else lane.speed)
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
        turns = [turn for link in self.arriving[node_id] for turn in link.turns.values()]
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
                    give_way = (first.lane_in.link.id not in major or first.lane_out.link.id not in major) and \
                               second.lane_in.link.id in major and second.lane_out.link.id in major
                first.conflicts.append((second, spot[0], spot[1], give_way))

    def _major_arms(self, node_id: str) -> set[str]:
        """An uncontrolled junction's major road: the two arms most nearly in line. Their links."""
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
            pair = set(min(((a, b) for i, a in enumerate(roads) for b in roads[i + 1:]),
                           key=lambda ab: arms[ab[0]][0] * arms[ab[1]][0] + arms[ab[0]][1] * arms[ab[1]][1]))
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

    def saturation(self, node_id: str, flows: dict, moves: dict) -> float | None:
        """The junction's degree of saturation (flow over capacity) under these flows (see assign), at its
        most loaded entry; None where no one gives way or waits (a bend, a merge, nothing arriving).
        - Signalised: the critical v/c as Webster times it (signal_timing.critical_saturation).
        - Roundabout: each entry's flow over its measured capacity (ROUNDABOUT_ENTRY_VPH) against the
          circulating flow in front of it, the movements whose ring passes that entry.
        - Priority (uncontrolled): each give-way movement's flow over its measured capacity per lane
          (PRIORITY_CAPACITY_VPH) against the movements it gives way to (a movement with none to give way to:
          the table's first point), lane by lane as at a signal (_lane_load)."""
        if node_id in self.signals:
            return critical_saturation(self.flow_ratios(node_id, moves))
        arriving = self.arriving[node_id]
        if node_id in self.roundabouts:
            rings = [(link, turn) for link in arriving for turn in link.turns.values() if turn.ring]
            worst = None
            for link in arriving:
                entering = sum(flow for (into, _), flow in moves.items() if into == link.id)
                entry = next((turn for into, turn in rings if into is link), None)
                if not entering or entry is None:
                    continue
                angle, spin = entry.ring[2], entry.ring[4]
                passing = {(into.id, turn.lane_out.link.id) for into, turn in rings if into is not link
                           and ((angle - turn.ring[2]) * spin) % (2 * pi) < (turn.ring[3] - turn.ring[2]) * spin}
                circulating = sum(moves.get(move, 0.0) for move in passing)
                x = entering / measured_capacity(ROUNDABOUT_ENTRY_VPH, circulating)
                worst = x if worst is None else max(worst, x)
            return worst
        worst = None
        for link in arriving:
            foes = {after: {(other.lane_in.link.id, other.lane_out.link.id) for (_, out), turn in link.turns.items()
                            if out == after for other, _, _, give_way in turn.conflicts if give_way}
                    for (_, after) in link.turns}
            if any(foes.values()):
                x = self._lane_load(link, lambda after, flow, kind: flow / measured_capacity(
                    PRIORITY_CAPACITY_VPH, sum(moves.get(foe, 0.0) for foe in foes[after])), moves)
                worst = x if worst is None else max(worst, x)
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
        changing), every movement shares all the lanes."""
        load, shared = [0.0] * len(link.lanes), 0.0
        for after, turn in {after: turn for (_, after), turn in link.turns.items()}.items():
            flow = moves.get((link.id, after), 0.0)
            if not flow:
                continue
            share = ratio(after, flow, turn.kind)
            if turn.kind == "straight" or len(link.lanes) == 1 or not link.lane_changing:
                shared += share
            else:
                load[0 if turn.kind == "kerb" else -1] += share
        ordered = sorted(load)  # the shared flow fills the emptiest lanes first, to a common level
        for count in range(1, len(ordered) + 1):
            level = (shared + sum(ordered[:count])) / count
            if count == len(ordered) or level <= ordered[count]:
                return max(level, ordered[-1])
        return ordered[-1]

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
            self._routes[key] = self._fastest(origin, destination, lambda link: link.length / link.speed)
        return self._routes[key]

    def _fastest(self, origin: str, destination: str, cost) -> list[Link] | None:
        """The least-cost links (cost(link) seconds) from the origin end to the destination (Dijkstra)."""
        best: dict[str, float] = {}
        came: dict[str, tuple] = {}  # link id -> (link before it, the detour's links between them)
        order = count()  # breaks ties, so links are never compared
        heap = [(cost(link), next(order), link, None, ()) for link in self.leaving[origin]]
        found = None
        while heap:
            spent, _, link, previous, via = heapq.heappop(heap)
            if link.id in best:
                continue
            best[link.id], came[link.id] = spent, (previous, via)
            if link.end == destination:
                found = link
                break
            moves = [(self.links[next_id], ()) for (_, next_id), turn in link.turns.items() if turn.kind != "uturn"]
            for after, via in moves + self.detours.get(link.id, []):
                if after.id not in best:
                    extra = sum(cost(piece) for piece in via)
                    heapq.heappush(heap, (spent + extra + cost(after), next(order), after, link, via))
        if not found:
            return None
        path, link = [], found
        while link is not None:
            path.append(link)
            link, via = came[link.id]
            path.extend(reversed(via))
        path.reverse()
        return path

    def link_time(self, link: Link) -> float:
        """Route choice's cost of a link, seconds: the network's measured time (assign_routes) or free flow."""
        return self.choice.get("link_times_s", {}).get(link.id) or link.length / link.speed

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
                    path = self._fastest(origin, destination, lambda link: self.link_time(link) * penalty[link.id])
                    found.setdefault(tuple(link.id for link in path), path)
                    if len(found) >= wanted:
                        break
                    for link in path:
                        penalty[link.id] *= ROUTE_PENALTY
                paths = list(found.values())
                users = Counter(link_id for ids in found for link_id in set(ids))
                theta = float(self.choice.get("theta_per_min", 0.5))
                utilities = {}
                for index, path in enumerate(paths):
                    length = sum(link.length for link in path)
                    size = sum(link.length / length / users[link.id] for link in {link.id: link for link in path}.values())
                    utilities[index] = -theta * sum(self.link_time(link) for link in path) / 60 + log(size)
                shares = logit(utilities)
                self._shares[key] = [(shares[index], path) for index, path in enumerate(paths)]
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


def _position(vehicle: Vehicle) -> float:
    return vehicle.s


def _place(segment, vehicle: Vehicle) -> None:
    """Put the vehicle in the segment's list, which stays ordered by position (rearmost first)."""
    vehicles = segment.vehicles
    index = len(vehicles)
    while index and vehicles[index - 1].s > vehicle.s:
        index -= 1
    vehicles.insert(index, vehicle)


def _poisson(rng: random.Random, mean: float) -> int:
    if mean <= 0:
        return 0
    if mean > 30:  # normal approximation, fine at these sizes
        return max(0, round(rng.gauss(mean, sqrt(mean))))
    limit, count, product = pow(2.718281828459045, -mean), 0, rng.random()
    while product > limit:
        count += 1
        product *= rng.random()
    return count


def arrivals(rng: random.Random, pattern: str, mean: float) -> int:
    """How many vehicles arrive in one counting interval with this mean, per the pattern."""
    if mean <= 0:
        return 0
    if pattern == "Poisson":
        return _poisson(rng, mean)
    if pattern == "Binomial":  # more regular than Poisson: variance = mean(1 - p)
        trials = max(1, ceil(2 * mean))
        return sum(rng.random() < mean / trials for _ in range(trials))
    if pattern == "Negative binomial":  # bunched: a Poisson whose rate itself varies (gamma)
        return _poisson(rng, rng.gammavariate(NEG_BINOMIAL_K, mean / NEG_BINOMIAL_K))
    return floor(mean) + (rng.random() < mean - floor(mean))  # Random: the flow's count, at random times


class Simulation:
    """One run: build it from the network, the demand and the vehicle types, then step()."""

    def __init__(self, network: Network, demand: list[PairDemand], modes: list, seed: int = 42) -> None:
        self.graph = Graph(network)
        self.modes = list(modes)
        self.rng = random.Random(seed)
        self.time = 0.0
        self.vehicles: list[Vehicle] = []
        self.pending: dict[str, deque] = defaultdict(deque)  # first link id -> vehicles waiting to enter
        self.departures: list = []  # heap of (time, order, pair, mode)
        self.spawned = self.arrived = 0
        self.spawned_by_mode: Counter = Counter()
        self._order = 0
        # Emergency vehicles (dispatch): handed over from any thread, then a heap of (time, order, origin,
        # destination, every_s); each one's trip (id, from, to, dispatched_s, arrived_s: None until it arrives).
        self._dispatched: deque = deque()
        self._emergencies: list = []
        self.emergency_trips: dict[str, dict] = {}
        self._into: dict[Lane, list[Turn]] = {}
        self._out: dict[Lane, list[Turn]] = defaultdict(list)  # the turns leaving each lane
        for link in self.graph.links.values():
            for (index, _next), turn in link.turns.items():
                self._out[link.lanes[index]].append(turn)
        self._bays_on = {bays[0].lane: bays for bays in self.graph.bays.values()}  # kerbside lanes with bus stops
        self.demand: list[PairDemand] = []
        self.unreachable: list[str] = []
        self._use_demand(demand)
        self._handed = self._taken = demand  # set_demand: the latest demand handed over, and the one in use
        self._loops: list = []  # (Loop, what reads it): the adaptive methods' detectors
        self.signal_plans = self._time_signals()  # before the run, from the demand
        # Called for every signalised arm each step when set (the TSP extension): (junction, arriving
        # road, planned state, context) -> the state to show. None: the plans run untouched.
        self.signal_override = None
        self.observed: dict | None = None  # observe() turns on per-step recording (headless tests)
        # Incidents and work zones (close()): all of them, those on now, and per lane / link what they hold
        self.closures: list[Closure] = []
        self._active_closures: tuple = ()
        self._closure_key = 0  # changes when a closure starts or ends (the vehicles' lane reach is worked out again)
        self._closed: dict[tuple[str, int], list[tuple[float, float]]] = {}
        self._zones: dict[str, list[tuple[float, float, float]]] = {}
        self.weather = "clear"  # set_weather()
        self.meters: dict[str, Meter] = {}  # ramp link id -> its meter (meter())
        # Passengers waiting at each bay with passengers (count, when last counted), from their own random stream so
        # the traffic's random numbers stay the same whether stops have passengers or not
        self._waiting: dict[int, list] = {}
        self._passenger_rng = random.Random(f"passengers {seed}")
        self._states: dict[str, str] = {}
        self._lane_states: dict[tuple[str, int], str] = {}  # (link id, lane index) -> its own state (an override's)
        self._box_m: dict[str, float] = {}  # distance across each signalised junction, for overrides
        self._time_step_signals()
        self._next_count = 0.0
        for pair in self.demand:  # timetabled types: first one at a random point in the headway
            for index, mode in enumerate(self.modes):
                if mode.headway_min and pair.timetabled:
                    self._schedule(self.rng.uniform(0, mode.headway_min * 60), pair, index)

    # --- signal timing -------------------------------------------------------------------

    def design_flows(self) -> tuple[int | None, dict[tuple[str, str], float], dict[tuple[str, str], float]]:
        """(design hour, veh/h arriving at each (junction, link), veh/h of each movement (link, next
        link)): every pair's flow routed along its fastest path. Daily demand is taken at the
        network's busiest hour of the day."""
        hour = None
        if any(pair.hourly is None for pair in self.demand):
            hour = max(range(24), key=lambda h: sum(self.rate(pair, h * 3600) for pair in self.demand))
        rates: dict[tuple[str, str], float] = defaultdict(float)
        for pair in self.demand:
            rates[(pair.origin, pair.destination)] += self.rate(pair, (hour or 0) * 3600)
        return (hour, *self.graph.assign(rates))

    def _time_signals(self) -> dict[str, dict]:
        """Each signalised junction's plan for this run, by its timing method (signal_timing.py), all
        from the demand's design flows (each arm's q over its saturation flow, SATURATION_PER_LANE_VPH per lane):
        - Webster: each junction on its own.
        - TRANSYT, Synchro, SCATS, SCOOT: every junction with the method is one group on a common
          cycle (_coordinate), held within the group's cycle limits.
        - Fixed time: the greens entered."""
        hour, flows, moves = self.design_flows()
        graph = self.graph
        self.saturation_vph = {link.id: graph.saturation_vph(link.id, moves)  # each signalised arm's, for every method
                               for node in graph.signals for link in graph.arriving[node]}
        design = "the hourly flows" if hour is None else f"{hour:02d}:00-{hour + 1:02d}:00"
        plans, groups = {}, defaultdict(list)
        for node_id, signal in graph.signals.items():
            method = graph.network.node(node_id).signal_method
            ratios = self.graph.flow_ratios(node_id, moves)
            if method == "webster":
                cycle, greens = webster(ratios)
                signal.set_greens(greens)
                plans[node_id] = {"method": "webster", "cycle_s": round(cycle, 1),
                                  "greens_s": [round(green, 1) for green in greens],
                                  "flow_ratios": [round(ratio, 3) for ratio in ratios], "Y": round(sum(ratios), 3),
                                  "design_hour": design}
            elif method in COORDINATED:
                groups[method].append(node_id)
            elif method in ACTUATED:
                cycle, greens = webster(ratios)  # the plan it starts on (and reports); then decided step by step
                signal.set_greens(greens)
                plans[node_id] = {"method": method, "cycle_s": round(cycle, 1), "greens_s": [round(g, 1) for g in greens],
                                  "note": "greens decided as it runs"}
                self._actuate(method, node_id, signal)
            else:
                plans[node_id] = {"method": "fixed", "cycle_s": round(signal.cycle, 1),
                                  "greens_s": [round(green, 1) for _, green in signal.phases], "note": "the greens entered"}
        self.signal_plans = plans
        for method, nodes in groups.items():
            plans.update(self._coordinate(method, nodes, flows, moves, design))
        return plans

    def _coordinate(self, method: str, nodes: list[str], flows: dict, moves: dict, design: str) -> dict[str, dict]:
        """One coordinated group's plans. TRANSYT and Synchro time it now, offline; SCATS and SCOOT
        start it on the design flows and adapt it from their detectors as the run goes."""
        graph, network = self.graph, self.graph.network
        signals = {node: graph.signals[node] for node in nodes}
        low = max(network.node(node).cycle_min_s for node in nodes)
        high = max(low, min(network.node(node).cycle_max_s for node in nodes))
        ratios = {node: self.graph.flow_ratios(node, moves) for node in nodes}
        if method == "synchro":  # each phase's critical (flow, saturation flow), veh/h
            critical = {node: [max(((flows.get((node, link), 0.0), self.saturation_vph[link])
                                    for link in group), key=lambda pair: pair[0] / pair[1])
                               for group, _ in signals[node].phases] for node in nodes}
            cycle = synchro_cycle(critical, low, high)
            greens = {node: percentile_greens(critical[node], cycle) for node in nodes}
        else:
            cycle = common_cycle(ratios, low, high)
            greens = {node: fit_greens(ratios[node], cycle) for node in nodes}
        weights = {node: network.node(node).stop_weight_s if method in ("transyt", "scoot") else 0.0 for node in nodes}
        model = TimingModel(self._approaches(nodes, flows, moves, weights), greens, cycle)
        result: dict = {}
        if method == "transyt":
            result = transyt(model, OPTIMISE_BUDGET_S)
        elif method == "synchro":
            result = synchro(model, OPTIMISE_BUDGET_S)
        else:
            model.progression()
        for node, signal in signals.items():
            signal.set_greens(model.greens[node])
            signal.offset = model.offsets[node]
        plans = {node: {"method": method, "cycle_s": round(signals[node].cycle, 1),
                        "greens_s": [round(green, 1) for green in model.greens[node]],
                        "offset_s": round(model.offsets[node], 1), "group": len(nodes), "design_hour": design}
                 for node in nodes}
        if result:
            for plan in plans.values():
                plan.update(pi_before=round(result["pi_before"]), pi_after=round(result["pi_after"]),
                            finished=result["finished"])
        if method in ADAPTIVE:
            self._adapt(method, nodes, model, low, high, ratios)
        return plans

    def _actuate(self, method: str, node: str, signal: Signal) -> None:
        """Hand a signal to actuated control (detectors DETECTOR_GAP_S of travel before each stop line placing calls)
        or to max-pressure (each phase's queued vehicles less those on the links it feeds)."""
        def report(node: str, plan: dict) -> None:
            self.signal_plans[node] = {**self.signal_plans.get(node, {}), **plan,
                                       "cycle_s": round(sum(g + SPAN_S for g in plan["greens_s"]), 1)}

        graph = self.graph
        if method == "actuated":
            controller = ActuatedJunction(node, len(signal.phases), report)
            for link in graph.arriving[node]:
                phase = signal.phase_of[link.id]
                for lane in link.lanes:
                    at = max(lane.length - DETECTOR_GAP_S * link.speed, 0.0)
                    self._loops.append((Loop(lane, at, at + 2.0, self._out[lane]),
                                        lambda occupied, passed, phase=phase: (occupied or passed) and
                                        controller.detect(phase, self.time, signal)))
        else:
            def on(link) -> int:  # Varaiya's x upstream: the vehicles on the approach, moving or not
                return sum(len(lane.vehicles) for lane in link.lanes)

            def held(link) -> int:  # downstream, only what can block the junction: vehicles standing in a queue
                return sum(1 for lane in link.lanes for vehicle in lane.vehicles if vehicle.speed < HELD_MPS)

            def pressure(group) -> float:
                total = 0.0
                for link_id in group:
                    link = graph.links[link_id]
                    after = {turn.lane_out.link for turn in link.turns.values()}
                    total += on(link) - (sum(held(out) for out in after) / len(after) if after else 0.0)
                return total

            controller = MaxPressureJunction(node, [group for group, _ in signal.phases], pressure, report)
        signal.adapt(controller)

    def _approaches(self, nodes: list[str], flows: dict, moves: dict, weights: dict) -> list[Approach]:
        """The group's arriving links for the offline model: fed by the links arriving at their
        upstream junction when it is in the group, else evenly."""
        graph, group = self.graph, set(nodes)
        leaving: dict[str, float] = defaultdict(float)  # link -> veh/h going on from it
        for (link, _after), rate in moves.items():
            leaving[link] += rate
        approaches = []
        for node in nodes:
            signal = graph.signals[node]
            for link in graph.arriving[node]:
                flow = flows.get((node, link.id), 0.0)
                feeds = []
                if link.start in group:
                    feeds = [(upstream.id, moves[(upstream.id, link.id)] / leaving[upstream.id])
                             for upstream in graph.arriving[link.start]
                             if moves.get((upstream.id, link.id)) and leaving[upstream.id] > 0]
                fed = sum(leaving[upstream] * share for upstream, share in feeds)
                approaches.append(Approach(link.id, node, signal.phase_of[link.id],
                                           self.saturation_vph[link.id] / 3600, flow / 3600,
                                           link.length / link.speed, feeds, max(0.0, flow - fed) / 3600, weights[node]))
        return approaches

    def _adapt(self, method: str, nodes: list[str], model: TimingModel, low: float, high: float, ratios: dict) -> None:
        """Hand the group to SCATS or SCOOT, with their detectors: SCATS a loop at every lane's stop
        line, SCOOT one near the upstream end of every lane."""
        graph, network, group = self.graph, self.graph.network, set(nodes)

        def report(node: str, plan: dict) -> None:  # each plan replaced whole, so published frames keep theirs
            self.signal_plans[node] = {**self.signal_plans.get(node, {}), **plan}

        region = (ScatsRegion if method == "scats" else ScootRegion)(model.cycle, low, high)
        for node in nodes:
            signal, junction = graph.signals[node], network.node(node)
            if method == "scats":
                shares = ratios[node] if sum(ratios[node]) > 0 else [1.0] * len(ratios[node])
                busiest = max(range(len(shares)), key=shares.__getitem__)
                plans = [shares, [1.0] * len(shares)] + [[share * (factor if i == busiest else 1.0)
                                                          for i, share in enumerate(shares)] for factor in (1.25, 0.75)]
                controller = ScatsJunction(node, region, plans, model.offsets[node], junction.target_saturation, report)
                for link in graph.arriving[node]:
                    for lane in link.lanes:
                        controller.add_lane(lane, signal.phase_of[link.id])
                        self._loops.append((Loop(lane, lane.length - STOP_LINE_LOOP_M, lane.length, self._out[lane]),
                                            lambda occupied, passed, lane=lane, link=link, controller=controller:
                                            controller.measure(lane, self._states.get(link.id) == "G", occupied, passed, DT)))
            else:
                links = []
                for link in graph.arriving[node]:
                    at = min(UPSTREAM_LOOP_AT_M, link.lanes[0].length / 3)
                    links.append(ScootLink(link.id, signal.phase_of[link.id], self.saturation_vph[link.id] / 3600,
                                           (link.lanes[0].length - at) / link.speed, link.start if link.start in group else None,
                                           junction.stop_weight_s))
                    for lane in link.lanes:
                        self._loops.append((Loop(lane, at, at + UPSTREAM_LOOP_M, self._out[lane]),
                                            lambda occupied, passed, link=link, node=node:
                                            region.junctions[node].count(link.id, self.time, passed)))
                controller = ScootJunction(node, region, ratios[node], model.offsets[node],
                                           junction.target_saturation, links, report)
            signal.adapt(controller)
            if method == "scoot":
                controller.start = signal.cycle_start(0.0)

    # --- demand -------------------------------------------------------------------------

    def _use_demand(self, demand: list[PairDemand]) -> None:
        """Keep the pairs with demand that have a route; list the others as unreachable."""
        wanted = [pair for pair in demand if pair.hourly or pair.daily]
        self.demand = [pair for pair in wanted if self.graph.route(pair.origin, pair.destination)]
        self.unreachable = [f"{pair.origin}->{pair.destination}" for pair in wanted
                            if not self.graph.route(pair.origin, pair.destination)]

    def set_demand(self, demand: list[PairDemand]) -> None:
        """New demand for the run under way, from any thread: the physics takes it at its next step
        (_take_demand), so the run carries on. Signal plans stay as timed when the run began."""
        self._handed = list(demand)  # one reference swap: the physics reads whichever is latest

    def _take_demand(self) -> None:
        """Swap in the demand handed over, if any. It counts from the next minute's arrivals (those
        already drawn for this minute still come). Timetabled types keep their timetable on pairs
        that still have demand, start one on new pairs, and stop on pairs whose demand has gone."""
        handed = self._handed
        if handed is self._taken:
            return
        self._taken = handed
        before = {(pair.origin, pair.destination) for pair in self.demand}
        self._use_demand(handed)
        now = {(pair.origin, pair.destination) for pair in self.demand}
        timetabled = [index for index, mode in enumerate(self.modes) if mode.headway_min]
        self.departures = [event for event in self.departures
                           if event[3] not in timetabled or (event[2].origin, event[2].destination) in now]
        heapq.heapify(self.departures)
        for pair in self.demand:
            if (pair.origin, pair.destination) not in before and pair.timetabled:
                for index in timetabled:
                    self._schedule(self.time + self.rng.uniform(0, self.modes[index].headway_min * 60), pair, index)

    def _schedule(self, at: float, pair: PairDemand, mode_index: int) -> None:
        self._order += 1
        heapq.heappush(self.departures, (at, self._order, pair, mode_index))

    @staticmethod
    def rate(pair: PairDemand, t: float) -> float:
        """The pair's flow at time t, veh/h."""
        if pair.hourly is not None:
            return pair.hourly
        return (pair.daily or 0) * pair.shares[int(t // 3600) % 24] if pair.shares else (pair.daily or 0) / 24

    def _count_arrivals(self, start: float) -> None:
        """Schedule this minute's arrivals of each pair's untimetabled types."""
        timetabled = sum(60 / mode.headway_min for mode in self.modes if mode.headway_min)
        free = [i for i, mode in enumerate(self.modes) if not mode.headway_min]
        for pair in self.demand:
            if not free:
                continue
            per_hour = max(0.0, self.rate(pair, start) - (timetabled if pair.timetabled else 0.0))
            weights = [pair.split[i] if i < len(pair.split) else 0 for i in free]
            for _ in range(arrivals(self.rng, pair.pattern, per_hour * COUNT_INTERVAL_S / 3600)):
                mode = self.rng.choices(free, weights=weights)[0] if sum(weights) else free[0]
                self._schedule(start + self.rng.uniform(0, COUNT_INTERVAL_S), pair, mode)

    def _spawn(self, pair: PairDemand, mode_index: int) -> None:
        mode = self.modes[mode_index]
        route = self.graph.pick_route(pair.origin, pair.destination, self.rng)
        self.spawned += 1
        self.spawned_by_mode[mode.name] += 1
        vehicle = create_vehicle(mode, f"{mode.name}{self.spawned}", route, self.time, pair.origin, pair.destination,
                                 self.rng)
        self._weathered(vehicle)
        vehicle.ahead = self.graph.landmarks(route)
        vehicle.route_m = list(accumulate(link.length for link in route))
        if mode.name.lower() in BUS_TYPES:  # every bus stop on its way, in order
            vehicle.stops = [(leg, bay) for leg, link in enumerate(route) for bay in self.graph.bays.get(link.id, ())]
        self.pending[route[0].id].append(vehicle)
        if mode.headway_min:
            self._schedule(self.time + mode.headway_min * 60, pair, mode_index)

    # --- incidents, work zones, weather ---------------------------------------------------

    def close(self, road: str, lanes: int = 1, from_m: float | None = None, length_m: float = 100.0,
              start_s: float = 0.0, end_s: float = inf, speed_kmh: float | None = None,
              direction: str = "both") -> list[Closure]:
        """Close `lanes` lanes of `road`, kerbside first (all of them: the road blocked, as by a crash), over
        `length_m` metres from `from_m` along it (from its from-node; centred when None), from `start_s` to `end_s`
        simulated seconds, in both directions or one ("forward": from its from-node, "back"); and/or limit every lane
        there to `speed_kmh` (a work zone). Vehicles leave a closed lane in good time, as for a lane that ends (SUMO
        withdraws a closed lane's permission, so its bestLanes avoids it; reverse_engineering 10_EDGE_CASES), and one
        still in it stops at the closure as at a standing obstacle. Checked first: ValueError says why, nothing closed."""
        links = [link for link in self.graph.links.values() if link.road == road]
        if not links:
            raise ValueError(f"No road called {road!r} in the network.")
        if direction not in ("both", "forward", "back"):
            raise ValueError("direction is both, forward or back.")
        if direction != "both":
            links = [link for link in links if link.id.endswith(">" if direction == "forward" else "<")]
            if not links:
                raise ValueError(f"{road} is one-way: give direction both (or the one it runs).")
        most = min(len(link.lanes) for link in links)
        if not 0 <= int(lanes) <= most or (int(lanes) == 0 and speed_kmh is None):
            raise ValueError(f"lanes is 1 to {most} (or 0 with a speed_kmh for a work zone's limit only).")
        if length_m <= 0 or start_s < 0 or end_s <= start_s:
            raise ValueError("length_m must be above 0, and the closure must end after it starts.")
        if speed_kmh is not None and not 5 <= float(speed_kmh) <= 130:
            raise ValueError("speed_kmh must be 5 to 130.")
        total = self.graph.network.road(road).length_m
        start = (total - length_m) / 2 if from_m is None else float(from_m)
        made = []
        for link in links:
            lane_length = min(lane.length for lane in link.lanes)
            forward = start if link.id.endswith(">") else total - start - length_m  # from this link's own start
            low = max(forward - link.cut_start, CLOSURE_CLEAR_M)
            high = min(forward + length_m - link.cut_start, lane_length)
            if high - low < 5:
                raise ValueError(f"That stretch of {road} is too near its ends: its lanes run {CLOSURE_CLEAR_M:.0f} to "
                                 f"{lane_length:.0f} m along (the first {CLOSURE_CLEAR_M:.0f} m stay open).")
            made.append(Closure(link.id, tuple(range(int(lanes))), round(low, 1), round(high, 1), float(start_s),
                                float(end_s), None if speed_kmh is None else float(speed_kmh) / 3.6))
        self.closures += made
        return made

    def meter(self, road: str, target_occupancy_percent: float | None = None, rate_vph: float | None = None,
              detector_road: str | None = None) -> Meter:
        """Meter the on-ramp `road` METER_SETBACK_M before it merges: ALINEA towards a target occupancy (%) of the main
        road just past the merge, or a fixed rate (veh/h). The ramp must end at a merge (a junction without signals
        where it and another road come in). The main road's detector goes on the road the ramp leads to (if several:
        the one another road drives straight on into, with the most lanes), or `detector_road`. Checked first:
        ValueError says why, nothing metered."""
        links = [link for link in self.graph.links.values() if link.road == road]
        ending = [link for link in links if link.end in self.graph.network.nodes
                  and link.end not in self.graph.signals and len(self.graph.arriving[link.end]) >= 2]
        if not links:
            raise ValueError(f"No road called {road!r} in the network.")
        if len(ending) != 1:
            raise ValueError(f"{road} isn't an on-ramp: it must end, one way, at a junction without signals where "
                             "another road also comes in (a merge).")
        if (target_occupancy_percent is None) == (rate_vph is None):
            raise ValueError("Give target_occupancy_percent (ALINEA) or rate_vph (a fixed rate), one of them.")
        if target_occupancy_percent is not None and not 5 <= float(target_occupancy_percent) <= 50:
            raise ValueError("target_occupancy_percent must be 5 to 50 (about 20 is usual).")
        if rate_vph is not None and not METER_RATES_VPH[0] <= float(rate_vph) <= METER_RATES_VPH[1]:
            raise ValueError(f"rate_vph must be {METER_RATES_VPH[0]:g} to {METER_RATES_VPH[1]:g}.")
        link = ending[0]
        after = {turn.lane_out.link for turn in link.turns.values()}
        if not after:
            raise ValueError(f"{road} leads nowhere past its merge.")
        if detector_road is not None:
            named = [out for out in after if out.road == detector_road]
            if not named:
                raise ValueError(f"detector_road is one of the roads {road} leads to: "
                                 f"{', '.join(sorted({out.road for out in after}))}.")
            main = named[0]
        else:
            straight_into = {turn.lane_out.link.id for other in self.graph.arriving[link.end] if other is not link
                             for turn in other.turns.values() if turn.kind == "straight"}
            main = max(after, key=lambda out: (out.id in straight_into, len(out.lanes)))
        at = min(METER_DETECTOR_M, main.lanes[0].length / 2)
        loops = [Loop(lane, at, at + 2.0, self._out[lane]) for lane in main.lanes]
        meter = Meter(link.id, float(rate_vph) if rate_vph is not None else METER_RATES_VPH[1],
                      None if target_occupancy_percent is None else float(target_occupancy_percent), loops,
                      max(min(lane.length for lane in link.lanes) - METER_SETBACK_M, 1.0))
        self.meters[link.id] = meter
        return meter

    def _run_meters(self) -> None:
        """Each step the meters' detectors; each METER_PERIOD_S, ALINEA's new rate."""
        for meter in self.meters.values():
            for loop in meter.loops:
                occupied, _ = loop.read()
                meter.occupied += occupied
            meter.steps += 1
            if meter.steps * DT >= METER_PERIOD_S - 1e-9:
                occupancy = 100.0 * meter.occupied / (meter.steps * len(meter.loops))
                if meter.target is not None:
                    meter.rate = min(max(meter.rate + METER_K_R * (meter.target - occupancy), METER_RATES_VPH[0]),
                                     METER_RATES_VPH[1])
                meter.history.append((round(self.time, 1), round(meter.rate), round(occupancy, 1)))
                meter.occupied, meter.steps = 0.0, 0

    def fork(self) -> "Simulation":
        """A copy of the run as it is now, to run on by itself (rollout): the same vehicles, signals, random numbers and
        demand to come. Its signal override, observations and detectors aren't copied, and adaptive or actuated
        signals in it keep their current greens. Copying takes a few tens of milliseconds for a few hundred vehicles."""
        memo = {id(self.signal_override): None, id(self.observed): None, id(self._loops): []}
        box: dict = {}

        def copy_it() -> None:
            limit = sys.getrecursionlimit()
            sys.setrecursionlimit(max(limit, FORK_RECURSION))
            try:
                box["copy"] = deepcopy(self, memo)
            except BaseException as exc:  # noqa: BLE001 - handed back to the caller's thread
                box["error"] = exc
            finally:
                sys.setrecursionlimit(limit)

        with _fork_lock:
            size = threading.stack_size(FORK_STACK_BYTES)
            try:
                thread = threading.Thread(target=copy_it, name="fork", daemon=True)
                thread.start()
            finally:
                threading.stack_size(size)
            thread.join()
        if "error" in box:
            raise box["error"]
        copy = box["copy"]
        for signal in copy.graph.signals.values():
            if signal.controller is not None:
                signal.controller = _Held()
        return copy

    def rollout(self, actions: dict, horizon_s: float, watch=()) -> dict:
        """What each action would do over the next `horizon_s` (Explainable AI for ITS Ch 2, model-based planning:
        candidate actions rolled forward from the state now): each on a fork of its own with it as the signal
        override ((junction, road, planned state, context) -> state; None: the plan). Per action the person-seconds of
        delay (each vehicle's time short of its free speed, times its people) and the vehicle-seconds, and for each
        `watch`ed vehicle how far it got (metres) and whether it left the link it was on."""
        people = {mode.name: float(getattr(mode, "occupancy", 1.0)) for mode in self.modes}
        found = {}
        for name, override in actions.items():
            copy = self.fork()
            copy.signal_override = override
            start = {v.id: (v.leg, v.route_m[v.leg - 1] if v.leg else 0.0, v.s) for v in copy.vehicles if v.id in set(watch)}
            end, person_s, vehicle_s = copy.time + horizon_s, 0.0, 0.0
            while copy.time < end - 1e-9:
                copy.step()
                for vehicle in copy.vehicles:
                    free = free_speed(vehicle, vehicle.segment.speed)
                    lost = DT * max(0.0, 1.0 - vehicle.speed / max(free, 0.1))
                    vehicle_s += lost
                    person_s += lost * (vehicle.load if vehicle.load is not None else people.get(vehicle.mode, 1.0))
            now = {v.id: v for v in copy.vehicles}
            watched = {}
            for vehicle_id, (leg, before, s) in start.items():
                vehicle = now.get(vehicle_id)
                if vehicle is None:
                    watched[vehicle_id] = {"arrived": True}
                    continue
                along = (vehicle.route_m[vehicle.leg - 1] if vehicle.leg else 0.0) + vehicle.s
                watched[vehicle_id] = {"metres": round(along - before - s, 1), "left_its_link": vehicle.leg > leg}
            found[name] = {"person_delay_s": round(person_s), "vehicle_delay_s": round(vehicle_s), "watched": watched}
        return found

    def set_weather(self, name: str) -> None:
        """Adverse weather for the vehicles spawned from now (WEATHER): ValueError for an unknown one."""
        if name not in WEATHER:
            raise ValueError(f"weather is one of {', '.join(WEATHER)}.")
        self.weather = name

    def _weathered(self, vehicle: Vehicle) -> None:
        speed, headway = WEATHER[self.weather]
        vehicle.speed_factor *= speed
        vehicle.tau *= headway

    def _update_closures(self) -> None:
        """The closures on now; when that changes, what each lane and link holds, and a new reach key."""
        active = tuple(c for c in self.closures if c.start_s <= self.time < c.end_s)
        if active == self._active_closures:
            return
        self._active_closures = active
        self._closure_key += 1
        closed, zones = defaultdict(list), defaultdict(list)
        for closure in active:
            for index in closure.lanes:
                closed[(closure.link, index)].append((closure.from_m, closure.to_m))
            if closure.speed:
                zones[closure.link].append((closure.from_m, closure.to_m, closure.speed))
        self._closed, self._zones = dict(closed), dict(zones)

    def _closed_ahead(self, vehicle: Vehicle) -> dict[int, dict[int, float]]:
        """leg -> {lane index: metres along it where a closure ahead of the vehicle starts}, its route's links."""
        found: dict[int, dict[int, float]] = {}
        for leg in range(vehicle.leg, len(vehicle.route)):
            link = vehicle.route[leg]
            for index in range(len(link.lanes)):
                starts = [start for start, end in self._closed.get((link.id, index), ())
                          if leg > vehicle.leg or start > vehicle.s]
                if starts:
                    found.setdefault(leg, {})[index] = min(starts)
        return found

    def _closure_ahead(self, vehicle: Vehicle, lane: Lane) -> tuple[float, float, float] | None:
        """A closed section ahead in its lane: a standing obstacle where it starts (not one it is already in)."""
        starts = [start for start, _ in self._closed.get((lane.link.id, lane.index), ()) if start >= vehicle.s - 1e-6]
        return (min(starts) - vehicle.s - STOP_MARGIN_M, 0.0, vehicle.decel) if starts else None

    def dispatch(self, origin: str, destination: str, every_s: float = 0.0, after_s: float = 0.0) -> None:
        """Send an emergency vehicle from `origin` to `destination` (boundary ends or junctions) `after_s` seconds
        after the next step (0: then), and again every `every_s` seconds after that (0: once). Safe from any
        thread. ValueError when there's no route between them."""
        if origin == destination or not self.graph.route(origin, destination):
            raise ValueError(f"No route from {origin} to {destination}.")
        if every_s < 0 or after_s < 0:
            raise ValueError("A frequency or delay can't be negative.")
        self._dispatched.append((origin, destination, float(every_s), float(after_s)))

    def _spawn_emergency(self, origin: str, destination: str) -> None:
        """An emergency vehicle, of the project's type called Emergency if it has one, waiting to enter."""
        mode = next((mode for mode in self.modes if mode.name.lower() == "emergency"), None) or SimpleNamespace(**EMERGENCY_MODE)
        route = self.graph.route(origin, destination)
        self.spawned += 1
        self.spawned_by_mode[mode.name] += 1
        vehicle = create_vehicle(mode, f"{mode.name}{self.spawned}", route, self.time, origin, destination, self.rng)
        self._weathered(vehicle)
        vehicle.ahead = self.graph.landmarks(route)
        vehicle.route_m = list(accumulate(link.length for link in route))
        vehicle.emergency = True
        self.emergency_trips[vehicle.id] = {"id": vehicle.id, "from": origin, "to": destination,
                                            "dispatched_s": self.time, "arrived_s": None}
        self.pending[route[0].id].append(vehicle)

    def _insert(self) -> None:
        """Put waiting vehicles on their first link where a lane has room (else they keep waiting)."""
        for link_id, queue in self.pending.items():
            while queue:
                vehicle = queue[0]
                link = self.graph.links[link_id]
                reach = self._lane_reach(vehicle)
                lanes = sorted(link.lanes, key=lambda lane: (-reach[lane.index], len(lane.vehicles)))
                for lane in lanes:
                    rear = lane.vehicles[0] if lane.vehicles else None
                    room = (rear.s - rear.length_m) if rear else lane.length
                    if room >= vehicle.length_m + vehicle.min_gap:
                        free = free_speed(vehicle, lane.speed)
                        speed = free if rear is None else max(0.0, min(free, rear.speed))
                        vehicle.segment, vehicle.s, vehicle.speed = lane, vehicle.length_m, speed
                        _place(lane, vehicle)
                        vehicle.pose = vehicle.prev_pose = self._pose(vehicle)
                        self.vehicles.append(vehicle)
                        queue.popleft()
                        break
                else:
                    break  # the queue waits for room

    # --- where vehicles are going -------------------------------------------------------

    def _next_turn(self, vehicle: Vehicle) -> Turn | None:
        if not isinstance(vehicle.segment, Lane) or vehicle.leg + 1 >= len(vehicle.route):
            return None
        return vehicle.segment.link.turns.get((vehicle.segment.index, vehicle.route[vehicle.leg + 1].id))

    @staticmethod
    def reach(route: list, leg: int, stops: dict | None = None, any_lane: bool = False,
              closed: dict | None = None) -> list[float]:
        """For each lane of route[leg]: metres from its start that a vehicle could drive along the
        route without changing lane (SUMO's bestLanes), looking REACH_HORIZON_M ahead. A lane that
        can't take the next turn ends at its stop line; the lanes that can go on into the lanes
        their turn leads to. Parallel lanes count their road's length, so they tie exactly. A bus
        stop to call at (`stops`: leg -> metres along the kerbside lane where its bay starts) ends
        the other lanes of that link there: only the kerbside lane goes on past it (so too before a signal,
        for an emergency vehicle). `any_lane`: every turn may be made from any lane (an emergency vehicle's).
        `closed`: leg -> {lane index: metres}: a closed lane ends where its closure starts (close())."""
        stops, closed = stops or {}, closed or {}

        def kerb(k: int, lanes: list[float]) -> list[float]:
            if k in stops:
                lanes = [metres if i == 0 else min(metres, stops[k]) for i, metres in enumerate(lanes)]
            if k in closed:
                lanes = [min(metres, closed[k][i]) if i in closed[k] else metres for i, metres in enumerate(lanes)]
            return lanes

        end, ahead = leg, 0.0
        while end + 1 < len(route) and ahead < REACH_HORIZON_M:
            end += 1
            ahead += route[end].length
        reach = kerb(end, [REACH_HORIZON_M] * len(route[end].lanes))  # the destination, or far enough: any lane will do
        for k in range(end - 1, leg - 1, -1):
            link, after = route[k], route[k + 1].id
            reach = kerb(k, [link.length + reach[turn.lane_out.index]
                             if (turn := link.turns.get((lane.index, after))) is not None and (turn.permitted or any_lane)
                             else lane.length
                             for lane in link.lanes])
        return reach

    def _lane_reach(self, vehicle: Vehicle) -> list[float]:
        key = (vehicle.leg, len(vehicle.stops), self._closure_key)
        if vehicle.reach_key != key:
            stops: dict[int, float] = {}
            for leg, bay in vehicle.stops:
                stops.setdefault(leg, bay.entry_s)  # the first bay on each link
            if vehicle.emergency:  # the kerbside lane over the last EMERGENCY_LANE_M before each signal
                for leg, link in enumerate(vehicle.route):
                    if link.end in self.graph.signals:
                        stops[leg] = max(0.0, link.lanes[0].length - EMERGENCY_LANE_M)
            vehicle.reach = self.reach(vehicle.route, vehicle.leg, stops, any_lane=vehicle.emergency,
                                       closed=self._closed_ahead(vehicle) if self._closed else None)
            vehicle.reach_key = key
        return vehicle.reach

    @staticmethod
    def _next_bay(vehicle: Vehicle) -> Bay | None:
        """The bay of the bus stop it calls at next, if it is on the link it is driving."""
        if vehicle.stops and vehicle.stops[0][0] == vehicle.leg:
            return vehicle.stops[0][1]
        return None

    # --- junctions ----------------------------------------------------------------------

    def signal_states(self) -> dict[tuple[str, str], str]:
        """(junction, arriving road) -> "G", "Y" or "R", for the map's signal heads."""
        return {(node, link.road): self._states[link.id]
                for node in self.graph.signals for link in self.graph.arriving[node]}

    def _lane_state(self, lane: Lane) -> str:
        """The signal a lane's vehicles obey: its own (an override's lane state), else its arm's."""
        return self._lane_states.get((lane.link.id, lane.index)) or self._states[lane.link.id]

    def _signal_for(self, vehicle: Vehicle, lane: Lane) -> str:
        """The signal a vehicle obeys in this lane: the lane's own, else its arm's. An emergency vehicle obeys red
        too, unless a lane of its arm is set apart green (DEVL's flashing green: every other movement is red then),
        whichever lane it is in."""
        if vehicle.emergency and any(state == "G" for (link_id, _), state in self._lane_states.items()
                                     if link_id == lane.link.id):
            return "G"
        return self._lane_state(lane)

    def lane_states(self) -> dict[tuple[str, str, int], str]:
        """(junction, arriving road, lane index) -> "G", "Y" or "R" for each lane an override set apart."""
        return {(self.graph.links[link_id].end, self.graph.links[link_id].road, index): state
                for (link_id, index), state in self._lane_states.items()}

    def _time_step_signals(self) -> None:
        """This step's signal states: each junction's plan, then the signal override, if one is set
        (the Transit Signal Priority and Dynamic Emergency Vehicle Lane extensions). An override returns the
        arm's state, or (the arm's state, {lane index: that lane's own state}). With no override set, nothing
        else runs."""
        override, states, lanes = self.signal_override, {}, {}
        for node, signal in self.graph.signals.items():
            check = getattr(signal.controller, "check", None)
            if check is not None:  # actuated, max-pressure: extend, rest or end the green now
                check(signal, self.time)
            view = self._junction_view(node, signal) if override is not None else None
            for link in self.graph.arriving[node]:
                state = signal.state(link.id, self.time)
                if override is not None:
                    state = override(node, link.road, state,
                                     {**view, "planned": state, "approaching": view["arms"][link.road]["approaching"]})
                    if isinstance(state, tuple):
                        state, own = state
                        lanes.update({(link.id, index): lane_state for index, lane_state in own.items()})
                states[link.id] = state
        self._states, self._lane_states = states, lanes

    def _junction_view(self, node: str, signal: Signal) -> dict:
        """What a signal override sees, once per junction per step: the time and plan; the plan's
        phases (the arms each serves, its green), amber, all-red and minimum green; each arm's
        vehicles, nearest the stop line first (type, distance to it, speed, lane, people on board by
        its type's occupancy, the exit lane it will take), speed limit and length; each of those exit
        lanes' room (`exit_room_m`: its length less what its vehicles and those turning into it take
        up, gaps included, as _may_enter reckons it); the vehicles crossing the junction; and the
        distance across it, stop line to far stop line; and the cycle (its length, when it began, the
        offset). The arm's own vehicles are also `approaching` in each arm's context."""
        cycle_start = signal.cycle_start(self.time)  # first: an adaptive plan moves on to now
        arriving = self.graph.arriving[node]
        if node not in self._box_m:
            self._box_m[node] = 2 * max(self.graph._trim(node, self.graph.network.road(link.road)) for link in arriving)
        if not hasattr(self, "_people"):
            self._people = {mode.name: float(getattr(mode, "occupancy", 1.0)) for mode in self.modes}
        rooms: dict = {}  # each exit lane a vehicle here will take: its room, as _may_enter reckons it

        def exit_of(vehicle) -> str | None:
            turn = self._next_turn(vehicle)
            if turn is None:
                return None
            lane = turn.lane_out
            if lane.name not in rooms:
                taken = sum(v.length_m + v.min_gap for v in lane.vehicles)
                taken += sum(v.length_m + v.min_gap for t in self._turns_into(lane) for v in t.vehicles)
                rooms[lane.name] = round(lane.length - taken, 1)
            return lane.name

        arms = {link.road: {"approaching": sorted(
                    ({"id": vehicle.id, "type": vehicle.mode, "distance_m": round(lane.length - vehicle.s, 1),
                      "speed_kmh": round(vehicle.speed * 3.6, 1), "lane": lane.index,
                      "people": vehicle.load if vehicle.load is not None else self._people.get(vehicle.mode, 1.0),
                      "exit": exit_of(vehicle),
                      "emergency": vehicle.emergency}
                     for lane in link.lanes for vehicle in lane.vehicles), key=lambda vehicle: vehicle["distance_m"]),
                "speed_limit_mps": link.speed, "length_m": min(lane.length for lane in link.lanes),
                "lanes": len(link.lanes)}
                for link in arriving}
        road_of = {link.id: link.road for link in arriving}
        return {"time_s": self.time, "plan": self.signal_plans.get(node, {}), "arms": arms, "exit_room_m": rooms,
                "crossing": [vehicle.id for link in arriving for turn in link.turns.values() for vehicle in turn.vehicles],
                "phases": [{"arms": sorted(road_of[link] for link in group), "green_s": green}
                           for group, green in signal.phases],
                "amber_s": AMBER_S, "all_red_s": ALL_RED_S, "min_green_s": MIN_GREEN_S, "box_m": self._box_m[node],
                "cycle_s": signal.cycle, "cycle_start_s": cycle_start, "offset_s": signal.offset,
                "rollout": self.rollout}  # what-ifs from this moment (an agent's look-ahead)

    def _may_enter(self, vehicle: Vehicle, turn: Turn) -> bool:
        lane = vehicle.segment
        distance = lane.length - vehicle.s
        can_stop = distance >= vehicle.speed * vehicle.speed / (2 * vehicle.decel)
        signal = self.graph.signals.get(turn.node)
        state = self._signal_for(vehicle, lane) if signal else "G"

        # Waited at the line all green for a gap it gives way to: it goes on amber, once the oncoming
        # traffic stops for its own amber (_gaps_clear), as drivers turning across traffic do.
        clearing = state == "Y" and vehicle.speed < 0.1 and any(give_way for *_, give_way in turn.conflicts)
        if state == "R" or (state == "Y" and can_stop and not clearing):
            return False
        if not turn.permitted and not vehicle.emergency:  # in a lane its turn isn't made from: impatient only while it could go
            if vehicle.speed < 0.1:
                vehicle.impatience_s += DT
            if vehicle.impatience_s < IMPATIENCE_S:
                return False
        # Keep the junction clear: room on the exit lane, its length less what its vehicles and those
        # on their way there take up, each with its gap (SUMO's brutto occupancy). A queue on the exit
        # lane leaves room behind it; traffic moving off it doesn't take the room a queue would.
        exit_lane = turn.lane_out
        taken = sum(v.length_m + v.min_gap for v in exit_lane.vehicles)
        taken += sum(v.length_m + v.min_gap for t in self._turns_into(exit_lane) for v in t.vehicles)
        if exit_lane.length - taken < vehicle.length_m + vehicle.min_gap and can_stop and \
                (signal is not None or turn.ring or self._foes_coming(turn)):
            return False  # an uncontrolled node (a merge, a bend) is kept clear only for someone coming
        if turn.ring:
            return self._ring_clear(vehicle, turn)  # a give-way line: always yield
        if not self._occupied_clear(turn):
            return False  # never into an occupied crossing or merge, however close
        return self._gaps_clear(turn) or not can_stop  # too close to stop for a gap: it goes (committed)

    @staticmethod
    def _foes_coming(turn: Turn) -> bool:
        """A vehicle on, or on its way to, a movement crossing or merging with this turn."""
        return any(other.vehicles or any(foe.turn is other for foe in other.lane_in.vehicles)
                   for other, *_ in turn.conflicts)

    def _turns_into(self, lane: Lane) -> list[Turn]:
        if lane not in self._into:
            self._into[lane] = [turn for link in self.graph.arriving[lane.link.start]
                                for turn in link.turns.values() if turn.lane_out is lane]
        return self._into[lane]

    @staticmethod
    def _occupied_clear(turn: Turn) -> bool:
        """No vehicle on a crossing or merging turn is still short of the conflict point, counting
        the tail of one that has driven out of it but not yet cleared the junction. Only that turn's
        own: a vehicle ahead that came through this turn (the queue discharging) is a leader, not a
        foe, even where the two turns merge into one lane."""
        for other, _here, there, _give_way in turn.conflicts:
            if any(foe.s - foe.length_m < there + 1.0 for foe in other.vehicles):
                return False
            if any(other.length + foe.s - foe.length_m < there + 1.0
                   for foe in other.lane_out.vehicles if foe.s < foe.length_m and foe.came_from is other):
                return False
        return True

    def _gaps_clear(self, turn: Turn) -> bool:
        """Gap acceptance: nothing it gives way to would reach the conflict within the critical gap,
        leaving out vehicles stopping for their red or amber."""
        for other, _here, there, give_way in turn.conflicts:
            if give_way:  # gap acceptance against vehicles arriving for that turn
                signal = self.graph.signals.get(other.node)
                state = self._lane_state(other.lane_in) if signal else "G"
                if state == "R":
                    continue
                gap = CRITICAL_GAP_S["offside" if signal else "minor"]
                for foe in other.lane_in.vehicles:
                    if foe.turn is other:
                        distance = other.lane_in.length - foe.s
                        if state == "Y" and distance >= foe.speed * foe.speed / (2 * foe.decel):
                            continue  # it can stop for its amber, so it will
                        if (distance + there) / max(foe.speed, 1.0) < gap:
                            return False
        return True

    def _ring_angle(self, turn: Turn, s: float) -> float | None:
        on_m, off_m, on, _off, spin, radius = turn.ring
        if on_m <= s <= off_m:
            return on + spin * (s - on_m) / radius
        return None

    def _ring_clear(self, vehicle: Vehicle, turn: Turn) -> bool:
        """Roundabout entry: give way to circulating traffic that would reach the entry point
        within the critical gap, and never cut in beside a vehicle already there."""
        _on_m, _off_m, entry, _off, spin, radius = turn.ring
        for link in self.graph.arriving[turn.node]:
            for other in link.turns.values():
                for foe in other.vehicles:
                    angle = self._ring_angle(other, foe.s)
                    if angle is None:
                        if foe.s < other.ring[0] and other.lane_in is not turn.lane_in:
                            angle = other.ring[2]  # about to join the ring at its own entry
                        else:
                            continue
                    to_us = ((entry - angle) * spin) % (2 * pi) * radius  # metres round to our entry
                    leaves = ((other.ring[3] - angle) * spin) % (2 * pi) * radius
                    if leaves < to_us:
                        continue  # it exits before reaching us
                    if to_us < foe.length_m + vehicle.length_m or to_us / max(foe.speed, 1.0) < CRITICAL_GAP_S["roundabout"]:
                        return False
        return True

    # --- one step -------------------------------------------------------------------------

    def _leader(self, vehicle: Vehicle, ahead: Vehicle | None) -> tuple[float, float, float] | None:
        """(free gap, speed, braking) of whatever the vehicle must keep behind, or None. `ahead`:
        the next vehicle on its own lane or turn, if any."""
        segment, s = vehicle.segment, vehicle.s
        best = self._ring_leader(vehicle, segment) if isinstance(segment, Turn) and segment.ring else None
        if ahead is not None:
            found = (ahead.s - ahead.length_m - s - vehicle.min_gap, ahead.speed, ahead.decel)
            return found if best is None or found[0] < best[0] else best
        distance = segment.length - s
        if isinstance(segment, Lane):
            tail = self._tail_ahead(segment, distance, vehicle)
            if tail is not None and (best is None or tail[0] < best[0]):
                best = tail
            turn = vehicle.turn
            if turn is None:
                return None  # its destination: drives off the end
            if not vehicle.may_enter:
                stop = (distance - STOP_MARGIN_M, 0.0, vehicle.decel)
                return stop if best is None or stop[0] < best[0] else best
            following = [turn, turn.lane_out]
        else:
            following = [segment.lane_out]
            merging = self._merging_leader(vehicle, segment, distance)
            if merging is not None and (best is None or merging[0] < best[0]):
                best = merging
        for piece in following:
            if distance > LOOKAHEAD_M:
                break
            if piece.vehicles:
                rear = piece.vehicles[0]
                found = (distance + rear.s - rear.length_m - vehicle.min_gap, rear.speed, rear.decel)
                return found if best is None or found[0] < best[0] else best
            distance += piece.length
        return best

    def _tail_ahead(self, lane: Lane, distance: float, vehicle: Vehicle) -> tuple[float, float, float] | None:
        """A vehicle that has driven from this lane into any of its turns but whose tail is still
        on the lane: whichever way it went, it is still in the way."""
        best = None
        for turn in self._out[lane]:
            for foe in turn.vehicles:
                if foe.s < foe.length_m:
                    gap = distance + foe.s - foe.length_m - vehicle.min_gap
                    if best is None or gap < best[0]:
                        best = (gap, foe.speed, foe.decel)
        return best

    def _merging_leader(self, vehicle: Vehicle, turn: Turn, distance: float) -> tuple[float, float, float] | None:
        """A vehicle on another turn into the same lane that is nearer the merge goes first."""
        best = None
        for other in self._turns_into(turn.lane_out):
            if other is turn:
                continue
            for foe in other.vehicles:
                foe_distance = other.length - foe.s
                if foe_distance < distance:
                    gap = distance - foe_distance - foe.length_m - vehicle.min_gap
                    if best is None or gap < best[0]:
                        best = (gap, foe.speed, foe.decel)
        return best

    @staticmethod
    def _ahead_on(segment, vehicle: Vehicle) -> Vehicle | None:
        vehicles, index = segment.vehicles, vehicle.order
        if index >= len(vehicles) or vehicles[index] is not vehicle:
            index = vehicles.index(vehicle)  # its list changed since the step began
        return vehicles[index + 1] if index + 1 < len(vehicles) else None

    def _ring_leader(self, vehicle: Vehicle, turn: Turn) -> tuple[float, float, float] | None:
        """Nearest vehicle ahead on the ring, whichever turn it is on."""
        angle = self._ring_angle(turn, vehicle.s)
        if angle is None:
            return None
        spin, radius = turn.ring[4], turn.ring[5]
        best = None
        for link in self.graph.arriving[turn.node]:
            for other in link.turns.values():
                if other is turn:
                    continue
                for foe in other.vehicles:
                    foe_angle = self._ring_angle(other, foe.s)
                    if foe_angle is None:
                        continue
                    ahead = ((foe_angle - angle) * spin) % (2 * pi) * radius
                    if ahead < LOOKAHEAD_M:
                        gap = ahead - foe.length_m - vehicle.min_gap
                        if best is None or gap < best[0]:
                            best = (gap, foe.speed, foe.decel)
        return best

    def _limit(self, vehicle: Vehicle) -> float:
        """Its free speed at the speed limit (free_speed: its speed factor, top and desired speed),
        lowered in good time for a slower turn ahead."""
        segment = vehicle.segment
        limit = free_speed(vehicle, segment.speed)
        turn = vehicle.turn
        if turn is not None:
            distance = max(segment.length - vehicle.s, 0.0)
            limit = min(limit, sqrt(free_speed(vehicle, turn.speed) ** 2 + 2 * vehicle.decel * distance))
        for start, end, speed in self._zones.get(segment.link.id, ()) if self._zones and isinstance(segment, Lane) else ():
            if vehicle.s < end:  # a work zone's limit, slowing for it in good time
                ahead = max(start - vehicle.s - vehicle.speed * DT, 0.0)  # what is left once this step is driven
                limit = min(limit, sqrt(free_speed(vehicle, speed) ** 2 + 2 * vehicle.decel * ahead))
        bay = self._next_bay(vehicle)
        if bay is not None and isinstance(segment, Lane) and segment.link is bay.link:  # slowing to pull in
            limit = min(limit, sqrt(free_speed(vehicle, bay.speed) ** 2
                                    + 2 * vehicle.decel * max(bay.entry_s - vehicle.s, 0.0)))
        return limit

    def _plan(self, vehicle: Vehicle) -> None:
        turn, segment = vehicle.turn, vehicle.segment
        ahead = self._ahead_on(segment, vehicle)
        # Only the first vehicle in its lane can reach the junction this step (the rest follow it),
        # and far off there is no need to ask yet.
        near = turn is not None and ahead is None and segment.length - vehicle.s < LOOKAHEAD_M
        vehicle.may_enter = near and self._may_enter(vehicle, turn)
        if isinstance(segment, Bay):
            if vehicle.dwell_end is None and vehicle.speed < 0.1 and vehicle.s >= segment.stop_s - 1.0:
                vehicle.dwell_end = self.time + self._serve(vehicle, segment)  # at the stop: passengers off and on
            # may_enter, in a bay: may merge back into the lane.
            vehicle.may_enter = ahead is None and self._dwelt(vehicle) and \
                self._merge_clear(vehicle, segment.lane, segment.exit_s)
            leader = self._bay_leader(vehicle, segment, ahead)
        else:
            leader = self._leader(vehicle, ahead)
            if segment in self._bays_on:
                leader = _closer(leader, self._bay_ahead(vehicle, segment))
            if self._closed and isinstance(segment, Lane):
                leader = _closer(leader, self._closure_ahead(vehicle, segment))
            meter = self.meters.get(segment.link.id) if self.meters and isinstance(segment, Lane) else None
            if meter is not None and vehicle.s < meter.at and not meter.green(self.time):
                leader = _closer(leader, (meter.at - vehicle.s - STOP_MARGIN_M, 0.0, vehicle.decel))  # its red
        if vehicle.yield_to is not None:  # letting a changer in: slowing for it, but no harder than comfortable
            letting_in = self._letting_in(vehicle)
            if letting_in is not None:
                polite = max(safe_speed(vehicle, *letting_in), vehicle.speed - vehicle.decel * DT, 0.0)
                vehicle.advice = polite if vehicle.advice is None else min(vehicle.advice, polite)
        vsafe = safe_speed(vehicle, *leader) if leader else float("inf")
        limit = self._limit(vehicle)
        if vehicle.advice is not None:
            limit, vehicle.advice = min(limit, vehicle.advice), None
        vehicle.next_speed = start_up(vehicle, next_speed(vehicle, vsafe, limit, DT, self.rng), DT)

    def _letting_in(self, vehicle: Vehicle) -> tuple[float, float, float] | None:
        """Cooperation (LC2013): the vehicle that asked to change into this one's lane, ahead of it
        in the next lane, is kept behind like a leader, so a gap opens for it."""
        changer, vehicle.yield_to = vehicle.yield_to, None
        lane = vehicle.segment if isinstance(vehicle.segment, Lane) else vehicle.segment.lane_out
        if not isinstance(changer.segment, Lane) or changer.segment.link is not lane.link:
            return None  # it has driven on
        ahead = changer.s * lane.length / max(changer.segment.length, 1e-6)
        behind = vehicle.s if vehicle.segment is lane else vehicle.s - vehicle.segment.length
        if ahead <= behind:
            return None  # it dropped back behind this one instead
        return ahead - changer.length_m - behind - vehicle.min_gap, changer.speed, changer.decel

    def _move(self, vehicle: Vehicle) -> bool:
        """Move one step; False once it has reached its destination and left."""
        vehicle.speed = vehicle.next_speed
        vehicle.waiting_s = vehicle.waiting_s + DT if vehicle.speed < 0.1 else 0.0
        vehicle.s += vehicle.speed * DT
        if vehicle.stops and isinstance(vehicle.segment, Lane):
            self._pull_in(vehicle)
        while vehicle.s > vehicle.segment.length:
            segment = vehicle.segment
            if isinstance(segment, Bay):  # the end of its bay: back into the lane, if the gap is still safe
                if not vehicle.may_enter or not self._merge_clear(vehicle, segment.lane, segment.exit_s):
                    vehicle.s, vehicle.speed = segment.length, 0.0
                    break
                over, next_piece = segment.exit_s + vehicle.s - segment.length, segment.lane
                vehicle.dwell_end = None
            elif isinstance(segment, Lane):
                turn = self._next_turn(vehicle)
                if turn is None:  # the end of its last link: arrived
                    segment.vehicles.remove(vehicle)
                    self.arrived += 1
                    if vehicle.id in self.emergency_trips:
                        self.emergency_trips[vehicle.id]["arrived_s"] = self.time
                    return False
                # Re-checked now: a vehicle that moved earlier this step may just have entered a
                # crossing or merging turn (everyone planned from the same moment).
                clear = self._ring_clear(vehicle, turn) if turn.ring else self._occupied_clear(turn)
                if not vehicle.may_enter or not clear:
                    vehicle.s, vehicle.speed = segment.length, 0.0
                    break
                over, next_piece = vehicle.s - segment.length, turn
            else:
                over, next_piece = vehicle.s - segment.length, segment.lane_out
                vehicle.leg, vehicle.came_from = vehicle.leg + 1, segment
            segment.vehicles.remove(vehicle)
            vehicle.segment, vehicle.s = next_piece, over
            _place(next_piece, vehicle)
        return True

    # --- bus stops -----------------------------------------------------------------------

    def _pull_in(self, vehicle: Vehicle) -> None:
        """A bus reaching the bay of its next stop in the kerbside lane pulls in off the lane. One
        that passes it in another lane, or has left its link, has missed it."""
        leg, bay = vehicle.stops[0]
        if leg < vehicle.leg:
            vehicle.stops.pop(0)
        elif leg == vehicle.leg and vehicle.s >= bay.entry_s:
            vehicle.stops.pop(0)
            if vehicle.segment is bay.lane and vehicle.s - bay.entry_s < BAY_ENTRY_M:
                bay.lane.vehicles.remove(vehicle)
                vehicle.segment, vehicle.s, vehicle.dwell_end = bay, vehicle.s - bay.entry_s, None
                _place(bay, vehicle)

    def _serve(self, vehicle: Vehicle, bay: Bay) -> float:
        """The bus's dwell at this stop: the stop's dwell_s with no passengers set, else its passengers off and on
        (HCM 2000 Eq 27-2; see DOOR_S): those alighting a share of its load, those boarding as many of the waiting
        (Poisson arrivals at the stop's rate since it was last counted) as fit."""
        if not bay.boarding_per_h and not bay.alighting_share:
            return bay.dwell_s
        rng, key = self._passenger_rng, id(bay)
        waiting = self._waiting.setdefault(key, [0, 0.0])
        waiting[0] += _poisson(rng, bay.boarding_per_h * (self.time - waiting[1]) / 3600)
        waiting[1] = self.time
        load = vehicle.load or 0.0
        alighting = sum(rng.random() < bay.alighting_share for _ in range(int(load)))
        capacity = round(CAPACITY_PER_M * vehicle.length_m)
        boarding = min(waiting[0], max(capacity - (load - alighting), 0))
        waiting[0] -= boarding
        vehicle.load = load - alighting + boarding
        standing = vehicle.load > SEATED_SHARE * capacity
        dwell = DOOR_S + alighting * ALIGHT_S + boarding * (BOARD_S + (STANDING_BOARD_S if standing else 0.0))
        if self.observed is not None:
            self.observed["boardings"].append((bay.stop, bay.lane.link.id, round(self.time, 1), vehicle.id, boarding,
                                               alighting, waiting[0], vehicle.load, round(dwell, 1)))
        return dwell

    def _dwelt(self, vehicle: Vehicle) -> bool:
        return vehicle.dwell_end is not None and self.time >= vehicle.dwell_end

    def _bay_leader(self, vehicle: Vehicle, bay: Bay, ahead: Vehicle | None) -> tuple[float, float, float] | None:
        """In a bay: the bus ahead; the stop, until it has stood there for its dwell time; then the
        end of the bay until it may merge back, and past it the traffic in the lane."""
        best = None if ahead is None else (ahead.s - ahead.length_m - vehicle.s - vehicle.min_gap, ahead.speed, ahead.decel)
        if not self._dwelt(vehicle):
            return _closer(best, (bay.stop_s - vehicle.s - STOP_MARGIN_M, 0.0, vehicle.decel))
        distance = bay.length - vehicle.s
        if not vehicle.may_enter:
            return _closer(best, (distance - STOP_MARGIN_M, 0.0, vehicle.decel))
        lane = bay.lane
        index = bisect_right(lane.vehicles, bay.exit_s, key=_position)
        if index < len(lane.vehicles):
            rear = lane.vehicles[index]
            best = _closer(best, (distance + rear.s - rear.length_m - bay.exit_s - vehicle.min_gap, rear.speed, rear.decel))
        return best

    def _bay_ahead(self, vehicle: Vehicle, lane: Lane) -> tuple[float, float, float] | None:
        """On a lane with bus stops: a bus still pulling into a bay ahead has its tail in the way.
        A bus about to pull in itself keeps behind the last bus in its bay (it waits while it's full)."""
        mine, best = self._next_bay(vehicle), None
        for bay in self._bays_on[lane]:
            if bay.entry_s < vehicle.s or not bay.vehicles:
                continue
            rear = bay.vehicles[0]
            if bay is mine or rear.s < rear.length_m:
                best = _closer(best, (bay.entry_s - vehicle.s + rear.s - rear.length_m - vehicle.min_gap,
                                      rear.speed, rear.decel))
        return best

    def _neighbours(self, lane: Lane, s: float) -> tuple[float, Vehicle | None, float, Vehicle | None]:
        """(position, vehicle) of the nearest leader and follower of a point `s` metres along the
        lane, (0, None) for none. Leaders: ahead on the lane, or gone on into a turn with the tail
        still on it. Followers: behind on the lane, or on a turn about to join it (placed behind its
        start)."""
        index = bisect_right(lane.vehicles, s, key=_position)  # the lists stay ordered by position
        leaders = [(lane.vehicles[index].s, lane.vehicles[index])] if index < len(lane.vehicles) else []
        leaders += [(lane.length + foe.s, foe) for turn in self._out[lane] for foe in turn.vehicles
                    if foe.s < foe.length_m]
        followers = [(lane.vehicles[index - 1].s, lane.vehicles[index - 1])] if index else []
        followers += [(foe.s - turn.length, foe) for turn in self._turns_into(lane) for foe in turn.vehicles]
        leader_s, leader = min(leaders, key=lambda pair: pair[0]) if leaders else (0.0, None)
        follower_s, follower = max(followers, key=lambda pair: pair[0]) if followers else (0.0, None)
        return leader_s, leader, follower_s, follower

    def _merge_clear(self, vehicle: Vehicle, lane: Lane, s: float) -> bool:
        """Room to join the lane with its front `s` metres along it (a bus leaving its bay): a gap
        safe for it behind the vehicle ahead and for the vehicle behind (SUMO's insertion safety)."""
        leader_s, leader, follower_s, follower = self._neighbours(lane, s)
        if leader and leader_s - leader.length_m - s < secure_gap(vehicle, vehicle.speed, leader.speed, leader.decel):
            return False
        return not follower or s - vehicle.length_m - follower_s >= \
            secure_gap(follower, follower.speed, vehicle.speed, vehicle.decel)

    def _change_lanes(self, vehicle: Vehicle) -> None:
        """SUMO's LC2013 motives, strongest first (simulation/lane_changing.md), simplified.

        Strategic: each lane is scored by how far it leads along the route (`reach`). A vehicle
        outside the lanes that lead furthest heads for them once its own would run out within
        STRATEGIC_LOOKAHEAD_S of driving per change still needed: urgently (a blocked change gets
        speed advice, and the vehicle behind in that lane lets it in), or a little before that
        when the next lane is no slower (drift). Then speed gain (past a slower leader within 60 m,
        into the lane with more room) and keeping to the kerb (when that lane has 80 m clear), only
        into lanes that can take its next turn and leave time for the strategic changes still to
        come, so the motives never undo each other. Every change needs a gap that is safe for it
        and for its new follower."""
        lane = vehicle.segment
        if not isinstance(lane, Lane) or not lane.link.lane_changing or len(lane.link.lanes) < 2:
            return
        link = lane.link
        if self.time - vehicle.changed_lane_s < LANE_CHANGE_COOLDOWN_S:
            return
        if vehicle.s < vehicle.length_m + 10:
            return  # not until it is clear of the junction it came through
        reach = self._lane_reach(vehicle)
        top = max(reach)
        best = [i for i, metres in enumerate(reach) if metres >= top - 1e-6]
        look = free_speed(vehicle, lane.speed) * STRATEGIC_LOOKAHEAD_S  # metres per change still needed

        def spare(i: int) -> float:  # metres in lane i before its strategic changes get pressing
            return reach[i] - vehicle.s - min(abs(i - b) for b in best) * look

        here = lane.index
        # The queue ahead in the lane it must reach shortens the room it has to get there (LC2013's
        # occupation of the best lanes): it moves over before it comes up beside a standing queue.
        nearest = min(best, key=lambda i: abs(i - here))
        s_there = vehicle.s * link.lanes[nearest].length / max(lane.length, 1e-6)
        queue = sum(v.length_m + v.min_gap for v in link.lanes[nearest].vehicles if v.s > s_there) if here not in best else 0.0
        if here not in best and spare(here) - queue < look:  # its lanes are coming up: head for them
            toward = link.lanes[here + (1 if nearest > here else -1)]
            urgent = spare(here) - queue < 0
            if urgent or self._room_ahead(toward, vehicle) >= self._room_ahead(lane, vehicle):
                room = max(reach[here] - vehicle.s - queue, 0.0)  # before its lane stops leading on
                self._try_change(vehicle, toward, urgent, room / max(vehicle.speed, 1.0))
            return
        ok = {i for i in range(len(link.lanes))  # never a lane its next turn can't be made from
              if i in best or (reach[i] > link.lanes[i].length and spare(i) >= 2 * look)}
        target = None
        if lane.length - vehicle.s > 60:
            ahead = self._ahead_on(lane, vehicle)
            slow = ahead is not None and ahead.s - vehicle.s < 60 and ahead.speed < free_speed(vehicle, lane.speed) - 2
            if slow:
                options = [i for i in (here + 1, here - 1) if i in ok]
                roomiest = max(options, key=lambda i: self._room_ahead(link.lanes[i], vehicle), default=None)
                if roomiest is not None and self._room_ahead(link.lanes[roomiest], vehicle) > ahead.s - vehicle.s + 20:
                    target = roomiest
            elif here - 1 in ok and self._room_ahead(link.lanes[here - 1], vehicle) > 80:
                target = here - 1
        if target is not None:
            self._try_change(vehicle, link.lanes[target])

    def _room_ahead(self, lane: Lane, vehicle: Vehicle) -> float:
        s = vehicle.s * lane.length / max(vehicle.segment.length, 1e-6)
        index = bisect_right(lane.vehicles, s, key=_position)  # the lists stay ordered by position
        if index < len(lane.vehicles):
            ahead = lane.vehicles[index]
            return ahead.s - ahead.length_m - s
        return lane.length - s + LOOKAHEAD_M

    def _try_change(self, vehicle: Vehicle, target: Lane, urgent: bool = False, time_left: float = 0.0) -> None:
        """Into `target` if the gap is safe for it and for its new follower. A blocked urgent change
        gets LC2013's speed advice for the next step: it drops in behind the leader in the way,
        braking no harder than is comfortable. The follower in the way is asked to let it in when,
        slowing at no more than its comfortable deceleration for the `time_left` the changer has
        before its lane stops leading on, it could open the gap it needs (LC2013's cooperation, asked
        again each step until the change is made); otherwise, when it is the faster, it is let past."""
        s = vehicle.s * target.length / max(vehicle.segment.length, 1e-6)
        leader_s, leader, follower_s, follower = self._neighbours(target, s)
        if leader:
            gap = leader_s - leader.length_m - s
            if gap < secure_gap(vehicle, vehicle.speed, leader.speed, leader.decel):
                if urgent:
                    behind = safe_speed(vehicle, gap - vehicle.min_gap, leader.speed, leader.decel)
                    vehicle.advice = max(behind, vehicle.speed - vehicle.decel * DT, 0.0)
                return
        if follower:
            gap = s - vehicle.length_m - follower_s
            if gap < secure_gap(follower, follower.speed, vehicle.speed, vehicle.decel):
                if urgent:
                    need = secure_gap(follower, follower.speed, vehicle.speed, vehicle.decel)
                    t = max(time_left, DT)  # the follower slowing comfortably while the changer keeps on
                    slowed = follower.speed * t - follower.decel * t * t / 2 if follower.speed > follower.decel * t \
                        else follower.speed ** 2 / (2 * follower.decel)
                    if gap + vehicle.speed * t - slowed >= need:
                        follower.yield_to = vehicle
                    elif follower.speed > vehicle.speed:
                        vehicle.advice = vehicle.speed
                return
        vehicle.segment.vehicles.remove(vehicle)
        vehicle.segment, vehicle.s = target, s
        _place(target, vehicle)
        vehicle.changed_lane_s = self.time

    def _refresh(self) -> None:
        """Each vehicle's place in its lane's or turn's list and its next turn, worked out once
        for all the uses this step (leaders, junctions, limits, gap acceptance)."""
        for segment in {vehicle.segment for vehicle in self.vehicles}:
            for order, vehicle in enumerate(segment.vehicles):
                vehicle.order = order
        for vehicle in self.vehicles:
            vehicle.turn = self._next_turn(vehicle)

    def _pose(self, vehicle: Vehicle) -> tuple[float, float, float, float]:
        return point_along(vehicle.segment.points, max(vehicle.s - vehicle.length_m / 2, 0.0))

    def step(self) -> None:
        """Advance DT seconds."""
        self._take_demand()
        if self.closures:
            self._update_closures()
        while self.time >= self._next_count:
            self._count_arrivals(self._next_count)
            self._next_count += COUNT_INTERVAL_S
        while self.departures and self.departures[0][0] <= self.time:
            _, _, pair, mode_index = heapq.heappop(self.departures)
            self._spawn(pair, mode_index)
        while self._dispatched:
            origin, destination, every_s, after_s = self._dispatched.popleft()
            self._order += 1
            heapq.heappush(self._emergencies, (self.time + after_s, self._order, origin, destination, every_s))
        while self._emergencies and self._emergencies[0][0] <= self.time:
            _, _, origin, destination, every_s = heapq.heappop(self._emergencies)
            self._spawn_emergency(origin, destination)
            if every_s > 0:
                self._order += 1
                heapq.heappush(self._emergencies, (self.time + every_s, self._order, origin, destination, every_s))
        self._refresh()
        for vehicle in self.vehicles:
            self._plan(vehicle)
        self.vehicles = [vehicle for vehicle in self.vehicles if self._move(vehicle)]
        for segment in {vehicle.segment for vehicle in self.vehicles}:  # moves may reorder a merge
            segment.vehicles.sort(key=_position)
        self._refresh()
        for vehicle in self.vehicles:
            self._change_lanes(vehicle)
        self._insert()
        for loop, read in self._loops:  # the adaptive signals' detectors
            read(*loop.read())
        if self.meters:
            self._run_meters()
        for vehicle in self.vehicles:
            vehicle.prev_pose, vehicle.pose = vehicle.pose, self._pose(vehicle)
        self.time += DT
        if self.observed is not None:
            self._observe()
        self._time_step_signals()  # for the next step: what the map shows is what vehicles obey

    def observe(self, keep: int | None = None) -> None:
        """From now on, each step records (headless tests; observations()): each vehicle passing the middle of
        its lane (a detector: time, speed, length), each link's traversal time (entering its lanes to entering
        the next link's, the junction included), rear-end conflicts (time to collision under CONFLICT_TTC_S
        with the vehicle ahead in its lane: an event from when it drops below to when it recovers, with its
        least TTC and greatest deceleration to avoid the crash, DRAC), buses arriving in a stop's bay, and
        vehicles crossing a signalised stop line (with when that green began and whether they had stopped); each
        link a vehicle drove (when it entered, how long it took to the next link: the junction's delay included), and
        the fuel light motor vehicles burnt each step (emissions.py), by type and road.
        `keep`: the most passages, conflicts, stop-line crossings and traversals kept, the latest (a live run's memory);
        None: all."""
        def events():
            return deque(maxlen=keep) if keep else []

        self.observed = {"passages": events(), "traversals": defaultdict(lambda: [0, 0.0]), "conflicts": events(),
                         "stops": events(), "stop_lines": events(), "traversal_log": events(), "boardings": events(),
                         "fuel_ml": defaultdict(float), "fuel_road_ml": defaultdict(float), "fuel_km": defaultdict(float),
                         "arrivals": defaultdict(lambda: [0, 0]), "green_starts": defaultdict(list),
                         "emergency_standstill": defaultdict(float)}
        self._speed_seen: dict[str, float] = {}  # vehicle id -> its speed at the last step (its acceleration)
        self._arrived_at: dict[str, str] = {}  # vehicle id -> the signalised approach it last arrived on (HCM's P)
        self._seen: dict[str, tuple] = {}  # vehicle id -> (link id, s, entered, segment, stopped on this link)
        self._active: dict[str, dict] = {}  # follower id -> its open conflict
        self._green_since: dict[str, float] = {}

    def _observe(self) -> None:
        now, observed, seen = self.time, self.observed, {}
        for link_id, state in self._states.items():
            if state == "G" and self._green_since.get(link_id, (None, "R"))[1] != "G":
                self._green_since[link_id] = (now, "G")
                observed["green_starts"][link_id].append(round(now, 1))
            elif state != "G":
                self._green_since[link_id] = (self._green_since.get(link_id, (now,))[0], state)
        for vehicle in self.vehicles:
            segment, before = vehicle.segment, self._seen.get(vehicle.id)
            if isinstance(segment, Lane):
                link = segment.link
                if link.end in self.graph.signals and self._arrived_at.get(vehicle.id) != link.id and \
                        (vehicle.speed < 0.5 or segment.length - vehicle.s < ARRIVAL_ZONE_M):
                    # it joins the queue, or reaches the stop line: on green or not (HCM 2000 Eq 16-10's P)
                    self._arrived_at[vehicle.id] = link.id
                    record = observed["arrivals"][link.id]
                    record[0] += 1
                    record[1] += self._lane_state(segment) == "G"
                if before is not None and before[0] == link.id:
                    middle = segment.length / 2
                    if before[1] < middle <= vehicle.s:
                        moved = vehicle.s - before[1]
                        observed["passages"].append((link.road, link.id, segment.index, now - DT * (vehicle.s - middle) / moved,
                                                     moved / DT, vehicle.length_m, vehicle.mode))
                    seen[vehicle.id] = (link.id, vehicle.s, before[2], segment, before[4] or vehicle.speed < 0.5)
                else:
                    if before is not None and before[0] is not None:
                        record = observed["traversals"][before[0]]
                        record[0] += 1
                        record[1] += now - before[2]
                        observed["traversal_log"].append((before[0], round(before[2], 1), round(now - before[2], 1),
                                                          vehicle.mode, vehicle.id))
                    seen[vehicle.id] = (link.id, vehicle.s, now, segment, vehicle.speed < 0.5)
            else:
                if isinstance(segment, Bay) and (before is None or before[3] is not segment):
                    mode = next((m for m in self.modes if m.name == vehicle.mode), None)
                    observed["stops"].append((segment.stop, segment.lane.link.id, now, vehicle.id, vehicle.mode,
                                              getattr(mode, "headway_min", 0), f"{vehicle.origin}->{vehicle.destination}"))
                if isinstance(segment, Turn) and before is not None and isinstance(before[3], Lane) \
                        and segment.node in self.graph.signals:
                    lane = before[3]
                    observed["stop_lines"].append((segment.node, lane.link.id, lane.index, now, before[4],
                                                   self._green_since.get(lane.link.id, (None,))[0], vehicle.mode,
                                                   segment.kind))
                seen[vehicle.id] = (before[0], before[1], before[2], segment, before[4]) if before else \
                    (None, 0.0, now, segment, False)
        self._seen = seen
        speeds = {}
        for vehicle in self.vehicles:  # fuel: the car model, for cars and vans only (emissions.py)
            speeds[vehicle.id] = vehicle.speed
            if vehicle.emergency:  # its longest standstill so far
                waits = observed["emergency_standstill"]
                waits[vehicle.id] = max(waits[vehicle.id], vehicle.waiting_s)
            if light_motor(vehicle.length_m, vehicle.desired_max * 3.6):
                accel = (vehicle.speed - self._speed_seen.get(vehicle.id, vehicle.speed)) / DT
                burnt = fuel_rate_ml_s(vehicle.speed, accel) * DT
                observed["fuel_ml"][vehicle.mode] += burnt
                observed["fuel_km"][vehicle.mode] += vehicle.speed * DT / 1000
                where = vehicle.segment.link.road if isinstance(vehicle.segment, (Lane, Bay)) else vehicle.segment.node
                observed["fuel_road_ml"][where] += burnt
        self._speed_seen = speeds
        still = {}
        for lane in {vehicle.segment for vehicle in self.vehicles if isinstance(vehicle.segment, Lane)}:
            queue = lane.vehicles  # rearmost first
            for follower, leader in zip(queue, queue[1:]):
                closing = follower.speed - leader.speed
                gap = leader.s - leader.length_m - follower.s
                if closing <= 0.1 or gap <= 0 or gap / closing >= CONFLICT_TTC_S:
                    continue
                ttc, drac = gap / closing, closing * closing / (2 * gap)
                event = self._active.get(follower.id)
                if event is None or event["leader"] != leader.id:
                    event = {"road": lane.link.road, "link": lane.link.id, "t": round(now, 1), "leader": leader.id,
                             "type": follower.mode, "leader_type": leader.mode, "speed_kmh": round(follower.speed * 3.6, 1),
                             "ttc_min": ttc, "drac_max": drac}
                    observed["conflicts"].append(event)
                event["ttc_min"], event["drac_max"] = min(event["ttc_min"], ttc), max(event["drac_max"], drac)
                still[follower.id] = event
        self._active = still

    def observations(self) -> dict:
        """What observe() recorded, plain data: the passages, each link's traversals (count, mean seconds) with
        its road, ends, length and free-flow seconds, the conflicts, the bus stop arrivals and the stop-line
        crossings."""
        if self.observed is None:
            return {}
        links = {link.id: {"road": link.road, "from": link.start, "to": link.end, "length_m": round(link.length, 1),
                           "free_s": round(link.length / link.speed, 1), "lanes": len(link.lanes),
                           # through its junction at the end, at each turn's own speed (the mean of its turns)
                           "cross_s": round(sum(t.length / max(t.speed, 1.0) for t in link.turns.values())
                                            / len(link.turns), 1) if link.turns else 0.0}
                 for link in self.graph.links.values()}
        graph = self.graph
        signals = {node: {"method": self.signal_plans.get(node, {}).get("method", "fixed"), "cycle_s": round(signal.cycle, 1),
                          "phases": [{"links": sorted(group), "green_s": round(green, 1)} for group, green in signal.phases],
                          "lost_s_per_phase": AMBER_S + ALL_RED_S,
                          "saturation_vph": {link.id: round(self.saturation_vph.get(link.id, 0.0))
                                             for link in graph.arriving[node]}}
                   for node, signal in graph.signals.items()}
        return {"passages": list(self.observed["passages"]), "conflicts": list(self.observed["conflicts"]),
                "stops": list(self.observed["stops"]), "stop_lines": list(self.observed["stop_lines"]),
                "traversals": {key: (n, total / n) for key, (n, total) in self.observed["traversals"].items() if n},
                "traversal_log": list(self.observed["traversal_log"]), "boardings": list(self.observed["boardings"]),
                "arrivals": {link: tuple(found) for link, found in self.observed["arrivals"].items()},
                "green_starts": {link: list(found) for link, found in self.observed["green_starts"].items()},
                "emergency_standstill": dict(self.observed["emergency_standstill"]),
                "fuel": {"by_type_ml": dict(self.observed["fuel_ml"]), "by_road_ml": dict(self.observed["fuel_road_ml"]),
                         "by_type_km": dict(self.observed["fuel_km"])},
                "signals": signals, "controls": {node: graph.network.node(node).control for node in graph.network.nodes},
                "waiting_to_enter": {link: len(queue) for link, queue in self.pending.items() if queue},
                "unreachable": list(self.unreachable), "weather": self.weather,
                "meters": {link: [list(entry) for entry in meter.history] for link, meter in self.meters.items()},
                "closures": [{"link": c.link, "lanes": list(c.lanes), "from_m": c.from_m, "to_m": c.to_m,
                              "start_s": c.start_s, "end_s": None if c.end_s == inf else c.end_s,
                              "speed_kmh": None if c.speed is None else round(c.speed * 3.6)} for c in self.closures],
                "links": links, "observed_s": self.time}

    def frame(self) -> Frame:
        """The run as it is now, frozen (see Frame)."""
        vehicles = tuple(VehicleState(v.id, v.mode, v.colour, v.length_m, v.width_m, *v.pose, v.prev_pose,
                                      v.speed, v.segment.node if isinstance(v.segment, Turn) else v.segment.link.road,
                                      v.segment.name, v.s, v.origin, v.destination, *self._heading_to(v))
                         for v in self.vehicles)
        return Frame(self.time, vehicles, self.signal_states(), self.spawned, self.arrived, dict(self.signal_plans))

    @staticmethod
    def _heading_to(vehicle: Vehicle) -> tuple[str, str, float]:
        """The next landmark on its route (Graph.landmarks) still ahead of it, and about how far to
        where its lanes end there (the stop or give-way line; a slip road: where it leaves): the rest
        of its way to the node at its leg's end, then the links on to that landmark's line."""
        segment, leg = vehicle.segment, vehicle.leg
        crossing = isinstance(segment, Turn)  # through the junction at its leg's end: that one is behind it
        for at, kind, identifier, metres in vehicle.ahead:
            if at > leg or (at == leg and not crossing):
                link = vehicle.route[leg]
                if crossing:  # part way across the gap between this link's lanes and the next one's
                    to_node = link.cut_end - vehicle.s / segment.length * (link.cut_end + vehicle.route[leg + 1].cut_start)
                elif isinstance(segment, Bay):
                    to_node = segment.length - vehicle.s + segment.lane.length - segment.exit_s + link.cut_end
                else:
                    to_node = segment.length - vehicle.s + link.cut_end
                return kind, identifier, to_node + metres - vehicle.route_m[leg] - vehicle.route[at].cut_end
        return "", "", 0.0

    def advance(self, target: float, budget_s: float = 0.010) -> None:
        """Step until the simulation reaches `target` seconds, or `budget_s` of real time has gone
        (so drawing never waits on it; the clock then waits for the traffic)."""
        started = clock.perf_counter()
        while self.time + DT <= target + 1e-9 and clock.perf_counter() - started < budget_s:
            self.step()


class Runner:
    """Runs a Simulation on its own thread, in step with the wall clock × speed.

    The physics never waits for anyone: every step is computed, in order, to keep simulated time
    at run time × speed, whether or not anything is drawn. After each batch of steps it publishes
    `frame`, the run frozen at that moment (every vehicle's position, heading, speed and place; the
    signals), which viewers draw, skipping frames when busy, and an AI can read at any moment
    without disturbing the run. Only if the CPU itself can't keep up at a very high speed does the
    physics fall behind; `speed_now` then says how fast it really runs.
    """

    PUBLISH_S = 1 / 60  # a fresh frame this often at most: one every step at high speed would waste time

    def __init__(self, simulation: Simulation, speed: float = 1.0, recorder=None) -> None:
        self.simulation = simulation
        self.speed = speed
        # results.RunRecorder: sampled here, on the physics thread, every `sample_s` simulated seconds, so no
        # sample is lost to a busy screen. One that raises is dropped (its `error` says why); the run carries on.
        self.recorder = recorder
        self._next_sample = getattr(recorder, "sample_s", 0.0)
        self.frame = simulation.frame()
        self.speed_now = 0.0
        self._go = threading.Event()
        self._idle = threading.Event()  # set while the physics waits, paused, with its last step published
        self._quit = False
        self._wall0 = self._sim0 = 0.0
        self._published, self._unpublished = clock.monotonic(), False
        self._thread = threading.Thread(target=self._loop, name="traffic physics", daemon=True)
        self._thread.start()

    @property
    def running(self) -> bool:
        return self._go.is_set()

    def target(self) -> float:
        """Where simulated time should be now: run time × speed (held while paused)."""
        if not self._go.is_set():
            return self.frame.time
        return self._sim0 + (clock.monotonic() - self._wall0) * self.speed

    def play(self) -> None:
        if not self._go.is_set():
            self._sim0, self._wall0 = self.simulation.time, clock.monotonic()  # the physics is idle
            self._go.set()

    def pause(self) -> None:
        """Pause; returns once the physics is idle and its last step is published."""
        self._go.clear()
        if threading.current_thread() is not self._thread:
            self._idle.wait(timeout=1.0)

    def set_speed(self, speed: float) -> None:
        self._sim0, self._wall0, self.speed = self.target(), clock.monotonic(), speed

    def stop(self) -> None:
        self._quit = True
        self._go.set()
        self._thread.join(timeout=5)

    def _record(self) -> None:
        self._next_sample += self.recorder.sample_s
        try:
            self.recorder.sample(self.simulation.frame())
        except Exception as exc:  # noqa: BLE001 - results must never stop the traffic
            self.recorder.error = f"Results stopped recording: {exc!r}"
            self.recorder = None

    def _publish(self) -> None:
        self.frame, self._published, self._unpublished = self.simulation.frame(), clock.monotonic(), False

    def _loop(self) -> None:
        measured = (clock.monotonic(), self.simulation.time)
        while True:
            if not self._go.is_set():
                self._idle.set()
                self._go.wait()
                self._idle.clear()
                measured = (clock.monotonic(), self.simulation.time)  # a pause isn't slowness
            if self._quit:
                return
            target = self.target()
            while self.simulation.time + DT <= target + 1e-9 and self._go.is_set() and not self._quit:
                self.simulation.step()
                self._unpublished = True
                if self.recorder is not None and self.simulation.time + 1e-9 >= self._next_sample:
                    self._record()
                if clock.monotonic() - self._published >= self.PUBLISH_S:
                    self._publish()
            if self._unpublished and (clock.monotonic() - self._published >= self.PUBLISH_S
                                      or not self._go.is_set()):
                self._publish()  # a paused run shows its last step
            if not self._go.is_set():
                continue  # paused: straight to waiting, no sleep
            now = clock.monotonic()
            if now - measured[0] >= 1.0:
                self.speed_now = (self.simulation.time - measured[1]) / (now - measured[0])
                measured = (now, self.simulation.time)
            due = (self.simulation.time + DT - self.target()) / max(self.speed, 1e-6)
            clock.sleep(min(max(due, 0.001), 0.05))  # until the next step is due (sleeping frees the GIL)


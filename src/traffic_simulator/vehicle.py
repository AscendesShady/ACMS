"""Vehicles: each one made from a vehicle type in Mode Behavior, and how it follows the car ahead.

create_vehicle() copies the type's attributes, as set in Mode Behavior, onto the vehicle: its
name, colour and headway, its size, and how it drives (top speed, desired top speed, acceleration,
comfortable and emergency braking, standstill gap, reaction time, imperfection), and draws its speed factor, as
SUMO does: each vehicle keeps to its own multiple of the speed limits, from normc(1, speedDev, 0.2,
2), so about 95 % drive at 80-120 % of the limit. Where a vehicle is stays lane-relative
and in metres: the piece of path it is on (a lane, or a turn through a junction; see
simulation.py) and how far along it its front bumper is. x, y and heading are derived from that
path's polyline, so a vehicle keeps to the road's geometry whatever shape the road is.

Car following is the Krauss model, SUMO's default (references/reverse_engineering/simulation/
car_following.md). Each step a vehicle drives at the fastest speed from which, after its
reaction time τ, it could stop within the gap g plus the distance its leader needs to stop
(SUMO's maximumSafeFollowSpeed, which its Krauss uses rather than the 1998 closed form):

    vsafe·τ + vsafe² / 2b = g + vl² / 2bl   →   vsafe = −τb + √((τb)² + 2b(g + vl²/2bl))

no faster than it can accelerate to (v + a·Δt) or than its free speed (free_speed: SUMO's
min(maxSpeed, limit × speedFactor, desiredMaxSpeed × speedFactor)), less a random "dawdle" (driver
imperfection, next_speed), and never slower than its emergency braking allows (v − emergencyDecel·Δt). A red
light or a junction it must wait at is a standing leader at the stop line. With the ballistic update (opt-in,
Simulation step_method) the safe speed is SUMO's ballistic form instead (safe_speed's `dt`).
"""

from __future__ import annotations

import random as _random
from dataclasses import dataclass, field
from math import sqrt
from typing import TYPE_CHECKING

from .models import LANE_WIDTH_M

# Lateral position (opt-in: Simulation lateral=True), SUMO's sublane model SL2015 at its smallest (Semrau & Erdmann
# 2016; simulation/lane_changing.md): the least gap kept sideways to another vehicle (SUMO's minGapLat default) and the
# fastest a vehicle moves sideways (SUMO's maxSpeedLat default, m/s), sumo.dlr.de/docs/Simulation/SublaneModel.html.
# A type that filters (VehicleMode.filters: made as these classes) does so only if it fits in half a lane with that gap.
MIN_GAP_LAT_M = 0.6
MAX_SPEED_LAT = 1.0
FILTERING_CLASSES = ("motorcycle", "moped", "bicycle")


# Vehicle classes a type can be made as (create_vehicle_types, VehicleModes.create): SUMO's defaults for
# each vClass (sumo.dlr.de/docs/Vehicle_Type_Parameter_Defaults): length, width, top speed, acceleration,
# comfortable and emergency braking (SUMO's decel and emergencyDecel), standstill gap and speed deviation; a
# bicycle's desired top speed 20 km/h, as the Bike type's. SUMO lists no braking for a coach: a bus's 4.0 and 7.0.
# Occupancy (people a vehicle carries, for passenger throughput) is the app's assumption, not SUMO's.
VEHICLE_CLASSES = {
    "passenger": dict(length_m=5.0, width_m=1.8, max_speed_kmh=200.0, accel=2.6, decel=4.5, emergency_decel=9.0, min_gap_m=2.5, speed_dev=0.1, occupancy=1.5),
    "taxi": dict(length_m=5.0, width_m=1.8, max_speed_kmh=200.0, accel=2.6, decel=4.5, emergency_decel=9.0, min_gap_m=2.5, speed_dev=0.05, occupancy=1.5),
    "bus": dict(length_m=12.0, width_m=2.5, max_speed_kmh=85.0, accel=1.2, decel=4.0, emergency_decel=7.0, min_gap_m=2.5, speed_dev=0.1, occupancy=20.0),
    "coach": dict(length_m=14.0, width_m=2.6, max_speed_kmh=100.0, accel=2.0, decel=4.0, emergency_decel=7.0, min_gap_m=2.5, speed_dev=0.05, occupancy=30.0),
    "delivery": dict(length_m=6.5, width_m=2.16, max_speed_kmh=200.0, accel=2.6, decel=4.5, emergency_decel=9.0, min_gap_m=2.5, speed_dev=0.05, occupancy=1.0),
    "truck": dict(length_m=7.1, width_m=2.4, max_speed_kmh=130.0, accel=1.3, decel=4.0, emergency_decel=7.0, min_gap_m=2.5, speed_dev=0.05, occupancy=1.0),
    "trailer": dict(length_m=16.5, width_m=2.55, max_speed_kmh=130.0, accel=1.0, decel=4.0, emergency_decel=7.0, min_gap_m=2.5, speed_dev=0.05, occupancy=1.0),
    "motorcycle": dict(length_m=2.2, width_m=0.9, max_speed_kmh=200.0, accel=6.0, decel=10.0, emergency_decel=10.0, min_gap_m=2.5, speed_dev=0.1, occupancy=1.0),
    "moped": dict(length_m=2.1, width_m=0.8, max_speed_kmh=45.0, accel=1.1, decel=7.0, emergency_decel=10.0, min_gap_m=2.5, speed_dev=0.1, occupancy=1.0),
    "bicycle": dict(length_m=1.6, width_m=0.65, max_speed_kmh=50.0, desired_max_kmh=20.0, accel=1.2, decel=3.0, emergency_decel=7.0, min_gap_m=0.5, speed_dev=0.1, occupancy=1.0),
    "tram": dict(length_m=22.0, width_m=2.4, max_speed_kmh=80.0, accel=1.0, decel=3.0, emergency_decel=7.0, min_gap_m=2.5, speed_dev=0.0, occupancy=60.0),
    "emergency": dict(length_m=6.5, width_m=2.16, max_speed_kmh=200.0, accel=2.6, decel=4.5, emergency_decel=9.0, min_gap_m=2.5, speed_dev=0.0, occupancy=2.0),
    "evehicle": dict(length_m=5.0, width_m=1.8, max_speed_kmh=200.0, accel=2.6, decel=4.5, emergency_decel=9.0, min_gap_m=2.5, speed_dev=0.1, occupancy=1.5),
}

# A dispatched emergency vehicle (Simulation.dispatch) when the project has no vehicle type called Emergency:
# SUMO's emergency vClass, with SUMO's passenger driver defaults; drawn red.
EMERGENCY_MODE = dict(name="Emergency", colour="#ff3b3b", headway_min=0, tau_s=1.0, sigma=0.5, desired_max_kmh=1e4,
                      startup_delay_s=0.9, **VEHICLE_CLASSES["emergency"])

if TYPE_CHECKING:  # the type only: this module and simulation.py stay free of Qt
    from .mode_behavior import VehicleMode


@dataclass(slots=True, eq=False)
class Vehicle:
    id: str
    mode: str  # its vehicle type's name
    colour: str  # its type's colour, drawn as is
    length_m: float
    width_m: float
    headway_min: int  # timetabled (a bus every n minutes); 0: not
    max_speed: float  # m/s
    accel: float  # m/s²
    decel: float  # comfortable braking, m/s²
    emergency_decel: float  # the hardest it can brake, m/s² (SUMO's emergencyDecel): no step slows it more
    min_gap: float  # m, standstill gap to the leader
    tau: float  # s, reaction time
    sigma: float  # 0-1, imperfection
    desired_max: float = 1e9  # m/s, the fastest it wants to go (before its speed factor)
    speed_factor: float = 1.0  # its multiple of every speed limit (SUMO's speedFactor)
    startup_delay: float = 0.0  # s, its wait before moving off after having had to stop (SUMO's startupDelay)
    route: list = field(default_factory=list)  # the links it drives, origin end to destination end
    origin: str = ""
    destination: str = ""
    depart_s: float = 0.0  # when it asked to depart (its trip's requested departure)
    inserted_s: float | None = None  # when it got onto its first lane (None: still waiting to enter)
    # Where it is and what it's doing, kept by the simulation:
    leg: int = 0  # index in `route` of the link it's on, or leaving (while on a turn)
    segment: object = None  # the lane or turn it's on
    s: float = 0.0  # its front, metres along `segment`
    speed: float = 0.0  # m/s
    next_speed: float = 0.0
    may_enter: bool = False  # cleared to enter its next junction turn this step
    turn: object = None  # its next junction turn, worked out once a step
    came_from: object = None  # the junction turn it last drove through
    order: int = 0  # its place in its lane's or turn's list, refreshed each step
    reach: object = None  # how far each lane of its link leads along its route without a change...
    reach_key: object = None  # ...worked out for this leg of its route and these stops still to come
    stops: list = field(default_factory=list)  # a bus: (leg, bay) of each stop still ahead on its route
    ahead: list = field(default_factory=list)  # its route's roundabouts, signals and slip roads (Graph.landmarks)
    emergency: bool = False  # dispatched as an emergency vehicle (Simulation.dispatch): it keeps to the kerbside lane
    route_m: list = field(default_factory=list)  # metres from its route's start to each leg's end
    dwell_end: float | None = None  # in a bay: when it has stood at the stop long enough
    load: float | None = None  # a bus's passengers, once it has called at a stop with passengers (None: its type's occupancy)
    waiting_s: float = 0.0  # standing still
    impatience_s: float = 0.0  # standing at the line, free to go but in a lane its turn isn't made from
    startup_wait: float = 0.0  # halted and free to move off: how long it has waited to (startup_delay)
    changed_lane_s: float = -1e9  # when it last changed lane
    # Lane-change speed advice for its next step (LC2013), cleared once used:
    yield_to: object = None  # a vehicle beside it that must change into its lane: it lets it in
    advice: float | None = None  # a speed cap, to drop in behind (or let pass) a vehicle in the lane it needs
    pose: tuple = (0.0, 0.0, 1.0, 0.0)  # centre x, y and heading ux, uy after the last step
    prev_pose: tuple = (0.0, 0.0, 1.0, 0.0)  # ...and the step before, to draw smooth motion between
    # Lateral runs only (Simulation lateral): its centre's offset from its lane's or turn's centre line, metres towards
    # (-uy, ux) (0 otherwise), and whether it may pass others within its lane (its type filters and it is narrow_enough)
    lat_m: float = 0.0
    filters: bool = False


def narrow_enough(width_m: float) -> bool:
    """Fits in half a lane with the lateral gap: may share a lane side by side and pass a queue within it."""
    return width_m + MIN_GAP_LAT_M <= LANE_WIDTH_M / 2


def create_vehicle(mode: VehicleMode, vehicle_id: str, route: list | None = None, depart_s: float = 0.0,
                   origin: str = "", destination: str = "", rng: _random.Random | None = None) -> Vehicle:
    """A vehicle of this type, with the type's attributes and colour, and its speed factor drawn
    with `rng` (none: exactly 1)."""
    vehicle = Vehicle(vehicle_id, mode.name, mode.colour, mode.length_m, mode.width_m, mode.headway_min,
                      mode.max_speed_kmh / 3.6, mode.accel, mode.decel, mode.emergency_decel, mode.min_gap_m, mode.tau_s,
                      mode.sigma, mode.desired_max_kmh / 3.6, speed_factor(mode.speed_dev, rng), mode.startup_delay_s,
                      list(route or []), origin, destination, depart_s)
    vehicle.filters = bool(getattr(mode, "filters", False)) and narrow_enough(mode.width_m)
    return vehicle


def speed_factor(deviation: float, rng: _random.Random | None) -> float:
    """SUMO's default speed factor distribution, normc(1, deviation, 0.2, 2): a normal draw about 1,
    drawn again until it falls within [0.2, 2]."""
    if rng is None or deviation <= 0:
        return 1.0
    while True:
        factor = rng.gauss(1.0, deviation)
        if 0.2 <= factor <= 2.0:
            return factor


def start_up(vehicle: Vehicle, planned: float, dt: float) -> float:
    """SUMO's startupDelay on the speed planned for the next step: a halted vehicle (under 0.1 m/s)
    that could move off waits startup_delay first, counted from when it could go; a delay ending
    part way through a step lets it pick up only that part of the step's acceleration."""
    if vehicle.speed >= 0.1 or planned <= vehicle.speed:
        vehicle.startup_wait = 0.0  # moving, or still held: the delay starts when it can go
        return planned
    remaining = vehicle.startup_delay - vehicle.startup_wait
    vehicle.startup_wait += dt
    if remaining >= dt:
        return vehicle.speed
    return vehicle.speed + (planned - vehicle.speed) * min(1.0, (dt - max(remaining, 0.0)) / dt)


def free_speed(vehicle: Vehicle, limit: float) -> float:
    """The fastest it drives where the speed limit is `limit` (m/s): SUMO's min(maxSpeed,
    limit × speedFactor, desiredMaxSpeed × speedFactor)."""
    return min(vehicle.max_speed, limit * vehicle.speed_factor, vehicle.desired_max * vehicle.speed_factor)


def brake_gap(speed: float, decel: float, reaction_s: float = 0.0) -> float:
    """Distance to stop from `speed`, braking at `decel` after reacting for `reaction_s`."""
    return speed * speed / (2 * decel) + speed * reaction_s


def safe_speed(vehicle: Vehicle, gap: float, leader_speed: float, leader_decel: float | None = None,
               dt: float | None = None) -> float:
    """The safe speed behind a leader `gap` metres ahead (already less the minimum gap) that
    brakes at `leader_decel` (its own braking; the vehicle's when unknown).
    `dt`: the step of the ballistic update (None: Euler's). Then, as SUMO's maximumSafeStopSpeedBallistic, the speed
    at the step's end v0 + a·Δt, where a is the even acceleration over τ from its speed v0 to v1 that leaves the room
    to stop: τ(v0 + v1)/2 + v1²/2b = room; or, if it can't drive τ first (v0·τ ≥ 2·room), a = −v0²/2·room. Below
    zero: it stops part way through the step (Simulation._move); no room at all: as hard as it can (−inf)."""
    b, tau = vehicle.decel, vehicle.tau
    room = gap + brake_gap(leader_speed, leader_decel or b)
    if dt is None:
        return -tau * b + sqrt((tau * b) ** 2 + 2 * b * room) if room > 0 else 0.0
    v0 = vehicle.speed
    if room <= 0:
        return -float("inf") if v0 > 0 else 0.0
    if v0 * tau >= 2 * room:
        return v0 - v0 * v0 / (2 * room) * dt
    v1 = -b * tau / 2 + sqrt((b * tau / 2) ** 2 + b * (2 * room - tau * v0))
    return v0 + (v1 - v0) / tau * dt


def next_speed(vehicle: Vehicle, vsafe: float, limit: float, dt: float, rng, ballistic: bool = False) -> float:
    """The speed for the coming step: as fast as the limits, acceleration and safety allow, less
    a random dawdle, as SUMO's default Krauss does it: σ·a·Δt·η (η uniform in [0, 1)), or σ·v·Δt·η
    while the speed v is still below one second's acceleration (a driver moving off does move off).
    Dawdling never brakes harder than comfortable, nor below that speed if it is braking harder
    already; never negative, and a vsafe of zero stops it within the step. Nothing slows it by more than its
    emergency braking (SUMO's emergencyDecel bound in finalizeSpeed): if that isn't enough, it overshoots, and the
    run counts the collision. `ballistic`: a speed below zero is kept, meaning it stops part way through the step."""
    desired = min(limit, vehicle.max_speed, vehicle.speed + vehicle.accel * dt, vsafe)
    random = rng.random()
    dawdle = vehicle.sigma * (max(desired, 0.0) if desired < vehicle.accel else vehicle.accel) * dt * random
    low = -float("inf") if ballistic else 0.0
    return max(min(max(low, vehicle.speed - vehicle.decel * dt), desired), desired - dawdle,
               vehicle.speed - vehicle.emergency_decel * dt, low)


def secure_gap(follower: Vehicle, follower_speed: float, leader_speed: float, leader_decel: float) -> float:
    """The gap a follower needs behind a leader to be safe whatever the leader does (SUMO's
    getSecureGap): its own stopping distance with reaction, less the leader's."""
    return max(0.0, brake_gap(follower_speed, follower.decel, follower.tau)
               - brake_gap(leader_speed, leader_decel)) + follower.min_gap

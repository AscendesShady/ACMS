"""The metro as its own network (no Qt): its stations (M1, M2...), rides between them, and the loads on its lines.

The metro is apart from the roads: its stations are points of their own, its demand its own OD matrix
(`Network.metro_demand`, station to station, passengers an hour), and road trips never turn into metro trips. Only an
institution draws on it, as it draws on the roads: its circle of influence takes in the stations inside it, as it takes
in the junctions (`places`).

- `ride(network, board, alight)`: between two different stations, on a line they share, or with one change at an
  interchange (a station on both lines): the track between them (curves included) at each line's commercial speed,
  half each headway's wait (random arrival, as network_tools prices a timetabled bus), the dearer fare (one ticket
  through: an assumption); the quickest. None with no such ride.
- `door(network, x, y)`: the station a building uses, the nearest within `METRO_WALK_M` (800 m, an assumption: about
  ten minutes' walk at `WALK_KMH`), straight-line; `places(network, item)`: that door and the stations on its line
  inside the building's circle (its catchment, as junctions are), but the door.
- `loads(network, flows)`: passengers an hour (board, alight) put on their line, each stop-to-stop segment's load each
  way against the line's person capacity, HCM 2000 Eq 27-28: P = (3,600 / headway) x carriages a train x
  passengers a carriage x PHF, PHF 0.75 (the HCM's recommended value for light rail; off-street rail it leaves to the
  TCQSM, not in the references); boardings and alightings a station, passenger-km, and the passengers no line serves.
"""

from __future__ import annotations

from math import hypot

from .geometry import nearest_along
from .models import Network

METRO = "Metro"  # the mode's name in an institution's mode choice (a vehicle type of the same name takes precedence)
METRO_WALK_M = 800.0  # the farthest walk between a building and its station: an assumption (about ten minutes)
METRO_PHF = 0.75  # HCM 2000 p27-25: the peak-hour factor for light rail's person capacity (Eq 27-28)
WALK_KMH = 5.0  # an assumed walking speed: to and from a station, and an institution's walk (network_tools)


def _along(network: Network, line) -> dict[str, float]:
    """Each station's metres along its line's track."""
    track = network.metro_track(line.id)
    return {stop_id: nearest_along(track, (network.metro_stops[stop_id].x, network.metro_stops[stop_id].y))
            for stop_id in line.stops}


def lines_of(network: Network, stop_id: str) -> list:
    """The lines a station is on (two or more: an interchange)."""
    return [line for line in network.metro_lines.values() if stop_id in line.stops]


def _leg(network: Network, line, board: str, alight: str) -> dict:
    along = _along(network, line)
    km = abs(along[alight] - along[board]) / 1000
    return {"line": line.id, "board": board, "alight": alight, "km": km, "ivt_min": km / line.speed_kmh * 60,
            "wait_min": line.headway_min / 2, "cost": line.fare_cents}


def ride(network: Network, board: str, alight: str) -> dict | None:
    """The quickest ride from `board` to `alight`, on one line or with one change at an interchange: {"line" (the
    first), "legs" (each {"line", "board", "alight", "km", ...}), "km", "ivt_min", "wait_min", "cost"}; None if they
    are the same station or no such ride joins them."""
    if board == alight:
        return None
    trips = [[_leg(network, line, board, alight)] for line in lines_of(network, board) if alight in line.stops]
    if not trips:  # one change: board's line to an interchange, then a line from there to alight
        trips = [[_leg(network, first, board, change), _leg(network, second, change, alight)]
                 for first in lines_of(network, board) for change in first.stops if change not in (board, alight)
                 for second in lines_of(network, change) if second is not first and alight in second.stops]
    if not trips:
        return None
    legs = min(trips, key=lambda legs: sum(leg["ivt_min"] + leg["wait_min"] for leg in legs))
    return {"line": legs[0]["line"], "legs": legs, "km": sum(leg["km"] for leg in legs),
            "ivt_min": sum(leg["ivt_min"] for leg in legs), "wait_min": sum(leg["wait_min"] for leg in legs),
            "cost": max(leg["cost"] for leg in legs)}


def door(network: Network, x: float, y: float) -> tuple[str, float] | None:
    """The nearest station within METRO_WALK_M of (x, y) and the metres to it; None beyond the walk."""
    nearest = min(((hypot(stop.x - x, stop.y - y), stop.id) for stop in network.metro_stops.values()), default=None)
    return (nearest[1], nearest[0]) if nearest and nearest[0] <= METRO_WALK_M else None


def places(network: Network, item) -> tuple[str | None, list[str]]:
    """A building's metro door (`door`) and its metro catchment: the other stations of the door's line inside its
    circle of influence (`radius_m`), nearest first. (None, []) with no station within the walk."""
    found = door(network, item.x, item.y)
    if found is None:
        return None, []
    entrance = found[0]
    inside = []
    for stop_id in dict.fromkeys(stop_id for line in lines_of(network, entrance) for stop_id in line.stops):
        stop = network.metro_stops[stop_id]
        metres = hypot(stop.x - item.x, stop.y - item.y)
        if stop_id != entrance and metres <= item.radius_m:
            inside.append((metres, stop_id))
    return entrance, [stop_id for _, stop_id in sorted(inside)]


def capacity_per_h(line) -> float:
    """HCM 2000 Eq 27-28: passengers an hour each way, (60 / headway) trains x carriages x passengers a carriage
    x METRO_PHF."""
    return 60.0 / line.headway_min * line.train_capacity * METRO_PHF


def loads(network: Network, flows: list[tuple[str, str, float]]) -> dict:
    """Each line's passengers, their stop-to-stop loads each way and its busiest segment against capacity_per_h, from
    `flows` (board station, alight station, passengers an hour)."""
    lines = {line_id: {"riders": 0.0, "passenger_km": 0.0, "segments": {}, "stops": {}} for line_id in network.metro_lines}
    unserved = 0.0
    for board, alight, people in flows:
        if people <= 0:
            continue
        trip = ride(network, board, alight)
        if trip is None:
            unserved += people
            continue
        for leg in trip["legs"]:  # each line it rides: a change boards the second at the interchange
            found = lines[leg["line"]]
            stops = network.metro_lines[leg["line"]].stops
            a, b = stops.index(leg["board"]), stops.index(leg["alight"])
            step = 1 if b > a else -1
            for i in range(a, b, step):  # each segment it rides, in its direction
                key = (stops[i], stops[i + step])
                found["segments"][key] = found["segments"].get(key, 0.0) + people
            for stop_id, kind in ((leg["board"], "boarding"), (leg["alight"], "alighting")):
                found["stops"].setdefault(stop_id, {"boarding": 0.0, "alighting": 0.0})[kind] += people
            found["riders"] += people
            found["passenger_km"] += people * leg["km"]
    report = []
    for line_id, found in lines.items():
        line = network.metro_lines[line_id]
        capacity = capacity_per_h(line)
        peak_key = max(found["segments"], key=found["segments"].get, default=None)
        peak = found["segments"][peak_key] if peak_key else 0.0
        report.append({"line": line_id, "riders_per_h": round(found["riders"]),
                       "passenger_km_per_h": round(found["passenger_km"]),
                       "capacity_per_h_each_way": round(capacity), "peak_load_per_h": round(peak),
                       "peak_segment": f"{peak_key[0]} to {peak_key[1]}" if peak_key else None,
                       "v_c": round(peak / capacity, 2), "over_capacity": peak > capacity,
                       "stops": {stop_id: {kind: round(value) for kind, value in counts.items()}
                                 for stop_id, counts in found["stops"].items()}})
    return {"lines": report, "unserved_per_h": round(unserved)}

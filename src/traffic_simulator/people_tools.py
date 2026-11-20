"""The assistant's tools for people and transit (no Qt).

Institutions (list, place, deploy, update), pedestrian crossings, paths, demand and spawn points, metro lines and
their demand, and bus stops. network_tools.run_tool dispatches to them.
"""

from __future__ import annotations

import random
import re
from copy import deepcopy
from math import cos, hypot, radians, sin, sqrt

from .daily_profile import DailyProfile, profile_record
from .demand import profile_from
from .geometry import polyline_length
from .institutions import KINDS, catchment, door, footprint_problem, hourly_people, links, place, shares
from .metro import ride as metro_ride
from .models import (BUS_STOP_DIRECTIONS, MAX_BOARDING_PER_H, MAX_METRO_DEMAND, MAX_WALK_DEMAND, METRO_MIN_SPACING_M,
                     METRO_SERVICE, MetroCrossing, Network)
from .results import clock_h
from .tool_args import _ids, _int, _limited, _parsed_json


def list_institutions(network: Network) -> dict:
    rows = []
    for item in network.institutions.values():
        arrive, _ = hourly_people(item)
        rows.append({"id": item.id, "kind": item.kind, "x": round(item.x, 1), "y": round(item.y, 1),
                     "area_m2": item.area_m2, "radius_m": round(item.radius_m, 1), "people": item.people,
                     "employees": item.employees, "shift": f"{clock_h(item.shift_start_h)}-{clock_h(item.shift_end_h)}",
                     "mode_shares_percent": shares(item), "split_by": "logit" if item.mode_shares else "car share",
                     "door": door(network, item), "catchment": catchment(network, item),
                     "people_per_day_each_way": round(sum(arrive)),
                     # hourly demand's typed figures, and the days shaped by curves rather than its shift (the panel's)
                     **({"people_per_hour_typed": item.per_hour} if item.per_hour else {}),
                     **({"days_by_own_curve": sorted(item.curves)} if item.curves else {})})
    return _limited(rows, "institutions")


def _catchment_radius(network: Network, x: float, y: float) -> float:
    """A building's default circle: 10 m past its 4th nearest junction or end (its door and three places its
    people come from; a grid block's four corners)."""
    distances = sorted(hypot(node.x - x, node.y - y) for node in network.all_nodes())
    return distances[min(3, len(distances) - 1)] + 10 if distances else 0.0


def place_institution(network: Network, kind, near=None, x=None, y=None, radius_m=None, area_m2=None, people=None,
                      employees=None, shift_start_h=None, shift_end_h=None, car_share=None) -> dict:
    """One building on clear ground, at x, y or the nearest clear spot to a junction or end (`near`): spots on
    rings 10 m apart round it, 16 directions each, diagonals first (block interiors on a grid)."""
    names = {name.lower(): name for name in KINDS}
    kind = names.get(str(kind).strip().lower())
    if kind is None:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}.")
    area, *figures = KINDS[kind].defaults
    area = float(area_m2) if area_m2 is not None else float(area)
    given = [people, employees, shift_start_h, shift_end_h, car_share]
    figures = [value if value is not None else default for value, default in zip(given, figures)]
    if x is not None and y is not None:
        spot = (float(x), float(y))
    elif near is not None:
        node = next((n for n in network.all_nodes() if n.id == str(near)), None)
        if node is None:
            raise KeyError(str(near))
        angles = [45 + 90 * i for i in range(4)] + [90 * i for i in range(4)] + [22.5 + 45 * i for i in range(8)]
        spots = ((node.x + d * cos(radians(a)), node.y + d * sin(radians(a))) for d in range(int(sqrt(area) / 2) + 10, 1000, 10)
                 for a in angles)
        spot = next((s for s in spots if footprint_problem(network, s[0], s[1], area) is None), None)
        if spot is None:
            raise ValueError(f"No clear ground for a {area:g} m² {kind} within 1 km of {near}.")
    else:
        raise ValueError("Give near (a junction or end) or x and y.")
    if radius_m is None:
        radius_m = _catchment_radius(network, *spot)
    item = place(network, kind, spot[0], spot[1], area, max(float(radius_m), sqrt(area) / 2), int(figures[0]),
                 int(figures[1]), float(figures[2]), float(figures[3]), float(figures[4]))
    return {"placed": item.id, "kind": kind, "x": round(item.x), "y": round(item.y), "radius_m": round(item.radius_m),
            "door": door(network, item), "catchment": catchment(network, item),
            "note": "A Run spawns its trips from its catchment to its door and back; its split is its car share until "
                    "institution_mode_split sets one."}


def deploy_institutions(network: Network, count, kinds=None, seed=None, radius_m=None) -> dict:
    """`count` buildings at random spots of clear ground in the network's extent, all or none."""
    count = _int(count)
    if not 1 <= count <= 50:
        raise ValueError("count must be 1 to 50.")
    names = {name.lower(): name for name in KINDS}
    chosen = [names.get(str(kind).strip().lower()) for kind in (_ids(kinds) if kinds else list(KINDS))]
    if None in chosen:
        raise ValueError(f"kinds must be among {', '.join(KINDS)}.")
    nodes = list(network.all_nodes())
    if not nodes:
        raise ValueError("There is no network to place them in: build its roads first.")
    xs, ys = [node.x for node in nodes], [node.y for node in nodes]
    rng = random.Random(seed)
    placed, tries = [], 0
    while len(placed) < count and tries < 300 * count:
        tries += 1
        kind = rng.choice(chosen)
        area, people, employees, start, end, car = KINDS[kind].defaults
        x, y = rng.uniform(min(xs), max(xs)), rng.uniform(min(ys), max(ys))
        if footprint_problem(network, x, y, area) is not None:
            continue
        reach = float(radius_m) if radius_m is not None else _catchment_radius(network, x, y)
        placed.append(place(network, kind, x, y, area, max(reach, sqrt(area) / 2), people, employees, start, end, car))
    if len(placed) < count:
        raise ValueError(f"Only {len(placed)} of {count} found clear ground in the network's extent: ask for fewer, "
                         "or smaller kinds.")
    return {"placed": [{"id": item.id, "kind": item.kind, "x": round(item.x), "y": round(item.y),
                        "radius_m": round(item.radius_m), "reaches": links(network, item.x, item.y, item.radius_m)}
                       for item in placed], "seed": seed,
            "note": "Each has its kind's usual figures; the user can see and delete them in Demand Generator > "
                    "Institutions. A Run spawns their trips; set their mode split with institution_mode_split."}


def _passengers_problem(boarding_per_h, alighting_percent) -> str | None:
    if boarding_per_h is not None and not 0 <= float(boarding_per_h) <= MAX_BOARDING_PER_H:
        return f"boarding_per_h must be 0 to {MAX_BOARDING_PER_H:g}."
    if alighting_percent is not None and not 0 <= float(alighting_percent) <= 100:
        return "alighting_percent must be 0 to 100."
    return None


def add_crossing(network: Network, road, at_m=None, kind="zebra") -> dict:
    road = network.road(str(road))  # KeyError for an unknown road
    at = network.road_geometry_length(road) / 2 if at_m is None else float(at_m)
    crossing = network.add_crossing(road.id, at, str(kind))  # refused (ValueError) with its reason
    return {"crossing": crossing.id, "kind": crossing.kind, "road": road.id, "at_m": round(at, 1)}


def add_path(network: Network, start, end, bends=None) -> dict:
    way = network.add_walkway(str(start), str(end), _parsed_json(bends) if isinstance(bends, str) else (bends or []))
    return {"path": way.id, "from": way.start, "to": way.end,
            "length_m": round(polyline_length(network.walkway_points(way)), 1)}


def _pair_demand(word: str, per_hour, per_day, most: float, shaped: dict) -> tuple[float, bool, dict | None]:
    """(how many, daily, the day's curve or None) from a pair demand tool's `word`_per_hour or `word`_per_day, one of
    them, at most `most` an hour (24 times it a day), and its curve's points, polynomial or preset (daily only)."""
    if (per_hour is None) == (per_day is None):
        raise ValueError(f"Give {word}_per_hour (hourly demand) or {word}_per_day (daily demand), one of them.")
    daily = per_day is not None
    people, most = float(per_day if daily else per_hour), most * (24 if daily else 1)
    if not 0 <= people <= most:
        raise ValueError(f"{word}_per_{'day' if daily else 'hour'} must be 0 to {most:,.0f}.")
    curve = profile_record(profile_from(shaped)) if any(v not in (None, "", []) for v in shaped.values()) else None
    if curve is not None and not daily:
        raise ValueError(f"A curve shapes daily demand: give {word}_per_day with it.")
    return people, daily, curve


def _store_pair_demand(keys: list, people: float, daily: bool, curve: dict | None) -> None:
    """Each (pair, its matrix) of `keys` set to `people` (0 removes it); daily, with `curve` or the pair's own."""
    for key, demand in keys:
        if not people:
            demand.pop(key, None)
        elif daily:  # its own curve kept unless one is given
            demand[key] = {"people": people, "curve": curve or demand.get(key, {}).get("curve")
                           or profile_record(DailyProfile())}
        else:
            demand[key] = people


def set_pedestrian_demand(network: Network, pairs, people_per_hour=None, people_per_day=None, points=None,
                          polynomial=None, preset=None) -> dict:
    people, daily, curve = _pair_demand("people", people_per_hour, people_per_day, MAX_WALK_DEMAND,
                                        {"points": points, "polynomial": polynomial, "preset": preset})
    keys = []
    for pair in _ids(pairs):
        ends = [end.strip() for end in re.split(r"->|=>|>>| to |-", str(pair)) if end.strip()]
        among = next((places for places in (network.approach_nodes, network.spawn_points)
                      if len(ends) == 2 and all(end in places for end in ends)), None)
        if among is None or ends[0] == ends[1]:
            raise ValueError(f"{pair!r}: a pair is two different boundary ends, as '1W->1E' (the ends: "
                             f"{', '.join(sorted(network.approach_nodes))}), or two different deploy points, as "
                             f"'S1->S2' (the points: {', '.join(sorted(network.spawn_points)) or 'none yet'}).")
        walk = among is network.approach_nodes
        keys.append((f"{ends[0]}->{ends[1]}", (network.walk_daily if walk else network.spawn_daily) if daily
                     else network.walk_demand if walk else network.spawn_demand))
    _store_pair_demand(keys, people, daily, curve)
    if not daily:
        return {"pedestrians_per_hour": {key: people for key, _ in keys}}
    return {"pedestrians_per_day": {key: people for key, _ in keys},
            "curves": {key: demand[key]["curve"] for key, demand in keys if key in demand},
            "runs_use": "daily walkers only while the Demand Generator's switch shows Daily (set_od_demand or "
                        "shape_daily_curve with daily demand switches it)"}


def add_metro_line(network: Network, stations, merge=False, **service) -> dict:
    stations = _parsed_json(stations) if isinstance(stations, str) else stations
    if not isinstance(stations, list) or len(stations) < 2 or not all(
            isinstance(p, (list, tuple)) and len(p) == 2 for p in stations):
        raise ValueError("stations must be a list of 2 or more [x, y] in metres.")
    points = [(float(x), float(y)) for x, y in stations]
    try:
        line = network.add_metro_line(points[0], points[1], bool(merge))
        target = 1
        while True:  # a merged leg ends at its interchange (as a click does): on from there to the point asked for
            last = network.metro_stops[line.stops[-1]]
            x, y = points[target]
            if any(last.id in other.stops for other in network.metro_lines.values() if other is not line) \
                    and hypot(x - last.x, y - last.y) >= METRO_MIN_SPACING_M:
                network.add_metro_stop(line.id, x, y, True)
                continue
            target += 1
            if target == len(points):
                break
            network.add_metro_stop(line.id, *points[target], bool(merge))
    except MetroCrossing as crossing:  # run_tool puts back whatever was built
        raise ValueError(f"A leg crosses line {crossing.line_id}: give merge true for an interchange station where "
                         "they cross, or route this line clear of it.") from None
    for name, value in service.items():  # metro_line_problem (validate) refuses one out of range
        setattr(line, name, value)
    return _metro_line_info(network, line.id)


def update_metro_line(network: Network, line, **service) -> dict:
    found = network.metro_lines[str(line)]  # KeyError for an unknown line
    for name, value in service.items():
        setattr(found, name, value)
    return _metro_line_info(network, found.id)


def _metro_line_info(network: Network, line_id: str) -> dict:
    line = network.metro_lines[line_id]
    return {"line": line.id, "stations": {stop_id: [round(network.metro_stops[stop_id].x, 1),
                                                    round(network.metro_stops[stop_id].y, 1)] for stop_id in line.stops},
            "track_km": round(network.metro_length_m(line.id) / 1000, 2),
            "service": {name: getattr(line, name) for name in METRO_SERVICE},
            "capacity_per_h_each_way": round(60 / line.headway_min * line.train_capacity)}


def set_metro_demand(network: Network, pairs, passengers_per_hour=None, passengers_per_day=None, points=None,
                     polynomial=None, preset=None) -> dict:
    people, daily, curve = _pair_demand("passengers", passengers_per_hour, passengers_per_day, MAX_METRO_DEMAND,
                                        {"points": points, "polynomial": polynomial, "preset": preset})
    keys = []
    for pair in _ids(pairs):
        ends = [end.strip() for end in re.split(r"->|=>|>>| to |-", str(pair)) if end.strip()]
        if len(ends) != 2 or ends[0] == ends[1] or not all(end in network.metro_stops for end in ends):
            raise ValueError(f"{pair!r}: a pair is two different stations, as 'M1->M2' (the stations: "
                             f"{', '.join(sorted(network.metro_stops)) or 'none yet: add_metro_line'}).")
        if not metro_ride(network, *ends):
            raise ValueError(f"No ride joins {ends[0]} and {ends[1]}: one line, or one change at an interchange.")
        keys.append((f"{ends[0]}->{ends[1]}", network.metro_daily if daily else network.metro_demand))
    _store_pair_demand(keys, people, daily, curve)
    if not daily:
        return {"passengers_per_hour": {key: people for key, _ in keys}}
    return {"passengers_per_day": {key: people for key, _ in keys},
            "curves": {key: demand[key]["curve"] for key, demand in keys if key in demand},
            "runs_use": "daily passengers only while the Demand Generator's switch shows Daily"}


def add_spawn_points(network: Network, points) -> dict:
    points = _parsed_json(points) if isinstance(points, str) else points
    if not isinstance(points, list) or not points or not all(isinstance(p, (list, tuple)) and len(p) == 2
                                                             for p in points):
        raise ValueError("points must be a list of [x, y] in metres.")
    placed = [network.add_spawn_point(float(x), float(y)).id for x, y in points]
    return {"spawn_points": placed}


def pedestrian_summary(network: Network) -> dict:
    from .walking import WalkGraph  # noqa: PLC0415 - walking builds on the network's own modules

    roads = list(network.all_roads())
    graph = WalkGraph(network)
    return {"roads_with_footpaths_both_sides": sum(len(road.footpaths) == 2 for road in roads),
            "roads_with_a_footpath_one_side": sum(len(road.footpaths) == 1 for road in roads),
            "roads_without_footpaths": sum(not road.footpaths for road in roads),
            "footpath_km": round(sum(len(road.footpaths) * network.road_geometry_length(road) for road in roads) / 1000, 2),
            "roads_with_cycle_lanes": sorted(road.id for road in roads if road.cycle_lanes),
            "paths": [{"id": way.id, "from": way.start, "to": way.end,
                       "length_m": round(polyline_length(network.walkway_points(way)))}
                      for way in network.walkways.values()],
            "crossings": [{"id": c.id, "kind": c.kind, "road": c.road, "at_m": round(c.distance_m)}
                          for c in network.pedestrian_crossings.values()],
            "pedestrians_per_hour": dict(network.walk_demand),
            "pedestrians_per_day": {pair: day["people"] for pair, day in network.walk_daily.items()},
            "spawn_points": {key: [round(p.x), round(p.y)] for key, p in network.spawn_points.items()},
            "spawn_pedestrians_per_hour": dict(network.spawn_demand),
            "spawn_pedestrians_per_day": {pair: day["people"] for pair, day in network.spawn_daily.items()},
            "pairs_nobody_can_walk": [pair for pair in {**network.walk_demand, **network.walk_daily}
                                      if graph.route(*pair.split("->")) is None]
            + [pair for pair in {**network.spawn_demand, **network.spawn_daily} if graph.route(*(
                (network.spawn_points[end].x, network.spawn_points[end].y) for end in pair.split("->"))) is None],
            "without_a_crossing": "walkers cross a junction's arm in a gap in its traffic (HCM 2000 Eq 18-17)"}


def add_bus_stop(network: Network, road, direction="both", dwell_s=None, boarding_per_h=None,
                 alighting_percent=None) -> dict:
    problem = _passengers_problem(boarding_per_h, alighting_percent)
    if problem:
        raise ValueError(problem)
    stop = network.add_bus_stop(str(road), str(direction))
    if dwell_s is not None:
        stop.dwell_s = float(dwell_s)
    if boarding_per_h is not None:
        stop.boarding_per_h = float(boarding_per_h)
    if alighting_percent is not None:
        stop.alighting_percent = float(alighting_percent)
    return {"added": stop.id, "road": stop.road, "distance_m": round(stop.distance_m, 1), "serves": stop.direction,
            "dwell_s": stop.dwell_s, "boarding_per_h": stop.boarding_per_h, "alighting_percent": stop.alighting_percent}


def update_bus_stop(network: Network, stop, distance_m=None, direction=None, dwell_s=None, boarding_per_h=None,
                    alighting_percent=None) -> dict:
    if stop not in network.bus_stops:
        raise ValueError(f"No bus stop called {stop}.")
    problem = _passengers_problem(boarding_per_h, alighting_percent)
    if problem:
        raise ValueError(problem)
    item = network.bus_stops[stop]
    if boarding_per_h is not None:
        item.boarding_per_h = float(boarding_per_h)
    if alighting_percent is not None:
        item.alighting_percent = float(alighting_percent)
    if direction is not None:
        if direction not in BUS_STOP_DIRECTIONS:
            raise ValueError(f"direction must be one of {', '.join(BUS_STOP_DIRECTIONS)}.")
        item.direction = direction
    if distance_m is not None and not network.move_bus_stop(stop, float(distance_m)):  # refused: the bay stays
        low, high = network.bus_stop_range(network.road(item.road))
        if low > high:
            raise ValueError(f"Road {item.road} is too short for bus stop {stop}'s bay clear of its junctions.")
        there = deepcopy(item)
        there.distance_m = min(max(float(distance_m), low), high)
        raise ValueError(f"{network.bus_stop_problem(there)} Give another distance_m ({low:.0f} to {high:.0f} m "
                         f"along road {item.road}), or move what is in the way first.")
    if dwell_s is not None:
        item.dwell_s = float(dwell_s)
    return {"stop": stop, "road": item.road, "distance_m": round(network.bus_stop_distance(item), 1),
            "serves": item.direction, "dwell_s": item.dwell_s, "boarding_per_h": item.boarding_per_h,
            "alighting_percent": item.alighting_percent}


def update_institution(network: Network, id, people=None, employees=None, shift_start_h=None, shift_end_h=None,  # noqa: A002
                       car_share=None, radius_m=None) -> dict:
    if id not in network.institutions:
        raise KeyError(id)
    item = network.institutions[id]
    if car_share is not None and not (1 <= float(car_share) <= 100 or float(car_share) == 0):
        raise ValueError(f"car_share is a percent, 0 to 100 ({float(car_share) * 100:g}, not {float(car_share):g}, "
                         "for that share)." if 0 < float(car_share) < 1 else "car_share is a percent, 0 to 100.")
    for key, value, low, high in (("shift_start_h", shift_start_h, 0, 24), ("shift_end_h", shift_end_h, 0, 24),
                                  ("radius_m", radius_m, 10, 5000)):
        if value is not None and not low <= float(value) <= high:
            raise ValueError(f"{key} must be {low} to {high}.")
    before = {key: getattr(item, key) for key in ("people", "employees", "shift_start_h", "shift_end_h", "car_share",
                                                  "radius_m")}
    if car_share is not None:
        item.mode_shares = {}  # a share typed in replaces the mode split
    for key, value in (("people", people), ("employees", employees)):
        if value is not None:
            setattr(item, key, _int(value))
    for key, value in (("shift_start_h", shift_start_h), ("shift_end_h", shift_end_h), ("car_share", car_share),
                       ("radius_m", radius_m)):
        if value is not None:
            setattr(item, key, float(value))
    changed = {key: f"{old:g} -> {getattr(item, key):g}" for key, old in before.items() if getattr(item, key) != old}
    return {"updated": id, "changed": changed or "nothing", "reaches": links(network, item.x, item.y, item.radius_m)}

"""Validated JSON persistence: a network file, or a whole project (no Qt).

A project file is one JSON document: the network (its configuration, nodes, roads, ends, bus stops,
institutions, interchanges and background, as a network file always had), plus `format` (what it is
and its version), `demand` (the OD matrix: which demand is shown and every pair entered, each with its
hourly flow and its 15-minute shares, trips a day and their curve, arrival pattern and split between vehicle types),
`vehicle_types` (every type and the default split) and `run` (the seed and speed), and the AI Mode
conversation. JSON, not JSON Lines: a project is one nested document, read and checked as a whole
before anything changes; JSON Lines suits streams of records appended one by one. A file without the
project parts (an older network file) opens with no demand and the default vehicle types."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

import re
from math import ceil, isfinite

from .daily_profile import ARRIVAL_PATTERNS, DEGREES, HOURS, quarter_shares
from .models import METRO_SERVICE, Background, BusStop, Institution, MetroLine, MetroStop, Network, Node, Road
from .vehicle import VEHICLE_CLASSES

PROJECT_FORMAT = {"app": "ACMS project", "version": 2}
MAX_SEED = 2**31 - 1
from .network_generator import add_boundary_approaches


def save_network(network: Network, path: str | Path, conversation: dict | None = None, project: dict | None = None) -> None:
    """Write the network, the AI Mode conversation with it when there is one, and the project's other
    parts (`project`: demand, vehicle_types, run; checked first by `project_problem`). Written whole to a
    temporary file and moved into place, so a failed save leaves the old file as it was."""
    network.synchronize_road_lengths()
    network.validate()
    document = {"network": network.configuration(), **network.as_records(), "background": asdict(network.background),
                "interchanges": network.interchanges, "route_choice": network.route_choice,
                "metro_demand": network.metro_demand}
    if project is not None:
        problem = project_problem(project)
        if problem:
            raise ValueError(problem)
        document = {"format": PROJECT_FORMAT, **document, **project}
    if conversation:
        document["conversation"] = conversation
    target = Path(path)
    part = target.with_name(target.name + ".part")
    part.write_text(json.dumps(document, indent=2, allow_nan=False), encoding="utf-8")  # NaN isn't JSON
    part.replace(target)


def _not_a_number(name: str):
    """json.loads' parse_constant: NaN, Infinity and -Infinity (Python writes them; JSON has none) are refused."""
    raise ValueError(f"{name} is not a number")


def _number(value, low: float, high: float) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(value) and low <= value <= high


def _profile_problem(profile) -> str | None:
    if not isinstance(profile, dict) or profile.get("kind") not in ("points", "poly"):
        return "a daily curve must be 'points' or 'poly'"
    if profile["kind"] == "poly":
        coefficients = profile.get("coefficients")
        if not (isinstance(coefficients, list) and min(DEGREES) + 1 <= len(coefficients) <= max(DEGREES) + 1
                and all(_number(a, -1e6, 1e6) for a in coefficients)):
            return f"a polynomial curve needs {min(DEGREES) + 1}-{max(DEGREES) + 1} coefficients"
        return None
    points = profile.get("points")
    if not (isinstance(points, list) and len(points) >= 2 and all(
            isinstance(point, list) and len(point) == 2 and _number(point[0], 0, HOURS) and _number(point[1], 0, 1e6)
            for point in points) and all(a[0] < b[0] for a, b in zip(points, points[1:]))):
        return "a curve needs at least two [hour, level] points, hours 0-24 in order, levels not below zero"
    return None


def project_problem(project: dict) -> str | None:
    """What is wrong with a project's parts (demand, vehicle_types, run), or None."""
    types = project.get("vehicle_types")
    count = None
    if types is not None:
        modes = types.get("types") if isinstance(types, dict) else None
        if not (isinstance(modes, list) and 1 <= len(modes) <= 8 and all(isinstance(mode, dict) for mode in modes)):
            return "Vehicle types: 1 to 8 types are needed."
        names = [str(mode.get("name", "")).strip() for mode in modes]
        if not all(names) or len({name.lower() for name in names}) != len(names):
            return "Vehicle types: each needs a name of its own."
        for mode in modes:
            if not re.fullmatch(r"#[0-9a-fA-F]{6}", str(mode.get("colour", ""))):
                return f"Vehicle type {mode.get('name')}: its colour must be like #3b82f6."
            if not (isinstance(mode.get("headway_min", 0), int) and 0 <= mode.get("headway_min", 0) <= 120):
                return f"Vehicle type {mode.get('name')}: its headway must be 0 to 120 minutes."
            if not isinstance(mode.get("timetabled", False), bool):
                return f"Vehicle type {mode.get('name')}: timetabled must be true or false."
            if not isinstance(mode.get("random_colour", False), bool):
                return f"Vehicle type {mode.get('name')}: random_colour must be true or false."
            if not isinstance(mode.get("filters", False), (bool, type(None))):  # none: by its class (VehicleMode)
                return f"Vehicle type {mode.get('name')}: filters must be true or false."
            if mode.get("headway_min") and mode.get("timetabled") is False:
                return f"Vehicle type {mode.get('name')}: it has a headway but isn't timetabled."
            if mode.get("vehicle_class", "bus") not in VEHICLE_CLASSES:  # none: inferred from its name (VehicleMode)
                return f"Vehicle type {mode.get('name')}: vehicle_class must be one of {', '.join(VEHICLE_CLASSES)}."
            for key, value in mode.items():
                if key not in ("name", "colour", "headway_min", "timetabled", "vehicle_class", "filters", "random_colour") \
                        and not _number(value, 0, 1e5):
                    return f"Vehicle type {mode.get('name')}: {key} must be a number, not below zero."
            if mode.get("emergency_decel") is not None and mode.get("decel", 4.5) > mode["emergency_decel"]:
                return f"Vehicle type {mode.get('name')}: decel must be no more than emergency_decel."
        split = types.get("default_split")
        count = len(modes)
        if not (isinstance(split, list) and len(split) == count and all(isinstance(v, int) and v >= 0 for v in split)
                and sum(split) == 100):
            return "Vehicle types: the default split needs one whole percent a type, adding up to 100."
    demand = project.get("demand")
    if demand is not None:
        if not isinstance(demand, dict) or demand.get("shown", "hourly") not in ("hourly", "daily") \
                or not isinstance(demand.get("pairs", []), list):
            return "Demand: it needs 'shown' (hourly or daily) and a list of pairs."
        seen = set()
        for pair in demand.get("pairs", []):
            if not isinstance(pair, dict):
                return "Demand: each pair must be an object."
            key = (pair.get("origin"), pair.get("destination"))
            name = f"{key[0]}->{key[1]}"
            if not all(isinstance(end, str) and end for end in key) or key[0] == key[1] or key in seen:
                return f"Demand {name}: each pair needs two different ends, once."
            seen.add(key)
            if pair.get("flow") is not None and not (isinstance(pair["flow"], int) and 0 <= pair["flow"] <= 100_000):
                return f"Demand {name}: its flow must be 0 to 100,000 veh/h."
            if pair.get("daily") is not None and not (isinstance(pair["daily"], int) and 0 <= pair["daily"] <= 2_000_000):
                return f"Demand {name}: its trips a day must be 0 to 2,000,000."
            if pair.get("pattern", "Random") not in ARRIVAL_PATTERNS:
                return f"Demand {name}: its arrival pattern must be one of {', '.join(ARRIVAL_PATTERNS)}."
            if pair.get("quarters") is not None:  # older files have none: an even hour
                try:
                    quarter_shares(pair["quarters"])
                except ValueError as exc:
                    return f"Demand {name}: its {exc}"
            split = pair.get("split")
            if not (isinstance(split, list) and all(isinstance(v, int) and v >= 0 for v in split) and sum(split) == 100
                    and (count is None or len(split) == count)):
                return f"Demand {name}: its split needs one whole percent a vehicle type, adding up to 100."
            problem = _profile_problem(pair.get("profile", {"kind": "points", "points": [[0, 1], [24, 1]]}))
            if problem:
                return f"Demand {name}: {problem}."
    run = project.get("run")
    if run is not None:
        if not isinstance(run, dict) or not (isinstance(run.get("seed", 42), int) and 0 <= run.get("seed", 42) <= MAX_SEED) \
                or not _number(run.get("speed", 1), 0.01, 1000):
            return f"Run settings: the seed must be a whole number 0 to {MAX_SEED:,} and the speed positive."
    return None


def saved_project(path: str | Path) -> dict:
    """A project file's demand, vehicle_types and run (those it has), checked. ValueError says what's wrong."""
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"), parse_constant=_not_a_number)
    except (OSError, ValueError) as exc:
        raise ValueError(f"Could not read the project: {exc}") from exc
    project = {key: document[key] for key in ("demand", "vehicle_types", "run") if key in document}
    problem = project_problem(project)
    if problem:
        raise ValueError(problem)
    return project


def saved_conversation(path: str | Path) -> dict | None:
    """The AI Mode conversation saved in a network file, or None."""
    try:
        conversation = json.loads(Path(path).read_text(encoding="utf-8")).get("conversation")
    except (OSError, ValueError, AttributeError):
        return None
    return conversation if isinstance(conversation, dict) else None


def _require_list(document: dict[str, Any], key: str) -> list[dict[str, Any]]:
    value = document.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError(f"JSON field {key!r} must be a list of objects.")
    return value


def load_network(path: str | Path) -> Network:
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"), parse_constant=_not_a_number)
    except (OSError, ValueError) as exc:
        raise ValueError(f"Could not read network JSON: {exc}") from exc
    if not isinstance(document, dict) or not isinstance(document.get("network"), dict):
        raise ValueError("JSON must contain a network configuration object.")

    config = document["network"]
    try:
        network = Network(
            horizontal_corridors=int(config["horizontal_corridors"]),
            vertical_corridors=int(config["vertical_corridors"]),
            default_spacing=float(config.get("default_spacing", 500.0)),
            driving_side=str(config.get("driving_side", "left")),
            default_speed_limit_kmh=float(config.get("default_speed_limit_kmh", 50.0)),
            default_lanes_per_direction=int(config.get("default_lanes_per_direction", 2)),
            default_lane_changing=bool(config.get("default_lane_changing", True)),
            default_signal_method=str(config.get("default_signal_method", "webster")),
        )
        for record in _require_list(document, "nodes"):
            node = Node(**record)
            if node.id in network.nodes:
                raise ValueError(f"Duplicate node ID: {node.id}")
            network.nodes[node.id] = node
        for record in _require_list(document, "roads"):
            road = Road(**record)
            if road.id in network.roads:
                raise ValueError(f"Duplicate road ID: {road.id}")
            network.roads[road.id] = road
        for record in _require_list(document, "approach_nodes"):
            node = Node(**record)
            if node.id in network.nodes or node.id in network.approach_nodes:
                raise ValueError(f"Duplicate node ID: {node.id}")
            network.approach_nodes[node.id] = node
        for record in _require_list(document, "approach_roads"):
            road = Road(**record)
            if road.id in network.roads or road.id in network.approach_roads:
                raise ValueError(f"Duplicate road ID: {road.id}")
            network.approach_roads[road.id] = road
        interchanges = document.get("interchanges", {})  # older files have none
        if not isinstance(interchanges, dict):
            raise ValueError("JSON field 'interchanges' must be an object.")
        network.interchanges = {str(name): list(axis) if isinstance(axis, list) else axis
                                for name, axis in interchanges.items()}
        network.route_choice = document.get("route_choice") or {}  # older files have none: fastest routes
        network.metro_demand = dict(document.get("metro_demand") or {})  # older files have none
        if isinstance(document.get("background"), dict):  # older files have none: the default
            network.background = Background(**document["background"])
        for record in _require_list(document, "bus_stops"):  # older files have none
            stop = BusStop(**record)
            if stop.id in network.bus_stops:
                raise ValueError(f"Duplicate bus stop ID: {stop.id}")
            network.bus_stops[stop.id] = stop
        for record in _require_list(document, "institutions"):  # older files have none
            item = Institution(**record)
            if item.id in network.institutions:
                raise ValueError(f"Duplicate institution ID: {item.id}")
            network.institutions[item.id] = item
        for record in _require_list(document, "metro_stops"):  # older files have none
            stop = MetroStop(**record)
            if stop.id in network.metro_stops:
                raise ValueError(f"Duplicate metro stop ID: {stop.id}")
            network.metro_stops[stop.id] = stop
        for record in _require_list(document, "metro_lines"):
            if "train_capacity" in record:  # saved before carriages: the fewest carriages that hold it (400 each at most)
                record = dict(record)
                total = record.pop("train_capacity")
                cars = max(1, ceil(total / METRO_SERVICE["car_capacity"][2])) if isinstance(total, (int, float)) else 1
                record.update(cars_per_train=cars, car_capacity=round(total / cars) if isinstance(total, (int, float)) else total)
            line = MetroLine(**record)
            if line.id in network.metro_lines:
                raise ValueError(f"Duplicate metro line ID: {line.id}")
            network.metro_lines[line.id] = line
        # Older files also list static mock-up vehicles; vehicles now come from the simulation.
        if not network.approach_nodes and not network.approach_roads:
            # Files produced by the initial editor did not persist approaches;
            # reconstruct them from the loaded, possibly edited, grid geometry.
            add_boundary_approaches(network)
        elif not network.approach_nodes or not network.approach_roads:
            raise ValueError("Approach nodes and approach roads must be provided together.")
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, ValueError) and str(exc).startswith("Duplicate"):
            raise
        raise ValueError(f"Invalid network record: {exc}") from exc

    network.validate()
    # Geometry is authoritative; reject files where duplicated lengths disagree.
    for road in network.all_roads():
        geometric = network.road_geometry_length(road)
        if abs(road.length_m - geometric) > max(0.01, geometric * 1e-6):
            raise ValueError(
                f"Road {road.id} length ({road.length_m:g}) does not match its node geometry ({geometric:g})."
            )
    return network

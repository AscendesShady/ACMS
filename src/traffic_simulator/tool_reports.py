"""Putting the assistant's tool calls, changes and refusals into words (no Qt).

describe_changes (what the user's edits changed), state_changes (the same plus demand and vehicle types), describe_call (one line per call shown for approval) and
explain_error/already_so (refusals as structured data).
"""

from __future__ import annotations

import re
from dataclasses import asdict
from math import hypot
from pathlib import Path

from .map_tiles import ZONE_DEFAULT_M
from .models import Network
from .tool_args import _ALL_WORDS, _bool, _ids, _road_from_other_end, _sides


def describe_changes(before: Network, after: Network, limit: int = 25) -> list[str]:
    """What changed between two states of the network, one line each: how the assistant
    hears about the user's own edits since its last reply."""
    config = ("horizontal_corridors", "vertical_corridors", "default_spacing", "driving_side")
    if any(getattr(before, key) != getattr(after, key) for key in config):
        return [f"Generated a new network: a {after.vertical_corridors} x {after.horizontal_corridors} grid "
                f"({after.vertical_corridors} columns, {after.horizontal_corridors} rows), "
                f"{after.default_spacing:g} m spacing, driving on the {after.driving_side}"]
    lines = []
    old_nodes, new_nodes = {**before.nodes, **before.approach_nodes}, {**after.nodes, **after.approach_nodes}

    def kind(node_id: str, network: Network) -> str:
        return "junction" if node_id in network.nodes else "boundary end"

    lines += [f"Added {kind(i, after)} {i}" for i in new_nodes if i not in old_nodes]
    lines += [f"Deleted {kind(i, before)} {i}" for i in old_nodes if i not in new_nodes]
    moved = set()
    for node_id in old_nodes.keys() & new_nodes.keys():
        old, new = old_nodes[node_id], new_nodes[node_id]
        if hypot(new.x - old.x, new.y - old.y) > 0.5:
            moved.add(node_id)
            lines.append(f"Moved {node_id} by ({new.x - old.x:+.0f}, {new.y - old.y:+.0f}) m")
        if old.control != new.control and node_id in after.nodes:
            lines.append(f"{node_id}: {old.control} → {new.control}")
        for key, label in (("supply_vph", "supply"), ("demand_vph", "demand")):
            if getattr(old, key) != getattr(new, key):
                lines.append(f"{node_id}: {label} {getattr(old, key):g} → {getattr(new, key):g} veh/h")
        timing = ("signal_phases", "signal_method", "green_times_s", "cycle_min_s", "cycle_max_s",
                  "target_saturation", "stop_weight_s", "manual_greens_s", "amber_s")
        if any(getattr(old, key) != getattr(new, key) for key in timing):
            lines.append(f"{node_id}: signal settings → {new.signal_phases} phases, {new.signal_method}, "
                         f"greens {', '.join(f'{green:g}' for green in new.green_times_s)} s, cycle "
                         f"{new.cycle_min_s:g}-{new.cycle_max_s:g} s, target {new.target_saturation:.0%}, "
                         f"stop weight {new.stop_weight_s:g} s" + (
                             "; windows " + ", ".join(f"{name} {w[0]:g}-{w[1]:g}" for road, name, _ in after.signal_arms(node_id)
                                                      if (w := new.manual_greens_s.get(road))) + f" s, amber {new.amber_s:g} s"
                             if new.signal_method == "manual" else ""))
    old_roads, new_roads = {**before.roads, **before.approach_roads}, {**after.roads, **after.approach_roads}
    lines += [f"Added road {i} ({new_roads[i].from_node} → {new_roads[i].to_node})"
              for i in new_roads if i not in old_roads and i not in new_nodes]  # an added end names its road
    lines += [f"Deleted road {i}" for i in old_roads if i not in new_roads and i not in old_nodes]
    for road_id in old_roads.keys() & new_roads.keys():
        old, new = old_roads[road_id], new_roads[road_id]
        changes = [f"{label} {getattr(old, key)} → {getattr(new, key)}"
                   for key, label in (("lanes_per_direction", "lanes/direction"), ("speed_limit_kmh", "speed"),
                                      ("direction", "direction"), ("lane_changing", "lane changing"),
                                      ("road_type", "type"), ("overpass", "overpass"), ("pockets", "turn pockets"),
                                      ("cycle_lanes", "cycle lanes to"), ("footpaths", "footpaths"))
                   if getattr(old, key) != getattr(new, key)]
        if (old.from_node, old.to_node) == (new.to_node, new.from_node):
            changes.append(f"now runs {new.from_node} → {new.to_node}")
        shape = old.shape[::-1] if (old.from_node, old.to_node) == (new.to_node, new.from_node) else old.shape
        if [tuple(p) for p in shape] != [tuple(p) for p in new.shape] and not {new.from_node, new.to_node} & moved:
            changes.append(f"curved through {len(new.shape)} point{'s' if len(new.shape) != 1 else ''}"
                           if new.shape else "now straight")  # a moved end carries its curves along (_carry_shapes)
        if abs(old.length_m - new.length_m) > 0.5 and not {new.from_node, new.to_node} & moved:
            changes.append(f"length {old.length_m:.0f} → {new.length_m:.0f} m")
        if changes:
            lines.append(f"Road {road_id}: {', '.join(changes)}")
    old_stops, new_stops = before.bus_stops, after.bus_stops
    for stop_id, new in new_stops.items():
        old = old_stops.get(stop_id)
        if old is None or (old.road, old.direction, old.dwell_s, old.boarding_per_h, old.alighting_percent) != \
                (new.road, new.direction, new.dwell_s, new.boarding_per_h, new.alighting_percent) \
                or abs(old.distance_m - new.distance_m) > 0.5:
            road = after.road(new.road)
            serves = {"forward": f"{road.from_node} → {road.to_node}", "backward": f"{road.to_node} → {road.from_node}",
                      "both": "both directions"}[new.direction]
            stands = (f"{new.boarding_per_h:g} passengers an hour boarding, {new.alighting_percent:g} % of each load "
                      f"alighting" if new.boarding_per_h or new.alighting_percent else f"buses stand {new.dwell_s:g} s")
            lines.append(f"{'Added' if old is None else 'Changed'} bus stop {stop_id}: {new.distance_m:.0f} m "
                         f"along road {road.id} from {road.from_node}, serving {serves}, {stands}")
    lines += [f"Deleted bus stop {stop_id}" for stop_id in old_stops if stop_id not in new_stops]
    lines += [f"Placed institution {key} ({item.kind}) at ({item.x:.0f}, {item.y:.0f}) m, its circle "
              f"{item.radius_m:.0f} m" for key, item in after.institutions.items() if key not in before.institutions]
    lines += [f"Deleted institution {key}" for key in before.institutions if key not in after.institutions]
    for key, new in after.institutions.items():
        old = before.institutions.get(key)
        if old is None or old == new:
            continue
        changes = [f"moved by ({new.x - old.x:+.0f}, {new.y - old.y:+.0f}) m"] \
            if hypot(new.x - old.x, new.y - old.y) > 0.5 else []
        changes += [f"kind {old.kind} → {new.kind}"] if old.kind != new.kind else []
        changes += [f"{label} {getattr(old, field):g} → {getattr(new, field):g}" for field, label in (
            ("radius_m", "circle (m)"), ("area_m2", "area (m²)"), ("people", "people"), ("employees", "employees"),
            ("shift_start_h", "shift start (h)"), ("shift_end_h", "shift end (h)"), ("car_share", "car share (%)"))
            if getattr(old, field) != getattr(new, field)]
        changes += [f"{label} changed" for field, label in (("mode_shares", "mode split"), ("curves", "daily curves"),
                                                            ("per_hour", "hourly demand"), ("height_m", "height"))
                    if getattr(old, field) != getattr(new, field)]
        if changes:
            lines.append(f"Institution {key}: {', '.join(changes)}")
    lines += [f"Added path {key} ({way.start} to {way.end})" for key, way in after.walkways.items()
              if key not in before.walkways]
    lines += [f"Deleted path {key}" for key in before.walkways if key not in after.walkways]
    for key, line in after.metro_lines.items():
        old = before.metro_lines.get(key)
        if old != line:
            lines.append(f"{'Added' if old is None else 'Changed'} MRT line {key}: {', '.join(line.stops)}, a train "
                         f"every {line.headway_min:g} min at {line.speed_kmh:g} km/h, {line.cars_per_train} carriages "
                         f"of {line.car_capacity}")
    lines += [f"Deleted MRT line {key}" for key in before.metro_lines if key not in after.metro_lines]
    for pair in sorted(set(before.metro_demand) | set(after.metro_demand)):
        old, new = before.metro_demand.get(pair), after.metro_demand.get(pair)
        if old != new:
            lines.append(f"MRT passengers {pair}: {old or 0:g} → {new or 0:g} an hour")
    lines += [f"Added deploy point {key} at ({p.x:.0f}, {p.y:.0f}) m" for key, p in after.spawn_points.items()
              if key not in before.spawn_points]
    lines += [f"Deleted deploy point {key}" for key in before.spawn_points if key not in after.spawn_points]
    for pair in sorted(set(before.spawn_demand) | set(after.spawn_demand)):
        old, new = before.spawn_demand.get(pair), after.spawn_demand.get(pair)
        if old != new:
            lines.append(f"Pedestrians {pair}: {old or 0:g} → {new or 0:g} an hour")
    lines += [f"Added {c.kind} {key} on {c.road}, {c.distance_m:.0f} m along" for key, c in after.pedestrian_crossings.items()
              if key not in before.pedestrian_crossings]
    lines += [f"Deleted crossing {key}" for key in before.pedestrian_crossings if key not in after.pedestrian_crossings]
    for pair in sorted(set(before.walk_demand) | set(after.walk_demand)):
        old, new = before.walk_demand.get(pair), after.walk_demand.get(pair)
        if old != new:
            lines.append(f"Pedestrians {pair}: {old or 0:g} → {new or 0:g} an hour")
    for label, old_days, new_days in (("Pedestrians", before.walk_daily, after.walk_daily),
                                      ("Pedestrians", before.spawn_daily, after.spawn_daily),
                                      ("MRT passengers", before.metro_daily, after.metro_daily)):
        for pair in sorted(set(old_days) | set(new_days)):
            old, new = (old_days.get(pair) or {}).get("people", 0), (new_days.get(pair) or {}).get("people", 0)
            if old_days.get(pair) != new_days.get(pair):
                lines.append(f"{label} {pair}: " + (f"{old:g} → {new:g} a day" if old != new else "its day reshaped"))
    if before.route_choice != after.route_choice:
        choice = after.route_choice
        lines.append("Route choice off: every vehicle takes the fastest route" if not choice else
                     f"Route choice: {choice.get('routes', 3)} routes a pair by path-size logit, theta "
                     f"{choice.get('theta_per_min', 0.5):g}/min, {len(choice.get('link_times_s', {}))} measured link times")
    if before.background != after.background:  # place_map, or the user's Background settings
        back = after.background
        lines.append("Background now " + (
            f"the OpenStreetMap map at {back.lat:.5f}, {back.lon:.5f}"
            + (f", turned {back.map_rotation_deg:g}°" if back.map_rotation_deg else "")
            + (f", work zone {back.zone[2]:.0f} x {back.zone[3]:.0f} m" if back.zone else "") if back.kind == "map" else
            f"the image {Path(back.image).name}, {back.width_m:g} m wide" if back.kind == "image" else
            f"the colour {back.colour}"))
    if len(lines) > limit:
        lines = lines[:limit] + [f"...and {len(lines) - limit} more changes"]
    return lines


def state_changes(before: tuple, after: tuple) -> list[str]:
    """What changed between two of the window's states (MainWindow._snapshot: (network, OD cells, (vehicle types,
    default split))): the network (describe_changes), the OD matrix pair by pair, and the vehicle types, one line each."""
    lines = describe_changes(before[0], after[0], limit=1000)
    old, new = before[1], after[1]
    for pair in sorted(set(old) | set(new)):
        a, b = old.get(pair), new.get(pair)
        what = []
        for key, label, unit in (("flow", "hourly", "veh/h"), ("daily", "daily", "trips/day")):
            x, y = getattr(a, key, None), getattr(b, key, None)
            if x != y:
                what.append(f"{label} {x if x is not None else 'none'} → {y if y is not None else 'none'} {unit}")
        if a is not None and b is not None:
            if a.pattern != b.pattern:
                what.append(f"arrivals {a.pattern} → {b.pattern}")
            if tuple(a.split) != tuple(b.split):
                what.append(f"split {list(a.split)} → {list(b.split)} %")
            if a.profile != b.profile:
                what.append("daily curve reshaped")
            if a.drops != b.drops:
                what.append("drop-offs changed")
        if what:
            lines.append(f"Demand {pair[0]}->{pair[1]}: " + ", ".join(what))
    old_types, new_types = {m.name: asdict(m) for m in before[2][0]}, {m.name: asdict(m) for m in after[2][0]}
    lines += [f"Vehicle type {name} added" for name in new_types if name not in old_types]
    lines += [f"Vehicle type {name} removed" for name in old_types if name not in new_types]
    for name in old_types.keys() & new_types.keys():
        what = [f"{key} {old_types[name][key]} → {value}" for key, value in new_types[name].items()
                if old_types[name].get(key) != value]
        if what:
            lines.append(f"Vehicle type {name}: " + ", ".join(what))
    if tuple(before[2][1]) != tuple(after[2][1]):
        lines.append(f"Default split {list(before[2][1])} → {list(after[2][1])} %")
    return lines


def _split_items(split) -> list[tuple[str, object]]:
    """(vehicle type, percent) from a mode_split given as a dict or a list of {mode, percent}."""
    if isinstance(split, dict):
        return list(split.items())
    if isinstance(split, list):
        return [(item.get("mode", "?"), item.get("percent", "?")) for item in split if isinstance(item, dict)]
    return []


ERROR_KINDS = (  # (what the message says, its type, what to do): ChatSUMO's structured errors, so a model can recover
    (("unknown id", "no road called", "no junction called", "no bus stop called", "no end or junction called",
      "no extension called",
      "no run called", "no experiment called", "no test called", "no figure", "no such", "isn't a signalised"),
     "unknown_id", "Use one of the ids it lists, or read them first (list_roads, list_junctions, read_results)."),
    (("no route",), "unroutable", "Connect the two places (add_road) or pick ends that are joined (analyse_network)."),
    (("a run is going on", "a run or test is going on", "stop it first"), "busy",
     "Wait for the user's run to end (they Stop it), then call again."),
    (("no demand", "no hourly demand"), "no_demand", "Generate demand first (generate_demand or set_od_demand)."),
    (("must be", "is one of", "takes ", "give ", "needs "), "bad_argument", "Call again with the value the message asks for."),
    (("nothing would change",), "no_change", "It is like that already: nothing to do."),
)


def already_so(message: str) -> dict:
    """An edit that would change nothing, as the answer it is: what was asked is how it already is."""
    return {"unchanged": True, "already": message,
            "note": "Nothing needed changing: it is like that already. Tell the user so; don't call it again."}


def explain_error(message: str, network: Network | None = None) -> dict:
    """A refused tool call as data: the message, its type and what to do (ERROR_KINDS); for an unknown id, the
    network's nearest ids by spelling (difflib), so the model can call again without guessing."""
    import difflib  # noqa: PLC0415

    lowered = message.lower()
    kind, fix = "refused", "Read the message: it says why; change the call or ask the user."
    for words, found, advice in ERROR_KINDS:
        if any(word in lowered for word in words):
            kind, fix = found, advice
            break
    answer = {"error": message, "error_type": kind, "suggested_fix": fix}
    if kind == "unknown_id" and network is not None:
        quoted = re.findall(r"(?:called|id) '([^']+)'|called ([\w,.<>-]+)", message)
        wanted = next((a or b for a, b in quoted), "")
        known = [*network.roads, *network.approach_roads, *network.nodes, *network.bus_stops, *network.institutions]
        close = difflib.get_close_matches(wanted, known, n=5, cutoff=0.5) if wanted else []
        other = _road_from_other_end(network, wanted) if wanted else None
        if other:  # a road named from its other end comes first
            close = [other, *[item for item in close if item != other]][:5]
        if close:
            answer["did_you_mean"] = close
    return answer


def _area(args: dict) -> str:
    """A call's width_m x height_m as the tools take it ("" when neither is given), for its card."""
    if args.get("width_m") is None and args.get("height_m") is None:
        return ""
    try:
        return "{:g} × {:g} m".format(*_sides(args.get("width_m"), args.get("height_m")))
    except (TypeError, ValueError):
        return f"{args.get('width_m')} × {args.get('height_m')} m"


def describe_call(name: str, args: dict) -> str:
    """One line saying what a tool call does, for the chat and the approval card. Arguments of any shape (a model's
    objects for ids, a number for a list) get one too, its name and arguments as sent: the line never raises, as it is
    written before the call is checked and for a refused one."""
    a = args if isinstance(args, dict) else {}
    try:
        return _call_line(name, a)
    except Exception:  # noqa: BLE001 - shapes the line can't read: the call as sent; its check says what's wrong
        return f"{name}({', '.join(f'{key}={value}' for key, value in a.items())})"


def _call_line(name: str, a: dict) -> str:
    """describe_call's line, from arguments in the shapes the tool's schema gives."""
    def listed(key: str) -> str:
        return ", ".join(_ids(a.get(key, [])))

    def every_pair() -> str:
        return "every pair" + (f" from {listed('origins')}" if a.get("origins") else "") + \
            (f" to {listed('destinations')}" if a.get("destinations") else "")

    def crossing() -> str:
        return f", {a['crossings']} where it crosses roads" if a.get("crossings") else ""

    if name == "set_junction_control":
        return f"Make {listed('junctions')} {a.get('control')}"
    if name == "update_roads":
        labels = {"lanes_per_direction": "lanes/direction", "add_lanes": "lanes added each way", "speed_limit_kmh": "speed (km/h)",
                  "direction": "direction", "lane_changing": "lane changing", "road_type": "type",
                  "length_m": "length (m)", "pocket_junction": "turn pocket at", "pocket_lanes": "pocket lanes",
                  "pocket_length_m": "pocket length (m)", "pocket_side": "pocket side",
                  "footpaths": "pedestrian walkways", "cycle_lanes": "cycle lanes"}
        changes = ", ".join(f"{label} → {a[key]}" for key, label in labels.items() if a.get(key) is not None)
        roads = "every road" if str(a.get("roads", "")).strip().lower() in _ALL_WORDS else f"road {listed('roads')}"
        return f"Change {roads}: {changes or 'nothing'}"
    if name == "add_junction":
        return f"Add a junction at ({float(a.get('x', 0)):.0f}, {float(a.get('y', 0)):.0f}) m"
    if name == "add_road":
        return f"Add a road {a.get('from_junction')} ↔ {a.get('to_junction')}{crossing()}"
    if name == "add_boundary_end":
        return (f"Add a boundary end from {a.get('from_junction')} at "
                f"({float(a.get('x', 0)):.0f}, {float(a.get('y', 0)):.0f}) m{crossing()}")
    if name == "move_junctions":
        return f"Move {listed('junctions')} by ({float(a.get('dx', 0)):+.0f}, {float(a.get('dy', 0)):+.0f}) m"
    if name == "delete_objects":
        return f"Delete {listed('ids')}"
    if name == "build_interchange":
        return (f"Build a {a.get('kind')} interchange at {listed('junctions')}"
                f" ({'keep' if a.get('replace') is False else 'replace'} existing roads)")
    if name == "add_slip_road":
        return f"Add a {'single-lane' if _bool(a.get('single_lane', False)) else 'two-lane'} slip road from {listed('roads')}"
    if name == "set_end_flows":
        flows = ", ".join(f"{label} → {a[key]:g} veh/h" for key, label in
                          (("supply_vph", "supply"), ("demand_vph", "demand"))
                          if isinstance(a.get(key), (int, float)))
        return f"Set {a.get('end')} {flows or 'flows'}"
    if name == "generate_grid":
        return (f"Replace the whole network with a new {a.get('vertical')} × {a.get('horizontal')} grid "
                f"({a.get('vertical')} columns, {a.get('horizontal')} rows)"
                + (", with pedestrian walkways" if a.get("footpaths") is not None and _bool(a["footpaths"]) else ""))
    if name == "set_signal_timing":
        what = ", ".join(f"{key.replace('_', ' ')} {a[key]}" for key in ("method", "phases", "green_times_s", "cycle_min_s",
                                                                          "cycle_max_s", "target_saturation_percent",
                                                                          "stop_weight_s") if a.get(key) is not None)
        return f"Time {listed('junctions')}: {what or 'no change'}"
    if name == "run_simulation":
        return f"Test the network: a {a.get('duration_min')} min run, seed {a.get('seed', 'from the Seed box')}"
    if name == "run_experiment":
        scenarios = [s.get("name", "?") for s in a.get("scenarios") or [] if isinstance(s, dict)]
        factors = [f for f in a.get("factors") or [] if isinstance(f, dict)]
        conditions = [c.get("name", "?") for c in a.get("conditions") or [] if isinstance(c, dict)]
        seeds = a.get("seeds") if isinstance(a.get("seeds"), list) else [a.get("seeds")]
        designs = 1
        for factor in factors:
            designs *= len(factor.get("levels") or []) or 1
        what = (" × ".join(f"{f.get('name')} ({len(f.get('levels') or [])} levels)" for f in factors) if factors
                else ", ".join(scenarios))
        count = (designs if factors else len(scenarios)) * max(len(conditions), 1) * len(seeds)
        return (f"Run an experiment: {what}" + (f" under {', '.join(conditions)}" if conditions else "")
                + f" × {len(seeds)} seed{'s' if len(seeds) != 1 else ''}, {a.get('duration_min')} min each ({count} runs)")
    if name == "set_extension":
        return f"{'Tick' if _bool(a.get('on')) else 'Untick'} the extension {a.get('name')}"
    if name == "preflight_check":
        return f"Check the network: runs at 10 % and 50 % of the demand, {a.get('duration_min') or 15} min each"
    if name == "schedule_emergency":
        rows = a.get("schedules") if isinstance(a.get("schedules"), list) else []
        listed = "; ".join(f"{row.get('from')} → {row.get('to')}" + (f" every {row['every_min']} min" if row.get("every_min")
                           else f" after {row['after_min']} min" if row.get("after_min") else " once")
                           for row in rows if isinstance(row, dict))
        return f"Schedule emergency vehicles: {listed or 'none'}" + (", and send them now" if _bool(a.get("dispatch_now")) else "")
    if name == "start_batch":
        tests = " and ".join(kind for kind in ("TSP", "DBL", "DEVL") if _bool(a.get(f"test_{kind.lower()}")))
        default = a.get("compare_default") is None or _bool(a.get("compare_default"))
        return (f"Start a batch: seeds {a.get('seeds')}, {a.get('duration_min')} min each" + (f", testing {tests}" if tests else "")
                + (", compared with the default configuration" if default else ""))
    if name == "replay_run":
        return f"Replay the run {a.get('path')}"
    if name == "calibrate_demand":
        counts = a.get("counts") if isinstance(a.get("counts"), list) else []
        return f"Fit the hourly OD matrix to {len(counts)} counted flow{'s' if len(counts) != 1 else ''}"
    if name == "build_scenario":
        what = a.get("place") or (f"a {a['grid']} grid" if a.get("grid") else "the network as it is")
        tests = len(a.get("what_if") or []) if isinstance(a.get("what_if"), list) else 0
        return f"Build and test {what}" + (f" with {tests} what-if{'s' if tests != 1 else ''}" if tests else "")
    if name == "assign_routes":
        if _bool(a.get("off", False)):
            return "Route choice off: every vehicle on the fastest route"
        return (f"Route choice by path-size logit, {a.get('routes', 3)} routes a pair, {a.get('iterations', 4)} "
                f"averaging runs of {a.get('duration_min', 30)} min")
    if name == "optimise_signals":
        return f"Optimise the fixed-time signals at {listed('junctions')} by a genetic algorithm ({a.get('duration_min')} min runs)"
    if name == "choose_modes":
        modes = [str(item.get("mode")) for item in a.get("alternatives", []) if isinstance(item, dict)] \
            if isinstance(a.get("alternatives"), list) else []
        return f"Split the pairs between {', '.join(modes) or 'the types'} by mode choice (logit)" + \
            (f", times from {a['from_run']}" if a.get("from_run") else "")
    if name == "place_map":
        where = a.get("place") or f"{a.get('lat')}, {a.get('lon')}"
        size = f", work zone {_area(a) or f'{ZONE_DEFAULT_M:g} × {ZONE_DEFAULT_M:g} m'}"
        return f"Put the map of {where} under the network{size}"
    if name == "add_bus_stop":
        return f"Add a bus stop on {a.get('road')} ({a.get('direction', 'both')})"
    if name == "add_crossing":
        return f"Add a {a.get('kind', 'zebra')} crossing on {a.get('road')}" + \
            (f", {a['at_m']:g} m along" if isinstance(a.get("at_m"), (int, float)) else "")
    if name == "add_path":
        return f"Add a path from {a.get('start')} to {a.get('end')}"
    if name == "set_pedestrian_demand":
        per = "a day" if a.get("people_per_day") is not None else "an hour"
        figure = a.get("people_per_day") if a.get("people_per_day") is not None else a.get("people_per_hour")
        people = f"{figure:g} " if isinstance(figure, (int, float)) else ""
        return f"Set {people}pedestrians {per} on {', '.join(map(str, a.get('pairs') or [])) or 'pairs'}"
    if name == "pedestrian_summary":
        return "Read the network for pedestrians"
    if name == "add_spawn_points":
        return f"Place {len(a.get('points') or [])} pedestrian deploy points" if a.get("points") else \
            "Place pedestrian deploy points"
    if name == "update_bus_stop":
        return f"Change bus stop {a.get('stop')}"
    if name == "update_institution":
        return f"Change institution {a.get('id')}"
    if name == "update_vehicle_type":
        return f"Change the vehicle type {a.get('name')}"
    if name == "note_plan":
        return "Note the plan"
    if name == "create_vehicle_types":
        made = [f"{spec.get('name')} ({spec.get('kind')}" + (f", every {spec['headway_min']} min" if spec.get("headway_min") else "")
                + ")" for spec in (a.get("types") if isinstance(a.get("types"), list) else []) if isinstance(spec, dict)]
        return f"Add vehicle types: {', '.join(made) or 'none'}"
    if name == "deploy_institutions":
        kinds = listed("kinds") or "any kind"
        return f"Place {a.get('count')} institutions at random on clear ground ({kinds})"
    if name == "place_institution":
        where = f"near {a['near']}" if a.get("near") else f"at {a.get('x')}, {a.get('y')}"
        circle = f", catchment {a['radius_m']:g} m" if isinstance(a.get("radius_m"), (int, float)) else ""
        return f"Place a {a.get('kind')} {where}{circle}"
    if name == "institution_mode_split":
        which = listed("institutions") or "every institution"
        calibrated = (" calibrated to " + ", ".join(f"{k} {v:g} %" for k, v in a["observed_percent"].items())
                      if isinstance(a.get("observed_percent"), dict) and a["observed_percent"] else "")
        return f"Set the mode split of {which} by logit{calibrated}"
    if name == "network_from_background":
        what = (f"the image's {a['road_colour']} roads" if a.get("road_colour") else
                f"OpenStreetMap's {a.get('road_classes') or 'all'} roads, " + (
                    f"{_area(a)} round the work zone's centre" if _area(a) else
                    "in the map's work zone (1200 × 1200 m round the network's centre without one)"))
        return f"Replace the whole network with {what}"
    if name == "generate_demand":
        which = "the pairs " + ", ".join(_ids(a["pairs"])[:4]) if a.get("pairs") else every_pair()
        total = (f"{a['total']:g} {'trips/day' if a.get('demand') == 'daily' else 'veh/h'} in all"
                 if isinstance(a.get("total"), (int, float)) else "the ends' supply and demand")
        return f"Generate {a.get('demand') or 'hourly'} demand for {which} by a gravity model from {total}"
    if name == "scale_demand":
        which = "the pairs " + ", ".join(_ids(a["pairs"])[:4]) if a.get("pairs") else every_pair()
        how = (f"so the busiest junction runs at {a['degree_of_saturation']:g} of capacity"
               if isinstance(a.get("degree_of_saturation"), (int, float)) else f"by {a.get('factor')}")
        return f"Scale the demand of {which} {how}"
    if name in ("set_od_demand", "shape_daily_curve"):
        if a.get("pairs"):
            named = [str(p).replace("->", " → ") for p in _ids(a["pairs"])]
            which = ", ".join(named[:4]) + (f" and {len(named) - 4} more" if len(named) > 4 else "")
        else:
            which = every_pair()
        if name == "shape_daily_curve":
            how = (f"the {a['preset']} shape" if a.get("preset") else f"a degree-{len(a['polynomial']) - 1} "
                   "polynomial" if isinstance(a.get("polynomial"), list) else
                   f"{len(a['points'])} curve points" if isinstance(a.get("points"), list) else "a new shape")
            return f"Shape the daily curve for {which} with {how}"
        unit = "trips/day" if a.get("demand") == "daily" else "veh/h"
        split = a.get("mode_split")
        parts = [f"{a['value']} {unit}" if a.get("value") is not None else "",
                 f"{a['arrival_pattern']} arrivals" if a.get("arrival_pattern") else "",
                 f"peak-hour factor {a['peak_hour_factor']}" if a.get("peak_hour_factor") is not None else
                 "15-minute quarters" if a.get("quarters") is not None else "",
                 " · ".join(f"{mode} {share}%" for mode, share in _split_items(split))]
        return f"Set {a.get('demand', '')} demand for {which}: {', '.join(p for p in parts if p) or 'no change'}"\
            .replace("Set  demand", "Set demand")
    if name == "read_od_matrix":
        return "Read the OD matrix"
    if name == "show_on_map":
        return f"Show {a.get('id')} on the map"
    shown = ", ".join(f"{key}={value}" for key, value in a.items())
    return f"{name}({shown})"

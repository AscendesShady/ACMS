"""Tools the System 2 assistant calls on the live network (no Qt).

Read tools describe the network. Edit tools change it through the same model
methods the editor uses; the chat (ai_mode.py) runs them only after the user
approves each one, and a failed edit is rolled back completely. Network design
only: nothing here simulates vehicles or signals.

Three tools build things wholesale, still only through these checked paths: generate_demand fills
the OD matrix by a gravity model (demand_plan, applied by the Demand Generator's controls),
deploy_institutions places buildings at random on clear ground (institutions.place), and
network_from_background replaces the network with the background's roads (background_network.py).
Their slow or random part happens first in `prepare`, off the UI thread, so the check and the
change the user approves agree.
"""

from __future__ import annotations

import ast
import json
import random
import re
import time
from collections import Counter
from copy import deepcopy
from math import ceil, isfinite

from . import background_network
from .daily_profile import ARRIVAL_PATTERNS
from .design_checks import analyse_network, network_indices
from .design_checks import critical_links_plan, cut_elements, route_lengths  # noqa: F401 - re-exported
from .institutions import footprint_problem, place, shares
from .models import CROSSING_KINDS
from .metro import WALK_KMH  # noqa: F401 - re-exported
from . import map_tiles
from .map_tiles import ZONE_DEFAULT_M, ZONE_M, Frame, mercator
from .models import Network
from .network_generator import generate_network
from . import scenario, stats
from .stats import geh  # noqa: F401 - re-exported
from .vehicle import VEHICLE_CLASSES
from .tool_schemas import (CONTROLS, CYCLE_LANE_SIDES, EDIT_TOOLS, FOOTPATH_SIDES, OD_TOOLS, RUN_TOOLS,  # noqa: F401
                           SCENARIO_TOOLS, TOOLS, TYPE_TOOLS, UI_TOOLS, tool_specs)  # re-exported
from .tool_args import (ALL_IDS, ENDS_ARGS, INCIDENT_FIELDS, LIST_LIMIT, SATURATION_RANGE, ToolError,  # noqa: F401
                        _bool, _every, _ids, _int, _limited, _parsed_json, _road_from_other_end, _sides,
                        drop_strays, fold_incident, normalise, repair_ends, signature)  # re-exported
from .tool_offer import (CORE_TOOLS, FIND_TOOLS, SKILLS, STUDY_GROUPS, TOOL_GROUPS, contradicts,  # noqa: F401
                         expand_tools, skills_for, tool_groups, tools_for)  # re-exported
from .tool_reports import already_so, describe_call, describe_changes, explain_error, state_changes  # noqa: F401
from .road_tools import (READ_CHARS, READ_LINES, add_boundary_end, add_junction, add_road, add_slip_road,  # noqa: F401
                         build_edge, build_interchange, delete_objects, describe, edge_crossings, list_junctions,
                         list_roads, move_junctions, network_summary, read_source, set_end_flows,
                         set_junction_control, set_signal_timing, update_roads)  # re-exported
from .demand_plans import (LOGIT_DEFAULTS, _routes, calibration_plan, default_alternative, demand_plan,  # noqa: F401
                           institution_split, metro_cost, mode_plan, scale_plan)  # re-exported
from .people_tools import (add_bus_stop, add_crossing, add_metro_line, add_path, add_spawn_points,  # noqa: F401
                           deploy_institutions, list_institutions, pedestrian_summary, place_institution,
                           set_metro_demand, set_pedestrian_demand, update_bus_stop, update_institution,
                           update_metro_line)  # re-exported

# The background image's road pixels, supplied by the window (background.BackgroundPainter.road_mask), so this
# module stays free of Qt: (background, colour, tolerance, metres a pixel) -> (width, height, mask, affine, metres).
image_mask = None
_PREPARED: dict = {}  # slow inputs by what they depend on: OSM road data, an image's traced roads (made, value)


GRID_SPACING_M = (20.0, 2000.0)  # generate_grid's block length: shorter than a junction, longer than an arterial's


def generate_grid(network: Network, horizontal=None, vertical=None, spacing_m=None, driving_side=None,
                  speed_limit_kmh=None, lanes_per_direction=None, lane_changing=None, signal_method=None,
                  width_m=None, height_m=None, footpaths=None) -> dict:
    if width_m is not None or height_m is not None:  # a size in metres: the rows and columns that make it
        width, height = float(width_m or height_m), float(height_m or width_m)
        spacing = float(spacing_m or network.default_spacing)
        if max(width, height) / spacing > 19:  # at most 20 a side: wider blocks instead
            spacing = 10 * ceil(max(width, height) / 19 / 10)
        spacing_m = spacing
        vertical, horizontal = round(width / spacing) + 1, round(height / spacing) + 1
    if horizontal is None or vertical is None:
        raise ValueError("Give horizontal and vertical (rows and columns, 1 to 20), or width_m and height_m.")
    for name, count in (("horizontal", horizontal), ("vertical", vertical)):
        if _int(count) > 20:  # llama3.2 3B sent horizontal 1500, vertical 2000 for a 2000 x 1500 m city
            raise ValueError(f"{name} is a count of {'rows' if name == 'horizontal' else 'columns'} (1 to 20), not "
                             "metres: for a size in metres give width_m and height_m instead.")
    if spacing_m is not None and not GRID_SPACING_M[0] <= float(spacing_m) <= GRID_SPACING_M[1]:
        # the stress test's 50,000 x 50,000 m city of 3 x 3 junctions became 12.5 and 50 km blocks
        raise ToolError(f"spacing_m must be {GRID_SPACING_M[0]:g}-{GRID_SPACING_M[1]:g} m (city blocks are about "
                        "200-400 m). For a bigger area use more columns and rows, up to 20 each; tell the user the "
                        "size you can build.")
    fresh = generate_network(
        _int(horizontal), _int(vertical), float(spacing_m or network.default_spacing),
        driving_side or network.driving_side, float(speed_limit_kmh or network.default_speed_limit_kmh),
        _int(lanes_per_direction or network.default_lanes_per_direction),
        network.default_lane_changing if lane_changing is None else _bool(lane_changing),
        signal_method or network.default_signal_method)
    walkways = footpaths is not None and _bool(footpaths)
    for road in fresh.all_roads() if walkways else ():  # pedestrian walkways on both sides
        road.footpaths = list(FOOTPATH_SIDES["both"])
    keep_background(network, fresh)  # as the Generate button: the map, its place and work zone kept
    vars(network).update(vars(fresh))  # same object: the window keeps its reference
    return {"generated": f"{vertical} x {horizontal} grid ({vertical} columns, {horizontal} rows)",
            "junctions": len(network.nodes),
            "roads": len(network.roads), "boundary_ends": len(network.approach_nodes),
            "footpaths": "both sides of every road" if walkways else "none"}


def _centre(network: Network) -> tuple[float, float]:
    """The middle of the network's junctions and ends (the map's anchor when there are none)."""
    nodes = list(network.all_nodes())
    if not nodes:
        return network.background.anchor_x, network.background.anchor_y
    xs, ys = [node.x for node in nodes], [node.y for node in nodes]
    return (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2


def keep_background(old: Network, fresh: Network) -> None:
    """A new network (a grid generated, by the user or the assistant) keeps the backdrop the old one was drawn
    over; over the map, the place under the old network's centre goes under the new one's, its work zone with it."""
    background = old.background
    if background.kind == "map":
        (ox, oy), (nx, ny) = _centre(old), _centre(fresh)
        background.lat, background.lon = Frame(background.lat, background.lon, background.anchor_x, background.anchor_y,
                                               background.map_rotation_deg).to_wgs84(ox, oy)
        background.anchor_x, background.anchor_y = nx, ny
        if background.zone:
            background.zone[:2] = [background.zone[0] + nx - ox, background.zone[1] + ny - oy]
    fresh.background = background


def _cached(key, make):
    """make()'s value, kept by key; a failure (of any kind: then a ValueError) is kept a minute, so a check and its
    change don't ask twice."""
    found = _PREPARED.get(key)
    if found is not None:
        made, value = found
        if not isinstance(value, Exception):
            return value
        if time.time() - made < 60:
            raise ValueError(str(value))
    try:
        value = make()
    except Exception as exc:
        _PREPARED[key] = (time.time(), exc)
        raise
    while len(_PREPARED) >= 8:
        _PREPARED.pop(next(iter(_PREPARED)))
    _PREPARED[key] = (time.time(), value)
    return value


def _background_graph(network: Network, road_classes="all", width_m=None, height_m=None, road_colour=None,
                      colour_tolerance=40.0, resolution_m=2.0) -> tuple:
    """The background's roads as a background_network.Graph, and what they came from. Over the map: its work zone
    (as much of it as one read takes, 4,000 m a side), or `width_m` × `height_m` round the zone's centre; without a
    zone, 1,200 m square round the network's centre."""
    background = network.background
    if background.kind == "map":
        classes = background_network.LEVELS.get(str(road_classes))
        if classes is None:
            raise ValueError("road_classes must be 'main' or 'all'.")
        zone, given = background.zone, width_m is not None or height_m is not None
        if given:
            width, height = _sides(width_m, height_m)
        else:
            width, height = (min(max(side, 200.0), 4000.0) for side in zone[2:]) if zone else (1200.0, 1200.0)
        if not (200 <= width <= 4000 and 200 <= height <= 4000):
            raise ValueError("width_m and height_m must be 200 to 4000 m.")
        cx, cy = (zone[0] + zone[2] / 2, zone[1] + zone[3] / 2) if zone else _centre(network)
        if not zone:
            where = f"{width:g} x {height:g} m round the network's centre"
        elif (width, height) == (zone[2], zone[3]):
            where = "the work zone"
        else:
            where = f"{width:g} x {height:g} m round the work zone's centre"
            if not given:  # the zone itself is out of one read's reach
                where += f" (the zone is {zone[2]:g} x {zone[3]:g} m; a read takes 200 to 4000 m a side)"
        rect = (cx - width / 2, cy - height / 2, cx + width / 2, cy + height / 2)
        frame = Frame(background.lat, background.lon, background.anchor_x, background.anchor_y, background.map_rotation_deg)
        corners = [frame.to_wgs84(x, y) for x in (rect[0], rect[2]) for y in (rect[1], rect[3])]
        query = background_network.overpass_query(min(lat for lat, _ in corners), min(lon for _, lon in corners),
                                                  max(lat for lat, _ in corners), max(lon for _, lon in corners), classes)
        document = _cached(("osm", query), lambda: background_network.fetch_osm(query))
        graph = background_network.osm_graph(document, lambda lat, lon: frame.to_scene(*mercator(lat, lon)), rect)
        copy = f" (a copy kept from {document['cached_at']}: {document.get('cached_why', '')})"             if document.get("cached_at") else ""
        return graph, f"OpenStreetMap road data, {road_classes} roads, {where}{copy}"
    if background.kind == "image":
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", str(road_colour or "")):
            raise ValueError("Say the colour of the roads on the image: road_colour like '#ffffff'. Ask the user if "
                             "you don't know it.")
        tolerance, resolution = float(colour_tolerance), float(resolution_m)
        if not (0 <= tolerance <= 255 and 0.5 <= resolution <= 20):
            raise ValueError("colour_tolerance must be 0-255 and resolution_m 0.5-20 m.")
        if image_mask is None:
            raise ValueError("The background image can't be read here.")
        key = ("image", background.image, background.page, background.width_m, background.x_m, background.y_m,
               background.rotation_deg, str(road_colour).lower(), tolerance, resolution)

        def trace():
            width, height, mask, (a, b, c, d, e, f), metres = image_mask(background, str(road_colour), tolerance, resolution)
            return background_network.raster_graph(width, height, mask,
                                                   lambda px, py: (a * px + c * py + e, b * px + d * py + f), metres)

        return _cached(key, trace), f"the image's {road_colour} roads, traced at {resolution:g} m a pixel"
    raise ValueError("The background is a plain colour: call place_map with the place first (or the user opens "
                     "an image: Create Network > Own Network), then network_from_background.")


def _lane_range(key: str, value) -> tuple[int, int] | None:
    """network_from_background's main_lanes or local_lanes: [fewest, most] lanes a direction, 1-6 (one number, or
    text like "2-3", reads the same)."""
    if value is None:
        return None
    numbers = [float(n) for n in re.findall(r"\d+(?:\.\d+)?", json.dumps(value))]
    if not 1 <= len(numbers) <= 2 or any(n != int(n) for n in numbers) or not 1 <= numbers[0] <= numbers[-1] <= 6:
        raise ValueError(f"{key} must be [fewest, most] lanes per direction, whole numbers 1 to 6, e.g. [2, 3].")
    return int(numbers[0]), int(numbers[-1])


def network_from_background(network: Network, road_classes="all", width_m=None, height_m=None, road_colour=None,
                            colour_tolerance=40.0, resolution_m=2.0, footpaths="none", main_lanes=None,
                            local_lanes=None) -> dict:
    lanes = {kind: held for kind, held in (("main", _lane_range("main_lanes", main_lanes)),
                                           ("local", _lane_range("local_lanes", local_lanes))) if held}
    graph, source = _background_graph(network, road_classes, width_m, height_m, road_colour, colour_tolerance,
                                      resolution_m)
    fresh = background_network.build(network, graph, lanes)
    walking = background_network.add_walking(fresh, graph, str(footpaths))  # what the map has for walkers
    kept = {key: item for key, item in network.institutions.items()
            if footprint_problem(fresh, item.x, item.y, item.area_m2) is None}
    dropped = sorted(set(network.institutions) - set(kept))
    fresh.institutions = kept
    vars(network).update(vars(fresh))  # same object: the window keeps its reference
    controls = [node.control for node in network.nodes.values()]
    result = {"source": source, "junctions": len(network.nodes), "signalised": controls.count("signalised"),
              "roundabouts": controls.count("roundabout"), "roads": len(network.roads),
              "boundary_ends": sorted(network.approach_nodes)[:60]}
    if graph.lanes_note:
        result["lanes"] = graph.lanes_note
    roads = list(network.all_roads())
    result["roads_by_lanes_per_direction"] = dict(sorted(Counter(road.lanes_per_direction for road in roads).items()))
    if lanes:  # said back, so a model doesn't "fix" the lanes with update_roads after (qwen3.5 4B did)
        result["lanes_held"] = {f"{kind}_lanes": list(held) for kind, held in lanes.items()}
        result["lanes_note"] = "Every road's lanes per direction are within these ranges: nothing more to change."
    if graph.unroutable:  # one-way traps, parts cut off at the edge: every OD pair left has a route
        result["streets_left_out"] = graph.unroutable
        result["streets_left_out_why"] = ("no car could drive them between the area's ends (a one-way street into a "
                                          "dead end, a part cut off at the area's edge), so they were left out and "
                                          "every OD pair has a route")
    result["pedestrians"] = {"roads_with_footpaths": sum(bool(road.footpaths) for road in roads),
                             "roads_with_cycle_lanes": sum(bool(road.cycle_lanes) for road in roads), **walking,
                             **({"crossings_not_taken": graph.walking_skipped} if graph.walking_skipped else {})}
    if graph.skipped:  # a pedestrian crossing's signal, a flashing one...: its junction gives way instead
        result["signals_not_junction_signals"] = dict(sorted(graph.skipped.items()))
        result["signals_note"] = ("Signals of these kinds don't signalise a junction, so the junctions near them "
                                  "stay priority junctions. Any junction can still be made signalised (the Edit "
                                  "tab's Control, or set_junction_control) and given any timing method.")
    if dropped:
        result["institutions_removed"] = dropped  # a new road runs where they stood
    if network.background.kind == "image" and not network.background.reference:
        result["note"] = "The image's scale was never set (Set Scale), so its roads are only as true as its width."
    return result


PREPARE_READS = {"network_from_background"}  # the tools whose prepare reads the network (the others get None)


def prepare(network: Network | None, name: str, args) -> None:
    """A tool call's slow or random part, done before its check, off the UI thread (ai_mode runs it on the
    model's thread, with a copy of the network the UI thread took, for PREPARE_READS): a random layout gets its seed,
    so the check and the change agree; the background's roads are fetched or traced into the cache, and a place
    looked up, with the arguments as run_tool reads them (normalise), so the tool finds them there. Problems are
    left for the tool itself to report (_cached keeps one)."""
    if name == "deploy_institutions" and isinstance(args, dict) and args.get("seed") is None:
        args["seed"] = random.randrange(1_000_000)
    if name not in ("network_from_background", "place_map"):
        return
    try:
        args = normalise(name, args)
        if name == "network_from_background":
            _background_graph(network, **{key: value for key, value in args.items()
                                          if key not in ("footpaths", "main_lanes", "local_lanes")})
        elif args.get("place"):
            _places(args["place"])
    except Exception:  # noqa: BLE001 - never on the model's thread: the tool reports it
        pass


place_download = None  # stands in for the network in tests: url -> bytes


def _places(query: str) -> list:
    """Places by name from Nominatim (its policy: one request a second from the whole app, map_tiles.search_turn;
    the app's User-Agent), cached."""
    query = " ".join(str(query).split())
    if not query:
        raise ValueError("Give a place name, or lat and lon.")

    def fetch():
        map_tiles.search_turn()
        url = map_tiles.SEARCH_URL.format(limit=map_tiles.SEARCH_LIMIT, query=__import__("urllib.parse").parse.quote(query))
        try:
            found = json.loads((place_download or map_tiles._download)(url))
        except OSError as exc:
            raise ValueError(f"Couldn't reach the place search ({exc}).") from exc
        places = [(str(p["display_name"]), float(p["lat"]), float(p["lon"])) for p in found if isinstance(p, dict)]
        return [p for p in places if abs(p[1]) <= map_tiles.MAX_LATITUDE]

    return _cached(("place", query.lower()), fetch)


def place_map(network: Network, place=None, lat=None, lon=None, rotation_deg=None, width_m=None, height_m=None) -> dict:
    """The map under the network, the place at its centre, and its work zone round it, as the user's Confirm makes
    one: `width_m` × `height_m` (ZONE_M; one alone, a square; neither, ZONE_DEFAULT_M square)."""
    given = width_m is not None or height_m is not None
    width, height = _sides(width_m, height_m) if given else (ZONE_DEFAULT_M, ZONE_DEFAULT_M)
    if not (ZONE_M[0] <= width <= ZONE_M[1] and ZONE_M[0] <= height <= ZONE_M[1]):
        raise ValueError(f"width_m and height_m (the work zone) must be {ZONE_M[0]:g} to {ZONE_M[1]:g} m.")
    if place:
        found = _places(place)
        if not found:
            raise ValueError(f"Nothing found for {place!r}: try a fuller name, or give lat and lon.")
        name, lat, lon = found[0]
    elif lat is None or lon is None:
        raise ValueError("Give a place, or lat and lon.")
    else:
        name = f"{float(lat):.6f}, {float(lon):.6f}"
    background = network.background
    background.kind, background.lat, background.lon = "map", float(lat), float(lon)
    background.anchor_x, background.anchor_y = cx, cy = _centre(network)
    background.zone = [cx - width / 2, cy - height / 2, width, height]
    if rotation_deg is not None:
        background.map_rotation_deg = float(rotation_deg)
    return {"placed": name, "lat": background.lat, "lon": background.lon, "work_zone_m": [width, height],
            "note": f"That place is under the network's centre, with a {width:g} x {height:g} m work zone round it (the "
                    "view keeps to it). Next, to use its roads: network_from_background with no arguments (it reads "
                    "this map and builds its work zone; it takes no place)."}


def cluster_conditions(network: Network, days, k=None, seed=1) -> dict:  # noqa: ARG001 - the tools' signature
    """Travel conditions from observed days (FHWA Toolbox III 2019, p26–31): attributes normalised 0–1 (eq. 1),
    k-means, k by the within/between variance ratio unless given; per cluster its size, weight, centroid in the
    attributes' units and its representative day (the member nearest the centroid)."""
    days = _parsed_json(days) if isinstance(days, str) else days
    if not isinstance(days, list) or len(days) < 4 or not all(isinstance(d, dict) and isinstance(d.get("values"), dict)
                                                              for d in days):
        raise ValueError("Give 4 or more days like {\"label\": \"Mon 3 Mar\", \"values\": {\"volume\": 5200, \"rain_mm\": 2}}.")
    features = sorted({key for day in days for key in day["values"]})
    if any(not isinstance(day["values"].get(f), (int, float)) for day in days for f in features):
        raise ValueError(f"Every day needs a number for each of {', '.join(features)}.")
    raw = [[float(day["values"][f]) for f in features] for day in days]
    low, high = [min(c) for c in zip(*raw)], [max(c) for c in zip(*raw)]
    points = [[(x - a) / (b - a) if b > a else 0.0 for x, a, b in zip(row, low, high)] for row in raw]
    seed = _int(seed)
    if k is None:
        k = stats.choose_k(points, seed)
    k = _int(k)
    if not 2 <= k < len(days):
        raise ValueError(f"k must be 2 to {len(days) - 1}.")
    labels, centroids, _ = stats.kmeans(points, k, seed)
    clusters = []
    for c in range(k):
        members = [i for i, label in enumerate(labels) if label == c]
        if not members:
            continue
        nearest = min(members, key=lambda i: sum((a - b) ** 2 for a, b in zip(points[i], centroids[c])))
        clusters.append({"size": len(members), "weight": round(len(members) / len(days), 3),
                         "centroid": {f: round(sum(raw[i][j] for i in members) / len(members), 2) for j, f in enumerate(features)},
                         "representative_day": str(days[nearest].get("label", nearest)),
                         "days": [str(days[i].get("label", i)) for i in members][:20]})
    return {"k": len(clusters), "attributes": features, "clusters": sorted(clusters, key=lambda c: -c["size"]),
            "note": "Test each cluster's representative day as a scenario (e.g. demand_scale from its volume against the "
                    "baseline's) and weight its results by weight (FHWA Toolbox III, p91)."}


_FUNCTIONS = {function.__name__: function for function in (
    set_signal_timing, place_map, add_bus_stop, update_bus_stop, update_institution,
    add_crossing, add_path, set_pedestrian_demand, pedestrian_summary, add_spawn_points,
    add_metro_line, update_metro_line, set_metro_demand,
    network_summary, list_roads, list_junctions, describe, analyse_network, read_source, list_institutions,
    set_junction_control, update_roads, add_junction, add_road, add_boundary_end, move_junctions,
    delete_objects, build_interchange, add_slip_road, set_end_flows, deploy_institutions, network_from_background, generate_grid,
    network_indices, cluster_conditions, place_institution)}


def run_tool(network: Network, name: str, args: dict) -> dict:
    """Run a tool; an edit that fails, or would change nothing, leaves the network exactly as it was.
    ToolError says why. A network edit's result lists what it changed (`changed`)."""
    function = _FUNCTIONS.get(name)
    if function is None:
        raise ToolError(f"There is no tool called {name!r}.")
    args = _every(network, name, normalise(name, args))
    flipped: dict = {}  # road ids named from the other end ("2,2-2,3" for 2,3-2,2), and the road's own id
    if name == "update_roads":  # its two junctions for one road (llama3.2 3B sent roads ["2,2", "3,2"] for 2,2-3,2)
        try:
            ends = _ids(args.get("roads") or [])
        except ValueError:  # objects in the list: the tool refuses them below, saying how
            ends = []
        road = next((f"{a}-{b}" for a, b in (ends, ends[::-1]) if f"{a}-{b}" in network.roads), None) \
            if len(ends) == 2 and all(end in network.nodes for end in ends) else None
        if road:
            flipped[f"{ends[0]}, {ends[1]}"] = road
            args = {**args, "roads": [road]}
    while True:
        before = deepcopy(network) if name in EDIT_TOOLS else None
        try:
            result = function(network, **args)
            if before is not None:
                network.validate()
                if network == before:
                    raise ToolError(f"Nothing would change ({describe_call(name, args)}): it is like that already.")
                if isinstance(result, dict):
                    result.setdefault("changed", describe_changes(before, network, limit=10))
            if flipped and isinstance(result, dict):
                result["read_as"] = {given: own for given, own in flipped.items()}  # the model learns the real ids
            return result
        except (ToolError, ValueError, KeyError, TypeError) as exc:
            if before is not None:
                vars(network).update(vars(before))
            if isinstance(exc, KeyError):
                given = str(exc.args[0])
                own = _road_from_other_end(network, given)
                if own and given not in flipped:  # the same road, its ends named the other way round: run again
                    flipped[given] = own
                    args = _swapped(args, given, own)
                    continue
                every = next((f" For every one, send {key} \"all\"." for tool, key in ALL_IDS if tool == name), "")
                raise ToolError(f"Unknown id {given!r}.{every}") from exc  # qwen3.5 4B sent junction names as roads
            if isinstance(exc, TypeError):
                raise ToolError(f"Bad arguments for {name}: {exc}. {signature(name)}.") from exc
            raise ToolError(str(exc)) from exc
        except Exception as exc:  # a fault in the tool itself: the network as it was all the same, and the model told
            if before is not None:
                vars(network).update(vars(before))
            raise ToolError(f"{name} failed: {type(exc).__name__}: {exc}. Nothing was changed.") from exc


def _swapped(value, old: str, new: str):
    """`value` (arguments: text, lists, objects) with every `old` string, whole, replaced by `new`."""
    if isinstance(value, str):
        return new if value == old else value
    if isinstance(value, list):
        return [_swapped(item, old, new) for item in value]
    if isinstance(value, dict):
        return {key: _swapped(item, old, new) for key, item in value.items()}
    return value

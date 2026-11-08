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
import heapq
import json
import random
import re
import time
from collections import Counter, defaultdict
from copy import deepcopy
from math import ceil, cos, exp, hypot, isfinite, log, radians, sin, sqrt
from pathlib import Path

from . import background_network
from .daily_profile import ARRIVAL_PATTERNS, DailyProfile, profile_record
from .demand import profile_from
from .design_checks import analyse_network, network_indices, roads_at, route_lengths, tightest
from .geometry import distance_to_segment, polyline_length
from .institutions import KINDS, WALK, catchment, door, footprint_problem, hourly_people, links, place, shares
from .models import (CROSSING_KINDS, MAX_METRO_DEMAND, MAX_WALK_DEMAND, METRO_MIN_SPACING_M, METRO_SERVICE,
                     MetroCrossing)
from .metro import METRO, METRO_WALK_M, WALK_KMH, door as metro_door, places as metro_places, ride as metro_ride
from .results import clock_h
from .interchanges import (build_diamond, build_slip_road, build_trumpet, curve_speed_kmh, fit_slip_roads, guess_stem,
                           pair_legs)
from . import map_tiles
from .map_tiles import ZONE_DEFAULT_M, ZONE_M, Frame, mercator
from .models import BUS_STOP_DIRECTIONS, MAX_BOARDING_PER_H, SIGNAL_METHODS, Network, Road
from .network_generator import generate_network
from .roundabouts import MAX_ENTRY_LANES, design_roundabout
from .lane_graph import CYCLE_LANE_TIME_FACTOR, Graph
from . import scenario, stats
from .stats import geh
from .vehicle import VEHICLE_CLASSES
from .tool_schemas import (CONTROLS, CYCLE_LANE_SIDES, EDIT_TOOLS, FOOTPATH_SIDES, OD_TOOLS, RUN_TOOLS,  # noqa: F401
                           SCENARIO_TOOLS, TOOLS, TYPE_TOOLS, UI_TOOLS, tool_specs)  # re-exported
from .tool_args import (ALL_IDS, ENDS_ARGS, INCIDENT_FIELDS, LIST_LIMIT, SATURATION_RANGE, ToolError,  # noqa: F401
                        _bool, _every, _ids, _int, _limited, _parsed_json, _road_from_other_end, _sides,
                        drop_strays, fold_incident, normalise, repair_ends, signature)  # re-exported
from .tool_offer import (CORE_TOOLS, FIND_TOOLS, SKILLS, STUDY_GROUPS, TOOL_GROUPS, contradicts,  # noqa: F401
                         expand_tools, skills_for, tool_groups, tools_for)  # re-exported
from .tool_reports import already_so, describe_call, describe_changes, explain_error  # noqa: F401

ROOT = Path(__file__).resolve().parents[2]
READ_LINES = 150  # lines per read_source call at most...
READ_CHARS = 6000  # ...and characters: sources.md's table rows are long (150 of its lines were 63k characters)
FIND_HITS = 25  # matching lines a read_source find returns
# The background image's road pixels, supplied by the window (background.BackgroundPainter.road_mask), so this
# module stays free of Qt: (background, colour, tolerance, metres a pixel) -> (width, height, mask, affine, metres).
image_mask = None
_PREPARED: dict = {}  # slow inputs by what they depend on: OSM road data, an image's traced roads (made, value)


# --- Helpers -----------------------------------------------------------------------------

def _road_info(network: Network, road: Road) -> dict:
    info = {"id": road.id, "from": road.from_node, "to": road.to_node, "length_m": round(road.length_m, 1),
            "lanes_per_direction": road.lanes_per_direction, "direction": road.direction,
            "speed_limit_kmh": road.speed_limit_kmh, "lane_changing": road.lane_changing}
    if road.road_type != "urban":
        info["road_type"] = road.road_type
    if road.overpass:
        info["overpass"] = True
    if road.single_lane:
        info["single_lane"] = True
    if road.pockets:
        info["turn_pockets"] = road.pockets
    if road.cycle_lanes:
        info["cycle_lanes_to"] = road.cycle_lanes  # bikes only, for the traffic arriving at these ends
    if road.footpaths:
        info["footpaths"] = road.footpaths  # its sides, from from_node to to_node
    crossings = [{"id": c.id, "kind": c.kind, "at_m": round(c.distance_m)} for c in network.pedestrian_crossings.values()
                 if c.road == road.id]
    if crossings:
        info["pedestrian_crossings"] = crossings
    if road.id in network.approach_roads:
        info["boundary_road"] = True
    if tightest(road):
        info["tightest_radius_m"] = round(1 / tightest(road), 1)
    return info


def _design(network: Network, node_id: str) -> dict:
    design = design_roundabout(network, node_id)
    return {"category": design.category, "entry_lanes": design.entry_lanes,
            "design_speed_kmh": design.design_speed_kmh,
            "inscribed_diameter_m": round(design.inscribed_diameter_m, 1),
            "circulatory_width_m": round(design.circulatory_width_m, 1),
            "island_diameter_m": round(design.island_diameter_m, 1),
            "circulating_speed_kmh": round(design.circulating_speed_kmh),
            "source": "NCHRP Report 672 (roundabouts.py)"}


def edge_crossings(network: Network, start: str, target: str | None, x: float = 0.0, y: float = 0.0) -> list:
    """Roads a planned edge would cross (see Network.crossings); ValueError if it can't be built."""
    if start not in network.nodes:
        raise ValueError(f"New roads start from a junction; {start} isn't one.")
    begin = network.nodes[start]
    if target is None:
        if hypot(x - begin.x, y - begin.y) < 1.0:
            raise ValueError("Place the end away from its intersection.")
        finish = (x, y)
    else:
        problem = network.connection_problem(start, target)
        if problem:
            raise ValueError(problem)
        finish = (network.node(target).x, network.node(target).y)
    return network.crossings([(begin.x, begin.y), finish], {start} | ({target} if target else set()))


def build_edge(network: Network, start: str, target: str | None, x: float = 0.0, y: float = 0.0,
               crossing: str | None = None) -> tuple[Road, list[str]]:
    """Add a road from junction `start` to junction `target`, or to a new boundary end at (x, y).

    Roads it would cross: crossing="roundabout" puts a roundabout junction at each (both
    roads split there), "overpass" bridges over them; anything else refuses. Everything is
    checked before any road changes. Returns the new road (the last one, when roundabouts
    split it) and the roundabouts added.
    """
    crossings = edge_crossings(network, start, target, x, y)
    if crossings and crossing not in ("roundabout", "overpass"):
        roads = sorted({road_id for *_, road_id in crossings})
        raise ValueError(f"The new road would cross {', '.join(roads)}. Choose crossings='roundabout' "
                         "(a roundabout at each crossing) or 'overpass' (bridge over them).")
    if crossings and crossing == "roundabout":  # each roundabout's widest entry: the new road's or a crossed road's
        lanes = max(network.default_lanes_per_direction, *(network.road(road_id).lanes_each_way for *_, road_id in crossings))
        if lanes > MAX_ENTRY_LANES:
            raise ValueError(f"A roundabout with {lanes} lanes entering from one road is more than NCHRP Report 672 "
                             f"designs (up to {MAX_ENTRY_LANES}). Choose crossings='overpass'.")
    junctions = []
    if crossings and crossing == "roundabout":
        for _, cross_x, cross_y, road_id in crossings:
            junction = network.add_node(cross_x, cross_y)
            junction.control = "roundabout"
            network.split_road(road_id, junction.id)
            junctions.append(junction.id)
    chain = [start, *junctions]
    for a, b in zip(chain, chain[1:]):
        network.add_road(a, b)
    road = network.add_end(chain[-1], x, y) if target is None else network.add_road(chain[-1], target)
    road.overpass = bool(crossings) and crossing == "overpass"
    return road, junctions


# --- Read tools --------------------------------------------------------------------------

def network_summary(network: Network) -> dict:
    points = [(node.x, node.y) for node in network.all_nodes()]
    ends = []
    for end in network.approach_nodes.values():
        road = next(iter(roads_at(network, end.id)), None)
        traffic = "in and out" if road is None or road.direction == "two-way" else \
            "in" if road.from_node == end.id else "out"
        ends.append({"id": end.id, "traffic": traffic, "supply_vph": end.supply_vph, "demand_vph": end.demand_vph})
    return {
        "coordinates": "metres; x to the right, y downward; grid junction '1,1' is bottom-left",
        "driving_side": network.driving_side,
        "grid": f"{network.horizontal_corridors} horizontal x {network.vertical_corridors} vertical corridors, "
                f"{network.default_spacing:g} m apart",
        "defaults": {"speed_limit_kmh": network.default_speed_limit_kmh,
                     "lanes_per_direction": network.default_lanes_per_direction,
                     "lane_changing": network.default_lane_changing,
                     "signal_method": network.default_signal_method},
        "junctions": {control: [node.id for node in network.nodes.values() if node.control == control]
                      for control in CONTROLS},
        "roads": len(network.roads),
        "one_way_roads": sum(road.direction == "one-way" for road in network.all_roads()),
        "curved_roads": sum(bool(road.shape) for road in network.all_roads()),
        "boundary_ends": ends,
        "institutions": len(network.institutions),
        "extent_m": {"x": [round(min(x for x, _ in points)), round(max(x for x, _ in points))],
                     "y": [round(min(y for _, y in points)), round(max(y for _, y in points))]} if points else None,
        "background": _background_summary(network.background),
    }


def _background_summary(background) -> str:
    """What the network is drawn over, as network_summary gives it: the map's place and its work zone (the area
    the view keeps to, network_from_background's), an image, or a colour."""
    if background.kind == "image":
        return f"the image {Path(background.image).name}, {background.width_m:g} m wide"
    if background.kind != "map":
        return "a plain colour: no map yet (place_map puts one under it)"
    if not background.zone:
        return f"OpenStreetMap at {background.lat:.5f}, {background.lon:.5f}, no work zone (place_map sets one)"
    x, y, width, height = background.zone
    return (f"OpenStreetMap at {background.lat:.5f}, {background.lon:.5f}; work zone {width:.0f} x {height:.0f} m, "
            f"x {x:.0f} to {x + width:.0f}, y {y:.0f} to {y + height:.0f}")


def list_roads(network: Network, node: str = "", direction: str = "") -> dict:
    roads = [road for road in network.all_roads()
             if (not node or node in (road.from_node, road.to_node)) and (not direction or road.direction == direction)]
    found = _limited([_road_info(network, road) for road in roads], "roads")
    if node or direction:  # filtered: how many in all
        found["filter"] = f"{len(roads)} of the network's {len(network.roads) + len(network.approach_roads)} roads"
    return found


def list_junctions(network: Network, control: str = "") -> dict:
    rows = [{"id": node.id, "x": round(node.x, 1), "y": round(node.y, 1), "control": node.control,
             "roads": len(roads_at(network, node.id))}
            for node in network.nodes.values() if not control or node.control == control]
    found = _limited(rows, "junctions")
    if control:  # filtered: how many in all, so "0 uncontrolled" isn't read as "0 junctions" (llama3.2 3B)
        found["filter"] = f"control {control}: {len(rows)} of the network's {len(network.nodes)} junctions"
    return found


def describe(network: Network, id: str) -> dict:  # noqa: A002 - the model's argument name
    if id in network.approach_nodes:  # a boundary end and its road share the name
        end, road = network.approach_nodes[id], network.approach_roads.get(id)
        return {"id": id, "kind": "boundary end", "x": round(end.x, 1), "y": round(end.y, 1),
                "supply_vph": end.supply_vph, "demand_vph": end.demand_vph,
                "road": _road_info(network, road) if road else None}
    if id in network.roads or id in network.approach_roads:
        road = network.road(id)
        info = _road_info(network, road)
        info["curved"] = bool(road.shape)
        if tightest(road):
            info["safe_curve_speed_kmh"] = curve_speed_kmh(1 / tightest(road))
        crossed = sorted({road_id for *_, road_id in
                          network.crossings(network.road_points(road), {road.from_node, road.to_node})})
        if crossed:
            info["crosses_without_junction"] = crossed
            info["note"] = "Grade separated: " + ("this road bridges over them." if road.overpass
                                                  else "roads marked overpass pass over.")
        return info
    node = network.nodes[id]
    roads = roads_at(network, id)
    info = {"id": id, "kind": "junction", "x": round(node.x, 1), "y": round(node.y, 1), "control": node.control,
            "grid_position": "off the grid" if node.row is None else network.node_label(node),
            "roads": [{"id": road.id, "to": road.to_node if road.from_node == id else road.from_node,
                       "traffic": "two-way" if road.direction == "two-way" else
                       ("one-way out" if road.from_node == id else "one-way in"),
                       "lanes_per_direction": road.lanes_per_direction,
                       "speed_limit_kmh": road.speed_limit_kmh} for road in roads]}
    if node.control == "roundabout":
        info["roundabout_design"] = _design(network, id)
    if node.control == "signalised":
        info["signal_settings"] = {"phases": node.signal_phases, "method": node.signal_method,
                                   "green_times_s": node.green_times_s, **_manual(network, id),
                                   "cycle_limits_s": [node.cycle_min_s, node.cycle_max_s],
                                   "target_saturation": node.target_saturation, "stop_weight_s": node.stop_weight_s,
                                   "note": "Each Run times it by its method (the greens entered are used by "
                                           "'fixed' only; the cycle limits by the coordinated methods, the target "
                                           "saturation by SCATS and SCOOT, the stop weight by TRANSYT and SCOOT). "
                                           "read_traffic's signal_plans give the plan a run is using."}
    return info


def read_source(network: Network, path: str = "", start_line: int = 1, end_line: int = 0, find: str = "") -> dict:
    del network  # the source, not the network
    found = {*ROOT.glob("*.md"), *ROOT.glob("*.py"), *(ROOT / "src" / "traffic_simulator").glob("*.py"),
             *(ROOT / "tests").glob("*.py"),
             *(ROOT / "docs" / "architecture").glob("*.md")}
    files = sorted(file.relative_to(ROOT).as_posix() for file in found)
    target = (ROOT / path).resolve() if path else None
    if target is not None and target not in found:  # also refuses anything outside the project, e.g. "../secrets"
        raise ToolError(f"Can't read {path!r}. Readable files: {', '.join(files)}")
    if find:  # the lines holding every word, so a question reads a few lines rather than whole pages
        words = str(find).lower().split()
        hits = [{"path": name, "line": number, "text": line.strip()[:200]}
                for name in ([target.relative_to(ROOT).as_posix()] if target else files)
                for number, line in enumerate((ROOT / name).read_text(encoding="utf-8").splitlines(), 1)
                if all(word in line.lower() for word in words)]
        return {"find": find, "hits": hits[:FIND_HITS], "more": max(len(hits) - FIND_HITS, 0),
                "next": "read_source with that path, start_line a few lines before the hit and end_line ~30 after"
                if hits else "No line has all those words: try fewer or other words."}
    if not path:
        return {"files": files}
    lines = target.read_text(encoding="utf-8").splitlines()
    start = max(1, _int(start_line or 1))
    end = min(len(lines), _int(end_line or 0) or len(lines), start + READ_LINES - 1)
    shown, used = [], 0
    for number, line in enumerate(lines[start - 1:end], start):  # whole lines up to READ_CHARS; a longer one is cut
        text = f"{number}: {line}"
        if shown and used + len(text) > READ_CHARS:
            break
        shown.append(text if len(text) <= READ_CHARS else text[:READ_CHARS] + " ... (line cut)")
        used += len(shown[-1]) + 1
    end = start + len(shown) - 1
    result = {"path": target.relative_to(ROOT).as_posix(), "lines": f"{start}-{end} of {len(lines)}",
              "text": "\n".join(shown)}
    if end < len(lines) and (not end_line or end < _int(end_line)):
        result["next_start_line"] = end + 1
    return result


# --- Edit tools --------------------------------------------------------------------------

def set_junction_control(network: Network, junctions, control: str) -> dict:
    ids = _ids(junctions)
    if control not in CONTROLS:
        raise ValueError(f"control must be one of {', '.join(CONTROLS)}.")
    for node_id in ids:
        network.nodes[_junction(network, node_id)].control = control
    result = {"junctions": ids, "control": control}  # what changed, before -> after: run_tool's `changed`
    moved = [slip for node_id in ids for slip in fit_slip_roads(network, node_id)]  # clear of a roundabout's circle
    if moved:
        result["slip_roads_moved_out"] = moved
    if control == "roundabout":
        result["designs"] = {node_id: _design(network, node_id) for node_id in ids}
    return result


def update_roads(network: Network, roads, lanes_per_direction=None, speed_limit_kmh=None, direction=None,
                 lane_changing=None, road_type=None, length_m=None, add_lanes=None, pocket_junction=None,
                 pocket_lanes=None, pocket_length_m=None, pocket_side=None, footpaths=None, cycle_lanes=None) -> dict:
    ids = _ids(roads)
    if footpaths is not None and footpaths not in FOOTPATH_SIDES:
        raise ValueError(f"footpaths must be {', '.join(map(repr, FOOTPATH_SIDES))}.")
    if cycle_lanes is not None and cycle_lanes not in CYCLE_LANE_SIDES:
        raise ValueError(f"cycle_lanes must be {', '.join(map(repr, CYCLE_LANE_SIDES))}.")
    for road_id in ids:
        network.road(road_id)  # KeyError for an unknown road, before anything changes
    if add_lanes is not None and lanes_per_direction is not None:
        raise ValueError("Give add_lanes or lanes_per_direction, not both.")
    if not pocket_junction:
        if (pocket_lanes is not None and _int(pocket_lanes) > 0) or pocket_length_m is not None:
            raise ValueError("Name pocket_junction: the end of the road the turn pocket is on the approach to.")
        pocket_lanes = pocket_side = None  # 0 lanes or a side with no pocket named: a small model's filler
    if direction not in (None, "two-way", "one-way", "reverse"):
        raise ValueError("direction must be 'two-way', 'one-way' or 'reverse'.")
    if speed_limit_kmh is not None and not 5 <= float(speed_limit_kmh) <= 130:  # as the incident zones' limits
        raise ValueError("speed_limit_kmh must be 5 to 130.")
    for road_id in ids:
        if length_m is not None:
            network.set_road_length(road_id, float(length_m))
        road = network.road(road_id)
        if add_lanes is not None:  # each road its own count plus this ("add a lane")
            lanes_per_direction = road.lanes_per_direction + _int(add_lanes)
        if lanes_per_direction is not None:
            if not 1 <= _int(lanes_per_direction) <= 12:
                raise ValueError("lanes_per_direction must be 1 to 12.")
            road.lanes_per_direction = _int(lanes_per_direction)
        if speed_limit_kmh is not None:
            road.speed_limit_kmh = float(speed_limit_kmh)
        if lane_changing is not None:
            road.lane_changing = _bool(lane_changing)
        if road_type:
            road.road_type = str(road_type)
        if direction == "reverse":
            network.reverse_road(road_id)  # a curve's points and curvature flip too
        if direction:
            road.direction = "two-way" if direction == "two-way" else "one-way"
            road.pockets = {end: pocket for end, pocket in road.pockets.items() if end in road.arrival_ends()}
        if footpaths is not None:
            road.footpaths = list(FOOTPATH_SIDES[footpaths])
        if cycle_lanes is not None:
            ends = {"both": list(road.arrival_ends()), "forward": [road.to_node], "backward": [road.from_node],
                    "none": []}[cycle_lanes]
            if not set(ends) <= set(road.arrival_ends()):
                raise ValueError(f"{road.id} is one-way from {road.from_node} to {road.to_node}: its cycle lane can "
                                 "only run forward.")
            road.cycle_lanes = ends
        road.cycle_lanes = [end for end in road.cycle_lanes if end in road.arrival_ends()]
        road.single_lane = road.single_lane and road.direction == "one-way" and road.lanes_per_direction == 1
        if pocket_junction:  # Network.validate (run_tool) checks where it is and that it fits
            pocket = dict(road.pockets.get(str(pocket_junction), {"lanes": 1, "length_m": 50.0, "side": "kerb"}))
            pocket.update({key: cast(value) for key, cast, value in (("lanes", _int, pocket_lanes),
                           ("length_m", float, pocket_length_m), ("side", str, pocket_side)) if value is not None})
            road.pockets = {**road.pockets, str(pocket_junction): pocket}
            if not pocket["lanes"]:
                del road.pockets[str(pocket_junction)]
    return {"updated": [_road_info(network, network.road(road_id)) for road_id in ids]}


def add_junction(network: Network, x: float, y: float) -> dict:
    node = network.add_node(float(x), float(y))
    return {"added": node.id, "x": node.x, "y": node.y}


def _junction(network: Network, node_id: str) -> str:
    """`node_id` if it is a junction; a boundary end refused saying so; an unknown id a KeyError (run_tool says
    "Unknown id", explain_error the nearest ids)."""
    node_id = str(node_id)
    if node_id in network.nodes:
        return node_id
    if node_id in network.approach_nodes:
        raise ValueError(f"{node_id} is a boundary end, not a junction: junction ids are like '2,2' or 'N12' "
                         "(list_junctions); a road to the edge is add_boundary_end.")
    raise KeyError(node_id)


def add_road(network: Network, from_junction: str, to_junction: str, crossings: str | None = None) -> dict:
    _junction(network, from_junction), _junction(network, to_junction)
    road, roundabouts = build_edge(network, str(from_junction), str(to_junction), crossing=crossings)
    return {"added": road.id, "roundabouts_added": roundabouts, "overpass": road.overpass}


def add_boundary_end(network: Network, from_junction: str, x: float, y: float, crossings: str | None = None) -> dict:
    road, roundabouts = build_edge(network, str(from_junction), None, float(x), float(y), crossings)
    return {"added_end": road.id, "roundabouts_added": roundabouts, "overpass": road.overpass}


def move_junctions(network: Network, junctions, dx: float, dy: float) -> dict:
    ids = _ids(junctions)
    for node_id in ids:
        network.node(node_id)
    if not network.move_nodes(ids, float(dx), float(dy)):
        raise ValueError("That move would squash a road below 1 m; nothing moved.")
    return {"moved": {node_id: [round(network.node(node_id).x, 1), round(network.node(node_id).y, 1)]
                      for node_id in ids}}


def delete_objects(network: Network, ids) -> dict:
    ids = _ids(ids)
    roads = {**network.roads, **network.approach_roads}
    nodes = {**network.nodes, **network.approach_nodes}
    unknown = [item for item in ids if item not in roads and item not in nodes and item not in network.bus_stops
               and item not in network.institutions and item not in network.walkways
               and item not in network.pedestrian_crossings and item not in network.spawn_points
               and item not in network.metro_lines]
    stations = [item for item in unknown if item in network.metro_stops]
    if stations:  # a station alone would cut its line's track in two
        raise ValueError(f"MRT station {stations[0]} goes only with its line: delete "
                         f"{' or '.join(line.id for line in network.metro_lines.values() if stations[0] in line.stops)}.")
    if unknown:  # run_tool tries a road named from its other end, else says "Unknown id" with the nearest
        raise KeyError(unknown[0])
    for line_id in ids:
        if line_id in network.metro_lines:
            network.delete_metro_line(line_id)
    for stop_id in ids:  # bus stops and institutions first, then roads; a node then takes whatever roads it still has
        network.bus_stops.pop(stop_id, None)
        network.institutions.pop(stop_id, None)
        network.walkways.pop(stop_id, None)
        network.pedestrian_crossings.pop(stop_id, None)
        if stop_id in network.spawn_points:
            network.delete_spawn_point(stop_id)
    for road_id in ids:
        if road_id in network.roads or road_id in network.approach_roads:
            network.delete_road(road_id)
    for node_id in ids:
        if node_id in network.nodes or node_id in network.approach_nodes:
            network.delete_node(node_id)
    return {"deleted": ids}


def build_interchange(network: Network, kind: str, junctions, stem=None, freeway=None, replace=True,
                      freeway_lanes=2) -> dict:
    ids = _ids(junctions)
    for node_id in ids:
        network.node(node_id)
    if kind == "trumpet":
        if len(ids) != 3:
            raise ValueError("A trumpet needs 3 junctions: two on the freeway and the terminating road's.")
        stem = str(stem) if stem else guess_stem(network, ids)
        if stem not in ids:
            raise ValueError("stem must be one of the 3 junctions.")
        legs = tuple(node_id for node_id in ids if node_id != stem)
        name = build_trumpet(network, legs, stem, _bool(replace), _int(freeway_lanes))
    elif kind in ("diamond", "spui"):
        if len(ids) != 4:
            raise ValueError(f"A {kind} needs 4 junctions: two on the freeway and two on the crossroad.")
        if freeway:
            legs = tuple(_ids(freeway))
            cross = tuple(node_id for node_id in ids if node_id not in legs)
            if len(legs) != 2 or len(cross) != 2:
                raise ValueError("freeway must be 2 of the 4 junctions.")
        else:
            legs, cross = pair_legs(network, ids)
        name = build_diamond(network, legs, cross, _bool(replace), _int(freeway_lanes), single_point=kind == "spui")
    else:
        raise ValueError("kind must be trumpet, diamond or spui.")
    return {"built": name, "kind": kind, "freeway": list(legs),
            "new_nodes": [node_id for node_id in network.nodes if node_id.startswith(name + ".")]}


def add_slip_road(network: Network, roads, single_lane=False) -> dict:
    ids = _ids(roads)
    if len(ids) != 2:
        raise ValueError("A slip road needs 2 roads that meet at a junction.")
    slip = network.road(build_slip_road(network, *ids, _bool(single_lane)))
    return {"added": slip.id, "from": slip.from_node, "to": slip.to_node, "lanes": slip.lanes,
            "length_m": round(slip.length_m, 1), "speed_limit_kmh": slip.speed_limit_kmh}


def set_end_flows(network: Network, end: str, supply_vph=None, demand_vph=None) -> dict:
    if end not in network.approach_nodes:
        if end in network.nodes:
            raise ValueError(f"{end} is a junction, not a boundary end: ends are like '1W' or '3N' (network_summary).")
        raise KeyError(end)
    node = network.approach_nodes[end]
    if supply_vph is not None:
        node.supply_vph = float(supply_vph)
    if demand_vph is not None:
        node.demand_vph = float(demand_vph)
    return {"end": end, "supply_vph": node.supply_vph, "demand_vph": node.demand_vph}


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


# Demand sizing stops before a link's 95th-percentile queue fills this share of it (HCM's queue storage ratio: 1 is
# failure; queues on neighbouring short links interact well before it)
STORAGE_LIMIT = 0.5


def _design_hour(trips: dict, shares: dict | None) -> int | None:
    """The busiest hour of the day for daily trips spread by each pair's 24 `shares` (a pair without
    any: evenly), as a Run picks it to time its signals; None for hourly demand (shares None)."""
    if shares is None:
        return None
    return max(range(24), key=lambda hour: sum(value * _share(shares, pair, hour) for pair, value in trips.items()))


def _share(shares: dict | None, pair, hour: int | None) -> float:
    if hour is None:
        return 1.0
    return shares[pair][hour] if shares.get(pair) else 1 / 24


def _load(graph: Graph, trips: dict, shares: dict | None, hour: int | None) -> tuple[dict, dict]:
    """graph.assign of the veh/h the trips make in that hour (hourly demand: as they are)."""
    return graph.assign({pair: value * _share(shares, pair, hour) for pair, value in trips.items()})


def _saturations(graph: Graph, flows: dict, moves: dict) -> dict[str, float]:
    """Each junction's degree of saturation where someone gives way or waits (Graph.saturation)."""
    found = {node: graph.saturation(node, flows, moves) for node in graph.network.nodes}
    return {node: x for node, x in found.items() if x is not None}


def _fit_saturation(graph: Graph, fixed: dict, scaled: dict, target: float,
                    shares: dict | None) -> tuple[float, str]:
    """The factor on `scaled` ({pair: veh/h, or trips/day with shares}) that brings the most saturated
    junction to `target`, the `fixed` pairs staying as they are, or first fills a link with its queue (its queue
    storage ratio reaching 1, Graph.storage_ratio: a short link spills back into the junction behind long before
    either junction reaches its X; an 80 m signal grid sized to X 0.6 jammed). Both rise with the factor: bisection
    on whichever binds first. (factor, "saturation" or "storage": which bound it)."""
    if not SATURATION_RANGE[0] <= target <= SATURATION_RANGE[1]:
        raise ValueError("degree_of_saturation must be 0.1 to 1.2 (0.8 = 80 % of capacity).")
    hour = _design_hour({**fixed, **scaled}, shares)
    base, extra = _load(graph, fixed, shares, hour), _load(graph, scaled, shares, hour)

    def loaded(factor: float):
        return ({key: a.get(key, 0.0) + factor * b.get(key, 0.0) for key in {*a, *b}} for a, b in zip(base, extra))

    def worst(factor: float) -> float:  # the binding share of its limit: X over target, or a queue over its storage
        flows, moves = loaded(factor)
        x = max(_saturations(graph, flows, moves).values(), default=0.0)
        spill = max((graph.storage_ratio(node, moves) or 0.0 for node in graph.network.nodes), default=0.0)
        return max(x, spill / STORAGE_LIMIT * target)

    def bound(factor: float) -> str:
        flows, moves = loaded(factor)
        x = max(_saturations(graph, flows, moves).values(), default=0.0)
        spill = max((graph.storage_ratio(node, moves) or 0.0 for node in graph.network.nodes), default=0.0)
        return "storage" if spill / STORAGE_LIMIT * target > x else "saturation"

    floor_x = worst(0.0)
    if worst(1.0) <= floor_x:
        raise ValueError("No route of these pairs crosses a junction where traffic waits (signalised, roundabout "
                         "or a priority junction's minor arm): there is nothing to size by. Give total or factor.")
    if floor_x >= target:
        raise ValueError(f"The other pairs alone already load a junction to {floor_x:.2f}: lower them, or aim higher.")
    low, high = 0.0, 1.0
    for _ in range(60):
        if worst(high) >= target:
            break
        high *= 2
    for _ in range(40):
        middle = (low + high) / 2
        low, high = (middle, high) if worst(middle) < target else (low, middle)
    return high, bound(high)


def _saturation_report(graph: Graph, trips: dict, shares: dict | None) -> dict:
    """What the demand does to the junctions: the five most saturated (flow/capacity) with their
    control, and for daily demand the hour that is."""
    hour = _design_hour(trips, shares)
    busiest = sorted(_saturations(graph, *_load(graph, trips, shares, hour)).items(), key=lambda item: -item[1])[:5]
    report = {}
    if busiest:
        report["degree_of_saturation"] = {node: round(x, 2) for node, x in busiest}
        report["junction_controls"] = {node: graph.network.nodes[node].control for node, _ in busiest}
    if hour is not None:
        report["design_hour"] = f"{hour:02d}:00-{hour + 1:02d}:00"
    return report


def _no_demand_reason(trips: dict, by_o: dict, by_d: dict, total: float, unit: str) -> str:
    """Why a gravity model gave no pair any demand, and what to change."""
    if not any(trips.values()):
        sources = ", ".join(sorted(end for end, weight in by_o.items() if weight)) or "none"
        sinks = ", ".join(sorted(end for end, weight in by_d.items() if weight)) or "none"
        return (f"No pair gets demand: trips leave only {sources} (supply) and arrive only at {sinks} (demand), and "
                "no route joins two different ends of these. Set supply and demand at more ends (set_end_flows), or "
                "set them to 0 everywhere so the total is shared by every pair. Nothing was changed.")
    return (f"{total:g} {unit} over {len(trips)} pairs is under 1 a pair, so every pair rounds to 0: give a total of "
            f"at least {len(trips)}, or fewer pairs. Nothing was changed.")


def _furness(km: dict, leave: dict, arrive: dict, decay: float) -> dict:
    """T = O·D·exp(−decay·km) over the pairs with a route, balanced to the origins' and destinations'
    totals by Furness's iterations (100 at most, to 0.1 %)."""
    trips = {(o, d): leave[o] * arrive[d] * exp(-decay * distance) for (o, d), distance in km.items()}
    for _ in range(100):  # Furness: rows to the origins' totals, then columns to the destinations'
        rows, columns = defaultdict(float), defaultdict(float)
        for (o, _d), value in trips.items():
            rows[o] += value
        trips = {(o, d): value * leave[o] / rows[o] if rows[o] else 0.0 for (o, d), value in trips.items()}
        for (_o, d), value in trips.items():
            columns[d] += value
        trips = {(o, d): value * arrive[d] / columns[d] if columns[d] else 0.0 for (o, d), value in trips.items()}
        rows = defaultdict(float)
        for (o, _d), value in trips.items():
            rows[o] += value
        if all(abs(rows[o] - leave[o]) <= 0.001 * leave[o] for o in rows):
            break
    return trips


def _mean_km(trips: dict, km: dict) -> float:
    total = sum(trips.values())
    return sum(value * km[pair] for pair, value in trips.items()) / total if total else 0.0


def demand_plan(network: Network, pairs: list, args: dict, values: dict | None = None,
                shares: dict | None = None) -> tuple[dict, dict]:
    """generate_demand's numbers for these (origin, destination) pairs, nothing changed: a gravity model
    T = O·D·exp(-β·km) over the shortest routes, balanced to the ends' totals (Furness). `values`: the
    matrix's demand of that kind now ({pair: value}; pairs outside `pairs` keep theirs and load the
    junctions too); `shares`: daily demand's 24 hourly shares by pair (a pair without: evenly).
    ValueError says what is missing."""
    demand = args.get("demand") or "hourly"
    if demand not in ("hourly", "daily"):
        raise ValueError("demand must be 'hourly' or 'daily'.")
    decay = float(args.get("distance_decay_per_km", 0.5) if args.get("distance_decay_per_km") is not None else 0.5)
    if not 0 <= decay <= 10:
        raise ValueError("distance_decay_per_km must be 0 to 10.")
    origins, destinations = sorted({o for o, _ in pairs}), sorted({d for _, d in pairs})
    ends, days = network.approach_nodes, 24 if demand == "daily" else 1
    supply = {o: ends[o].supply_vph if o in ends else 0.0 for o in origins}
    wanted = {d: ends[d].demand_vph if d in ends else 0.0 for d in destinations}
    note = ""
    target = args.get("degree_of_saturation")
    if target is not None:  # sized below, once the routes are known: any total does
        target = float(target)
        if args.get("total") not in (None, "", 0):  # 0: a small model's filler, no total
            raise ValueError("Give total or degree_of_saturation, not both.")
        if not SATURATION_RANGE[0] <= target <= SATURATION_RANGE[1]:
            raise ValueError("degree_of_saturation must be 0.1 to 1.2 (0.8 = 80 % of capacity).")
        total = 10_000.0 * days
    elif args.get("total") not in (None, ""):
        total = float(args["total"])
        if not 1 <= total <= 2_000_000:
            raise ValueError("total must be 1 to 2,000,000.")
    else:
        total = (sum(supply.values()) or sum(wanted.values())) * days
        if not total:
            raise ValueError("Give degree_of_saturation for how busy (light 0.4, medium 0.65, busy or peak 0.85), "
                             "or total (veh/h hourly, trips per day daily), or set the ends' supply and demand first "
                             "(set_end_flows).")
        note = "Totals from the ends' flows" + (" (veh/h × 24 for a day)." if days > 1 else ".")
    by_o = supply if any(supply.values()) else dict.fromkeys(origins, 1.0)
    by_d = wanted if any(wanted.values()) else dict.fromkeys(destinations, 1.0)
    leave = {o: total * by_o[o] / sum(by_o.values()) for o in origins}
    arrive = {d: total * by_d[d] / sum(by_d.values()) for d in destinations}
    routes = route_lengths(network, origins)
    km = {(o, d): routes[o][d] / 1000 for o, d in pairs if d in routes[o]}
    fitted = args.get("mean_trip_km") or None  # 0 km is no trip: a small model's filler (llama3.2), not a length
    if fitted is not None:  # Hyman's method: the decay whose balanced matrix has this mean trip length
        if args.get("distance_decay_per_km") is not None:
            raise ValueError("Give mean_trip_km or distance_decay_per_km, not both.")
        fitted = float(fitted)
        shortest, longest = (_mean_km(_furness(km, leave, arrive, beta), km) for beta in (10.0, 0.0))
        if longest <= 0 or longest - shortest < 0.01:  # no trips to spread (flows at one end), or all one length
            raise ValueError("A mean trip length can't be fitted here: " + (
                "no trip can be made between these ends (flows set at only one end?). Set supply and demand at "
                "more ends (set_end_flows), or give total." if longest <= 0 else
                f"every route is about {longest:.2f} km, so the decay doesn't change it. Give distance_decay_per_km "
                "instead.") + " Nothing was changed.")
        if not shortest <= fitted <= longest:
            raise ValueError(f"mean_trip_km must be {shortest:.2f} to {longest:.2f} km on these ends' routes (the "
                             "shortest and the longest mean a gravity model can make here).")
        low, high = 0.0, 10.0
        for _ in range(40):  # the mean trip length falls as the decay rises
            middle = (low + high) / 2
            low, high = (middle, high) if _mean_km(_furness(km, leave, arrive, middle), km) > fitted else (low, middle)
        decay = round((low + high) / 2, 4)
    trips = _furness(km, leave, arrive, decay)
    shares = (shares or {}) if days > 1 else None
    others = {pair: float(value) for pair, value in (values or {}).items() if value and pair not in set(pairs)}
    graph = Graph(network)
    if target is not None:  # every pair scaled until the most saturated junction is at the target
        factor, sized_by = _fit_saturation(graph, others, trips, target, shares)
        trips = {pair: value * factor for pair, value in trips.items()}
        total = sum(trips.values())
    plan = {pair: round(value) for pair, value in trips.items() if round(value) > 0}
    unit = "trips/day" if days > 1 else "veh/h"
    if not plan:  # refused before anything is typed in: the matrix would only be cleared
        raise ValueError(_no_demand_reason(trips, by_o, by_d, total, unit))
    info = {"method": "gravity model, exp(-distance_decay_per_km x km) over the shortest routes, balanced to the "
                      "ends' totals (Furness)" + ("; the decay fitted to mean_trip_km (Hyman)" if fitted is not None else ""),
            "demand": demand, "unit": unit, "distance_decay_per_km": decay,
            "mean_trip_km": round(_mean_km(plan, km), 2),
            "total_asked": round(total), "total_made": sum(plan.values()), "pairs_with_demand": len(plan),
            "busiest_pairs": {f"{o}->{d}": value for (o, d), value in
                              sorted(plan.items(), key=lambda item: -item[1])[:5]}}
    if len(plan) < len(trips):
        info["pairs_rounded_to_0"] = len(trips) - len(plan)  # under 0.5 a pair: a larger total reaches them
    missing = [f"{o}->{d}" for o, d in pairs if d not in routes[o]]
    if missing:
        info["no_route"] = missing[:20]
    info.update(_saturation_report(graph, {**others, **plan}, shares))
    if target is not None:
        info["degree_of_saturation_target"] = target
        if sized_by == "storage":
            info["sized_by"] = "a link's queue fills its length before any junction reaches that degree of saturation (short links spill back into the junction behind): sized to what the links hold instead"
    if note:
        info["note"] = note
    return plan, info


def scale_plan(network: Network, values: dict, pairs: list, args: dict, shares: dict | None = None) -> tuple[dict, dict]:
    """scale_demand's numbers, nothing changed: the matrix's demand `values` ({pair: veh/h, or trips/day
    with `shares` as in demand_plan}) in `pairs` times `factor`, or times whatever brings the most
    saturated junction to `degree_of_saturation`; the other pairs stay and still load the junctions."""
    factor, target = args.get("factor"), args.get("degree_of_saturation")
    if (factor is None) == (target is None):
        raise ValueError("Give factor (e.g. 1.2 for 20 % more) or degree_of_saturation (e.g. 0.8), one of them.")
    daily = args.get("demand") == "daily"
    scope = set(pairs)
    scaled = {pair: float(value) for pair, value in values.items() if value and pair in scope}
    if not scaled:
        raise ValueError(f"There's no {'daily' if daily else 'hourly'} demand in those pairs to scale: enter some "
                         "first (set_od_demand or generate_demand).")
    others = {pair: float(value) for pair, value in values.items() if value and pair not in scope}
    shares = (shares or {}) if daily else None
    graph = Graph(network)
    if factor is not None:
        factor = float(factor)
        if not 0.01 <= factor <= 100:
            raise ValueError("factor must be 0.01 to 100.")
    else:
        factor, sized_by = _fit_saturation(graph, others, scaled, float(target), shares)
    plan = {pair: round(value * factor) for pair, value in scaled.items()}
    if not any(plan.values()):
        raise ValueError(f"A factor of {factor:.3g} rounds every pair's demand to 0: nothing would be left. "
                         "Nothing was changed.")
    info = {"demand": "daily" if daily else "hourly", "unit": "trips/day" if daily else "veh/h",
            "factor": round(factor, 3), "pairs_scaled": len(plan), "total_before": round(sum(scaled.values())),
            "total_after": sum(plan.values())}
    if any(not value for value in plan.values()):
        info["pairs_cleared"] = sum(not value for value in plan.values())  # scaled under 0.5: their demand goes
    info.update(_saturation_report(graph, {**others, **plan}, shares))
    if target is not None:
        info["degree_of_saturation_target"] = float(target)
        if sized_by == "storage":
            info["sized_by"] = "a link's queue fills its length before any junction reaches that degree of saturation (short links spill back into the junction behind): sized to what the links hold instead"
    return plan, info


def _manual(network: Network, node_id: str) -> dict:
    """A manual junction's windows by arm name and its amber; nothing for the other methods."""
    node = network.nodes[node_id]
    if node.signal_method != "manual":
        return {}
    return {"windows": {name: node.manual_greens_s.get(road, "none: always red")
                        for road, name, _ in network.signal_arms(node_id)}, "amber_s": node.amber_s}


def set_signal_timing(network: Network, junctions, method=None, phases=None, green_times_s=None, cycle_min_s=None,
                      cycle_max_s=None, target_saturation_percent=None, stop_weight_s=None, windows=None,
                      amber_s=None) -> dict:
    ids = [_junction(network, node_id) for node_id in _ids(junctions)]
    unsignalised = [node_id for node_id in ids if network.nodes[node_id].control != "signalised"]
    if unsignalised:  # all of them at once, so one call can put them right
        raise ValueError(f"{', '.join(unsignalised)} {'is' if len(unsignalised) == 1 else 'are'} not signalised: "
                         "make them signalised first (set_junction_control), or leave them out.")
    if method is not None and method not in SIGNAL_METHODS:
        raise ValueError(f"method must be one of {', '.join(SIGNAL_METHODS)}.")
    if phases is not None and _int(phases) not in (2, 4):
        raise ValueError("phases must be 2 or 4.")
    greens = None if green_times_s is None else \
        [float(g) for g in (json.loads(green_times_s) if isinstance(green_times_s, str) else green_times_s)]
    note = "Each Run times them by their method, from its demand (fixed: the greens given)."
    if greens is not None:  # greens are only ever used by a fixed plan: given alone, they mean one
        if method not in (None, "fixed"):
            raise ValueError(f"green_times_s are only used by method fixed; {method} times the greens from demand. "
                             "Give method fixed with them, or leave them out.")
        for node_id in ids:
            count = _int(phases) if phases is not None else network.nodes[node_id].signal_phases
            if len(greens) != count:
                raise ValueError(f"green_times_s needs one green a phase: {node_id} has {count} phases, "
                                 f"{len(greens)} greens given" + (" (or give phases too)." if phases is None else "."))
        if method is None:
            method, note = "fixed", "Fixed time with the greens given (they are used only by a fixed plan)."
    if windows is not None:  # likewise only a manual plan uses windows
        windows = json.loads(windows) if isinstance(windows, str) else dict(windows)
        if method not in (None, "manual"):
            raise ValueError(f"windows are only used by method manual. Give method manual with them, or leave them out.")
        method, note = "manual", "Manual: each arm goes in its window every cycle, red the rest."
    for node_id in ids:
        node = network.nodes[node_id]
        if windows is not None:
            arms = {key: road for road, name, _ in network.signal_arms(node_id) for key in (road, name)}
            unknown = [arm for arm in windows if arm not in arms]
            if unknown:
                raise ValueError(f"{node_id} has no arm {', '.join(map(str, unknown))}: its arms are "
                                 f"{', '.join(name for _, name, _ in network.signal_arms(node_id))}.")
            node.manual_greens_s = {arms[arm]: [float(s) for s in window] for arm, window in windows.items()}
        if amber_s is not None:
            node.amber_s = float(amber_s)
        if method is not None:
            node.signal_method = method
        if phases is not None:
            node.signal_phases = _int(phases)
            if len(node.green_times_s) != node.signal_phases:
                node.green_times_s = (node.green_times_s * 2)[:node.signal_phases]
        if greens is not None:
            node.green_times_s = list(greens)
        if cycle_min_s is not None:
            node.cycle_min_s = float(cycle_min_s)
        if cycle_max_s is not None:
            node.cycle_max_s = float(cycle_max_s)
        if target_saturation_percent is not None:
            node.target_saturation = float(target_saturation_percent) / 100
        if stop_weight_s is not None:
            node.stop_weight_s = float(stop_weight_s)
    network.validate()  # phases, greens, limits: refused (and rolled back) with the reason
    return {"timed": {node_id: {"method": network.nodes[node_id].signal_method, "phases": network.nodes[node_id].signal_phases,
                                "green_times_s": network.nodes[node_id].green_times_s,
                                **_manual(network, node_id),
                                "cycle_limits_s": [network.nodes[node_id].cycle_min_s, network.nodes[node_id].cycle_max_s],
                                "target_saturation_percent": round(network.nodes[node_id].target_saturation * 100)}
                      for node_id in ids},
            "note": note}


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


def _routes(network: Network, pairs) -> dict:
    """The roads on each pair's shortest route (one-way roads kept): {(o, d): [road ids]}; absent: no route."""
    ahead = defaultdict(list)
    for road in network.all_roads():
        ahead[road.from_node].append((road.to_node, road.length_m, road.id))
        if road.direction == "two-way":
            ahead[road.to_node].append((road.from_node, road.length_m, road.id))
    found = {}
    for origin in {o for o, _ in pairs}:
        best, via, todo = {origin: 0.0}, {}, [(0.0, origin)]
        while todo:
            metres, here = heapq.heappop(todo)
            if metres > best.get(here, float("inf")):
                continue
            for there, step, road_id in ahead[here]:
                if metres + step < best.get(there, float("inf")):
                    best[there], via[there] = metres + step, (here, road_id)
                    heapq.heappush(todo, (metres + step, there))
        for o, d in pairs:
            if o == origin and d in best:
                path, node = [], d
                while node != origin:
                    node, road_id = via[node]
                    path.append(road_id)
                found[(o, d)] = path[::-1]
    return found


def calibration_plan(network: Network, flows: dict, args: dict) -> tuple[dict, dict]:
    """calibrate_demand's numbers, nothing changed: the prior hourly flows `flows` {(o, d): veh/h} scaled
    so the roads' modelled flows meet the counts, ME2's multiplicative balancing (Van Zuylen and Willumsen 1980):
    T_od ← T_od × (count_a / modelled_a)^p_od,a for each counted road a, repeated. p_od,a is the share of the
    pair's vehicles that drive road a on the routes the simulation gives them (Graph.route_shares: fastest, with a
    two-phase signal's offside turn as an out-and-back, counted each time it is driven), not the shortest
    distance: on those, 80 of a 3 x 3 grid's 132 pairs went another way, and a fit of GEH 0 loaded 16.8-23.7."""
    counts = args.get("counts")
    if isinstance(counts, str):
        counts = _parsed_json(counts)
    if not isinstance(counts, list) or not counts:
        raise ValueError("Give counts like [{\"road\": \"1,1-2,1\", \"vph\": 800}].")
    wanted = {}
    for count in counts:
        road = str(count.get("road", "")) if isinstance(count, dict) else ""
        if road not in network.roads and road not in network.approach_roads:
            raise ValueError(f"No road called {road!r} to count on.")
        vph = count.get("vph")
        if not isinstance(vph, (int, float)) or vph < 0:
            raise ValueError(f"{road}: vph must be a count, not below zero.")
        wanted[road] = float(vph)
    prior = {pair: float(value) for pair, value in flows.items() if value}
    if not prior:
        raise ValueError("There's no hourly demand to start from: enter some first (set_od_demand or generate_demand).")
    iterations = max(1, min(_int(args.get("iterations") or 50), 500))
    graph = Graph(network)
    use: dict = {road: {} for road in wanted}  # road -> {pair: vehicles on it per vehicle of the pair}
    for pair in prior:
        for share, route in graph.route_shares(*pair):
            for link in route:
                if link.road in use:
                    use[link.road][pair] = use[link.road].get(pair, 0.0) + share
    on = {road: pairs for road, pairs in use.items()}
    uncovered = [road for road, pairs in on.items() if not pairs]
    if len(uncovered) == len(wanted):
        raise ValueError(f"No OD pair's route uses {', '.join(uncovered)}: the counts can't be met by this demand.")

    def modelled(trips):
        return {road: sum(trips[pair] * p for pair, p in pairs.items()) for road, pairs in on.items()}

    before, trips = modelled(prior), dict(prior)
    for _ in range(iterations):
        for road, pairs in on.items():
            now = sum(trips[pair] * p for pair, p in pairs.items())
            if now > 0:
                factor = wanted[road] / now
                for pair, p in pairs.items():
                    trips[pair] *= factor ** min(p, 1.0)
    after = modelled(trips)
    plan = {pair: round(value) for pair, value in trips.items() if round(value) > 0}
    if not plan:
        raise ValueError("Fitted to those counts, every pair's demand rounds to 0: the matrix would be cleared. "
                         "Check the counts (a 0 on a road every route uses empties it). Nothing was changed.")
    rows = [{"road": road, "counted_vph": wanted[road], "before_vph": round(before[road]), "after_vph": round(after[road]),
             "geh_before": round(geh(before[road], wanted[road]), 2), "geh_after": round(geh(after[road], wanted[road]), 2)}
            for road in wanted]
    info = {"method": "ME2 multiplicative balancing (Van Zuylen and Willumsen 1980) on the routes the simulation "
                      "drives, from the demand entered", "counts": rows, "iterations": iterations,
            "fits": sum(row["geh_after"] < 5 for row in rows), "of": len(rows),
            "total_before_vph": round(sum(prior.values())), "total_after_vph": sum(plan.values())}
    if uncovered:
        info["note"] = f"No route uses {', '.join(uncovered)}: left as it was."
    return plan, info


# HCM 2000's default mode-choice utility (p1057-1058): U = -0.025 IVT - 0.050 OVT - 0.005 cost (minutes, cents)
LOGIT_DEFAULTS = {"in_vehicle_per_min": -0.025, "out_of_vehicle_per_min": -0.050, "cost": -0.005}


def metro_cost(alternative: dict, ride: dict) -> float:
    """A metro ride's cost: its line's fare, plus an alternative's own cost and cost_per_km (on the km ridden). WALK_KMH
    (metro.py) is also an institution's walk_kmh by default."""
    return ride["cost"] + float(alternative.get("cost") or 0) + float(alternative.get("cost_per_km") or 0) * ride["km"]


def default_alternative(mode) -> dict:
    """An institution's mode split's assumed attributes for a vehicle type, when none are given: a timetabled
    type (a bus or tram) a 5 min walk to its stop and a 100 cent fare; any other 3 min to park and walk, and 15 cents a km."""
    if mode.headway_min or getattr(mode, "timetabled", False):
        return {"mode": mode.name, "out_of_vehicle_min": 5.0, "cost": 100.0}
    if getattr(mode, "vehicle_class", "") == "bicycle":  # a minute to lock it up; next to no running cost
        return {"mode": mode.name, "out_of_vehicle_min": 1.0, "cost_per_km": 0.0}
    return {"mode": mode.name, "out_of_vehicle_min": 3.0, "cost_per_km": 15.0}


def _cycled(mode, roads: list, minutes: float) -> float:
    """A bicycle's in-vehicle minutes as a cyclist feels them: those on roads with cycle lanes at
    lane_graph.CYCLE_LANE_TIME_FACTOR (in the length's proportion); any other type's, as they are."""
    if getattr(mode, "vehicle_class", "") != "bicycle":
        return minutes
    total = sum(road.length_m for road in roads)
    laned = sum(road.length_m for road in roads if road.cycle_lanes) / total if total else 0.0
    return minutes * (1 - laned + laned * CYCLE_LANE_TIME_FACTOR)


def institution_split(network: Network, item, modes: list, args: dict) -> tuple[dict, dict]:
    """An institution's mode split, nothing changed: people % by mode. Each junction or end of its catchment
    is an origin, its door the destination, the route km between them the trip. Each mode's utility U =
    constant + in_vehicle_per_min x IVT + out_of_vehicle_per_min x OVT + cost x cost: vehicle types as choose_modes
    (IVT at free flow, OVT the minutes given plus half the headway), Walk all out-of-vehicle (km at walk_kmh).
    The shares are the logit's, averaged over the catchment (each origin an equal share, as its trips are).
    observed_percent calibrates the observed modes' constants, c += ln(observed / predicted) until they match
    (Train 2009, section 2.8); the other modes share the rest in the model's proportions."""
    names = {mode.name.lower(): mode for mode in modes}
    by_metro = bool(network.metro_lines) and METRO.lower() not in names  # the network's metro as a mode
    offered = ", ".join(["Walk", *(m.name for m in modes), *([METRO] if by_metro else [])])
    given = _parsed_json(args["alternatives"]) if isinstance(args.get("alternatives"), str) else args.get("alternatives")
    assumed = not given
    if assumed:
        given = [{"mode": WALK}, *(default_alternative(mode) for mode in modes), *([{"mode": METRO}] if by_metro else [])]
    if not isinstance(given, list) or len(given) < 2 or not all(isinstance(a, dict) for a in given):
        raise ValueError(f"Give 2 or more alternatives like [{{\"mode\": \"Walk\"}}, {{\"mode\": \"Car\", "
                         f"\"cost_per_km\": 15}}]; modes: {offered}.")
    chosen = [WALK if str(a.get("mode", "")).strip().lower() == "walk" else
              METRO if by_metro and str(a.get("mode", "")).strip().lower() in (METRO.lower(), "metro") else
              getattr(names.get(str(a.get("mode", "")).strip().lower()), "name", None) for a in given]
    if None in chosen or len(set(chosen)) != len(chosen):
        raise ValueError(f"Each alternative is a different mode: {offered}.")
    coefficients = {**LOGIT_DEFAULTS, **(args.get("coefficients") or {})}
    if set(coefficients) != set(LOGIT_DEFAULTS) or any(not isinstance(v, (int, float)) or v > 0 for v in coefficients.values()):
        raise ValueError(f"coefficients are {', '.join(LOGIT_DEFAULTS)}, each 0 or below (more time or cost is worse).")
    walk_kmh = float(args.get("walk_kmh") or WALK_KMH)
    if not 1 <= walk_kmh <= 10:
        raise ValueError("walk_kmh must be 1 to 10.")
    for alternative in given:
        for key in ("cost", "cost_per_km", "out_of_vehicle_min"):
            if float(alternative.get(key) or 0) < 0:
                raise ValueError(f"{alternative['mode']}: {key} can't be below 0.")
    observed_in = _parsed_json(args["observed_percent"]) if isinstance(args.get("observed_percent"), str) \
        else args.get("observed_percent") or {}
    canonical = {name.lower(): name for name in chosen}
    observed = {}
    for name, percent in (observed_in.items() if isinstance(observed_in, dict) else [(None, None)]):
        mode = canonical.get(str(name).strip().lower())
        if mode is None or not isinstance(percent, (int, float)) or isinstance(percent, bool) or not 0 <= percent <= 100:
            raise ValueError(f"observed_percent is a share 0-100 for any of {', '.join(chosen)}, e.g. {{\"Car\": 30}}.")
        observed[mode] = float(percent)
    total = sum(observed.values())
    if total > 100.5 or (len(observed) == len(chosen) and abs(total - 100) > 0.5):
        raise ValueError(f"The observed shares add up to {total:g} %: at most 100, and exactly 100 when every mode has one.")
    alternatives = {mode: alternative for mode, alternative in zip(chosen, given) if observed.get(mode, 1) > 0}
    observed = {mode: percent / 100 for mode, percent in observed.items() if percent > 0}
    if len(alternatives) < 2:
        raise ValueError("At least two modes must be possible (an observed 0 % leaves a mode out).")
    entrance, origins = door(network, item), catchment(network, item)
    if entrance is None or not origins:
        raise ValueError(f"{item.id}'s circle reaches no junction or end other than its door ({entrance}): make its "
                         "circle bigger (update_institution radius_m).")
    routes = _routes(network, [(origin, entrance) for origin in origins])
    trips = [(origin, [network.road(road_id) for road_id in routes[(origin, entrance)]])
             for origin in origins if (origin, entrance) in routes]
    if not trips:
        raise ValueError(f"No route from {item.id}'s catchment to its door {entrance}.")
    attributes = []  # per place (a junction or end of its circle, or a station): {mode: (IVT, OVT, cost)}
    for origin, roads in trips:
        km = sum(road.length_m for road in roads) / 1000
        found = {}
        for mode, alternative in alternatives.items():
            ovt, cost = float(alternative.get("out_of_vehicle_min") or 0), \
                float(alternative.get("cost") or 0) + float(alternative.get("cost_per_km") or 0) * km
            if mode == WALK:
                found[mode] = (0.0, ovt + km / walk_kmh * 60, cost)
                continue
            if mode == METRO:  # the roads' places don't ride the metro: its own stations are places (below)
                continue
            vehicle = names[mode.lower()]
            top = min(vehicle.max_speed_kmh, vehicle.desired_max_kmh)
            ivt = _cycled(vehicle, roads, sum(road.length_m / (min(top, road.speed_limit_kmh) / 3.6) for road in roads) / 60)
            found[mode] = (ivt, ovt + vehicle.headway_min / 2, cost)
        attributes.append((origin, km, found))
    # The stations in its circle are places its people come from too, as the junctions are (each an equal share):
    # there, the metro to its door station and the walk on, or walking all the way
    station_door, stations = metro_places(network, item) if METRO in alternatives else (None, [])
    walk_on = metro_door(network, item.x, item.y)[1] / 1000 / walk_kmh * 60 if station_door else 0.0
    rides = {}
    for station in stations:
        trip = rides[station] = metro_ride(network, station, station_door)
        stop = network.metro_stops[station]
        km = hypot(stop.x - item.x, stop.y - item.y) / 1000
        metro_alternative = alternatives[METRO]
        found = {METRO: (trip["ivt_min"], float(metro_alternative.get("out_of_vehicle_min") or 0) + trip["wait_min"]
                         + walk_on, metro_cost(metro_alternative, trip))}
        if WALK in alternatives:
            found[WALK] = (0.0, float(alternatives[WALK].get("out_of_vehicle_min") or 0) + km / walk_kmh * 60,
                           float(alternatives[WALK].get("cost") or 0))
        attributes.append((station, km, found))
    constants = {mode: float(alternative.get("constant") or 0) for mode, alternative in alternatives.items()}

    def predict() -> tuple[dict, list]:
        rows = [(origin, km, stats.logit({mode: constants[mode] + coefficients["in_vehicle_per_min"] * ivt
                                          + coefficients["out_of_vehicle_per_min"] * ovt + coefficients["cost"] * cost
                                          for mode, (ivt, ovt, cost) in found.items()}))
                for origin, km, found in attributes]
        return {mode: sum(row[2].get(mode, 0.0) for row in rows) / len(rows) for mode in alternatives}, rows

    predicted, rows = predict()
    steps = 0
    while observed and steps < 200:
        free = [mode for mode in alternatives if mode not in observed]
        rest, free_total = 1 - sum(observed.values()), sum(predicted[mode] for mode in free)
        target = {**observed, **{mode: rest * predicted[mode] / free_total for mode in free if free_total}}
        if max(abs(predicted[mode] - target[mode]) for mode in observed) < 1e-6:
            break
        for mode in observed:
            constants[mode] += log(target[mode] / max(predicted[mode], 1e-12))
        predicted, rows = predict()
        steps += 1
    percent = {mode: round(100 * share, 2) for mode, share in predicted.items()}
    people = sum(hourly_people(item)[0])
    vehicles = {}
    for mode, share in predicted.items():
        vehicle = names.get(mode.lower()) if mode != WALK else None
        if vehicle is not None and not vehicle.headway_min:
            vehicles[mode] = round(people * share / max(vehicle.occupancy, 1e-9))
    info = {"institution": item.id, "people_percent": percent, "door": entrance, "origins": len(trips),
            "mean_trip_km": round(sum(km for _, km, _ in attributes) / len(attributes), 2),
            "method": "multinomial logit over the catchment's trips to the door, U = constant + in_vehicle_per_min·IVT "
                      "+ out_of_vehicle_per_min·OVT + cost·cost (HCM 2000's coefficients unless given); Walk all "
                      "out-of-vehicle at walk_kmh",
            "coefficients": coefficients, "walk_kmh": walk_kmh,
            "constants": {mode: round(value, 3) for mode, value in constants.items()},
            "people_per_day_each_way": round(people), "vehicles_per_day_each_way": vehicles,
            "by_origin": [{"from": origin, "km": round(km, 2), "people_percent": {m: round(100 * p, 1) for m, p in shares_.items()}}
                          for origin, km, shares_ in rows[:12]]}
    if observed:
        info["calibrated_to_percent"] = {mode: round(100 * share, 2) for mode, share in observed.items()}
        info["calibration"] = (f"constants fitted in {steps} steps (c += ln(observed/predicted), Train 2009 §2.8); "
                               "the unobserved modes keep their relative odds")
    if assumed:
        info["assumed"] = ("Walk, and every vehicle type: timetabled ones a 5 min walk to the stop and a 100 cent fare, "
                           "the others 3 min to park and 15 cents a km; give alternatives to change them, and "
                           "observed_percent to calibrate to known shares")
    timed = [mode for mode in alternatives if mode not in (WALK, METRO) and names[mode.lower()].headway_min]
    if timed:
        info["note"] = f"{', '.join(timed)} riders ride the network's timetabled buses: they add no vehicles of their own."
    if METRO in alternatives:
        info["metro"] = {"door": station_door, "stations": stations, "places": len(attributes),
                         "rides": [{"from": station, **{k: round(v, 2) if isinstance(v, float) else v
                                                        for k, v in trip.items()}} for station, trip in rides.items()],
                         "note": (f"The stations of its door station's line inside its circle are places its people "
                                  f"come from, as the junctions are; its door station is the nearest within "
                                  f"{METRO_WALK_M:g} m. Riders add no vehicles." if station_door else
                                  f"No station within {METRO_WALK_M:g} m of it: nobody comes by MRT.")}
    return percent, info


def mode_plan(network: Network, pairs: list, args: dict, modes: list, values: dict, trips=None,
              current: dict | None = None) -> tuple[dict, dict]:
    """choose_modes' split, nothing changed: per pair with demand ({pair: value} in `values`), each
    alternative's utility U = constant + β_ivt·IVT + β_ovt·OVT + β_cost·cost, its people share by
    multinomial logit, and the vehicle split over the untimetabled types (people ÷ occupancy). IVT is
    the mean trip time of that pair and type in `trips` (a run's (origin, destination, type, trip s, delay s)),
    else the free-flow time along the shortest route at min(type's speed, limit); OVT is the out-of-vehicle
    minutes given plus half the type's headway (random arrival at a timetabled stop). {pair: {type: share}}.
    With `current` ({pair: {type: share}}, the split the pair has now), each pair's people (its vehicles × their
    mean occupancy now) ride the new split, so its vehicles change: info["values"] {pair: veh/h or trips/day}. A
    shift to fuller vehicles then means fewer of them (with the vehicles kept, a shift to the bus left car traffic
    as it was: audit 2026-10-13, M6); people on a timetabled type add no vehicles (its runs are fixed)."""
    alternatives = _parsed_json(args.get("alternatives")) if isinstance(args.get("alternatives"), str) else args.get("alternatives")
    names = {mode.name.lower(): mode for mode in modes}
    if not isinstance(alternatives, list) or len(alternatives) < 2 or not all(isinstance(a, dict) for a in alternatives):
        raise ValueError("Give 2 or more alternatives like [{\"mode\": \"Car\", \"cost_per_km\": 30}, "
                         f"{{\"mode\": \"Bus\", \"cost\": 150}}]; types: {', '.join(m.name for m in modes)}.")
    chosen = [str(a.get("mode", "")).strip().lower() for a in alternatives]
    unknown = [name for name in chosen if name not in names]
    if unknown or len(set(chosen)) != len(chosen):
        raise ValueError(f"Each alternative is a different vehicle type: {', '.join(m.name for m in modes)}"
                         + (f" (no {', '.join(unknown)})." if unknown else "."))
    coefficients = {**LOGIT_DEFAULTS, **(args.get("coefficients") or {})}
    if set(coefficients) != set(LOGIT_DEFAULTS) or any(not isinstance(v, (int, float)) or v > 0 for v in coefficients.values()):
        raise ValueError(f"coefficients are {', '.join(LOGIT_DEFAULTS)}, each 0 or below (more time or cost is worse).")
    for alternative in alternatives:
        for key in ("cost", "cost_per_km", "out_of_vehicle_min"):
            if float(alternative.get(key) or 0) < 0:
                raise ValueError(f"{alternative['mode']}: {key} can't be below 0.")
    nests = {}
    for nest in (_parsed_json(args["nests"]) if isinstance(args.get("nests"), str) else args.get("nests")) or []:
        members = [str(m).strip().lower() for m in (nest.get("modes") or [])] if isinstance(nest, dict) else []
        scale = nest.get("scale", 1.0) if isinstance(nest, dict) else None
        if len(members) < 2 or any(m not in chosen for m in members) or not isinstance(scale, (int, float)) \
                or not 0 < scale <= 1 or any(m in {x for v, _ in nests.values() for x in v} for m in members):
            raise ValueError("Each nest is {\"name\": ..., \"modes\": [2 or more of the alternatives, in no other "
                             "nest], \"scale\": over 0 and at most 1}.")
        nests[str(nest.get("name") or len(nests) + 1)] = ([names[m].name for m in members], float(scale))
    if all(names[name].headway_min for name in chosen):
        raise ValueError("Every alternative runs to a timetable, so its vehicles are fixed: include an untimetabled type.")
    scope = [pair for pair in pairs if values.get(pair)]
    if not scope:
        raise ValueError("Those pairs have no demand of the kind shown: enter some first (set_od_demand or generate_demand).")
    measured: dict = defaultdict(list)
    for origin, destination, kind, trip_s, *_ in trips or ():
        measured[(origin, destination, kind)].append(trip_s)
    routes = _routes(network, scope)
    plan, rows, elasticities, new_values = {}, [], defaultdict(lambda: {"cost": [], "in_vehicle": []}), {}
    for pair in scope:
        if pair not in routes:
            continue
        roads = [network.road(road_id) for road_id in routes[pair]]
        km = sum(road.length_m for road in roads) / 1000
        utilities, sources, attributes = {}, set(), {}
        for alternative, name in zip(alternatives, chosen):
            mode = names[name]
            times = measured.get((*pair, mode.name))
            if times:
                ivt = sum(times) / len(times) / 60
                sources.add("run")
            else:
                top = min(mode.max_speed_kmh, mode.desired_max_kmh)
                ivt = sum(road.length_m / (min(top, road.speed_limit_kmh) / 3.6) for road in roads) / 60
                sources.add("free flow")
            ivt = _cycled(mode, roads, ivt)
            ovt = float(alternative.get("out_of_vehicle_min") or 0) + mode.headway_min / 2
            cost = float(alternative.get("cost") or 0) + float(alternative.get("cost_per_km") or 0) * km
            attributes[mode.name] = (ivt, cost)
            utilities[mode.name] = (float(alternative.get("constant") or 0) + coefficients["in_vehicle_per_min"] * ivt
                                    + coefficients["out_of_vehicle_per_min"] * ovt + coefficients["cost"] * cost)
        people, top = stats.nested_logit(utilities, nests)
        for name, share in people.items():  # direct point elasticities, β·x·(1 − P) (the logit form; nests: approximate)
            ivt, cost = attributes[name]
            elasticities[name]["cost"].append(coefficients["cost"] * cost * (1 - share))
            elasticities[name]["in_vehicle"].append(coefficients["in_vehicle_per_min"] * ivt * (1 - share))
        vehicles = {name: share / max(getattr(names[name.lower()], "occupancy", 1.0), 1e-9)
                    for name, share in people.items() if not names[name.lower()].headway_min}
        total = sum(vehicles.values())
        plan[pair] = {name: share / total for name, share in vehicles.items()}
        if current and current.get(pair):  # people now: vehicles × mean occupancy; then the vehicles they need
            occupancy = sum(share * getattr(names[name.lower()], "occupancy", 1.0)
                            for name, share in current[pair].items() if name.lower() in names)
            new_values[pair] = values[pair] * occupancy * total
        rows.append({"pair": f"{pair[0]}->{pair[1]}", "km": round(km, 2), "times_from": " and ".join(sorted(sources)),
                     "logsum": round(top, 3),
                     "people_percent": {name: round(100 * share, 1) for name, share in people.items()},
                     "vehicle_percent": {name: round(100 * share, 1) for name, share in plan[pair].items()}})
    if not plan:
        raise ValueError("None of those pairs has a route: nothing to choose between.")
    info = {"method": "multinomial logit, U = constant + in_vehicle_per_min·IVT + out_of_vehicle_per_min·OVT + cost·cost "
                      "(HCM 2000's defaults unless coefficients say); people shares to vehicle shares by occupancy",
            "coefficients": coefficients, "pairs_split": len(plan), "pairs": rows[:12],
            "model": "nested logit" if nests else "multinomial logit",
            "elasticities": {name: {key: round(sum(v) / len(v), 3) for key, v in found.items()}
                             for name, found in elasticities.items()}}
    if coefficients["cost"]:
        info["value_of_time_per_hour"] = round(60 * coefficients["in_vehicle_per_min"] / coefficients["cost"], 1)  # cents
    timed = [names[name].name for name in chosen if names[name].headway_min]
    if timed:
        info["note"] = (f"{', '.join(timed)} run to a timetable: their vehicles are fixed by the headway, so their people "
                        "share is reported but the split goes to the other types.")
    if len(rows) > 12:
        info["more_pairs"] = len(rows) - 12
    if new_values:
        info["values"] = new_values
        info["vehicles_before"], info["vehicles_after"] = (round(sum(values[p] for p in new_values)),
                                                           round(sum(new_values.values())))
    return plan, info


def _area_closure(network: Network, flows: dict, ahead: dict, area: dict) -> dict:
    """critical_links' area closure: the roads touching the circle closed together; extra veh-h and veh/h cut off."""
    try:
        x, y, radius = float(area["x"]), float(area["y"]), float(area["radius_m"])
    except (KeyError, TypeError, ValueError):
        raise ValueError("area is {\"x\": metres, \"y\": metres, \"radius_m\": metres}.") from None
    if not 10 <= radius <= 5000:
        raise ValueError("area radius_m must be 10 to 5000.")
    closed = {road.id for road in network.all_roads()
              if any(hypot(px - x, py - y) <= radius for px, py in network.road_points(road))
              or any(distance_to_segment((x, y), a, b) <= radius
                     for a, b in zip(network.road_points(road), network.road_points(road)[1:]))}
    if not closed:
        raise ValueError("No road touches that circle.")

    def times(origin, shut):
        best, todo = {origin: 0.0}, [(0.0, origin)]
        while todo:
            spent, here = heapq.heappop(todo)
            if spent > best[here]:
                continue
            for there, step, road_id in ahead[here]:
                if road_id not in shut and spent + step < best.get(there, float("inf")):
                    best[there] = spent + step
                    heapq.heappush(todo, (spent + step, there))
        return best

    extra = lost = 0.0
    for origin in {o for o, _ in flows}:
        before, after = times(origin, set()), times(origin, closed)
        for (o, d), q in flows.items():
            if o != origin or d not in before:
                continue
            if d in after:
                extra += q * (after[d] - before[d]) / 3600
            else:
                lost += q
    return {"area": {"x": x, "y": y, "radius_m": radius}, "roads_closed": sorted(closed),
            "extra_veh_h_per_h": round(extra, 2), "cut_off_vph": round(lost),
            "note": "Every road touching the circle closed at once, the demand re-routed at free flow (static). Test it "
                    "in a run with incidents on those roads (run_experiment)."}


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


def cut_elements(network: Network) -> dict:
    """The network's single points of failure (Tarjan's bridges and articulation points on the undirected road
    graph; Taylor, Vulnerability Analysis for Transportation, Ch 3): the roads and junctions whose loss splits it,
    and what each cuts off (the junctions and boundary ends left on the smaller side)."""
    around: dict = defaultdict(list)
    for road in network.all_roads():
        around[road.from_node].append((road.to_node, road.id))
        around[road.to_node].append((road.from_node, road.id))
    order, low, bridges, joints, counter = {}, {}, [], set(), [0]

    def visit(root: str) -> None:  # iterative depth-first search (no recursion limit on big networks)
        order[root] = low[root] = counter[0]
        counter[0] += 1
        stack, children = [(root, None, iter(around[root]))], {root: 0}
        while stack:
            node, via, edges = stack[-1]
            advanced = False
            for there, road in edges:
                if road == via:
                    continue
                if there not in order:
                    order[there] = low[there] = counter[0]
                    counter[0] += 1
                    children[node] = children.get(node, 0) + 1
                    stack.append((there, road, iter(around[there])))
                    advanced = True
                    break
                low[node] = min(low[node], order[there])
            if advanced:
                continue
            stack.pop()
            if stack:
                parent = stack[-1][0]
                low[parent] = min(low[parent], low[node])
                if low[node] > order[parent]:
                    bridges.append(via)
                if parent != root and low[node] >= order[parent]:
                    joints.add(parent)
        if children.get(root, 0) > 1:
            joints.add(root)

    for node in [node.id for node in network.all_nodes()]:
        if node not in order:
            visit(node)

    def cut_off(closed_road=None, closed_node=None) -> list[str]:
        nodes = [node.id for node in network.all_nodes() if node.id != closed_node]
        seen, parts = set(), []
        for start in nodes:
            if start in seen:
                continue
            part, todo = {start}, [start]
            while todo:
                for there, road in around[todo.pop()]:
                    if road != closed_road and there != closed_node and there not in part:
                        part.add(there)
                        todo.append(there)
            seen |= part
            parts.append(part)
        main = max(parts, key=len)  # all that is split away from the main part
        return sorted(node for part in parts if part is not main for node in part)

    real = [road for road in bridges if road not in network.approach_roads]  # an end's own road always is one
    junctions = []
    for node in sorted(joints):
        cut = cut_off(closed_node=node) if node in network.nodes else []
        if any(other in network.nodes for other in cut):  # more than its own boundary ends
            junctions.append({"junction": node, "cuts_off": cut[:12]})
    return {"bridge_roads": [{"road": road, "cuts_off": cut_off(closed_road=road)[:12]} for road in real],
            "cut_junctions": junctions,
            "note": "Bridge roads and cut junctions split the network when lost; a junction cutting off only its own "
                    "boundary ends isn't listed (every edge junction does)."}


def critical_links_plan(network: Network, flows: dict, limit: int = 10, area: dict | None = None) -> dict:
    """Vulnerability by closing each used road in turn (Taylor, Vulnerability Analysis for Transportation,
    p107–112, p204): each OD pair's free-flow time at the speed limits (one-way roads kept) with and without the
    road; the extra vehicle-hours an hour, Σ q·Δt, and the veh/h cut off. Then the single points of failure
    (cut_elements). `area` {"x", "y", "radius_m"}: every road touching that circle closed at once instead (a flood,
    a fire, an event: Taylor Ch 6, area-wide disruption)."""
    ahead = defaultdict(list)
    for road in network.all_roads():
        seconds = road.length_m / max(road.speed_limit_kmh, 1) * 3.6
        ahead[road.from_node].append((road.to_node, seconds, road.id))
        if road.direction == "two-way":
            ahead[road.to_node].append((road.from_node, seconds, road.id))

    def tree(origin, closed=None):
        best, via, todo = {origin: 0.0}, {}, [(0.0, origin)]
        while todo:
            spent, here = heapq.heappop(todo)
            if spent > best[here]:
                continue
            for there, step, road_id in ahead[here]:
                if road_id != closed and spent + step < best.get(there, float("inf")):
                    best[there], via[there] = spent + step, (here, road_id)
                    heapq.heappush(todo, (spent + step, there))
        return best, via

    flows = {pair: q for pair, q in flows.items() if q > 0}
    if not flows:
        raise ValueError("There's no demand to route: enter some first.")
    if area is not None:
        return _area_closure(network, flows, ahead, area)
    base, used = {}, defaultdict(list)
    for origin in {o for o, _ in flows}:
        best, via = tree(origin)
        for (o, d), q in flows.items():
            if o == origin and d in best:
                base[(o, d)] = best[d]
                node = d
                while node != o:
                    node, road_id = via[node]
                    used[road_id].append((o, d))
    rows = []
    for road_id, pairs in used.items():
        if road_id in network.approach_roads:  # an end's own road: closing it only cuts that end off
            continue
        extra = lost = 0.0
        for origin in {o for o, _ in pairs}:
            best, _ = tree(origin, road_id)
            for o, d in pairs:
                if o != origin:
                    continue
                if d in best:
                    extra += flows[(o, d)] * (best[d] - base[(o, d)]) / 3600
                else:
                    lost += flows[(o, d)]
        rows.append({"road": road_id, "pairs_using": len(pairs), "extra_veh_h_per_h": round(extra, 2),
                     "cut_off_vph": round(lost)})
    rows.sort(key=lambda row: (-row["cut_off_vph"], -row["extra_veh_h_per_h"]))
    return {"roads_tested": len(rows), "critical": rows[:max(1, min(_int(limit), 50))], **cut_elements(network),
            "note": "Free-flow times at the speed limits, no congestion; boundary roads left out (closing one only "
                    "cuts off its end). Test the worst with run_experiment, a scenario deleting the road (delete_objects)."}


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

"""The assistant's tools that read and change roads, junctions and signals (no Qt).

List and describe them, add, move and delete them, update lanes and speeds, build interchanges and slip roads, set
signal timing, and read the project's source; moved out of network_tools.py, which re-exports them.
"""

from __future__ import annotations

import json
from math import hypot
from pathlib import Path

from .design_checks import roads_at, tightest
from .interchanges import (build_diamond, build_slip_road, build_trumpet, curve_speed_kmh, fit_slip_roads, guess_stem,
                           pair_legs)
from .models import SIGNAL_METHODS, Network, Road
from .roundabouts import MAX_ENTRY_LANES, design_roundabout
from .tool_args import ToolError, _bool, _ids, _int, _limited
from .tool_schemas import CONTROLS, CYCLE_LANE_SIDES, FOOTPATH_SIDES


ROOT = Path(__file__).resolve().parents[2]
READ_LINES = 150  # lines per read_source call at most...
READ_CHARS = 6000  # ...and characters: sources.md's table rows are long (150 of its lines were 63k characters)
FIND_HITS = 25  # matching lines a read_source find returns


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

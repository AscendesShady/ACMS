"""A road network made from the background (no Qt): OpenStreetMap's own road data under the map, or the
roads traced from a background image. The AI Mode tool network_tools.network_from_background uses it.

- The map: the roads come from OSM's Overpass API as data, not from the tiles' pixels: every drivable
  way in the area (`CLASSES`), with its lanes, speed limit and one-way tags. A node tagged
  highway=traffic_signals makes the junction it stands at signalised when it is a junction's signal
  (`JUNCTION_SIGNALS`; a pedestrian crossing's, an emergency or flashing signal, a ramp meter... leave the
  junction a priority one, counted in `Graph.skipped`), a junction=roundabout ring (or a
  mini-roundabout node) becomes one roundabout junction, and a bridge an overpass. Positions go
  through the map's georeference, so the roads lie on the map's streets.
- An image: the pixels within a tolerance of the road colour, specks dropped, thinned to one-pixel
  centre lines (T. Y. Zhang and C. Y. Suen, "A fast parallel algorithm for thinning digital patterns",
  Comm. ACM 27(3), 1984), traced into a graph (junctions where lines meet, thinning spurs pruned,
  lines simplified by Douglas-Peucker); the road width, and so the lanes, is the road pixels over the
  centre lines' length.
- Both give a `Graph`, and `build` turns it into a Network: junctions within MERGE_M merged into one,
  the roads between them with their shapes, boundary ends where roads leave the area.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from math import floor, hypot
from pathlib import Path

from .map_tiles import USER_AGENT
from .models import LANE_WIDTH_M, Network

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
# Tried in turn when one is busy (a timeout, a 5xx): public Overpass servers listed on the OSM wiki.
OVERPASS_URLS = (OVERPASS_URL, "https://overpass.kumi.systems/api/interpreter",
                 "https://overpass.private.coffee/api/interpreter")
OVERPASS_RETRY_S = 5.0  # all busy (504s and timeouts come in bursts): once more round them after this pause
# Seconds a server may take (the query asks it for 5 fewer). At 90, six tries took a live build 6.5 minutes when
# every server was down; a 1.2 km area answers in about 10 s.
OVERPASS_TIMEOUT_S = 40
# Each answer kept here by its query (the same area and size): when no server answers, the last copy is used and
# said to be one (a live Dhanmondi test met all three servers down, twice each, in two of four runs). A copy
# younger than OSM_FRESH_DAYS is used without asking (roads change slowly; delete the folder for a fresh download).
OSM_CACHE = Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".cache") / "ACMS" / "osm-roads"
OSM_FRESH_DAYS = 7
# OSM highway=* taken as a road: its speed limit (km/h) and lanes per direction when its tags don't say.
CLASSES = {
    "motorway": (100, 2), "trunk": (80, 2), "primary": (60, 1), "secondary": (50, 1), "tertiary": (50, 1),
    "motorway_link": (60, 1), "trunk_link": (50, 1), "primary_link": (50, 1), "secondary_link": (40, 1),
    "tertiary_link": (40, 1), "unclassified": (40, 1), "residential": (30, 1), "living_street": (20, 1),
}
LEVELS = {"main": tuple(name for name in CLASSES if not name.startswith(("unclassified", "residential", "living"))),
          "all": tuple(CLASSES)}
# traffic_signals=* values of a signal controlling a road junction (none given counts too). Everything else
# isn't one: crossing and pedestrian_crossing (a pedestrian crossing's only), emergency (dark until a fire
# engine leaves), blinker (flashing amber: priority rules apply), ramp_meter, level_crossing, lane (lane
# control), bus_priority, tram_priority and on_demand (dark or green until called), no.
JUNCTION_SIGNALS = ("signal", "traffic_lights", "yes", "junction")
# Junctions nearer than this become one: a dual carriageway's crossing is mapped as 2-4 nodes 12-19 m apart (Dhanmondi:
# three signals with their own plans, and four give-way nodes in a loop), with 2-3 cars of room between them, which
# gridlocked the network. At 12 m, a quarter of a peak demand jammed (130 vehicles stuck); at 20 m it held, none
# stuck. SUMO's netconvert joins such clusters too (junctions.join, tls.join-dist 20 m).
MERGE_M = 20.0
SIGNAL_M = 30.0  # a traffic signal node this near a junction signalises it
MAX_JUNCTIONS = 800  # more is too big a network to edit and run; a smaller area or main roads only
# A road leaving the area becomes an entry road at least this long: clipped just past a junction it was a stub of
# 5-15 m that held one car, so traffic waited off the network and a check called any real demand a jam (Dhanmondi)
ENTRY_ROAD_M = 60.0
MPH = 1.609344


@dataclass
class Street:
    """A road between two graph nodes: its line in scene metres, from start's position to end's."""
    start: object
    end: object
    points: list
    lanes: int = 1  # per direction
    speed_kmh: float = 50.0
    one_way: bool = False  # traffic runs start → end
    bridge: bool = False
    kind: str = "urban"  # the road type label
    ring: bool = False  # part of a roundabout's ring: the ring becomes one junction


@dataclass
class Graph:
    positions: dict  # node key -> (x, y), scene metres
    streets: list
    border: set = field(default_factory=set)  # keys on the area's edge: they become boundary ends
    signals: list = field(default_factory=list)  # (x, y) of junction traffic signals
    skipped: dict = field(default_factory=dict)  # other signals, by kind: they don't signalise a junction
    rings: list = field(default_factory=list)  # sets of keys, each one roundabout
    lanes_note: str = ""


# --- OpenStreetMap ---------------------------------------------------------------------------------

def overpass_query(south: float, west: float, north: float, east: float, classes: tuple) -> str:
    """Every drivable way of these classes in the box, with all their nodes (tags included)."""
    return (f'[out:json][timeout:{OVERPASS_TIMEOUT_S - 5}];way["highway"~"^({"|".join(classes)})$"]["area"!="yes"]'
            f'["access"!~"^(no|private)$"]({south:.6f},{west:.6f},{north:.6f},{east:.6f});(._;>;);out body;')


def post(url: str, data: bytes) -> bytes:
    """The network (tests put a stand-in here): an identifying User-Agent, as OSM's services ask."""
    request = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=OVERPASS_TIMEOUT_S) as response:
        return response.read()


def fetch_osm(query: str) -> dict:
    """The roads' data: this very query's copy if younger than OSM_FRESH_DAYS, else from the first Overpass server
    that answers, else the older copy. A copy says so (`cached_at`, `cached_why`)."""
    kept = OSM_CACHE / f"{hashlib.sha256(query.encode()).hexdigest()[:32]}.json"

    def copy(why: str) -> dict | None:
        try:
            document = json.loads(kept.read_text(encoding="utf-8"))
            document["cached_at"] = time.strftime("%Y-%m-%d %H:%M", time.localtime(kept.stat().st_mtime))
            document["cached_why"] = why
            return document
        except (OSError, ValueError, TypeError):
            return None

    try:
        fresh = time.time() - kept.stat().st_mtime < OSM_FRESH_DAYS * 86400
    except OSError:
        fresh = False
    document = copy(f"downloaded in the last {OSM_FRESH_DAYS} days") if fresh else None
    if document is not None:
        return document
    failures = []
    for attempt, url in enumerate(OVERPASS_URLS * 2):  # every server, then (after a pause) every one again
        if attempt == len(OVERPASS_URLS):
            time.sleep(OVERPASS_RETRY_S)
        try:
            document = json.loads(post(url, urllib.parse.urlencode({"data": query}).encode()))
            break
        except OSError as exc:  # busy, offline or refused: the next server
            failures.append(f"{urllib.parse.urlparse(url).netloc}: {exc}")
        except ValueError as exc:
            raise ValueError("OpenStreetMap's Overpass API sent an answer that couldn't be read.") from exc
    if document is None:  # the last answer for this very area, said to be a copy and how old
        document = copy("OpenStreetMap's servers didn't answer")
    elif isinstance(document, dict) and isinstance(document.get("elements"), list):
        try:
            kept.parent.mkdir(parents=True, exist_ok=True)
            kept.write_text(json.dumps(document), encoding="utf-8")
        except OSError:
            pass  # no cache: the next outage just can't fall back
    if document is None:
        raise ValueError(f"Couldn't reach OpenStreetMap's Overpass API ({'; '.join(failures[-len(OVERPASS_URLS):])}, "
                         "each tried twice). Try again in a minute.")
    if not isinstance(document, dict) or not isinstance(document.get("elements"), list):
        raise ValueError("OpenStreetMap's Overpass API sent no road data.")
    return document


def _speed(text) -> float | None:
    match = re.match(r"\s*(\d+(?:\.\d+)?)\s*(mph)?", str(text or ""))
    if not match:
        return None
    value = float(match.group(1)) * (MPH if match.group(2) else 1.0)
    return round(value) if 5 <= value <= 150 else None


def _attributes(tags: dict) -> tuple[dict, bool]:
    """A way's road settings, and whether it runs against its node order (oneway=-1)."""
    kind = tags["highway"]
    speed, lanes = CLASSES[kind]
    ring = tags.get("junction") in ("roundabout", "circular")
    one_way = tags.get("oneway") in ("yes", "true", "1", "-1") or ring or \
        (kind in ("motorway", "motorway_link") and tags.get("oneway") != "no")
    try:
        total = int(str(tags.get("lanes", "")).split(";")[0])
    except ValueError:
        total = 0
    if total > 0:  # a one-way road's carriageway is two lanes per direction wide in the editor
        lanes = max(1, (total + 1) // 2) if one_way else max(1, total // 2)
    return {"lanes": min(lanes, 6), "speed_kmh": _speed(tags.get("maxspeed")) or speed, "one_way": one_way,
            "bridge": tags.get("bridge") not in (None, "no"), "kind": kind, "ring": ring}, tags.get("oneway") == "-1"


def _liang_barsky(a, b, rect) -> tuple[float, float] | None:
    """The part of segment a-b inside the rectangle, as (t0, t1) along it, or None."""
    left, top, right, bottom = rect
    dx, dy = b[0] - a[0], b[1] - a[1]
    t0, t1 = 0.0, 1.0
    for p, q in ((-dx, a[0] - left), (dx, right - a[0]), (-dy, a[1] - top), (dy, bottom - a[1])):
        if p == 0:
            if q < 0:
                return None
        elif p < 0:
            t0 = max(t0, q / p)
        else:
            t1 = min(t1, q / p)
    return (t0, t1) if t0 <= t1 else None


def _clip(chain: list, rect, graph: Graph, tag) -> list[list]:
    """A way's (key, point) chain cut to the rectangle: its pieces inside, as keys; where one crosses
    the edge a border key is made there."""
    pieces, current = [], []

    def border(point, index, side):
        key = ("edge", tag, index, side)
        graph.positions[key] = point
        graph.border.add(key)
        return key

    for index, ((ka, a), (kb, b)) in enumerate(zip(chain, chain[1:])):
        span = _liang_barsky(a, b, rect)
        if span is None:
            continue
        t0, t1 = span
        if not current:
            current = [border((a[0] + (b[0] - a[0]) * t0, a[1] + (b[1] - a[1]) * t0), index, 0) if t0 > 0 else ka]
        if t1 < 1:
            current.append(border((a[0] + (b[0] - a[0]) * t1, a[1] + (b[1] - a[1]) * t1), index, 1))
            pieces.append(current)
            current = []
        else:
            current.append(kb)
    if len(current) > 1:
        pieces.append(current)
    return [piece for piece in pieces if len(piece) > 1]


def osm_graph(document: dict, to_scene, rect) -> Graph:
    """The roads of an Overpass answer inside `rect` (left, top, right, bottom, scene metres);
    `to_scene(lat, lon)` places a point."""
    nodes = {element["id"]: element for element in document["elements"] if element.get("type") == "node"}
    ways = [element for element in document["elements"] if element.get("type") == "way"
            and element.get("tags", {}).get("highway") in CLASSES]
    graph = Graph({}, [])
    for osm_id, node in nodes.items():
        graph.positions[osm_id] = to_scene(node["lat"], node["lon"])
        tags = node.get("tags", {})
        if tags.get("highway") == "traffic_signals":
            kind = str(tags.get("traffic_signals", "signal")).strip().lower()
            if kind in JUNCTION_SIGNALS:
                graph.signals.append(graph.positions[osm_id])
            else:
                graph.skipped[kind] = graph.skipped.get(kind, 0) + 1
        if tags.get("highway") == "mini_roundabout":
            graph.rings.append({osm_id})
    pieces = []
    for way in ways:
        refs = [ref for ref in way.get("nodes", []) if ref in nodes]
        if len(refs) < 2:
            continue
        attributes, backwards = _attributes(way["tags"])
        if backwards:
            refs.reverse()
        if attributes["ring"]:
            graph.rings.append(set(refs))
        for piece in _clip([(ref, graph.positions[ref]) for ref in refs], rect, graph, way["id"]):
            pieces.append((piece, attributes))
    uses = Counter(key for piece, _ in pieces for key in set(piece))
    junctions = {key for key, count in uses.items() if count > 1}
    junctions |= {piece[0] for piece, _ in pieces} | {piece[-1] for piece, _ in pieces}
    junctions |= {key for ring in graph.rings for key in ring if len(ring) == 1}  # mini-roundabouts
    for piece, attributes in pieces:
        start = 0
        for index in range(1, len(piece)):
            if piece[index] in junctions or index == len(piece) - 1:
                keys = piece[start:index + 1]
                graph.streets.append(Street(keys[0], keys[-1], [graph.positions[key] for key in keys], **attributes))
                start = index
    return graph


# --- An image --------------------------------------------------------------------------------------

def raster_graph(width: int, height: int, mask, to_scene, metres_per_px: float, min_area_px: int = 30,
                 spur_px: int = 8) -> Graph:
    """The road network in a width × height mask (1 = road pixel, row by row); `to_scene(px, py)`
    places a pixel in scene metres. Lines reaching the image's edge become boundary ends."""
    stride = width + 2  # a border of empty pixels round the grid: neighbours never wrap
    grid = bytearray(stride * (height + 2))
    for y in range(height):
        grid[(y + 1) * stride + 1:(y + 1) * stride + 1 + width] = bytes(mask[y * width:(y + 1) * width])
    around = (-stride, -stride + 1, 1, stride + 1, stride, stride - 1, -1, -stride - 1)  # N, NE, E, ... NW
    on = {index for index, value in enumerate(grid) if value}
    seen: set[int] = set()
    for first in list(on):  # specks (text, noise) dropped
        if first in seen:
            continue
        part, todo = {first}, [first]
        while todo:
            here = todo.pop()
            for step in around:
                there = here + step
                if grid[there] and there not in part:
                    part.add(there)
                    todo.append(there)
        seen |= part
        if len(part) < min_area_px:
            for index in part:
                grid[index] = 0
            on -= part
    area = len(on)
    changed = True
    while changed:  # Zhang-Suen: peel the roads down to one-pixel centre lines
        changed = False
        for second in (False, True):
            peel = []
            for index in on:
                p = [grid[index + step] for step in around]
                if not 2 <= sum(p) <= 6 or sum(p[k] == 0 and p[(k + 1) % 8] == 1 for k in range(8)) != 1:
                    continue
                if second:
                    if p[0] * p[2] * p[6] or p[0] * p[4] * p[6]:
                        continue
                elif p[0] * p[2] * p[4] or p[2] * p[4] * p[6]:
                    continue
                peel.append(index)
            for index in peel:
                grid[index] = 0
            on.difference_update(peel)
            changed = changed or bool(peel)
    return _trace(grid, stride, width, height, on, around, to_scene, area, metres_per_px, spur_px)


def _trace(grid, stride, width, height, line, around, to_scene, area, metres_per_px, spur_px) -> Graph:
    """Centre-line pixels to a graph: node pixels (not exactly two neighbours) grouped where they touch,
    the lines between them traced, spurs pruned, bends (two lines) joined."""
    def neighbours(index):
        return [index + step for step in around if grid[index + step]]

    def xy(index):
        return index % stride - 1, index // stride - 1

    nodes = {index for index in line if len(neighbours(index)) != 2}
    group: dict[int, int] = {}
    centres: list[tuple[float, float]] = []

    def gather(first):
        members, todo = [first], [first]
        group[first] = len(centres)
        while todo:
            for there in neighbours(todo.pop()):
                if there in nodes and there not in group:
                    group[there] = group[first]
                    members.append(there)
                    todo.append(there)
        points = [xy(member) for member in members]
        centres.append((sum(x for x, _ in points) / len(points), sum(y for _, y in points) / len(points)))

    for index in nodes:
        if index not in group:
            gather(index)
    edges, used = [], set()

    def walk(start, first):
        path, previous, here = [start, first], start, first
        while here not in nodes:
            used.add(here)
            ahead = [there for there in neighbours(here) if there != previous and there not in used]
            if not ahead:
                return None
            here, previous = next((there for there in ahead if there in nodes), ahead[0]), here
            path.append(here)
        return path

    while True:  # lines from node to node, then loops with no node on them
        for index in list(nodes):
            for first in neighbours(index):
                if first not in nodes and first not in used:
                    path = walk(index, first)
                    if path is not None:
                        edges.append([group[path[0]], group[path[-1]], path])
        loose = next((index for index in line if index not in nodes and index not in used), None)
        if loose is None:
            break
        nodes.add(loose)  # a loop with no junction on it: one of its pixels becomes one
        gather(loose)

    margin = max(3.0, area / max(len(line), 1) / 2 + 2)  # thinning stops a line about half a road width short

    def at_edge(g):
        x, y = centres[g]
        return x < margin or y < margin or x > width - 1 - margin or y > height - 1 - margin

    def branches():
        count = Counter()
        for a, b, _ in edges:
            count[a] += 1
            count[b] += 1
        return count

    count = branches()  # thinning leaves short spurs off the lines: prune them
    edges = [edge for edge in edges if not (len(edge[2]) < spur_px and edge[0] != edge[1] and any(
        count[g] == 1 and not at_edge(g) for g in edge[:2]) and max(count[edge[0]], count[edge[1]]) > 1)]
    joined = True
    while joined:  # a node on only two lines is a bend: one line through it
        joined, count = False, branches()
        for g, n in count.items():
            if n != 2 or at_edge(g):
                continue
            two = [edge for edge in edges if g in edge[:2]]
            if len(two) != 2 or two[0] is two[1]:
                continue
            first, second = two
            a_path = first[2] if first[1] == g else first[2][::-1]
            b_path = second[2] if second[0] == g else second[2][::-1]
            a_start = first[0] if first[1] == g else first[1]
            b_end = second[1] if second[0] == g else second[0]
            edges = [edge for edge in edges if edge is not first and edge is not second]
            edges.append([a_start, b_end, a_path + b_path[1:]])
            joined = True
            break
    # Pieces of line too short to be a road on their own (a label's white halo, a courtyard) go: under 60 m.
    shortest, joined_to = max(20.0, 60.0 / metres_per_px), {}
    for a, b, _ in edges:
        joined_to.setdefault(a, set()).add(b)
        joined_to.setdefault(b, set()).add(a)
    part_of, lengths = {}, Counter()
    for first in joined_to:
        if first in part_of:
            continue
        part_of[first], todo = first, [first]
        while todo:
            for there in joined_to[todo.pop()]:
                if there not in part_of:
                    part_of[there] = first
                    todo.append(there)
    for a, _, path in edges:
        lengths[part_of[a]] += len(path)
    edges = [edge for edge in edges if lengths[part_of[edge[0]]] >= shortest]
    skeleton = sum(len(path) for _, _, path in edges) or 1
    width_m = area / skeleton * metres_per_px
    lanes = max(1, min(4, round(width_m / (2 * LANE_WIDTH_M))))
    graph = Graph({g: to_scene(*centres[g]) for g in range(len(centres))}, [],
                  lanes_note=f"roads about {width_m:.0f} m wide: {lanes} lane{'s' if lanes > 1 else ''} each way")
    count = branches()
    for a, b, path in edges:
        points = _simplify([centres[a]] + [xy(index) for index in path[1:-1]] + [centres[b]], 1.0)
        graph.streets.append(Street(a, b, [to_scene(x, y) for x, y in points], lanes=lanes, kind="traced"))
    graph.border = {g for g in range(len(centres)) if count[g] == 1 and at_edge(g)}
    for g in graph.border:  # out to the edge itself: the boundary end sits there
        x, y = centres[g]
        gaps = {(0.0, y): x, (width - 1.0, y): width - 1 - x, (x, 0.0): y, (x, height - 1.0): height - 1 - y}
        graph.positions[g] = to_scene(*min(gaps, key=gaps.get))
        for street in graph.streets:
            if street.start == g:
                street.points[0] = graph.positions[g]
            if street.end == g:
                street.points[-1] = graph.positions[g]
    return graph


def _simplify(points: list, tolerance: float) -> list:
    """Douglas-Peucker: the fewest points within `tolerance` of the line."""
    if len(points) < 3:
        return points
    (ax, ay), (bx, by) = points[0], points[-1]
    length = hypot(bx - ax, by - ay)
    far, index = -1.0, 0
    for i, (x, y) in enumerate(points[1:-1], 1):
        distance = abs((bx - ax) * (ay - y) - (ax - x) * (by - ay)) / length if length else hypot(x - ax, y - ay)
        if distance > far:
            far, index = distance, i
    if far <= tolerance:
        return [points[0], points[-1]]
    return _simplify(points[:index + 1], tolerance)[:-1] + _simplify(points[index:], tolerance)


# --- The network ----------------------------------------------------------------------------------

def _length(points) -> float:
    return sum(hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(points, points[1:]))


def _halves(street: Street, key) -> tuple[Street, Street]:
    """A street cut at the point half way along it (a new node `key` there)."""
    points, half, walked = street.points, _length(street.points) / 2, 0.0
    for index, (a, b) in enumerate(zip(points, points[1:])):
        step = hypot(b[0] - a[0], b[1] - a[1])
        if walked + step >= half and step > 0:
            t = (half - walked) / step
            middle = (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)
            first = Street(street.start, key, points[:index + 1] + [middle], street.lanes, street.speed_kmh,
                           street.one_way, street.bridge, street.kind)
            second = Street(key, street.end, [middle] + points[index + 1:], street.lanes, street.speed_kmh,
                            street.one_way, street.bridge, street.kind)
            return first, second
        walked += step
    raise ValueError("A road with no length.")


def build(template: Network, graph: Graph) -> Network:
    """A new network from the graph, with the template's settings and background. ValueError if it
    would be empty or too big."""
    parent = {key: key for key in graph.positions}

    def find(key):
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    def union(a, b):
        parent[find(a)] = find(b)

    streets = [street for street in graph.streets if street.start in parent and street.end in parent]
    ring_keys = set()
    for ring in graph.rings:  # a roundabout's ring (or a mini-roundabout) is one junction
        ring = [key for key in ring if key in parent]
        ring_keys.update(ring)
        for key in ring[1:]:
            union(ring[0], key)
    ends = {key for street in streets for key in (street.start, street.end) if key not in graph.border}
    cells = defaultdict(list)
    for key in ends:
        x, y = graph.positions[key]
        cells[(floor(x / MERGE_M), floor(y / MERGE_M))].append(key)
    for (cx, cy), keys in cells.items():  # junctions a few metres apart: one
        near = [other for dx in (-1, 0, 1) for dy in (-1, 0, 1) for other in cells.get((cx + dx, cy + dy), ())]
        for key in keys:
            x, y = graph.positions[key]
            for other in near:
                if other != key and hypot(graph.positions[other][0] - x, graph.positions[other][1] - y) < MERGE_M:
                    union(key, other)
    members = defaultdict(list)
    for key in parent:
        members[find(key)].append(key)
    rings = {find(key) for key in ring_keys}
    position = {}
    for root, keys in members.items():
        points = [graph.positions[key] for key in keys]
        position[root] = (sum(x for x, _ in points) / len(points), sum(y for _, y in points) / len(points))
    border = {find(key) for key in graph.border}
    kept = []
    for street in streets:
        if street.ring and find(street.start) in rings:
            continue  # the roundabout's own ring: the junction stands for it
        start, end = find(street.start), find(street.end)
        points = [position[start], *street.points[1:-1], position[end]]
        clean = [points[0]]
        for point in points[1:]:
            if hypot(point[0] - clean[-1][0], point[1] - clean[-1][1]) >= 0.5:
                clean.append(point)
        clean[-1] = points[-1]
        if start == end and _length(clean) < 4 * MERGE_M:
            continue  # squeezed into its junction
        if len(clean) < 2 or _length(clean) < 1.0:
            continue
        kept.append(Street(start, end, clean, street.lanes, street.speed_kmh, street.one_way, street.bridge, street.kind))
    streets = _join_bends(kept, border | rings, graph.signals, position)
    fresh = Network(horizontal_corridors=template.horizontal_corridors, vertical_corridors=template.vertical_corridors,
                    default_spacing=template.default_spacing, driving_side=template.driving_side,
                    default_speed_limit_kmh=template.default_speed_limit_kmh,
                    default_lanes_per_direction=template.default_lanes_per_direction,
                    default_lane_changing=template.default_lane_changing,
                    default_signal_method=template.default_signal_method)
    fresh.background = template.background
    ids: dict = {}
    count = Counter(key for street in streets for key in (street.start, street.end))
    if not streets:
        raise ValueError("No roads were found there.")
    inner = [key for key in count if key not in border]
    if len(inner) > MAX_JUNCTIONS:
        raise ValueError(f"That makes {len(inner)} junctions, more than {MAX_JUNCTIONS}: choose a smaller area, or "
                         "main roads only.")

    def junction(key):
        if key not in ids:
            x, y = position[key]
            node = fresh.add_node(x, y)
            node.control = "roundabout" if key in rings else "signalised" if count[key] >= 3 and any(
                hypot(sx - x, sy - y) <= SIGNAL_M for sx, sy in graph.signals) else "uncontrolled"
            ids[key] = node.id
        return ids[key]

    def dress(road, street, forward=True):
        road.lanes_per_direction, road.speed_limit_kmh, road.road_type = street.lanes, float(street.speed_kmh), street.kind
        road.overpass = street.bridge
        if street.one_way:
            road.direction = "one-way"
            if not forward:
                fresh.reverse_road(road.id)

    def order(street):  # the edge's roads last, in order along each side, so their ends are numbered 1, 2, 3...
        if street.start not in border and street.end not in border:
            return 0, "", 0.0
        far, near = (street.points[-1], street.points[-2]) if street.end in border else street.points[:2]
        across = abs(far[0] - near[0]) >= abs(far[1] - near[1])  # east or west (as add_end names it)
        return 1, "EW" if across else "NS", -far[1] if across else far[0]  # E/W up from the bottom, N/S from the left

    todo, extra = sorted(streets, key=order, reverse=True), 0  # pop() takes from the end: the first in order
    while todo:
        street = todo.pop()
        extra += 1
        inbound = False  # a one-way street whose traffic comes in from the edge
        if street.start in border and street.end not in border:  # turned so the edge is at its end
            street, inbound = Street(street.end, street.start, street.points[::-1], street.lanes, street.speed_kmh,
                                     street.one_way, street.bridge, street.kind), True
        if street.end in border and street.start in border or street.start == street.end:
            todo.extend(_halves(street, ("cut", extra)))  # through the area without a junction, or a loop
            position[("cut", extra)] = todo[-1].points[0]
            continue
        if street.end in border:
            inner, points = junction(street.start), street.points
            far = points[-1]
            mid = next((i for i in range(len(points) - 2, 0, -1)
                        if hypot(points[i][0] - far[0], points[i][1] - far[1]) >= 5.0
                        and hypot(points[i][0] - points[0][0], points[i][1] - points[0][1]) >= 2.0), None)
            if mid is not None:  # the bends up to near the edge, then the end
                corner = fresh.add_node(*points[mid])
                corner.control = "uncontrolled"
                road = fresh.add_road(inner, corner.id)
                road.shape = [tuple(point) for point in points[1:mid]]
                dress(road, street, not inbound)
                inner = corner.id
            stub = hypot(far[0] - fresh.nodes[inner].x, far[1] - fresh.nodes[inner].y)
            if stub < 1.0:
                continue
            if stub < ENTRY_ROAD_M:  # cut just past a junction: carried on out along its line, room to enter
                far = (fresh.nodes[inner].x + (far[0] - fresh.nodes[inner].x) * ENTRY_ROAD_M / stub,
                       fresh.nodes[inner].y + (far[1] - fresh.nodes[inner].y) * ENTRY_ROAD_M / stub)
            road = fresh.add_end(inner, *far)
            dress(road, street, not inbound)
            continue
        a, b = junction(street.start), junction(street.end)
        if fresh.connection_problem(a, b):  # already joined (two roads between the same junctions)
            todo.extend(_halves(street, ("cut", extra)))
            position[("cut", extra)] = todo[-1].points[0]
            continue
        road = fresh.add_road(a, b)
        road.shape = [tuple(point) for point in street.points[1:-1]]
        dress(road, street)
    for node_id in [node_id for node_id in fresh.nodes if not any(
            node_id in (road.from_node, road.to_node) for road in fresh.all_roads())]:
        del fresh.nodes[node_id]
    fresh.synchronize_road_lengths()
    return fresh


def _join_bends(streets: list, keep: set, signals: list, position: dict) -> list:
    """A node on only two streets alike (a bend, a change of way in OSM) goes: one street through it."""
    while True:
        at = defaultdict(list)
        for street in streets:
            at[street.start].append(street)
            at[street.end].append(street)
        for key, two in at.items():
            if len(two) != 2 or key in keep or two[0] is two[1]:
                continue
            first, second = two
            if (first.lanes, first.speed_kmh, first.one_way, first.bridge, first.kind) != \
                    (second.lanes, second.speed_kmh, second.one_way, second.bridge, second.kind):
                continue
            if first.end != key:
                if first.one_way:
                    continue
                first = Street(first.end, first.start, first.points[::-1], first.lanes, first.speed_kmh, False,
                               first.bridge, first.kind)
            if second.start != key:
                if second.one_way:
                    continue
                second = Street(second.end, second.start, second.points[::-1], second.lanes, second.speed_kmh, False,
                                second.bridge, second.kind)
            if first.start == second.end:
                continue  # it would close a loop on itself
            streets = [street for street in streets if street is not two[0] and street is not two[1]]
            streets.append(Street(first.start, second.end, first.points + second.points[1:], first.lanes,
                                  first.speed_kmh, first.one_way, first.bridge, first.kind))
            break
        else:
            return streets

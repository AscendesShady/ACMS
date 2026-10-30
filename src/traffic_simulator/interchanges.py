"""Grade-separated interchanges built from selected junctions.

The network is a flat plan: two roads that cross without sharing a node are
grade-separated (one passes over the other), so an interchange is ordinary
nodes and roads. Each layout is drawn in a local frame around the centre O:
x runs along the freeway towards its second junction, y runs across it.
Ramps are one-way roads on the side their traffic keeps to, so the same
layouts serve left- and right-hand driving.

Every check runs before anything is removed: a build that fails leaves the
network untouched.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from math import ceil, cos, dist, floor, hypot, radians, sin

from .geometry import distance_to_segment, point_along, polyline_length
from .models import LANE_WIDTH_M, Network, Node

FREEWAY_SPEED_KMH = 100.0
RAMP_SPEED_KMH = 60.0
LOOP_SPEED_KMH = 40.0
SLIP_ROOM_M = 40.0  # lane kept between a slip road's nodes and its junction: queues, and room to exit into
MARGIN_M = 40.0  # ordinary road kept between the interchange and each selected junction
# Minimum-radius relation R = V^2 / (127 (e + f)): 6 % superelevation, side friction 0.15
# (the AASHTO value near 60 km/h; conservative for slower ramps, which may use more).
SUPERELEVATION, SIDE_FRICTION = 0.06, 0.15


@dataclass
class _Frame:
    ox: float
    oy: float
    ux: float
    uy: float
    vx: float
    vy: float

    def at(self, x: float, y: float) -> tuple[float, float]:
        return self.ox + x * self.ux + y * self.vx, self.oy + x * self.uy + y * self.vy

    def along(self, node: Node) -> float:
        return (node.x - self.ox) * self.ux + (node.y - self.oy) * self.uy

    def across(self, node: Node) -> float:
        return (node.x - self.ox) * self.vx + (node.y - self.oy) * self.vy


class _Builder:
    """Adds one interchange's nodes (named e.g. DI1.1, DI1.2, ...) and roads."""

    def __init__(self, network: Network, prefix: str, frame: _Frame) -> None:
        self.network, self.frame = network, frame
        number = 1
        while any(node_id.startswith(f"{prefix}{number}.") for node_id in network.nodes):
            number += 1
        self.name, self.count = f"{prefix}{number}", 0
        network.interchanges[self.name] = [frame.ox, frame.oy, frame.ux, frame.uy]  # mirrored if the side changes

    def node(self, x: float, y: float, control: str = "uncontrolled") -> str:
        self.count += 1
        node_id = f"{self.name}.{self.count}"
        wx, wy = self.frame.at(x, y)
        self.network.nodes[node_id] = Node(node_id, wx, wy, None, None, control=control,
                                           signal_method=self.network.default_signal_method)
        return node_id

    def chain(self, node_ids: list[str], lanes: int, speed: float) -> None:
        """Two-way roads through consecutive nodes."""
        for a, b in zip(node_ids, node_ids[1:]):
            road = self.network.add_road(a, b)
            road.lanes_per_direction, road.speed_limit_kmh = lanes, speed

    def ramp(self, points: list, speed: float = RAMP_SPEED_KMH) -> None:
        """One-way ramp from its first to its last node; (x, y) points become bend nodes."""
        ids = [point if isinstance(point, str) else self.node(*point) for point in points]
        for a, b in zip(ids, ids[1:]):
            road = self.network.add_road(a, b)
            road.lanes_per_direction, road.speed_limit_kmh, road.direction = 1, speed, "one-way"
            road.road_type = "ramp"

    def curve(self, a: str, b: str, pieces: list[tuple[float, float, float]],
              speed: float = RAMP_SPEED_KMH) -> None:
        """One-way curved ramp from node a to node b.

        `pieces` are local (x, y, curvature) points ending at node b's position; each
        curvature belongs to the segment that ends at that point. The frame may mirror
        local space, which flips the sign of curvature.
        """
        road = self.network.add_road(a, b)
        road.lanes_per_direction, road.speed_limit_kmh, road.direction = 1, speed, "one-way"
        road.road_type = "ramp"
        frame = self.frame
        mirror = 1.0 if frame.ux * frame.vy - frame.uy * frame.vx > 0 else -1.0
        road.shape = [frame.at(x, y) for x, y, _ in pieces[:-1]]
        road.curvature = [kappa * mirror for *_, kappa in pieces]
        road.length_m = self.network.road_geometry_length(road)
        tightest = max(abs(kappa) for kappa in road.curvature)
        if tightest:
            road.speed_limit_kmh = min(speed, curve_speed_kmh(1 / tightest))


# Curve pieces, sampled about every 4 m. Each returns (x, y, curvature) for its
# points after the start, with the exact curvature of the segment ending there, so
# pieces chain and a bend's curvature can change sharply between pieces.
STEP_M = 4.0
CurvePoint = tuple[float, float, float]


def _arc(cx: float, cy: float, radius: float, start_deg: float, end_deg: float) -> list[CurvePoint]:
    """Circular arc from start to end angle (decreasing angles run clockwise): curvature
    is exactly 1/radius, positive when the angle increases."""
    steps = max(6, round(abs(radians(end_deg - start_deg)) * radius / STEP_M))
    kappa = (1 if end_deg > start_deg else -1) / radius
    return [(cx + radius * cos(radians(start_deg + (end_deg - start_deg) * i / steps)),
             cy + radius * sin(radians(start_deg + (end_deg - start_deg) * i / steps)), kappa)
            for i in range(1, steps + 1)]


def curve_speed_kmh(radius_m: float) -> float:
    """Highest speed (km/h, rounded down to 5) for a bend of this radius; under 15 km/h, the exact
    speed (rounding would reach 0, and a floor would be faster than the bend allows)."""
    speed = (127 * (SUPERELEVATION + SIDE_FRICTION) * radius_m) ** 0.5
    return 5 * floor(speed / 5) if speed >= 15 else speed


def _spiral(x0: float, radius: float, turn_deg: float) -> list[CurvePoint]:
    """Clockwise transition spiral (clothoid) leaving (x0, 0) along +x: curvature grows
    linearly with distance from 0 to 1/radius while turning `turn_deg`, so a straight
    joins a circle with no jump in curvature. Returned like the other pieces."""
    length = 2 * radius * radians(turn_deg)  # turning = length / (2 radius)
    steps = max(8, round(length / (STEP_M * 0.75)))
    substeps, segment = 20, length / steps
    x, y, walked, points = x0, 0.0, 0.0, []
    for _ in range(steps):
        for _ in range(substeps):  # integrate the heading, -s^2 / (2 R L), by midpoints
            middle = walked + segment / substeps / 2
            angle = -middle * middle / (2 * radius * length)
            x += segment / substeps * cos(angle)
            y += segment / substeps * sin(angle)
            walked += segment / substeps
        points.append((x, y, -(walked - segment / 2) / (radius * length)))
    return points


def _loop_entry(radius: float, half_width: float) -> tuple[float, float, float, list[CurvePoint]]:
    """(start x, loop centre depth, spiral turn, spiral) for a trumpet loop whose
    centre sits at x = radius, so it leaves the loop heading +y at x = 0. The spiral
    turns just enough to clear the freeway edge before the circle, while starting
    past the bridge at x = 0."""
    best = None
    for turn in range(30, 81, 2):  # a tight site keeps the gentlest, 30 degrees
        spiral = _spiral(0.0, radius, turn)
        end_x, end_y, _ = spiral[-1]
        heading = radians(-turn)
        centre_x, centre_y = end_x + radius * sin(heading), end_y - radius * cos(heading)
        start = radius - centre_x
        if start < half_width + 8 and best is not None:
            break  # any more turning would start the exit before the bridge
        best = (start, -centre_y, turn, [(x + start, y, kappa) for x, y, kappa in spiral])
        if -end_y >= half_width + 6:
            break  # clear of the freeway edge
    return best


def _bell_join(radius: float, ring: float, depth: float, bridge_y: float) -> tuple[int, float]:
    """(ring angle, handle pull) where the trumpet's outer ramp joins its ring road.

    Joining where the ring already runs the ramp's way leaves one gentle bend off the
    bridge (no S). Picks the gentlest single bend that stays clear of the loop once
    past the bridge end; falls back to the gentlest bend if none qualifies.
    """
    best = fallback = None
    for angle in range(180, 271, 5):
        a = radians(angle)
        point, tangent = (radius + ring * cos(a), -depth + ring * sin(a)), (-sin(a), cos(a))
        for pull in (0.35, 0.45, 0.55):
            points = _transition((0, bridge_y), (-0.45, -1), point, tangent, pull)
            kappas = [kappa for *_, kappa in points]
            candidate = (1 / max(map(abs, kappas)), angle, pull)
            one_bend = min(kappas) > -1e-9 or max(kappas) < 1e-9
            clear = min(hypot(x - radius, y + depth) for x, y, _ in points[len(points) // 4:]) - radius
            if one_bend and clear > 7 and (best is None or candidate > best):
                best = candidate
            fallback = max(fallback or candidate, candidate)
    return (best or fallback)[1:]


def _straight(x: float, y: float) -> list[CurvePoint]:
    """A straight run to (x, y): zero curvature."""
    return [(x, y, 0.0)]


def _transition(start: tuple[float, float], heading: tuple[float, float],
                end: tuple[float, float], end_heading: tuple[float, float],
                pull: float = 0.55, end_pull: float | None = None) -> list[CurvePoint]:
    """Cubic Bezier leaving `start` along `heading`, arriving along `end_heading`.
    pull / end_pull = handle lengths as a share of the chord (about 0.39 traces a
    quarter circle; longer handles bunch the turning into the middle, and a longer
    start handle hugs the freeway like a real taper).
    Each segment carries the curve's exact curvature at its middle."""
    chord = dist(start, end)
    h0, h1 = hypot(*heading), hypot(*end_heading)
    k0, k1 = pull * chord, (pull if end_pull is None else end_pull) * chord
    p0 = start
    p1 = (start[0] + heading[0] / h0 * k0, start[1] + heading[1] / h0 * k0)
    p2 = (end[0] - end_heading[0] / h1 * k1, end[1] - end_heading[1] / h1 * k1)
    p3 = end

    def curvature(t: float) -> float:
        dx, dy = (3 * (1 - t) ** 2 * (q1 - q0) + 6 * (1 - t) * t * (q2 - q1) + 3 * t ** 2 * (q3 - q2)
                  for q0, q1, q2, q3 in zip(p0, p1, p2, p3))
        ddx, ddy = (6 * (1 - t) * (q2 - 2 * q1 + q0) + 6 * t * (q3 - 2 * q2 + q1)
                    for q0, q1, q2, q3 in zip(p0, p1, p2, p3))
        return (dx * ddy - dy * ddx) / max((dx * dx + dy * dy) ** 1.5, 1e-12)

    steps = max(16, round(chord / (STEP_M * 0.75)))  # finer than arcs: S bends turn faster
    points = []
    for i in range(1, steps + 1):
        t = i / steps
        a, b, c, d = (1 - t) ** 3, 3 * (1 - t) ** 2 * t, 3 * (1 - t) * t ** 2, t ** 3
        points.append((a * p0[0] + b * p1[0] + c * p2[0] + d * p3[0],
                       a * p0[1] + b * p1[1] + c * p2[1] + d * p3[1], curvature((i - 0.5) / steps)))
    return points


def _check_legs(network: Network, legs: list[str]) -> None:
    if len(set(legs)) != len(legs):
        raise ValueError("Pick different junctions for each leg.")
    missing = [leg for leg in legs if leg not in network.nodes]
    if missing:
        raise ValueError(f"Interchange legs must be junctions: {', '.join(missing)}.")


def _existing_roads(network: Network, legs: list[str]) -> tuple[str | None, list[str]]:
    """The hub junction touching the most legs (if at least two) and the roads an
    interchange replaces: roads between legs, and roads from the legs to the hub."""
    leg_set = set(legs)
    touching: dict[str, set[str]] = {}
    for road in network.roads.values():
        for a, b in ((road.from_node, road.to_node), (road.to_node, road.from_node)):
            if a in leg_set and b not in leg_set:
                touching.setdefault(b, set()).add(a)
    hub = max(touching, key=lambda junction: len(touching[junction]), default=None)
    if hub is not None and len(touching[hub]) < 2:
        hub = None
    roads = [road.id for road in network.roads.values()
             if {road.from_node, road.to_node} <= leg_set
             or (hub in (road.from_node, road.to_node)
                 and {road.from_node, road.to_node} - {hub} <= leg_set)]
    return hub, roads


def roads_left_at_hub(network: Network, legs: list[str]) -> tuple[str | None, list[str]]:
    """The junction Replace would clear, and the other nodes it stays connected to
    (when some of its roads are not part of the interchange, it is kept for them)."""
    hub, replaced = _existing_roads(network, legs)
    if hub is None:
        return None, []
    others = [road.to_node if road.from_node == hub else road.from_node
              for road in network.all_roads()
              if hub in (road.from_node, road.to_node) and road.id not in replaced]
    return hub, others


def _centre(network: Network, hub: str | None, legs: list[str]) -> tuple[float, float] | None:
    """The hub when it joins every leg (a grid junction being upgraded), else None."""
    if hub is None:
        return None
    joined = {node for road in network.roads.values() if hub in (road.from_node, road.to_node)
              for node in (road.from_node, road.to_node)}
    return (network.nodes[hub].x, network.nodes[hub].y) if set(legs) <= joined else None


def _keep_if_valid(network: Network, trial: Network) -> None:
    """Take what was built on `trial`, a copy of `network`, only if it validates: a refusal (ValueError) leaves the
    network untouched. Its dicts (roads, nodes...) stay the same objects, refilled: a caller holding one sees the build."""
    trial.validate()
    for key, value in vars(trial).items():
        mine = getattr(network, key)
        if isinstance(mine, dict) and isinstance(value, dict):
            mine.clear()
            mine.update(value)
        else:
            setattr(network, key, value)


def _remove(network: Network, roads: list[str], hub: str | None) -> None:
    for road_id in roads:
        network.delete_road(road_id)
    if hub is not None and not any(hub in (road.from_node, road.to_node) for road in network.all_roads()):
        network.delete_node(hub)


def _frame(network: Network, origin: tuple[float, float], a: str, b: str, towards: str) -> _Frame:
    """x from freeway junction a towards b; y across it, pointing to `towards`."""
    first, second, target = network.nodes[a], network.nodes[b], network.nodes[towards]
    length = hypot(second.x - first.x, second.y - first.y)
    ux, uy = (second.x - first.x) / length, (second.y - first.y) / length
    vx, vy = -uy, ux
    if (target.x - origin[0]) * vx + (target.y - origin[1]) * vy < 0:
        vx, vy = -vx, -vy
    return _Frame(origin[0], origin[1], ux, uy, vx, vy)


def _keep_sign(network: Network, frame: _Frame) -> int:
    """+1 when freeway traffic heading +x keeps to the +y side, else -1 (screen y points down)."""
    if network.driving_side == "right":
        keep_x, keep_y = -frame.uy, frame.ux
    else:
        keep_x, keep_y = frame.uy, -frame.ux
    return 1 if keep_x * frame.vx + keep_y * frame.vy > 0 else -1


def _line_intersection(p1: Node, p2: Node, p3: Node, p4: Node) -> tuple[float, float] | None:
    denominator = (p1.x - p2.x) * (p3.y - p4.y) - (p1.y - p2.y) * (p3.x - p4.x)
    if abs(denominator) < 1e-9:
        return None
    a = p1.x * p2.y - p1.y * p2.x
    b = p3.x * p4.y - p3.y * p4.x
    return ((a * (p3.x - p4.x) - (p1.x - p2.x) * b) / denominator,
            (a * (p3.y - p4.y) - (p1.y - p2.y) * b) / denominator)


def pair_legs(network: Network, ids: list[str]) -> tuple[tuple[str, str], tuple[str, str]]:
    """Split four junctions into the two roads that cross: the pairing whose
    segments intersect, else the one with the longest pairs."""
    nodes = network.nodes
    options = [((ids[0], ids[1]), (ids[2], ids[3])), ((ids[0], ids[2]), (ids[1], ids[3])),
               ((ids[0], ids[3]), (ids[1], ids[2]))]

    def side(p: Node, q: Node, r: Node) -> float:
        return (q.x - p.x) * (r.y - p.y) - (q.y - p.y) * (r.x - p.x)

    def crosses(first: tuple[str, str], second: tuple[str, str]) -> bool:
        a, b, c, d = (nodes[i] for i in (*first, *second))
        return side(a, b, c) * side(a, b, d) < 0 and side(c, d, a) * side(c, d, b) < 0

    def span(option: tuple[tuple[str, str], tuple[str, str]]) -> float:
        return sum(hypot(nodes[p].x - nodes[q].x, nodes[p].y - nodes[q].y) for p, q in option)

    crossing = [option for option in options if crosses(*option)]
    return max(crossing or options, key=span)


def guess_stem(network: Network, ids: list[str]) -> str:
    """For three junctions, the one left over once the farthest-apart pair (the
    freeway) is taken out."""
    def pair_span(candidate: str) -> float:
        a, b = (network.nodes[i] for i in ids if i != candidate)
        return hypot(b.x - a.x, b.y - a.y)
    return max(ids, key=pair_span)


def build_trumpet(network: Network, freeway: tuple[str, str], stem: str,
                  replace: bool = True, freeway_lanes: int = 2) -> str:
    """Trumpet: the freeway runs through `freeway`; the road from `stem` ends at it.

    Canonical layout (X along the freeway, near-side traffic heading -X, Y
    towards the stem): the stem splits at J, where a direct exit (from the near
    carriageway, upstream) and entry (downstream) join it. A two-way bridge J-K
    crosses the freeway. Beyond it the far carriageway exits into a 270-degree
    loop that climbs into K, and the entry from K wraps round the loop to merge
    downstream: the trumpet's bell.
    """
    a, b = freeway
    legs = [a, b, stem]
    _check_legs(network, legs)
    first, second, end = (network.nodes[i] for i in legs)
    hub, existing = _existing_roads(network, legs)
    origin = _centre(network, hub, legs)
    if origin is None:  # foot of the stem on the freeway line
        length = hypot(second.x - first.x, second.y - first.y)
        ux, uy = (second.x - first.x) / length, (second.y - first.y) / length
        t = (end.x - first.x) * ux + (end.y - first.y) * uy
        origin = (first.x + t * ux, first.y + t * uy)
    frame = _frame(network, origin, a, b, stem)
    reach_a, reach_b, reach_stem = -frame.along(first), frame.along(second), frame.across(end)
    if min(reach_a, reach_b) <= 0 or reach_stem <= 0:
        raise ValueError("The terminating road must meet the freeway between its two junctions.")

    half_width = freeway_lanes * LANE_WIDTH_M
    # Full-size metres, scaled down together to fit: loop radius, gap to the
    # ring road wrapping it, the loop's drop below the freeway (an estimate for sizing;
    # the spiral sets it exactly), stem split.
    sizes = {"loop": 55.0, "ring_gap": 30.0, "clearance": 12.0, "stem": 70.0}
    span = 3 * sizes["loop"] + sizes["ring_gap"] + sizes["clearance"]  # merge point, less half_width
    scale = min(1.0, (min(reach_a, reach_b) - MARGIN_M - half_width) / span,
                (reach_stem - MARGIN_M - half_width) / sizes["stem"])
    if scale < 0.35:
        raise ValueError("Not enough room for a trumpet here: choose junctions further apart "
                         f"(about {span + half_width + MARGIN_M:.0f} m each way along the freeway).")
    rho, ring_gap, _, stem_len = (sizes[key] * scale for key in sizes)
    loop_start, depth, turn, spiral = _loop_entry(rho, half_width)  # depth: loop centre below the axis
    ring = rho + ring_gap  # radius of the entry ramp wrapping the loop
    bridge_y = -(half_width + 12)
    split_y = half_width + stem_len
    merge = rho + ring + depth  # far entry rejoins here; the near exit leaves here
    near = _keep_sign(network, frame)  # the near carriageway heads +x when +1

    def local(big_x: float, big_y: float) -> tuple[float, float]:
        return -near * big_x, big_y

    def curve_points(*pieces: list[CurvePoint]) -> list[CurvePoint]:
        """Chain canonical pieces into local points. Flipping X mirrors the curve when
        near = +1, so curvature takes the same sign as the flip (-near)."""
        return [(*local(x, y), -near * kappa) for piece in pieces for x, y, kappa in piece]

    original, network = network, deepcopy(network)  # built on a copy, kept if it validates (_keep_if_valid)
    if replace:
        _remove(network, existing, hub)
    build = _Builder(network, "TR", frame)
    split = build.node(*local(0, split_y))
    bridge_end = build.node(*local(0, bridge_y))
    loop_exit = build.node(*local(loop_start, 0))
    far_entry = build.node(*local(merge, 0))
    near_entry = build.node(*local(-merge, 0))
    axis = sorted([loop_exit, far_entry, near_entry], key=lambda i: frame.along(network.nodes[i]))
    build.chain([a, *axis, b], freeway_lanes, FREEWAY_SPEED_KMH)
    lanes, speed = network.default_lanes_per_direction, network.default_speed_limit_kmh
    build.chain([stem, split, bridge_end], lanes, speed)  # stem, then the bridge over the freeway
    network.roads[f"{split}-{bridge_end}"].overpass = True
    # Near carriageway: exit upstream, entry downstream, both leaving/joining tangentially.
    build.curve(far_entry, split, curve_points(
        _transition((merge, 0), (-1, 0), (0, split_y), (-0.6, 0.8), 0.35, 0.2)))
    build.curve(split, near_entry, curve_points(
        _transition((0, split_y), (-0.6, -0.8), (-merge, 0), (-1, 0), 0.2, 0.35)))
    # Far carriageway exit: a transition spiral eases off the freeway into the loop
    # circle, round it until heading back towards the stem, then up to the bridge.
    build.curve(loop_exit, bridge_end, curve_points(
        spiral, _arc(rho, -depth, rho, 90 - turn, -180), _straight(0.0, bridge_y)), LOOP_SPEED_KMH)
    # Far carriageway entry: one bend off the bridge onto the ring road round the loop
    # (joined where it already runs the ramp's way), then a quarter circle onto the freeway.
    join, pull = _bell_join(rho, ring, depth, bridge_y)
    build.curve(bridge_end, far_entry, curve_points(
        _transition((0, bridge_y), (-0.45, -1), (rho + ring * cos(radians(join)), -depth + ring * sin(radians(join))),
                    (-sin(radians(join)), cos(radians(join))), pull),
        _arc(rho, -depth, ring, join, 360),
        _arc(merge, -depth, depth, 180, 90)))
    _keep_if_valid(original, network)
    return build.name


def build_diamond(network: Network, freeway: tuple[str, str], crossroad: tuple[str, str],
                  replace: bool = True, freeway_lanes: int = 2, single_point: bool = False) -> str:
    """Diamond: ramps meet the crossroad at two signals, one each side of the freeway.
    single_point=True builds a SPUI: all four ramps meet at one signal over the freeway."""
    a, b = freeway
    c, d = crossroad
    legs = [a, b, c, d]
    _check_legs(network, legs)
    first, second, cross_first, cross_second = (network.nodes[i] for i in legs)
    hub, existing = _existing_roads(network, legs)
    origin = _centre(network, hub, legs) or _line_intersection(first, second, cross_first, cross_second)
    if origin is None:
        raise ValueError("The freeway and the crossroad are parallel; pick junctions whose roads cross.")
    frame = _frame(network, origin, a, b, d)
    if not frame.along(first) < 0 < frame.along(second):
        raise ValueError("The crossroad must cross the freeway between the freeway's two junctions.")
    if not frame.across(cross_first) < 0 < frame.across(cross_second):
        raise ValueError("The crossroad's junctions must lie on opposite sides of the freeway.")
    half_width = freeway_lanes * LANE_WIDTH_M
    reach = min(250.0, min(-frame.along(first), frame.along(second)) - MARGIN_M)
    terminal = min(80.0, 0.45 * min(-frame.across(cross_first), frame.across(cross_second)))
    if reach < 80 or terminal < half_width + 15:
        name = "SPUI" if single_point else "diamond"
        raise ValueError(f"Not enough room for a {name} here: choose junctions further apart.")
    keep = _keep_sign(network, frame)  # side of the carriageway heading +x

    original, network = network, deepcopy(network)  # built on a copy, kept if it validates (_keep_if_valid)
    if replace:
        _remove(network, existing, hub)
    build = _Builder(network, "SP" if single_point else "DI", frame)
    lanes, speed = network.default_lanes_per_direction, network.default_speed_limit_kmh
    if single_point:
        # Each side has its own diverge (out at the reach) and merge point, so every
        # curved ramp joins a different pair of nodes.
        merge_x = 0.8 * reach
        exits = {-1: build.node(-reach, 0), 1: build.node(reach, 0)}
        entries = {-1: build.node(-merge_x, 0), 1: build.node(merge_x, 0)}
        build.chain([a, exits[-1], entries[-1], entries[1], exits[1], b], freeway_lanes, FREEWAY_SPEED_KMH)
        point = build.node(0, 0, "signalised")
        build.chain([c, point, d], lanes, speed)
        offset_x, offset_y = 0.4 * reach, min(terminal, half_width + 25)

        def spui_ramp(qx: int, qy: int, entering: bool) -> None:
            """Curved ramp in quadrant (qx, qy): tapers off (or onto) the freeway, runs
            alongside it, and meets the single signal diagonally."""
            heading = qx if entering else -qx  # exits come upstream, entries downstream
            beside = (qx * offset_x, qy * offset_y)
            if entering:
                axis = (qx * merge_x, 0.0)
                pieces = (_transition((0, 0), (heading, qy), beside, (heading, 0), 0.4),
                          _transition(beside, (heading, 0), axis, (heading, 0), 0.4))
                ends = (point, entries[qx])
            else:
                axis = (qx * reach, 0.0)
                pieces = (_transition(axis, (heading, 0), beside, (heading, 0), 0.4),
                          _transition(beside, (heading, 0), (0, 0), (heading, -qy), 0.4))
                ends = (exits[qx], point)
            build.curve(*ends, [point for piece in pieces for point in piece])

        spui_ramp(-1, keep, entering=False)  # +x traffic exits
        spui_ramp(1, keep, entering=True)  # +x traffic enters
        spui_ramp(1, -keep, entering=False)  # -x traffic exits
        spui_ramp(-1, -keep, entering=True)  # -x traffic enters
    else:
        before, after = build.node(-reach, 0), build.node(reach, 0)
        build.chain([a, before, after, b], freeway_lanes, FREEWAY_SPEED_KMH)
        low, high = build.node(0, -terminal, "signalised"), build.node(0, terminal, "signalised")
        build.chain([c, low, high, d], lanes, speed)  # passes over the freeway between signals
        network.roads[f"{low}-{high}"].overpass = True
        own, other = (high, low) if keep > 0 else (low, high)
        build.ramp([before, own])  # +x traffic exits before the crossroad...
        build.ramp([own, after])  # ...and re-enters after it
        build.ramp([after, other])  # -x traffic, mirrored
        build.ramp([other, before])
    _keep_if_valid(original, network)
    return build.name


def build_slip_road(network: Network, first: str, second: str, single_lane: bool = False) -> str:
    """A slip road between two roads meeting at a junction: a one-way road that takes the kerb-side
    turn (left when driving on the left) past the junction. Each road is split at the same setback
    from the junction (_plan_slip) and the slip joins the two new uncontrolled nodes. Its kerbside
    lane leaves the arriving road's kerbside lane and merges into the leaving road's. Everything is
    checked before anything changes. One lane (`single_lane`) or two. Returns the slip road's ID."""
    roads = [network.road(first), network.road(second)]
    shared = {roads[0].from_node, roads[0].to_node} & {roads[1].from_node, roads[1].to_node}
    if first == second or len(shared) != 1 or next(iter(shared)) not in network.nodes:
        raise ValueError("Choose two roads that meet at one junction.")
    (junction,) = shared
    keep = 1.0 if network.driving_side == "left" else -1.0

    def carries(road, inwards: bool) -> bool:
        return road.direction == "two-way" or (road.to_node == junction) == inwards

    arrive = leave = None
    for a, b in (roads, roads[::-1]):
        _, _, ax, ay = point_along(_away(network, a, junction)[0], 0.0)
        _, _, vx, vy = point_along(_away(network, b, junction)[0], 0.0)
        ux, uy = -ax, -ay  # heading into the junction along a
        kerb = ux * vx + uy * vy <= cos(radians(35)) and (ux * vy - uy * vx) * keep < 0  # simulation's kerb turn
        if kerb and carries(a, True) and carries(b, False):
            arrive, leave = a, b
    if arrive is None:
        raise ValueError("These roads have no kerb-side turn between them for a slip road to take.")
    lines = [_away(network, road, junction)[0] for road in (arrive, leave)]
    _, start, end, pieces = _plan_slip(network, junction, (arrive, leave), lines, single_lane, {arrive.id, leave.id})

    original, network = network, deepcopy(network)  # built on a copy, kept if it validates (_keep_if_valid)
    ends = []
    for road, (x, y) in ((arrive, start), (leave, end)):
        node = network.add_node(x, y)
        node.control = "uncontrolled"
        network.split_road(road.id, node.id)
        ends.append(node.id)
    slip = network.add_road(*ends)
    slip.lanes_per_direction, slip.direction, slip.road_type, slip.single_lane = 1, "one-way", "ramp", single_lane
    _lay_slip(network, slip, pieces, min(arrive.speed_limit_kmh, leave.speed_limit_kmh))
    _keep_if_valid(original, network)  # e.g. a turn pocket left on a stub too short for it
    return slip.id


def fit_slip_roads(network: Network, junction: str) -> list[str]:
    """Move the slip roads past this junction out along their roads where it now needs more room
    (it has become a roundabout): their nodes slide to the setback _plan_slip asks for, the road
    pieces either side are re-cut there (curves kept) and the slip is laid again, so it runs outside
    the circle and the roundabout's arms keep their lanes. Slip roads already far enough out stay.
    Refused, changing nothing, if one can't move far enough. Returns the slip roads moved."""
    roads_at: dict[str, list] = {}
    for road in network.all_roads():
        for node_id in (road.from_node, road.to_node):
            roads_at.setdefault(node_id, []).append(road)
    plans = []
    for slip in [road for road in network.roads.values() if _is_slip(network, road, junction, roads_at)]:
        ends = []
        for node_id in (slip.from_node, slip.to_node):
            inner, outer = sorted((road for road in roads_at[node_id] if road is not slip),
                                  key=lambda road: junction not in (road.from_node, road.to_node))
            near_points, near_bends = _away(network, inner, junction)
            far_points, far_bends = _away(network, outer, node_id)
            ends.append((node_id, inner, outer, near_points + far_points[1:], near_bends + far_bends))
        ignore = {slip.id, *(road.id for end in ends for road in end[1:3])}
        setback, *_, pieces = _plan_slip(network, junction, (ends[0][1], ends[1][1]), [end[3] for end in ends],
                                         slip.single_lane, ignore)
        if setback > polyline_length(_away(network, ends[0][1], junction)[0]) + 1e-6:
            plans.append((slip, ends, setback, pieces))
    for slip, ends, setback, pieces in plans:  # every plan checked: now change
        for node_id, inner, outer, line, bends in ends:
            _recut(network, node_id, junction, inner, outer, line, bends, setback)
        _lay_slip(network, slip, pieces, min(ends[0][2].speed_limit_kmh, ends[1][2].speed_limit_kmh))
    network.synchronize_road_lengths()
    return [slip.id for slip, *_ in plans]


def _is_slip(network: Network, road, junction: str, roads_at: dict) -> bool:
    """A slip road past `junction`, as build_slip_road leaves it: a one-way ramp between two
    uncontrolled nodes of three roads each, both a road away from the junction."""
    # ponytail: recognised by its layout, not recorded; an interchange ramp laid out the same way would match.
    if road.direction != "one-way" or road.road_type != "ramp":
        return False
    for node_id in (road.from_node, road.to_node):
        node = network.nodes.get(node_id)
        if node is None or node.control != "uncontrolled" or len(roads_at.get(node_id, [])) != 3:
            return False
        if not any(junction in (other.from_node, other.to_node) for other in roads_at[node_id] if other is not road):
            return False
    return True


def _away(network: Network, road, node_id: str) -> tuple[list[tuple[float, float]], list[float]]:
    """A road's centre line from `node_id` outwards, with each segment's curvature that way."""
    points = network.road_points(road)
    bends = list(road.curvature) or [0.0] * (len(points) - 1)
    if road.from_node == node_id:
        return points, bends
    return points[::-1], [-kappa for kappa in reversed(bends)]


def _plan_slip(network: Network, junction: str, roads, lines, single_lane: bool, ignore: set):
    """Where a slip road between two roads at a junction goes: (setback, start, end, curve pieces).
    `lines` run from the junction out along the arriving and the leaving road. The setback is the
    smallest (5 m steps) that leaves SLIP_ROOM_M of lane between the slip's nodes and where the
    junction's lanes end (its stop line, a roundabout's give-way line), at which the curve's middle
    clears both roads and all of it the junction (a roundabout's circle), leaving each road 20 m beyond. It leaves
    the arriving road along it and curves round to join the leaving road along it (a quarter circle at a right
    angle), so it tapers out of and into their kerbside lanes; the simulation takes a ramp leaving or joining a
    road at a small angle as a kerb turn (Simulation._kind), so only the kerbside lane takes it. Its curve is
    also wide enough that, at the speed its innermost lane allows, it is no slower than the two setbacks through
    the junction at the roads' limit: otherwise routes (free-flow times) would never take it."""
    width = (1 if single_lane else 2) * LANE_WIDTH_M
    clearance = (max(road.width_m for road in roads) + width) / 2
    inset = max(network.lane_inset(junction, road) for road in roads)
    hub = network.nodes[junction]
    longest = min(polyline_length(line) for line in lines) - 20.0
    limit = min(road.speed_limit_kmh for road in roads)

    room = inset + max(road.width_m for road in roads) / 2 + SLIP_ROOM_M  # the junction's line, the slip node's edge, lanes between
    for setback in range(5 * ceil(room / 5), int(longest) + 1, 5):
        (sx, sy, ax, ay), (ex, ey, vx, vy) = (point_along(line, setback) for line in lines)
        pieces = _transition((sx, sy), (-ax, -ay), (ex, ey), (vx, vy), 0.39)
        mx, my, _ = pieces[len(pieces) // 2]
        near = min(distance_to_segment((mx, my), p, q) for line in lines for p, q in zip(line, line[1:]))
        outside = min(hypot(x - hub.x, y - hub.y) for x, y, _ in pieces) >= inset + width / 2
        inner = 1 / max(abs(kappa) for *_, kappa in pieces) - (width - LANE_WIDTH_M) / 2  # innermost lane's radius
        quick = polyline_length([(sx, sy), *((x, y) for x, y, _ in pieces)]) / min(limit, curve_speed_kmh(inner))             <= 2 * setback / limit
        if near >= clearance and outside and quick:
            break
    else:
        raise ValueError(f"Not enough room for a slip road past {junction}: its roads are too short.")
    points = [(sx, sy), *((x, y) for x, y, _ in pieces)]
    crossed = [road_id for *_, road_id in network.crossings(points) if road_id not in ignore]
    if crossed:
        raise ValueError(f"A slip road past {junction} would cross {', '.join(dict.fromkeys(crossed))}.")
    return setback, (sx, sy), (ex, ey), pieces


def _lay_slip(network: Network, slip, pieces, speed_kmh: float) -> None:
    """The slip road's curve and its speed limit: the roads' own, capped for its tightest bend."""
    slip.shape = [(x, y) for x, y, _ in pieces[:-1]]
    slip.curvature = [kappa for *_, kappa in pieces]
    slip.length_m = network.road_geometry_length(slip)
    tightest = max(abs(kappa) for kappa in slip.curvature)
    slip.speed_limit_kmh = min(speed_kmh, curve_speed_kmh(1 / tightest) if tightest else speed_kmh)


def _recut(network: Network, node_id: str, junction: str, inner, outer, line, bends, setback: float) -> None:
    """Slide node `node_id` to `setback` metres along `line` (from the junction, through the node,
    to the outer road's far end), re-cutting the two roads either side of it there."""
    walked = 0.0
    for i, (p, q) in enumerate(zip(line, line[1:])):
        segment = dist(p, q)
        if walked + segment >= setback:
            break
        walked += segment
    t = (setback - walked) / segment
    cut = (p[0] + (q[0] - p[0]) * t, p[1] + (q[1] - p[1]) * t)
    near, near_bends = line[:i + 1] + [cut], bends[:i + 1]
    far, far_bends = [cut] + line[i + 1:], bends[i:]
    if t > 1 - 1e-9:  # the cut is a point already there
        far, far_bends = line[i + 1:], bends[i + 1:]
    for road, start, points, kappas in ((inner, junction, near, near_bends), (outer, node_id, far, far_bends)):
        if road.from_node != start:
            points, kappas = points[::-1], [-kappa for kappa in reversed(kappas)]
        road.shape, road.curvature = points[1:-1], (kappas if any(kappas) else [])
    network.nodes[node_id].x, network.nodes[node_id].y = cut

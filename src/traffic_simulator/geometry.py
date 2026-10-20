"""Vector geometry on polylines: the primitives all network maths builds on.

Coordinates are continuous metres (x to the right, y down, as the scene draws
them). Roads, lane centre lines and, later, vehicle paths are polylines; a
position on one is a distance along it plus a sideways offset. Nothing here is
raster or cell based, and future physics (vehicle movement, lane changes,
conflicts) should build on these functions rather than on screen coordinates.
"""

from __future__ import annotations

from math import acos, atan2, ceil, cos, hypot, pi, sin

Point = tuple[float, float]


def polyline_length(points: list[Point]) -> float:
    return sum(hypot(bx - ax, by - ay) for (ax, ay), (bx, by) in zip(points, points[1:]))


def point_along(points: list[Point], distance: float) -> tuple[float, float, float, float]:
    """(x, y, ux, uy): the point `distance` metres along the polyline (clamped to its
    ends) and the unit direction of travel there."""
    for i in range(len(points) - 1):
        (ax, ay), (bx, by) = points[i], points[i + 1]
        segment = hypot(bx - ax, by - ay)
        if segment and (distance <= segment or i == len(points) - 2):
            t = min(max(distance / segment, 0.0), 1.0)
            return ax + (bx - ax) * t, ay + (by - ay) * t, (bx - ax) / segment, (by - ay) / segment
        distance -= segment
    return points[0][0], points[0][1], 1.0, 0.0  # degenerate: every segment has zero length


def offset_polyline(points: list[Point], offset: float) -> list[Point]:
    """The polyline moved sideways by `offset` metres along its normal (-uy, ux),
    e.g. a lane centre line from a road's centre line."""
    shifted = []
    for i, (x, y) in enumerate(points):
        (ax, ay), (bx, by) = points[max(i - 1, 0)], points[min(i + 1, len(points) - 1)]
        length = hypot(bx - ax, by - ay) or 1.0
        shifted.append((x - (by - ay) / length * offset, y + (bx - ax) / length * offset))
    return shifted


def curvature_at(points: list[Point], curvature: list[float], distance: float,
                 offset: float = 0.0) -> float:
    """Signed curvature (1/m) `distance` metres along a road's centre line, or on a
    line `offset` metres to its (-uy, ux) side, such as a lane.

    `curvature` holds one value per segment of `points` (empty: straight). Positive
    bends towards the (-uy, ux) side; the radius is 1/|curvature|. A line offset
    towards the centre of a bend is tighter: kappa / (1 - kappa * offset).
    """
    if not curvature:
        return 0.0
    index, walked = len(curvature) - 1, 0.0
    for i, ((ax, ay), (bx, by)) in enumerate(zip(points, points[1:])):
        walked += hypot(bx - ax, by - ay)
        if distance <= walked:
            index = i
            break
    kappa = curvature[index]
    return kappa / (1 - kappa * offset)


def segment_hit(ax: float, ay: float, bx: float, by: float,
                cx: float, cy: float, dx: float, dy: float) -> tuple[float, float] | None:
    """(t, u) where segment ab meets segment cd strictly inside both; None if they don't."""
    rx, ry, sx, sy = bx - ax, by - ay, dx - cx, dy - cy
    denominator = rx * sy - ry * sx
    if abs(denominator) < 1e-12:  # parallel
        return None
    qx, qy = cx - ax, cy - ay
    t, u = (qx * sy - qy * sx) / denominator, (qx * ry - qy * rx) / denominator
    return (t, u) if 0 < t < 1 and 0 < u < 1 else None


def polyline_meets_box(points: list[Point], left: float, top: float, right: float, bottom: float) -> bool:
    """Whether the polyline touches the box: a point inside it, or a segment crossing an edge."""
    if any(left <= x <= right and top <= y <= bottom for x, y in points):
        return True
    edges = (((left, top), (right, top)), ((right, top), (right, bottom)), ((right, bottom), (left, bottom)),
             ((left, bottom), (left, top)))
    return any(segment_hit(ax, ay, bx, by, cx, cy, dx, dy) is not None
               for (ax, ay), (bx, by) in zip(points, points[1:]) for (cx, cy), (dx, dy) in edges)


def distance_to_segment(point: Point, a: Point, b: Point) -> float:
    (px, py), (ax, ay), (bx, by) = point, a, b
    length_sq = (bx - ax) ** 2 + (by - ay) ** 2 or 1e-12
    t = min(max(((px - ax) * (bx - ax) + (py - ay) * (by - ay)) / length_sq, 0.0), 1.0)
    return hypot(px - (ax + t * (bx - ax)), py - (ay + t * (by - ay)))


def nearest_along(points: list[Point], point: Point) -> float:
    """How far along the polyline its nearest point to `point` is, e.g. where the mouse
    sits along a road."""
    (px, py), best, closest, walked = point, 0.0, float("inf"), 0.0
    for (ax, ay), (bx, by) in zip(points, points[1:]):
        segment = hypot(bx - ax, by - ay)
        t = min(max(((px - ax) * (bx - ax) + (py - ay) * (by - ay)) / segment ** 2, 0.0), 1.0) if segment else 0.0
        distance = hypot(px - ax - t * (bx - ax), py - ay - t * (by - ay))
        if distance < closest:
            best, closest = walked + t * segment, distance
        walked += segment
    return best


def sub_polyline(points: list[Point], start: float, end: float) -> list[Point]:
    """The piece of the polyline from `start` to `end` metres along it (clamped to its ends),
    e.g. a lane cut back to the stop lines at both of its junctions."""
    start, end = max(start, 0.0), max(end, start)
    piece = [point_along(points, start)[:2]]
    walked = 0.0
    for (ax, ay), (bx, by) in zip(points, points[1:]):
        walked += hypot(bx - ax, by - ay)
        if start < walked < end:
            piece.append((bx, by))
    piece.append(point_along(points, end)[:2])
    return piece


def bezier(p0: Point, p1: Point, p2: Point, p3: Point, pieces: int = 12) -> list[Point]:
    """A cubic Bézier curve as a polyline: smooth turns through junctions."""
    curve = []
    for i in range(pieces + 1):
        t = i / pieces
        a, b, c, d = (1 - t) ** 3, 3 * (1 - t) ** 2 * t, 3 * (1 - t) * t * t, t ** 3
        curve.append((a * p0[0] + b * p1[0] + c * p2[0] + d * p3[0],
                      a * p0[1] + b * p1[1] + c * p2[1] + d * p3[1]))
    return curve


def _bend(phi: float, side: float, radius: float, transition: float, step: float = 5.0) -> tuple[list, Point]:
    """A rail bend turning `phi` radians towards `side` (+1: the (-uy, ux) side) from heading (1, 0) at the origin:
    a transition (a clothoid: curvature rising evenly from 0 to 1 / radius), the circular arc, a transition back; on a
    turn too small for both transitions they are shortened, so the curvature never passes 1 / radius. Its points every
    `step` metres after the origin (the last its end) and its end."""
    ramp = min(transition, phi * radius)  # each transition turns ramp / (2 radius)
    arc = phi * radius - ramp
    total = 2 * ramp + arc
    if total < 1e-9:
        return [], (0.0, 0.0)

    def heading(at: float) -> float:  # the angle turned `at` metres in
        if at < ramp:
            return at * at / (2 * ramp * radius)
        if at < ramp + arc:
            return ramp / (2 * radius) + (at - ramp) / radius
        return phi - (total - at) ** 2 / (2 * ramp * radius) if ramp else phi

    points, x, y, walked, ds = [], 0.0, 0.0, 0.0, 1.0
    next_mark = step
    while walked < total - 1e-9:
        piece = min(ds, total - walked)
        angle = side * heading(walked + piece / 2)  # the middle of the piece: second-order accurate
        x, y, walked = x + piece * cos(angle), y + piece * sin(angle), walked + piece
        if walked >= next_mark - 1e-9 or walked >= total - 1e-9:
            points.append((x, y))
            next_mark += step
    return points, (x, y)


def rail_leg(track: list[Point], x: float, y: float, radius: float, max_turn: float, platform_m: float,
             transition_m: float, heading: Point | None = None) -> list[Point]:
    """A metro leg from a track's last point (a station) towards (x, y), laid out as a railway is: straight on for
    half a platform (the station on straight track), a transition into a circular curve of `radius`, the curve, a
    transition out (_bend), then straight to the next station, at least half a platform. The leg's points after the
    track's last, ending at the station.

    A plain click (`heading` None): the turn that points the last straight at (x, y); one that needs a turn past
    `max_turn`, or lies inside the bend's reach, is drawn in like a rubber band: the most the leg may turn, the
    station as far out as the point (and half a platform past the bend at the least). Aimed (`heading`, a unit
    direction the track runs through the station, the pen tool's drag): the turn is the angle between the two
    headings, and the two straights are solved to meet the point; ValueError when the aim turns past `max_turn` or
    can't be met with those straights (an S-bend: a station between). A track of one point: straight to (x, y)."""
    if len(track) < 2:
        return [(x, y)]
    (ax, ay), (bx, by) = track[-2], track[-1]
    length = hypot(bx - ax, by - ay)
    hx, hy = (bx - ax) / length, (by - ay) / length
    half = platform_m / 2
    sx, sy = bx + hx * half, by + hy * half  # the platform's end: the bend may start here

    def placed(phi: float, side: float, lead: float = 0.0) -> tuple[list, Point, Point]:
        """The bend `lead` metres past the platform: its points, end and end heading, in the scene."""
        local, (ex, ey) = _bend(phi, side, radius, transition_m)
        ox, oy = sx + hx * lead, sy + hy * lead
        c, s = hx, hy
        moved = [(ox + px * c - py * s, oy + px * s + py * c) for px, py in local]
        angle = side * phi
        return moved, (ox + ex * c - ey * s, oy + ex * s + ey * c), (cos(angle) * c - sin(angle) * s,
                                                                      cos(angle) * s + sin(angle) * c)

    if heading is not None:
        gx, gy = heading
        norm = hypot(gx, gy)
        gx, gy = gx / norm, gy / norm
        cross, dot = hx * gy - hy * gx, hx * gx + hy * gy
        phi, side = abs(atan2(cross, dot)), (1.0 if cross >= 0 else -1.0)
        if phi > max_turn:
            raise ValueError("Aimed too sharply: trains don't take bends of a right angle or more.")
        bend, (ex, ey), _ = placed(phi, side)
        rx, ry = x - ex, y - ey  # what the two straights must cover: lead * h + last * g
        det = hx * gy - hy * gx
        if abs(det) < 1e-6:  # aimed straight on: the station must be ahead on the line
            if abs(hx * ry - hy * rx) > 1.0 or hx * rx + hy * ry < 0:
                raise ValueError("That aim needs an S-bend: place a station between the two bends.")
            return [(sx, sy), (x, y)] if hx * (x - sx) + hy * (y - sy) > 0 else [(x, y)]
        lead, last = (rx * gy - ry * gx) / det, (hx * ry - hy * rx) / det
        if lead < -1e-6 or last < half - 1e-6:
            raise ValueError("The track can't arrive aimed that way from here (it would need an S-bend, or the "
                             "station is too near the bend): aim it differently or place a station between.")
        bend, _, _ = placed(phi, side, max(lead, 0.0))
        return [(sx, sy), (sx + hx * max(lead, 0.0), sy + hy * max(lead, 0.0)), *bend, (x, y)]
    side = 1.0 if hx * (y - sy) - hy * (x - sx) >= 0 else -1.0

    def aim(phi: float) -> tuple[float, float]:  # how far the last straight misses (x, y) sideways, and runs to it
        _, (ex, ey), (gx, gy) = placed(phi, side)
        return side * (gx * (y - ey) - gy * (x - ex)), gx * (x - ex) + gy * (y - ey)

    steps, found = 60, None
    previous, ahead = aim(0.0)
    if abs(previous) < 0.5 and ahead > 0:
        found, steps = 0.0, 0  # straight ahead: no bend
    for k in range(1, steps + 1):
        phi = max_turn * k / steps
        miss, ahead = aim(phi)
        if previous > 0 >= miss and ahead > 0:
            low, high = max_turn * (k - 1) / steps, phi
            for _ in range(30):
                middle = (low + high) / 2
                low, high = (middle, high) if aim(middle)[0] > 0 else (low, middle)
            found = high
            break
        previous = miss
    phi = max_turn if found is None else found
    bend, (ex, ey), (gx, gy) = placed(phi, side)
    out = max(hypot(x - ex, y - ey) if found is None else gx * (x - ex) + gy * (y - ey), half)
    end = (x, y) if found is not None and out > half else (ex + gx * out, ey + gy * out)  # met, or drawn in
    return [(sx, sy), *bend, end] if phi > 1e-9 else [end]

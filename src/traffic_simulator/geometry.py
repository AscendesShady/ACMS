"""Vector geometry on polylines: the primitives all network maths builds on.

Coordinates are continuous metres (x to the right, y down, as the scene draws
them). Roads, lane centre lines and, later, vehicle paths are polylines; a
position on one is a distance along it plus a sideways offset. Nothing here is
raster or cell based, and future physics (vehicle movement, lane changes,
conflicts) should build on these functions rather than on screen coordinates.
"""

from __future__ import annotations

from math import hypot

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

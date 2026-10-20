"""Display-only overhead metro scenery, in metres. No simulation, clocks or Qt.

Dimensions are visual assumptions, not structural or clearance design checks.
There are no published train poses: one parked, labelled preview illustrates each line's stock.
"""

from math import ceil, hypot

from .geometry import nearest_along, offset_polyline, point_along, polyline_length, sub_polyline
from .asset_mesh import Face, box as _box, side_panel, solid, vertex
from .models import METRO_PLATFORM_M


UNDERSIDE_M, TRACK_M = 8.0, 9.0
PLATFORM_M, CANOPY_M = 9.4, 14.0
TRAIN_BASE_M, TRAIN_HEIGHT_M = 9.15, 3.4
CAR_LENGTH_M, CAR_WIDTH_M, CAR_GAP_M = 18.0, 2.8, .6
DECK_WIDTH_M, TRACK_OFFSET_M, GAUGE_M = 9.0, 2.05, 1.435
PIER_SPACING_M = 40.0
# Ink indices belong to theme.METRO_INKS: concrete, livery, rail, glass, body, roof, trim, tactile edge.


def box(x0, y0, x1, y1, z0, z1, ink):
    """Short longitudinal faces keep long decks from sorting over a train at their far end."""
    count = max(1, ceil((x1 - x0) / 3.))
    faces = []
    for index in range(count):
        left, right = x0 + (x1 - x0) * index / count, x0 + (x1 - x0) * (index + 1) / count
        piece = _box(left, y0, right, y1, z0, z1, ink)
        faces += [piece[i] for i in (0, 1, 3)]
        if index == 0:
            faces.append(piece[2])
        if index == count - 1:
            faces.append(piece[4])
    return faces


def train_length(cars):
    return cars * CAR_LENGTH_M + (cars - 1) * CAR_GAP_M


def station_length(cars):
    return max(METRO_PLATFORM_M, train_length(cars) + 8.)


def placed(faces, x, y, ux, uy):
    return [Face(tuple((x + ux * p[0] - uy * p[1], y + uy * p[0] + ux * p[1], p[2])
                       for p in face.vertices), face.ink, face.part) for face in faces]


def track(length):
    half = length / 2
    faces = box(-half, -DECK_WIDTH_M / 2, half, DECK_WIDTH_M / 2, UNDERSIDE_M, TRACK_M, 0)
    for centre in (-TRACK_OFFSET_M, TRACK_OFFSET_M):
        for lateral in (centre - GAUGE_M / 2, centre + GAUGE_M / 2):
            faces += box(-half, lateral - .06, half, lateral + .06, TRACK_M, TRACK_M + .12, 2)
    # Supports meet the ground; the deck alone has the minimum overhead clearance.
    count = max(1, int(length / PIER_SPACING_M) + 1)
    for index in range(count):
        along = -half + length * (index + .5) / count
        faces += box(along - .7, -.7, along + .7, .7, 0., UNDERSIDE_M, 0)
        faces += box(along - 1., -3.6, along + 1., 3.6, UNDERSIDE_M - .6, UNDERSIDE_M, 0)
    return faces


def station(cars):
    half = station_length(cars) / 2
    faces = []
    # Side platforms leave both running tracks and the parked preview visible from above.
    for side in (-1, 1):
        y0, y1 = sorted((side * 4.0, side * 6.5))
        faces += box(-half, y0, half, y1, UNDERSIDE_M, PLATFORM_M, 0)
        faces += box(-half, side * 4.0 - .12, half, side * 4.0 + .12, PLATFORM_M, PLATFORM_M + .04, 7)
        for x in (-half + 4., 0., half - 4.):
            faces += box(x - .18, side * 5.8 - .18, x + .18, side * 5.8 + .18, PLATFORM_M, CANOPY_M - .25, 2)
            faces += box(x - .8, side * 5.3 - .6, x + .8, side * 5.3 + .6, 0., UNDERSIDE_M, 0)
        faces += canopy(sampled([(-half - 1., 0.), (half + 1., 0.)]), y0 - .4, y1 + .4)
    return faces


def carriage(front=True, back=True):
    half, width, base = CAR_LENGTH_M / 2, CAR_WIDTH_M / 2, TRAIN_BASE_M
    faces = []
    levels = ((.55, 1.1, 4), (1.1, 1.24, 1), (1.24, TRAIN_HEIGHT_M, 4))
    roof = box(-half + .8, -width, half - .8, width, base + .55, base + TRAIN_HEIGHT_M, 4)
    faces += [f for f in roof if all(p[2] == base + TRAIN_HEIGHT_M for p in f.vertices)]
    def panel(x0, x1, side, lower, upper, ink):
        count = max(1, ceil((x1 - x0) / 3.))
        for i in range(count):
            faces.append(side_panel(x0 + (x1 - x0) * i / count, x0 + (x1 - x0) * (i + 1) / count,
                                    side * width, base + lower, base + upper, ink))
    # Glass replaces the facade panels rather than lying over an opaque wall.
    for side in (-1, 1):
        for lower, upper, ink in ((.55, 1.1, 4), (1.1, 1.24, 1), (1.24, 1.3, 4), (2.8, TRAIN_HEIGHT_M, 4)):
            panel(-half + .8, half - .8, side, lower, upper, ink)
        left = -half + .8
        for index in range(5):
            x = -half + 1.4 + index * 2.9
            panel(left, x, side, 1.3, 2.8, 4)
            panel(x, x + 2., side, 1.3, 2.8, 3)
            left = x + 2.
        panel(left, half - .8, side, 1.3, 2.8, 4)
    # Short facade pieces let every coplanar window sort after its support surface.
    for ring in (((half, width * .65), (half - .8, width), (half - .8, -width), (half, -width * .65)),
                 ((-half + .8, width), (-half, width * .65), (-half, -width * .65), (-half + .8, -width))):
        def at_height(z):
            return tuple(vertex(x - (.65 * (z - .55) / (TRAIN_HEIGHT_M - .55) * (1 if x > 0 else -1)
                                      if abs(x) == half else 0.), y, base + z) for x, y in ring)
        for lower, upper, ink in levels:
            faces += solid(at_height(lower), at_height(upper), ink)
    faces += box(-half + 1.3, -1., half - 1.3, 1., base, base + .55, 6)
    for side in (-1, 1):
        if not (front if side > 0 else back):
            continue
        points = tuple(vertex(side * (half - .65 * (z - .55) / (TRAIN_HEIGHT_M - .55)), y, base + z)
                       for y, z in ((-.75, 1.5), (.75, 1.5), (.75, 2.9), (-.75, 2.9)))
        faces.append(Face(points if side > 0 else tuple(reversed(points)), 3))
    return faces


def sampled(path):
    """Preserve every saved vertex, subdividing long faces only for painter sorting."""
    result = [tuple(path[0])]
    for a, b in zip(path, path[1:]):
        count = max(1, ceil(hypot(b[0] - a[0], b[1] - a[1]) / 3.))
        result += [(a[0] + (b[0] - a[0]) * i / count, a[1] + (b[1] - a[1]) * i / count)
                   for i in range(1, count)] + [tuple(b)]  # the vertex itself, not a rounding of it
    return result


def sweep(path, lateral0, lateral1, z0, z1, ink):
    """A closed ribbon with shared cross-sections: no internal caps, cracks or overlapping segment ends."""
    left, right = offset_polyline(path, lateral0), offset_polyline(path, lateral1)
    faces = []
    for i in range(len(path) - 1):
        ring = (right[i], left[i], left[i + 1], right[i + 1])
        piece = solid(tuple(vertex(x, y, z0) for x, y in ring),
                      tuple(vertex(x, y, z1) for x, y in ring), ink)
        faces += [piece[j] for j in (0, 2, 4)]
        if i == 0:
            faces.append(piece[1])
        if i == len(path) - 2:
            faces.append(piece[3])
    return faces


def supports(path):
    """Pier poses at equal world arc-distance spacing, independent of the curve's sampling density."""
    length = polyline_length(path)
    count = max(1, ceil(length / PIER_SPACING_M))
    return [point_along(path, length * (i + .5) / count) for i in range(count)]


def canopy(path, y0, y1):
    """A pitched metal shell: ridge remains within the station's existing total height."""
    mid, eave, thickness = (y0 + y1) / 2, CANOPY_M - .5, .16
    profile = ((y0, eave - thickness), (mid, CANOPY_M - thickness), (y1, eave - thickness),
               (y1, eave), (mid, CANOPY_M), (y0, eave))
    offsets = [offset_polyline(path, y) for y, _ in profile]
    rings = [tuple((offsets[j][i][0], offsets[j][i][1], z) for j, (_, z) in enumerate(profile))
             for i in range(len(path))]
    faces = [Face(tuple(reversed(rings[0])), 5), Face(rings[-1], 5)]
    for a, b in zip(rings, rings[1:]):
        for j in range(len(profile)):
            k = (j + 1) % len(profile)
            faces.append(Face((a[j], a[k], b[k], b[j]), 5))
    return faces


def station_on_path(path):
    faces = []
    length = polyline_length(path)
    for side in (-1, 1):
        y0, y1 = sorted((side * 4., side * 6.5))
        faces += sweep(path, y0, y1, UNDERSIDE_M, PLATFORM_M, 0)
        faces += sweep(path, side * 4. - .12, side * 4. + .12, PLATFORM_M, PLATFORM_M + .04, 7)
        faces += canopy(path, y0 - .4, y1 + .4)
        for along in (4., length / 2, length - 4.):
            x, y, ux, uy = point_along(path, along)
            local = [*box(-.18, side * 5.8 - .18, .18, side * 5.8 + .18, PLATFORM_M, CANOPY_M - .25, 2),
                     *box(-.8, side * 5.3 - .6, .8, side * 5.3 + .6, 0., UNDERSIDE_M, 0),
                     *box(-.15, y0 - .35, .15, y1 + .35, CANOPY_M - .8, CANOPY_M - .55, 2)]
            faces += placed(local, x, y, ux, uy)
    return faces


def line_mesh(stops, cars, path=None):
    """Sweep the authoritative track, including curves and interchanges; never reconstruct it from stops."""
    path = path if path is not None else stops
    path = [tuple(p) for i, p in enumerate(path) if i == 0 or hypot(p[0] - path[i - 1][0], p[1] - path[i - 1][1]) > 1e-6]
    if len(path) < 2:
        return tuple(placed(station(cars), *stops[0], 1., 0.)) if stops else ()
    extension = station_length(cars) / 2 + 2.
    first, last = point_along(path, 0.), point_along(path, polyline_length(path))
    extended = [(first[0] - extension * first[2], first[1] - extension * first[3]), *path,
                (last[0] + extension * last[2], last[1] + extension * last[3])]
    centre = sampled(extended)
    faces = sweep(centre, -DECK_WIDTH_M / 2, DECK_WIDTH_M / 2, UNDERSIDE_M, TRACK_M, 0)
    for offset in (-TRACK_OFFSET_M, TRACK_OFFSET_M):
        for rail in (offset - GAUGE_M / 2, offset + GAUGE_M / 2):
            faces += sweep(centre, rail - .06, rail + .06, TRACK_M, TRACK_M + .12, 2)
    for x, y, ux, uy in supports(extended):
        pier = [*box(-.7, -.7, .7, .7, 0., UNDERSIDE_M, 0),
                *box(-1., -3.6, 1., 3.6, UNDERSIDE_M - .6, UNDERSIDE_M, 0)]
        faces += placed(pier, x, y, ux, uy)
    for stop in stops:
        along = nearest_along(extended, stop)
        half = station_length(cars) / 2
        faces += station_on_path(sampled(sub_polyline(extended, along - half, along + half)))
    if len(stops) > 1:
        anchor = nearest_along(extended, stops[0])
        for index in range(cars):
            along = anchor + (index - (cars - 1) / 2) * (CAR_LENGTH_M + CAR_GAP_M)
            x, y, ux, uy = point_along(extended, along)
            faces += placed(carriage(front=index == cars - 1, back=index == 0),
                            x + uy * TRACK_OFFSET_M, y - ux * TRACK_OFFSET_M, ux, uy)
    return tuple(placed(faces, 0., 0., 1., 0.))

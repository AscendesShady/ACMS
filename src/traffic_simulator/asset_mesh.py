"""Small opaque polygon meshes shared by the raster and instanced GPU map renderers. No Qt.

A vertex is (longitudinal half-length fraction, half-width fraction, height/width,
longitudinal offset/width). The fourth component keeps round tyres round at any vehicle L/W.
Face winding points outwards; front is +x and height is +z, all expanded to physical metres.
"""

from functools import lru_cache
from math import cos, pi, sin, sqrt
from typing import NamedTuple


PEDESTRIAN_HEIGHT_M = 1.75
FOOTBRIDGE_DECK_M = 6.0  # display assumption consistent with walking's assumed stair-climb height


class Face(NamedTuple):
    vertices: tuple
    ink: int = -1  # -1: body paint; other entries index theme's material inks
    part: int = 0  # tyres have a separate part so the raster fallback can hide the far axle side


def vertex(x, y, z, offset=0.):
    return float(x), float(y), float(z), float(offset)


def wheel_fit(skin, length, width):
    axle, radius = (.55, .48) if skin == 6 else (.61, .33) if skin == 7 else (.63, .18) if skin in (1, 2, 3) else (.68, .18)
    if skin == 4:
        radius = .21  # the bus skirt's wheel opening must fit inside very short edited footprints too
    return min(1., length / 2 * (1 - axle) / (radius * width))


def expand(p, length, width, fit=1.):
    return p[0] * length / 2 + p[3] * width * fit, p[1] * width / 2, p[2] * width


def normal(points):
    a, b, c = points[:3]
    u, v = [b[i] - a[i] for i in range(3)], [c[i] - a[i] for i in range(3)]
    n = (u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2], u[0] * v[1] - u[1] * v[0])
    size = sqrt(sum(t * t for t in n)) or 1.
    return tuple(t / size for t in n)


def solid(bottom, top, ink=-1, walls=None):
    faces = [Face(tuple(top), ink)]
    for i, a in enumerate(bottom):
        j = (i + 1) % len(bottom)
        faces.append(Face((a, bottom[j], top[j], top[i]), ink if walls is None else walls[i]))
    return faces


def box(x0, y0, x1, y1, z0, z1, ink=-1):
    ring = ((x1, y1), (x0, y1), (x0, y0), (x1, y0))
    return solid(tuple(vertex(x, y, z0) for x, y in ring), tuple(vertex(x, y, z1) for x, y in ring), ink)


def wheel(x, radius, y0, y1, spokes=False):
    """Eight-sided tyres around the lateral axle; radius is a fraction of physical width."""
    faces = []
    inner = radius * .72 if spokes else radius * .48
    for i in range(8):
        a, b = i * pi / 4, (i + 1) * pi / 4
        def p(angle, r, y):
            return vertex(x, y, radius + sin(angle) * r, cos(angle) * r)
        faces.append(Face((p(a, radius, y0), p(a, radius, y1), p(b, radius, y1), p(b, radius, y0)), 1))
        for y, reverse in ((y0, False), (y1, True)):
            ring = (p(a, radius, y), p(b, radius, y), p(b, inner, y), p(a, inner, y))
            faces.append(Face(tuple(reversed(ring)) if reverse else ring, 1))
            if spokes:
                if i % 2 == 0:
                    # Broad enough to survive small RTS scales; open space remains between spokes.
                    spoke = (vertex(x, y, radius), p(a - .06, inner, y), p(a + .06, inner, y))
                    faces.append(Face(tuple(reversed(spoke)) if reverse else spoke, 5))
            else:
                disc = (vertex(x, y, radius), p(a, inner, y), p(b, inner, y))
                faces.append(Face(tuple(reversed(disc)) if reverse else disc, 5))
    return [face._replace(part=1) for face in faces]


def tube(a, b, thickness=.045, ink=-1):
    """A narrow frame bar with a real cross section, leaving the frame's triangular gaps open."""
    d = tuple(b[i] - a[i] for i in range(3))
    size = sqrt(sum(t * t for t in d)) or 1.
    d = tuple(t / size for t in d)
    u = (-d[1], d[0], 0.) if abs(d[2]) < .95 else (1., 0., 0.)
    size = sqrt(sum(t * t for t in u)) or 1.
    u = tuple(t / size for t in u)
    v = (d[1] * u[2] - d[2] * u[1], d[2] * u[0] - d[0] * u[2], d[0] * u[1] - d[1] * u[0])
    offsets = (u, v, tuple(-t for t in u), tuple(-t for t in v))
    bottom = tuple(vertex(*(a[i] + o[i] * thickness for i in range(3))) for o in offsets)
    top = tuple(vertex(*(b[i] + o[i] * thickness for i in range(3))) for o in offsets)
    return [*solid(bottom, top, ink), Face(tuple(reversed(bottom)), ink)]


def side_panel(x0, x1, y, z0, z1, ink):
    points = tuple(vertex(x, y, z) for x, z in ((x0, z0), (x1, z0), (x1, z1), (x0, z1)))
    return Face(tuple(reversed(points)) if y > 0 else points, ink)


def cross_on_roof(x, y, z, length=.22, width=.16):
    return [*box(x - length, y - width / 3, x + length, y + width / 3, z, z + .002, 3),
            *box(x - length / 3, y - width, x + length / 3, y + width, z, z + .003, 3)]


@lru_cache(maxsize=9)
def vehicle_mesh(skin):
    """Original low-poly models, inspired by readable RTS silhouettes rather than copied game assets."""
    if skin == 0:
        return tuple(box(-1, -1, 1, 1, 0, .7))  # legacy frames retain their old plain footprint
    faces = []
    if skin in (6, 7):
        bicycle = skin == 6
        radius = .48 if bicycle else .33
        axle = .55 if bicycle else .61
        faces += wheel(-axle, radius, -.09, .09, bicycle)
        faces += wheel(axle, radius, -.09, .09, bicycle)
        rear, crank, saddle, head, front = (-axle, 0, radius), (-.04, 0, .4), (-.22, 0, 1.05), (.35, 0, 1.15), (axle, 0, radius)
        for a, b in ((rear, crank), (rear, saddle), (crank, saddle), (saddle, head), (head, crank), (head, front)):
            faces += tube(a, b, .035 if bicycle else .07)
        faces += box(-.38, -.35, -.07, .35, 1.02, 1.13, 1)  # saddle
        faces += tube((.32, -.98, 1.2), (.32, .98, 1.2), .025, 5)  # handlebar, full physical width
        if not bicycle:
            faces += box(-.42, -.5, .25, .5, .55, .95)  # tank and engine
        # Rider: bent legs, torso and helmet give a cycle silhouette in both projections.
        for y in (-.28, .28):
            faces += tube((-.19, y, 1.2), (.08, y, .8), .09, 1)
            faces += tube((.08, y, .8), (-.02, y, .45), .065, 1)
            faces += tube((-.1, y, 1.65), (.33, y * 2, 1.2), .065, 5)
        faces += box(-.28, -.36, -.03, .36, 1.1, 1.65)
        faces += box(-.15, -.28, .12, .28, 1.65, 1.98, 5)
        return tuple(faces)
    if skin in (1, 2, 3):
        # Chamfered body, bonnet and boot below a separate, sloped glazed cabin.
        ring = ((1, .58), (.8, .87), (-.82, .9), (-1, .66), (-1, -.66), (-.82, -.9), (.8, -.87), (1, -.58))
        faces += solid(tuple(vertex(x, y, .2) for x, y in ring), tuple(vertex(x, y, .43) for x, y in ring))
        back, front, roof_back, roof_front, height = (-.69, .5, -.4, .12, .78)
        if skin == 2:
            back, roof_back, roof_front, height = -.85, -.68, .08, .82
        elif skin == 3:
            back, roof_back, roof_front, height = -.83, -.62, .22, .94
        bottom = tuple(vertex(x, y, .43) for x, y in ((front, .78), (back, .78), (back, -.78), (front, -.78)))
        top = tuple(vertex(x, y, height) for x, y in ((roof_front, .62), (roof_back, .62), (roof_back, -.62), (roof_front, -.62)))
        faces += solid(bottom, top, walls=(0, 0, 0, 0))
        # Roof rails distinguish the taller utility body without altering dimensions.
        if skin == 3:
            for y in (-.5, .43):
                faces += box(roof_back + .04, y, roof_front - .04, y + .06, height, height + .025, 1)
        for x in (-.63, .63):
            for y0, y1 in ((-.99, -.78), (.78, .99)):
                faces += wheel(x, .18, y0, y1)
        for x, ink in ((1, 2), (-1, 3)):
            for y in (-.46, .24):
                p = tuple(vertex(x, v, z) for v, z in ((y, .27), (y + .22, .27), (y + .22, .37), (y, .37)))
                faces.append(Face(tuple(reversed(p)) if x < 0 else p, ink))
        return tuple(faces)
    # Commercial vehicles: a distinct cab, tall passenger/load body and visible wheels.
    if skin == 4:
        ring = ((1, .8), (.94, 1), (-.94, 1), (-1, .8), (-1, -.8), (-.94, -1), (.94, -1), (1, -.8))
        # The passenger body clears the tyre crown; a recessed skirt has real gaps at the axles.
        # Outer tyre faces remain at +/- half-width, so extrusion never expands the collision footprint.
        faces += solid(tuple(vertex(x, y, .39) for x, y in ring), tuple(vertex(x, y, 1.12) for x, y in ring))
        faces += box(-.94, -.7, .94, .7, .18, .39, 1)
        for side in (-1, 1):
            for (x0, off0), (x1, off1) in (((-1, 0), (-.68, -.21)),
                                         ((-.68, .21), (.68, -.21)), ((.68, .21), (1, 0))):
                lower = tuple(vertex(x, side * y, .2, off) for x, off, y in
                              ((x1, off1, .74), (x0, off0, .74), (x0, off0, .86), (x1, off1, .86)))
                upper = tuple(vertex(p[0], p[1], .39, p[3]) for p in lower)
                # Reverse the negative-side winding as well as the side's lateral coordinates.
                if side > 0:
                    lower, upper = tuple(reversed(lower)), tuple(reversed(upper))
                faces += solid(lower, upper)
        for y in (-1, 1):
            for x in (-.8, -.53, -.26, .01, .28, .55):
                faces.append(side_panel(x, x + .21, y, .61, .98, 0))
        front = tuple(vertex(1, y, z) for y, z in ((-.7, .55), (.7, .55), (.7, 1.02), (-.7, 1.02)))
        faces.append(Face(front, 0))
        for x in (-.36, .17):
            faces += box(x, -.43, x + .23, .43, 1.12, 1.23, 5)
    else:
        faces += box(-.97, -.95, .35, .95, .22, 1.12, 5 if skin == 5 else -1)
        bottom = tuple(vertex(x, y, .25) for x, y in ((1, .86), (.35, .86), (.35, -.86), (1, -.86)))
        top = tuple(vertex(x, y, z) for x, y, z in ((.92, .8, .7), (.35, .8, .89), (.35, -.8, .89), (.92, -.8, .7)))
        faces += solid(bottom, top, walls=(-1, -1, -1, 0))
        if skin == 5:
            faces += cross_on_roof(-.3, 0, 1.12, .2, .56)
            for y, ink in ((-.65, 3), (.02, 4)):
                faces += box(.4, y, .5, y + .63, .9, .98, ink)
            for y in (-.95, .95):
                faces.append(side_panel(-.65, .1, y, .44, .55, 3))
    for x in (-.68, .68):
        for y0, y1 in ((-1, -.8), (.8, 1)):
            faces += wheel(x, .18, y0, y1)
    return tuple(faces)


@lru_cache(maxsize=9)
def vehicle_low_mesh(skin):
    """A metre-scaled distant silhouette: omit details smaller than a pixel, never enlarge isometric bodies."""
    if skin in (6,7):
        radius,axle=(.48,.55) if skin==6 else (.33,.61)
        faces=[]
        for x in (-axle,axle):
            faces += [f for i,f in enumerate(wheel(x,radius,-.09,.09)) if i%5==0]
        faces += tube((-.5,0,radius),(.35,0,1.1),.05)
        faces += box(-.28,-.36,-.03,.36,1.1,1.65)
        faces += box(-.15,-.28,.12,.28,1.65,1.98,5)
        return tuple(faces)
    return tuple(f for f in vehicle_mesh(skin) if f.part==0 and f.ink in (-1,0))


@lru_cache(maxsize=1)
def pedestrian_mesh():
    """Simple metre-scaled person: separate legs, torso, arms and head; forward is +x."""
    faces = []
    for y0, y1 in ((-.22, -.07), (.07, .22)):
        faces += box(-.11, y0, .11, y1, 0., .78, 1)
    faces += box(-.14, -.21, .14, .21, .75, 1.36)
    for y0, y1 in ((-.30, -.22), (.22, .30)):
        faces += box(-.09, y0, .09, y1, .78, 1.3)
    faces += box(-.12, -.13, .16, .13, 1.39, PEDESTRIAN_HEIGHT_M, 5)
    return tuple(faces)


def footbridge_mesh(half):
    """Deck across local y, with parapets, supports and illustrative stairs outside the carriageway."""
    h = FOOTBRIDGE_DECK_M
    faces = box(-1.5, -half, 1.5, half, h - .35, h)
    for x in (-1.5, 1.35):
        faces += box(x, -half, x + .15, half, h, h + 1.1, 1)
    for side in (-1, 1):
        y = side * (half - .8)
        faces += box(-1.1, y - .25, 1.1, y + .25, 0., h - .35, 1)
        for i in range(12):
            faces += box(-5.5 + i / 3, y - .65, -5.5 + (i + 1) / 3, y + .65,
                         0., h * (i + 1) / 12)
    return tuple(faces)

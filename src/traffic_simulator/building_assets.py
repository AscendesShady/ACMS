"""Metre-scaled low-poly institution structures. Defaults are illustrative, never surveyed data. No Qt."""

from functools import lru_cache
from math import sqrt

from .asset_mesh import Face, box, side_panel, vertex
from .models import BUILDING_HEIGHTS_M


def height(item):
    return BUILDING_HEIGHTS_M[item.kind] if item.height_m is None else item.height_m


@lru_cache(maxsize=128)
def mesh(kind, area_m2, height_m):
    """The entire structure, including its roof, fits the exact footprint and requested total height."""
    side = sqrt(area_m2)
    h = height_m / side
    wall = h * .8 if kind == "Residential" else max(h - min(2., height_m * .18) / side, h * .8)
    faces = box(-1, -1, 1, 1, 0, wall)
    if kind == "Residential":
        # Hipped roof with a short ridge: an instantly readable RTS residential silhouette.
        a, b, c, d = (vertex(x, y, wall) for x, y in ((1, 1), (-1, 1), (-1, -1), (1, -1)))
        r, s = vertex(-.6, 0, h), vertex(.6, 0, h)
        faces += [Face((a, b, r, s), 1), Face((b, c, r), 1), Face((c, d, s, r), 1), Face((d, a, s), 1)]
    else:
        # A recessed service penthouse and broad terrace; total height remains h, not h plus scenery.
        faces += box(-.5, -.48, .35, .44, wall, h, 1)
        if kind == "Hospital":
            for dx,dy in ((.16,.4/3),(.16/3,.4)):
                faces.append(Face(tuple(vertex(x,y,h) for x,y in
                                        ((-.08+dx,dy),(-.08-dx,dy),(-.08-dx,-dy),(-.08+dx,-dy))),3))
        elif kind == "Shop":
            faces += box(-.9, .82, .9, 1., wall * .72, wall * .8, 1)  # recessed awning, within footprint
    # Storey and bay spacing use metres. Bound detail count on very large imported footprints.
    floors = max(1, min(30, int(wall * side / 3.2)))
    bays = max(1, min(24, int(side / 4.)))
    for floor in range(floors):
        z0 = (floor * (wall * side / floors) + .9) / side
        z1 = min(z0 + 1.5 / side, wall - .25 / side)
        if z1 <= z0:
            continue
        for i in range(bays):
            x0 = -1 + (i + .22) * 2 / bays
            x1 = -1 + (i + .78) * 2 / bays
            for y in (-1, 1):
                faces.append(side_panel(x0, x1, y, z0, z1, 0))
            for x in (-1, 1):
                panel = side_panel(x0, x1, -x, z0, z1, 0)
                # A quarter turn preserves winding and gives the other two facades identical real spacing.
                faces.append(Face(tuple(vertex(-p[1], p[0], p[2]) for p in panel.vertices), 0))
    return tuple(faces)

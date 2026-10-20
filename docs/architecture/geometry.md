# geometry.py: vector primitives (no Qt)

- `polyline_length(points)`.
- `point_along(points, d)`: returns `(x, y, ux, uy)`, the point and unit direction `d` metres along.
- `offset_polyline(points, offset)`: a parallel line, e.g. a lane centre line.
- `curvature_at(points, curvature, d, offset)`: curvature on the centre line, or on a line
  offset towards one side: `κ / (1 − κ·offset)`, so a lane inside a bend is tighter.
- `segment_hit(a, b, c, d)`: the parametric intersection of two segments (strictly inside both).
- `polyline_meets_box(points, left, top, right, bottom)`: a point inside the box or a segment
  crossing one of its edges (a crossing selection box).
- `distance_to_segment(p, a, b)`.
- `nearest_along(points, p)`: how far along the polyline its nearest point to p is (a dragged bus
  stop, a bay's middle on its lane).
- `rail_leg(track, x, y, radius, max_turn, platform_m, transition_m, heading)`: a metro leg from a track's last
  point (a station) laid out as a railway is: straight on for half a platform, a bend (`_bend`: a clothoid transition,
  curvature rising evenly to 1 / radius, the circular arc, a transition back; transitions shortened on a small turn so
  the curvature never passes 1 / radius; integrated in 1 m pieces, points every 5 m), then straight into the next
  station for half a platform at least. A plain click: the turn found by bisection so the last straight points at
  (x, y) (straight ahead: no bend); one needing more than `max_turn`, or inside the bend's reach, drawn in to
  `max_turn` and as far out as the point. Aimed (`heading`, a unit direction through the station: the pen tool's
  drag): the turn is the angle between the two headings, the two straights solved (2 x 2); ValueError for an aim past
  `max_turn` or one the straights can't meet (an S-bend: a station between). One point of track: straight.

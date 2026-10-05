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

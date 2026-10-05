"""Vector geometry checks. Run with `python tests/test_geometry.py` (or the test monitor)."""

from math import hypot, isclose

from traffic_simulator.geometry import (
    distance_to_segment,
    nearest_along,
    offset_polyline,
    point_along,
    polyline_length,
    segment_hit,
)

L_SHAPE = [(0.0, 0.0), (100.0, 0.0), (100.0, 50.0)]  # 100 m east, then 50 m down


def test_a_point_projects_onto_the_nearest_place_along_the_line():
    assert nearest_along(L_SHAPE, (40.0, -7.0)) == 40.0  # beside the first leg
    assert nearest_along(L_SHAPE, (130.0, 30.0)) == 130.0  # beside the second: 100 m + 30 m down it
    assert nearest_along(L_SHAPE, (-20.0, 5.0)) == 0.0 and nearest_along(L_SHAPE, (100.0, 90.0)) == 150.0  # clamped


def test_positions_are_continuous_distances_along_the_line():
    assert polyline_length(L_SHAPE) == 150
    assert point_along(L_SHAPE, 37.25) == (37.25, 0.0, 1.0, 0.0)  # no snapping to a grid
    x, y, ux, uy = point_along(L_SHAPE, 120.5)
    assert (x, y, ux, uy) == (100.0, 20.5, 0.0, 1.0)  # past the corner, heading down
    assert point_along(L_SHAPE, 999)[:2] == (100.0, 50.0)  # clamped to the end


def test_lane_lines_are_offsets_of_the_centre_line():
    lane = offset_polyline(L_SHAPE, 1.75)  # half a 3.5 m lane, to the normal (-uy, ux)
    assert lane[0] == (0.0, 1.75) and lane[-1] == (98.25, 50.0)


def test_crossings_and_distances():
    t, u = segment_hit(0, 0, 10, 10, 0, 10, 10, 0)
    assert isclose(t, 0.5) and isclose(u, 0.5)
    assert segment_hit(0, 0, 10, 0, 0, 1, 10, 1) is None  # parallel
    assert segment_hit(0, 0, 10, 0, 10, 0, 10, 5) is None  # touching at an end is not a crossing
    assert isclose(distance_to_segment((5, 3), (0, 0), (10, 0)), 3)
    assert isclose(distance_to_segment((13, 4), (0, 0), (10, 0)), hypot(3, 4))


if __name__ == "__main__":
    for name, check in list(globals().items()):
        if name.startswith("test_"):
            check()
    print("ok")

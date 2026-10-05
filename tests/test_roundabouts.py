"""Roundabout sizing checks. Run with `python tests/test_roundabouts.py` (or the test monitor)."""

from traffic_simulator.network_generator import generate_network
from traffic_simulator.roundabouts import CATEGORIES, design_roundabout, fastest_path_speed_kmh


def design(lanes=2, speed=50.0):
    return design_roundabout(generate_network(2, 2, lanes_per_direction=lanes, speed_limit_kmh=speed), "1,1")


def test_fastest_path_relation():
    assert 29 < fastest_path_speed_kmh(30.48) < 31  # 100 ft path on a -2 % slope: about 18.8 mph
    assert fastest_path_speed_kmh(40) > fastest_path_speed_kmh(20)  # wider path, faster


def test_category_follows_entry_lanes_and_speed():
    for lanes, speed, expected in ((1, 30, "Mini"), (1, 50, "Single"), (2, 50, "Two"), (3, 60, "Three")):
        result = design(lanes, speed)
        assert result.category.startswith(expected), (lanes, speed, result.category)
        low, high = next(entry[2] for entry in CATEGORIES.values() if entry[0] == result.category)
        assert low <= result.inscribed_diameter_m <= high  # inside the guide's typical ICD range
        assert result.island_diameter_m > 0
        assert abs(result.inscribed_diameter_m - result.island_diameter_m
                   - 2 * result.circulatory_width_m) < 1e-9


def test_faster_approaches_get_larger_roundabouts():
    sizes = [design(1, speed).inscribed_diameter_m for speed in (35, 40, 50)]
    assert sizes[0] < sizes[1] == sizes[2]  # single-lane design speed is capped at 40 km/h
    assert design(1, 35).design_speed_kmh == 35 and design(1, 50).design_speed_kmh == 40


def test_one_way_entries_count_all_their_lanes():
    network = generate_network(2, 2, lanes_per_direction=1)
    network.roads["1,1-2,1"].direction = "one-way"  # 1,1 -> 2,1: two merged lanes arrive at 2,1
    assert design_roundabout(network, "2,1").entry_lanes == 2
    assert design_roundabout(network, "1,1").entry_lanes == 1  # that road only leaves 1,1


if __name__ == "__main__":
    for name, check in list(globals().items()):
        if name.startswith("test_"):
            check()
    print("ok")

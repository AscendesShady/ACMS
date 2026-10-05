"""Network design checks (design_checks.py), accessibility (accessibility.py), vulnerability (network_tools.cut_elements,
critical_links' area closure) and the assistant's structured errors (network_tools.explain_error).
Run with `python tests/test_design_checks.py` (or the test monitor)."""

from math import exp

from traffic_simulator import accessibility, design_checks, network_tools
from traffic_simulator.mode_behavior import VehicleMode
from traffic_simulator.models import Institution
from traffic_simulator.network_generator import generate_network

CARS = [VehicleMode("Car", "#888888")]


def test_the_formulas_reproduce_the_green_books_own_figures():
    # Table 3-1 (metric, rounded for design): 50 km/h 65 m, 100 km/h 185 m; Eq 3-2 gives 63.4 and 184.2
    assert abs(design_checks.stopping_sight_m(50) - 63.4) < 0.1 and abs(design_checks.stopping_sight_m(100) - 184.2) < 0.1
    # Eq 9-1's worked example: a car turning left onto a 100 km/h road, 7.5 s: 0.278 x 100 x 7.5 = 208.5 m
    assert design_checks.GAPS_S["car"] == (7.5, 0.5) and abs(0.278 * 100 * 7.5 - 208.5) < 1e-9
    # Eq 3-37: HSO = R(1 - cos(28.65 S / R)); a 200 m radius and 85 m sight: about 4.5 m
    assert abs(design_checks.sight_offset_m(85, 200) - 200 * (1 - __import__("math").cos(__import__("math").radians(28.65 * 85 / 200)))) < 1e-9
    assert 4.4 < design_checks.sight_offset_m(85, 200) < 4.6
    bus, trailer = VehicleMode("Bus", "#888888", length_m=12.0), VehicleMode("Lorry", "#888888", length_m=16.5)
    assert design_checks.design_vehicle(CARS[0]) == ("passenger car", 6.40)
    assert design_checks.design_vehicle(bus) == ("city bus", 11.52) and design_checks.design_vehicle(trailer)[0] == "semitrailer"


def test_the_audit_finds_each_kind_of_problem():
    network = generate_network(3, 3)
    for node in network.nodes.values():
        node.control = "uncontrolled"
    plain = design_checks.audit(network, CARS)
    assert plain["design_vehicle"] == "passenger car" and plain["counts"]["skew"] == 0
    assert plain["counts"]["intersection_sight"] == 9 and "keep each sight triangle" in plain["findings"]["intersection_sight"][0]["note"]
    with_bus = design_checks.audit(network, CARS + [VehicleMode("Bus", "#888888", length_m=12.0)])
    assert with_bus["design_vehicle"] == "city bus" and with_bus["counts"]["turning_paths"] > 0  # grid corners: tight
    worst = with_bus["findings"]["turning_paths"][0]
    assert worst["tightest_m"] < 11.52 and worst["turns_too_tight"] >= 1 and worst["needs_m"] == 11.52
    # a building in the corner of junction 2,2 (the triangle runs 4.4 m back from the major road's edge) blocks it
    node = network.node("2,2")
    network.institutions["IN1"] = Institution("IN1", "Shop", node.x + 12, node.y + 12, 64.0, 100.0)  # 8-16 m off the corner
    blocked = design_checks.audit(network, CARS)
    assert any(row["junction"] == "2,2" and "IN1" in row["blocked_by"] for row in blocked["findings"]["intersection_sight"])
    # arterial junctions too close: a fast road between two junctions under 250 m
    short = generate_network(2, 2, default_spacing=200)
    for road in short.roads.values():
        road.speed_limit_kmh = 70
    spaced = design_checks.audit(short, CARS)
    assert spaced["counts"]["junction_spacing"] > 0 and spaced["findings"]["junction_spacing"][0]["needs_m"] == 250
    # a skewed leg: move a junction so two legs meet at a sharp angle
    skewed = generate_network(2, 2)
    network_tools.move_junctions(skewed, ["1,2"], 380, 0)
    assert design_checks.audit(skewed, CARS)["counts"]["skew"] > 0


def test_accessibility_counts_what_people_can_reach():
    network = generate_network(2, 2)
    home, office = network.node("1,1"), network.node("2,2")
    network.institutions["IN1"] = Institution("IN1", "Residential", home.x + 60, home.y + 60, 900.0, 100.0, people=500)
    network.institutions["IN2"] = Institution("IN2", "Office", office.x + 60, office.y + 60, 900.0, 100.0, employees=300)
    found = accessibility.measures(network, budgets=(1, 10))
    times = accessibility.minutes_from(accessibility.link_minutes(network), "1,1")
    row = found["by_junction"]["1,1"]
    assert row["jobs_within_1_min"] == (300 if times["2,2"] <= 1 else 0) and row["jobs_within_10_min"] == 300
    assert abs(row["potential_jobs"] - 300 * exp(-0.1 * times["2,2"])) < 1e-6  # Hansen's measure
    assert found["residents"] == 500 and found["residents_mean"]["jobs_within_10_min"] == 300
    assert accessibility.weighted_gini([(0, 1), (10, 1)]) == 0.5 and accessibility.weighted_gini([(5, 2), (5, 3)]) == 0.0
    report = accessibility.report(network, close_road="1,1-2,1")
    assert report["residents"] == 500 and "loss_percent" in report["closing"]
    assert report["closing"]["loss_percent"]["potential_jobs"] >= 0  # a detour can only lengthen times
    for kwargs, reason in (({"close_road": "nowhere"}, "No road"), ({"budgets": (0,)}, "budgets_min")):
        try:
            accessibility.report(network, **kwargs)
        except ValueError as exc:
            assert reason in str(exc)
        else:
            raise AssertionError(kwargs)
    try:
        accessibility.report(generate_network(2, 2))
    except ValueError as exc:
        assert "No buildings" in str(exc)
    else:
        raise AssertionError("no buildings: nothing to reach")


def test_single_points_of_failure_and_area_closures():
    network = generate_network(3, 3)
    assert network_tools.cut_elements(network)["cut_junctions"] == []  # a grid has none but its edge junctions' own ends
    first = network_tools.add_junction(network, -300, 900)["added"]
    network_tools.add_road(network, "1,1", first)
    second = network_tools.add_junction(network, -600, 900)["added"]
    network_tools.add_road(network, first, second)
    found = network_tools.cut_elements(network)
    bridges = {row["road"]: row["cuts_off"] for row in found["bridge_roads"]}
    assert bridges == {f"{first}-{second}": [second], f"1,1-{first}": [first, second]}
    joints = {row["junction"]: row["cuts_off"] for row in found["cut_junctions"]}
    assert set(joints) == {"1,1", first} and {first, second} <= set(joints["1,1"])
    flows = {(o, d): 100 for o in network.approach_nodes for d in network.approach_nodes if o != d}
    centre = network.node("2,2")
    closed = network_tools.critical_links_plan(network, flows, 5, {"x": centre.x, "y": centre.y, "radius_m": 50})
    assert set(closed["roads_closed"]) == {"1,2-2,2", "2,2-2,1", "2,2-3,2", "2,3-2,2"} and closed["extra_veh_h_per_h"] > 0
    for bad, reason in (({"x": 0}, "area is"), ({"x": 0, "y": 0, "radius_m": 5}, "10 to 5000"),
                        ({"x": 1e6, "y": 1e6, "radius_m": 100}, "No road touches")):
        try:
            network_tools.critical_links_plan(network, flows, 5, bad)
        except ValueError as exc:
            assert reason in str(exc), (bad, exc)
        else:
            raise AssertionError(bad)


def test_refusals_come_back_as_data_with_a_fix():
    network = generate_network(3, 3)
    unknown = network_tools.explain_error("No road called '1,1-2,2'.", network)
    assert unknown["error_type"] == "unknown_id" and "1,2-2,2" in unknown["did_you_mean"]
    assert network_tools.explain_error("No route from 1W to 1E.")["error_type"] == "unroutable"
    assert network_tools.explain_error("A run is going on: stop it first.")["error_type"] == "busy"
    assert network_tools.explain_error("duration_min must be 1 to 240.")["error_type"] == "bad_argument"
    other = network_tools.explain_error("Something odd.")
    assert other == {"error": "Something odd.", "error_type": "refused", "suggested_fix": other["suggested_fix"]}


if __name__ == "__main__":
    for name, check in list(globals().items()):
        if name.startswith("test_"):
            check()
    print("ok")

"""Institutions: where a building can stand, its circle of influence and links, the trips it makes, its
door and catchment, its mode split by logit (calibrated to observed shares), its trips in a Run, saving
them, placing one by tool and on the real map RTS-style, and the Mode split window (offscreen).
Run with `python tests/test_institutions.py` (or the test monitor)."""

import os
import tempfile
from copy import deepcopy
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QWheelEvent  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from traffic_simulator import network_tools as tools  # noqa: E402
from traffic_simulator.institutions import (GROW, Placement, _through, catchment, door, footprint_problem,  # noqa: E402
                                            hourly_people, hourly_trips, links, place, run_demand, vehicle_split,
                                            COMMUTE_OCCUPANCY, KINDS)
from traffic_simulator.mode_behavior import VehicleModes  # noqa: E402
from traffic_simulator.models import Institution, institution_problem  # noqa: E402
from traffic_simulator.simulation import Simulation  # noqa: E402
from traffic_simulator.network_generator import generate_network  # noqa: E402
from traffic_simulator.network_io import load_network, save_network  # noqa: E402

APP = QApplication.instance() or QApplication([])


def block_centre(network):
    """The middle of the grid's first block: clear ground, 250 m from its four junctions."""
    a, c = network.nodes["1,1"], network.nodes["2,2"]
    return (a.x + c.x) / 2, (a.y + c.y) / 2


def test_a_building_stands_only_on_clear_ground():
    network = generate_network(3, 4)
    x, y = block_centre(network)
    assert footprint_problem(network, x, y, 6000) is None
    node = network.nodes["1,1"]
    assert footprint_problem(network, node.x, node.y + 150, 400).startswith("road ")  # on the road south of it
    assert footprint_problem(network, node.x + 12, node.y + 12, 100).startswith(("road ", "junction "))  # the verge
    assert footprint_problem(network, node.x + 20, node.y + 20, 100) is None  # 15 m out: carriageway 7, verge 3, half 5
    network.nodes["1,1"].control = "roundabout"  # a roundabout's circle reaches past its roads
    assert footprint_problem(network, node.x + 30, node.y + 30, 100) == "junction 1,1"
    first = place(network, "School", x, y, 6000, 100)
    assert footprint_problem(network, x + 60, y, 2500) == first.id  # on the school
    assert footprint_problem(network, x + 200, y + 200, 100) is None  # beyond it, still off the roads
    before = deepcopy(network)
    for bad in ({"x": node.x, "y": node.y + 150}, {"car_share": 150}, {"shift_end_h": 8.5}, {"radius_m": 3}):
        args = {"kind": "School", "x": x + 200, "y": y + 200, "area_m2": 100, "radius_m": 100,
                "shift_start_h": 8.5, **bad}
        try:
            place(network, **args)
        except ValueError:
            pass
        else:
            raise AssertionError(f"{bad} should have been refused")
    assert network == before  # refusals change nothing
    assert place(network, "Shop", x + 200, y + 200, 100, 100).id == "IN2"


def test_placing_is_move_click_scroll_click():
    network = generate_network(3, 4)
    x, y = block_centre(network)
    node = network.nodes["1,1"]
    placing = Placement(6000)
    assert not placing.click(network) and not placing.grow(network, 1)  # nowhere yet, no circle yet
    placing.move(network, node.x, node.y + 150)
    assert placing.problem and not placing.click(network)  # red: on a road, refused
    placing.move(network, x, y)
    assert placing.problem is None and not placing.click(network)  # green: set down, now the circle
    assert placing.radius_m is not None and placing.links == []  # just past its corners: nothing yet
    placing.move(network, 0, 0)
    assert (placing.x, placing.y) == (x, y)  # it stays where it was set down
    start = placing.radius_m
    while not placing.links:
        placing.grow(network, 1)
    assert placing.radius_m >= start * GROW and placing.radius_m >= 353  # the corner junctions are 353.6 m away
    assert set(placing.links) == {"1,1", "2,1", "1,2", "2,2"}  # all four at once: they are equally far
    for _ in range(50):
        placing.grow(network, -1)
    assert abs(placing.radius_m - 6000 ** 0.5 / 2) < 1e-9  # never inside its own walls
    assert placing.click(network)  # done


def test_trips_follow_the_shift_and_are_shared_by_the_links():
    school = Institution("IN1", "School", 0, 0, 6000, 400, 600, 50, 8.5, 15.5, 30)
    arrive, leave = hourly_trips(school)
    assert arrive[8] == leave[15] == (600 + 50) * 0.3 and sum(arrive) == sum(leave) == 195
    homes = Institution("IN2", "Residential", 0, 0, 4000, 400, 400, 0, 8.0, 17.5, 50)
    arrive, leave = hourly_trips(homes)
    assert (leave[7], leave[8], leave[9]) == (arrive[16], arrive[17], arrive[18]) == (50, 100, 50)  # out, then home,
    assert sum(arrive) == sum(leave) == 200  # spread over the hours round the shift's start and end (RESIDENT_SPREAD)
    office = KINDS["Office"].defaults  # ITE 710: about 3.3 car trips a day per employee at ~95 % by car
    staff_car = 2 * office[2] * office[5] / 100 / COMMUTE_OCCUPANCY
    assert 0.85 * 3.3 * office[2] * office[5] / 95 < staff_car + 2 * office[1] * office[5] / 100 / 1.5 < 1.15 * 3.3 * office[2] * office[5] / 95
    shop = Institution("IN3", "Shop", 0, 0, 3000, 400, 1200, 0, 9.0, 21.0, 100)
    arrive, _ = hourly_trips(shop)
    assert arrive[8] == 0 and abs(arrive[9] - 100) < 1e-9 and abs(sum(arrive) - 1200) < 1e-9  # evenly, all day
    night = _through(22, 6)  # past midnight
    assert abs(sum(night) - 1) < 1e-9 and night[23] == night[2] == 1 / 8 and night[12] == 0

    network = generate_network(3, 4)
    x, y = block_centre(network)
    school.x, school.y = x, y
    assert links(network, x, y, 400) == ["1,1", "1,2", "2,1", "2,2"]  # equally far: in name order
    assert door(network, school) == "1,1" and catchment(network, school) == ["1,2", "2,1", "2,2"]  # the rest come
    assert sum(hourly_people(school)[0]) == 650 and hourly_people(school)[0][8] == 650  # everyone, any mode
    school.radius_m = 100
    assert catchment(network, school) == []  # a circle reaching nothing but... nothing: no trips


def _school(network):
    x, y = block_centre(network)
    return place(network, "School", x, y, 6000, 600, people=600, employees=50, shift_start_h=8.5, shift_end_h=15.5,
                 car_share=30)


def test_the_mode_split_is_a_logit_calibrated_to_what_is_observed():
    network, modes = generate_network(3, 4), VehicleModes().modes
    school = _school(network)
    percent, info = tools.institution_split(network, school, modes, {})
    assert set(percent) == {"Walk", "Bus", "Car", "Bike"} and abs(sum(percent.values()) - 100) < 0.05
    assert info["door"] == "1,1" and info["origins"] >= 3 and "assumed" in info and info["mean_trip_km"] > 0
    calibrated, info = tools.institution_split(network, school, modes, {"observed_percent": {"Car": 30}})
    assert abs(calibrated["Car"] - 30) < 0.01 and "Train 2009" in info["calibration"]  # c += ln(observed/predicted)
    rest = {m: percent[m] / (100 - percent["Car"]) for m in percent if m != "Car"}
    assert all(abs(calibrated[m] / 70 - rest[m]) < 0.002 for m in rest)  # the others keep their relative odds
    every = {"Walk": 40, "Bus": 10, "Car": 30, "Bike": 20}
    assert all(abs(v - every[m]) < 0.01 for m, v in tools.institution_split(network, school, modes,
                                                                            {"observed_percent": every})[0].items())
    no_bus, _ = tools.institution_split(network, school, modes, {"observed_percent": {"Bus": 0}})
    assert "Bus" not in no_bus  # an observed 0 leaves the mode out
    far = tools.institution_split(network, school, modes, {"walk_kmh": 2})[0]
    assert far["Walk"] < percent["Walk"]  # slower walking, fewer walk
    dear = tools.institution_split(network, school, modes, {"alternatives": [
        {"mode": "Walk"}, {"mode": "Car", "out_of_vehicle_min": 3, "cost_per_km": 300}]})[0]
    assert set(dear) == {"Walk", "Car"} and dear["Walk"] > 50
    for args, message in (({"observed_percent": {"Car": 80, "Walk": 30}}, "add up to"),
                          ({"observed_percent": every | {"Car": 20}}, "add up to"),
                          ({"observed_percent": {"Tram": 5}}, "observed_percent is"),
                          ({"alternatives": [{"mode": "Walk"}, {"mode": "Rocket"}]}, "different mode"),
                          ({"coefficients": {"cost": 0.1}}, "0 or below"), ({"walk_kmh": 30}, "walk_kmh")):
        try:
            tools.institution_split(network, school, modes, args)
        except ValueError as exc:
            assert message in str(exc), (message, str(exc))
        else:
            raise AssertionError(args)
    school.radius_m = 80
    try:
        tools.institution_split(network, school, modes, {})
    except ValueError as exc:
        assert "make its circle bigger" in str(exc)
    else:
        raise AssertionError("no catchment, no split")


def test_its_trips_go_into_a_run_catchment_to_door_and_back():
    network, modes = generate_network(3, 4), VehicleModes().modes
    school = _school(network)
    school.mode_shares = {"Walk": 40.0, "Bus": 10.0, "Car": 30.0, "Bike": 20.0}
    per_person, split, unknown = vehicle_split(school, modes)
    assert abs(per_person - (0.10 / 20 + 0.30 / 1.5 + 0.20 / 1.0)) < 1e-9 and not unknown  # people / occupancy
    modes[0].headway_min = 10  # the bus runs to a timetable: its riders add no vehicle
    per_person, split, _ = vehicle_split(school, modes)
    assert abs(per_person - 0.4) < 1e-9 and split[0] == 0 and abs(split[1] - 0.5) < 1e-9
    daily, notes = run_demand(network, modes, daily=True)
    origins = catchment(network, school)
    assert len(daily) == 4 * len(origins) and not notes and all(not pair.timetabled for pair in daily)  # staff, pupils
    arriving = [pair for pair in daily if pair.destination == "1,1"]
    assert {pair.origin for pair in arriving} == set(origins)
    staff = 50 * (0.30 / COMMUTE_OCCUPANCY + 0.20)  # commuting: at most 1.1 to a car
    pupils = 600 * 0.4 * 2  # each car that brings or fetches a pupil drives on again: in and out, morning and afternoon
    assert abs(sum(pair.daily for pair in arriving) - (staff + pupils)) < 1e-6 and arriving[0].shares[8] == 1  # 08:00
    assert any(pair.origin == "1,1" and abs(pair.shares[8] - 0.5) < 1e-9 for pair in daily)  # drop-off cars drive on
    hourly, notes = run_demand(network, modes, daily=False)
    assert "08:00-09:00" in notes[0] and abs(sum(p.hourly for p in hourly) - (staff + pupils)) < 1e-6  # the design hour
    simulation = Simulation(network, hourly, modes, seed=3)
    while simulation.time < 600:
        simulation.step()
    assert simulation.spawned_by_mode["Car"] and simulation.spawned_by_mode["Bike"]
    assert not simulation.spawned_by_mode["Bus"]  # no timetable of its own on its pairs
    school.mode_shares = {"Walk": 50.0, "Hovercraft": 50.0}
    assert "no vehicle type called Hovercraft" in run_demand(network, modes, daily=True)[1][0]
    school.mode_shares, school.radius_m = {}, 80
    assert "make its circle bigger" in run_demand(network, modes, daily=True)[1][0]


def test_a_tool_places_one_near_a_junction_with_its_catchment():
    network = generate_network(3, 4)
    result = tools.run_tool(network, "place_institution", {"kind": "hospital", "near": "2,2"})
    item = network.institutions[result["placed"]]
    assert item.kind == "Hospital" and footprint_problem(network, item.x, item.y, item.area_m2) == item.id
    assert result["door"] == "2,2" and len(result["catchment"]) >= 3 and item.people == 800  # its kind's figures
    there = tools.run_tool(network, "place_institution", {"kind": "Shop", "x": 400, "y": 600, "radius_m": 900,
                                                         "people": 2000})
    assert network.institutions[there["placed"]].radius_m == 900 and network.institutions[there["placed"]].people == 2000
    for args, message in (({"kind": "Castle", "near": "2,2"}, "kind must be"), ({"kind": "Shop"}, "Give near"),
                          ({"kind": "Shop", "near": "9,9"}, "Unknown id"), ({"kind": "Shop", "x": 500, "y": 500},
                                                                             "can't stand")):
        try:
            tools.run_tool(network, "place_institution", args)
        except (ValueError, tools.ToolError) as exc:
            assert message in str(exc), (message, str(exc))
        else:
            raise AssertionError(args)
    assert len(network.institutions) == 2 and "near 2,2" in tools.describe_call("place_institution", {"kind": "Shop",
                                                                                                    "near": "2,2"})
    tools.run_tool(network, "update_institution", {"id": item.id, "car_share": 50})
    assert item.mode_shares == {}  # a typed car share replaces a mode split


def test_institutions_are_saved_and_checked():
    network = generate_network(3, 4)
    x, y = block_centre(network)
    place(network, "Hospital", x, y, 12000, 420, people=800, employees=300, shift_start_h=22, shift_end_h=6)
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "net.json"
        save_network(network, path)
        network.institutions["IN1"].mode_shares = {"Walk": 60.0, "Car": 40.0}
        save_network(network, path)
        loaded = load_network(path)
        assert loaded.institutions == network.institutions and loaded.institutions["IN1"].mode_shares["Car"] == 40
        network.institutions["IN1"].mode_shares = {"Walk": 60.0, "Car": 30.0}
        assert "adding up to 100" in institution_problem(network.institutions["IN1"])
        network.institutions["IN1"].mode_shares = {"Walk": 60.0, "Car": 40.0}
        network.institutions["IN1"].kind = "Castle"
        try:
            save_network(network, path)
        except ValueError as exc:
            assert "must be one of" in str(exc)
        else:
            raise AssertionError("an unknown kind should be refused")


def test_placing_on_the_map_like_an_rts():
    from traffic_simulator.main import MainWindow

    window = MainWindow()
    window.install_network(generate_network(3, 4))
    window._commit()
    window.resize(1500, 950)
    window.show()
    QTest.qWaitForWindowExposed(window)
    panel, scene, view = window.institution_panel, window.scene, window.view
    window.tabs.setCurrentWidget(window.demand_tab)
    assert window.workspace.currentWidget() is window.od_matrix
    window.od_panel.demand.setCurrentIndex(window.od_panel.demand.findData("institutions"))
    assert window.workspace.currentWidget() is view and panel.isVisible()  # the map, to build on
    panel.kind_buttons["School"].click()
    assert scene.placing is not None and panel.form_box.isVisible() and panel.people_label.text() == "Students"
    panel.car_share.setValue(50)
    assert "cars come" in panel.trips.text()

    x, y = block_centre(window.network)
    view.centerOn(x, y)
    viewport = view.viewport()

    def at(px, py):
        return view.mapFromScene(QPointF(px, py))

    node = window.network.nodes["1,1"]
    QTest.mouseMove(viewport, at(node.x, node.y + 150))
    assert scene.placing.problem and "Red" in panel.banner.text()
    QTest.mouseClick(viewport, Qt.MouseButton.LeftButton, pos=at(node.x, node.y + 150))
    assert scene.placing.radius_m is None  # refused on the road
    QTest.mouseMove(viewport, at(x, y))
    assert scene.placing.problem is None and "Green" in panel.banner.text()
    QTest.mouseClick(viewport, Qt.MouseButton.LeftButton, pos=at(x, y))
    start = scene.placing.radius_m
    zoom = view.transform().m11()

    def wheel(steps):
        spot = at(x, y)
        event = QWheelEvent(QPointF(spot), QPointF(viewport.mapToGlobal(spot)), QPoint(), QPoint(0, 120 * steps),
                            Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)
        QApplication.sendEvent(viewport, event)

    for _ in range(12):
        wheel(1)
    assert scene.placing.radius_m > start and view.transform().m11() == zoom  # the wheel sized the circle, no zoom
    assert set(scene.placing.links) == {"1,1", "2,1", "1,2", "2,2"} and "reaching" in panel.banner.text()
    QTest.mouseClick(viewport, Qt.MouseButton.LeftButton, pos=at(x, y))  # done
    assert scene.placing is None and not panel.kind_buttons["School"].isChecked()
    school = window.network.institutions["IN1"]
    assert (school.kind, school.car_share) == ("School", 50) and abs(school.x - x) < 5 and abs(school.y - y) < 5  # a pixel
    assert scene.selection == ("institution", "IN1") and scene._zone is not None  # its circle shows
    assert "Catchment" in panel.details.text() and "2,2" in panel.details.text() and panel.details_box.isVisible()
    assert "By its car share: Car 50 % · Walk 50 %" in panel.details.text()

    panel.split_button.click()  # Mode split: the statistics by hand
    dialog = window._mode_split_dialog
    rows = {mode: (use, boxes, seen) for mode, use, boxes, seen in dialog.rows}
    assert list(rows) == ["Walk", "Bus", "Car", "Bike"] and "Walk" in dialog.result.text()
    rows["Car"][2].setValue(30)  # observed: 30 % by car
    assert "Car 30.0 %" in dialog.result.text() and "Constants" in dialog.result.text()
    rows["Bus"][0].setChecked(False)
    assert "Bus" not in dialog.result.text().splitlines()[0]
    dialog.apply_button.click()
    school = window.network.institutions["IN1"]
    assert abs(school.mode_shares["Car"] - 30) < 0.01 and "Bus" not in school.mode_shares and school.car_share == 30
    assert "Mode split (logit)" in panel.details.text()
    window.undo()
    assert window.network.institutions["IN1"].mode_shares == {}
    preview = window._runs_command("institution_mode_split", {"observed_percent": {"Car": 25}}, check_only=True)
    assert "IN1: Walk" in preview["preview"] and window.network.institutions["IN1"].mode_shares == {}
    done = window._runs_command("institution_mode_split", {"observed_percent": {"Car": 25}})
    assert abs(window.network.institutions["IN1"].mode_shares["Car"] - 25) < 0.01 and done["institutions"][0]["door"]
    assert any(pair.destination == done["institutions"][0]["door"] and not pair.timetabled
               for pair in window._pair_demand())  # a Run gets its trips
    dialog.close()
    window.undo()

    window.select("", "")
    window.tabs.setCurrentWidget(window.edit_tab)
    assert scene._zone is None
    QTest.mouseClick(viewport, Qt.MouseButton.LeftButton, pos=at(x + 20, y + 20))  # click the building...
    assert scene.selection == ("institution", "IN1") and window.tabs.currentWidget() is window.demand_tab
    assert window.workspace.currentWidget() is view and panel.details_box.isVisible()  # ...to see its zone

    window.undo()
    assert not window.network.institutions
    window.redo()
    assert "IN1" in window.network.institutions
    window.select("institution", "IN1")
    panel.delete_button.click()
    assert not window.network.institutions and not panel.details_box.isVisible()
    window.undo()
    window.select("institution", "IN1")
    window.delete_selection()  # the Delete key on a selected building (it once raised NameError)
    assert not window.network.institutions
    panel.kind_buttons["Shop"].click()
    QTest.keyClick(window, Qt.Key.Key_Escape)  # Esc cancels placing
    assert scene.placing is None and not panel.kind_buttons["Shop"].isChecked()
    window.close()


def test_its_walkers_walk_from_the_catchment_to_the_building_and_back():
    from traffic_simulator.walking import walk_pairs

    network, modes = generate_network(3, 4), VehicleModes().modes
    school = _school(network)
    school.mode_shares = {"Walk": 40.0, "Car": 60.0}
    origins = catchment(network, school)
    daily = walk_pairs(network, modes, daily=True)
    arrive, leave = hourly_people(school)
    coming = [pair for pair in daily if pair.destination == "1,1"]
    assert {pair.origin for pair in coming} == set(origins) and all(pair.finish == (school.x, school.y) for pair in coming)
    assert abs(sum(pair.daily for pair in coming) - 0.4 * sum(arrive)) < 1e-6  # its Walk share of those arriving
    assert all(pair.start == (school.x, school.y) for pair in daily if pair.origin == "1,1")  # and back
    hourly = walk_pairs(network, modes, daily=False)  # in the same design hour as its cars
    assert "08:00-09:00" in run_demand(network, modes, daily=False)[1][0]
    assert abs(sum(pair.hourly for pair in hourly if pair.destination == "1,1") - 0.4 * arrive[8]) < 1e-6
    simulation = Simulation(network, run_demand(network, modes, daily=False)[0], modes, seed=3)
    while simulation.time < 120:
        simulation.step()
    assert simulation.walk_spawned == 0 and simulation.walk_unreachable  # no footpath yet: noted, nobody walks
    for road in network.all_roads():
        road.footpaths = ["left", "right"]
    simulation = Simulation(network, run_demand(network, modes, daily=False)[0], modes, seed=3)
    while simulation.time < 600:
        simulation.step()
    assert simulation.walk_spawned and not simulation.walk_unreachable
    ends = {tuple(walker.points[-1]) for walker in simulation.walkers}
    assert (school.x, school.y) in ends  # on to the building, past its door


if __name__ == "__main__":
    for name, check in list(globals().items()):
        if name.startswith("test_"):
            check()
    print("ok")

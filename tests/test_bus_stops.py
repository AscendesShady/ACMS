"""Bus stops: placed clear of the junctions and of each other and kept there through edits, saved
and loaded; buses pull into their bays, stand for each stop's dwell time and merge back while traffic
drives past; added, slid, nudged, re-pointed, re-timed, deleted and undone in the window.
Run with `python tests/test_bus_stops.py` (or the test monitor)."""

import os
import tempfile
from copy import deepcopy
from math import isclose
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QPointF, Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QGraphicsTextItem  # noqa: E402

from traffic_simulator.mode_behavior import PALETTE, VehicleMode  # noqa: E402
from traffic_simulator.models import BAY_LENGTH_M, stop_line_inset  # noqa: E402
from traffic_simulator.network_generator import generate_network  # noqa: E402
from traffic_simulator.network_io import load_network, save_network  # noqa: E402
from traffic_simulator.network_tools import describe_changes  # noqa: E402
from traffic_simulator.simulation import Bay, PairDemand, Simulation  # noqa: E402

APP = QApplication.instance() or QApplication([])
ROAD = "1,1-2,1"  # generate_network(1, 2): 1,1 to 2,1, 500 m east; two lanes each way, driving on the left
MODES = [VehicleMode("Bus", PALETTE[0], headway_min=2, length_m=12.0, width_m=2.55, max_speed_kmh=100, accel=1.2,
                     decel=4.0),
         VehicleMode("Car", PALETTE[1])]


def test_a_bus_stop_sits_clear_of_the_junctions_and_follows_edits():
    network = generate_network(1, 2)
    stop = network.add_bus_stop(ROAD, "forward")
    clear = stop_line_inset(14.0, 500.0) + BAY_LENGTH_M / 2  # the whole bay behind the stop line
    assert (stop.id, stop.distance_m) == ("BS1", 250.0) and network.bus_stop_range(network.road(ROAD)) == (clear, 500 - clear)
    network.move_bus_stop("BS1", 3.0)
    assert stop.distance_m == clear  # slid no further than the junction allows
    network.move_bus_stop("BS1", 120.25)
    second_stop = network.add_bus_stop(ROAD)
    assert stop.distance_m == 120.25 and second_stop.id == "BS2"
    assert isclose(second_stop.distance_m, (120.25 + 500 - clear) / 2)  # mid-way along the free stretch past BS1
    placed = second_stop.distance_m
    network.node("1,1").control = "roundabout"  # a bigger junction: the bay keeps clear of its circle
    assert network.bus_stop_range(network.road(ROAD))[0] > clear
    network.reverse_road(ROAD)  # measured from the other end now, serving the same traffic
    assert (stop.distance_m, stop.direction) == (500 - 120.25, "backward")
    network.reverse_road(ROAD)
    network.road(ROAD).direction = "one-way"
    assert network.bus_stop_sides(network.bus_stops["BS2"]) == [True]  # one-way: the one direction there is
    network.road(ROAD).direction = "two-way"
    start = network.node("1,1")
    junction = network.add_node(start.x + 300.0, start.y)
    junction.control = "uncontrolled"
    first, second = network.split_road(ROAD, junction.id)  # each stop stays where it was, on its part
    network.add_bus_stop(second)
    assert (stop.road, stop.distance_m) == (first, 120.25)
    assert (network.bus_stops["BS2"].road, network.bus_stops["BS2"].distance_m) == (first, placed)
    network.delete_road(second)  # takes its own bus stop with it
    assert set(network.bus_stops) == {"BS1", "BS2"}
    network.delete_node(junction.id)  # its roads go, and their bus stops with them
    assert network.bus_stops == {}
    network.validate()


def test_a_road_too_short_for_a_bay_is_refused():
    network = generate_network(1, 2)
    near = network.add_node(network.node("1,1").x, network.node("1,1").y + 45.0)
    road = network.add_road("1,1", near.id)
    before = deepcopy(network)
    try:
        network.add_bus_stop(road.id)
    except ValueError as exc:
        assert "too short" in str(exc)
    else:
        raise AssertionError("a 45 m road has no room for a 40 m bay clear of its junctions")
    assert network == before


def test_each_new_stop_goes_where_its_bay_is_clear_of_the_others():
    network = generate_network(1, 2)
    low, high = network.bus_stop_range(network.road(ROAD))
    spots = [network.add_bus_stop(ROAD).distance_m for _ in range(3)]
    expected = [250.0, (low + 250) / 2, (250 + high) / 2]  # the middle, then the longest free stretch's middle
    assert all(isclose(a, b) for a, b in zip(spots, expected, strict=True)), spots
    assert isclose(network.add_bus_stop("1E").distance_m, sum(network.bus_stop_range(network.road("1E"))) / 2)  # own road
    while True:  # fill the road
        try:
            spots.append(network.add_bus_stop(ROAD).distance_m)
        except ValueError as exc:
            assert "no room" in str(exc)
            break
    assert len(spots) >= 7 and all(abs(a - b) >= BAY_LENGTH_M for i, a in enumerate(spots) for b in spots[i + 1:])
    # Too little room between the middle and the junctions: the next stop goes to the far end instead.
    start = network.node("1,1")
    short = network.add_road("1,1", network.add_node(start.x, start.y + 162.0).id)
    low, high = network.bus_stop_range(short)
    placed = [network.add_bus_stop(short.id).distance_m for _ in range(3)]
    ends = sorted(placed[1:])  # one end each, equally far from the first
    assert all(isclose(a, b) for a, b in zip([placed[0], *ends], [(low + high) / 2, low, high], strict=True)), placed
    before = deepcopy(network)
    try:
        network.add_bus_stop(short.id)
    except ValueError as exc:
        assert "no room" in str(exc)
    else:
        raise AssertionError("a fourth bay would land on another")
    assert network == before


def test_bus_stops_are_saved_loaded_and_checked():
    network = generate_network(1, 2)
    network.add_bus_stop(ROAD, "backward")
    network.move_bus_stop("BS1", 180.5)
    network.bus_stops["BS1"].dwell_s = 42.0
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "stops.json"
        save_network(network, path)
        assert load_network(path).bus_stops == network.bus_stops
    network.bus_stops["BS1"].road = "nowhere"
    try:
        network.validate()
    except ValueError as exc:
        assert "unknown road" in str(exc)
    else:
        raise AssertionError("a stop on a road that isn't there must not validate")
    network.bus_stops["BS1"].road, network.bus_stops["BS1"].dwell_s = ROAD, -5.0
    try:
        network.validate()
    except ValueError as exc:
        assert "dwell time" in str(exc)
    else:
        raise AssertionError("a negative dwell time must not validate")
    before, after = generate_network(1, 2), generate_network(1, 2)
    after.add_bus_stop(ROAD)
    assert describe_changes(before, after) == [
        "Added bus stop BS1: 250 m along road 1,1-2,1 from 1,1, serving both directions, buses stand 20 s"]
    assert describe_changes(after, before) == ["Deleted bus stop BS1"]


def test_buses_pull_in_stand_and_merge_back_while_traffic_passes():
    network = generate_network(1, 2)
    network.add_bus_stop(ROAD, "both")
    network.add_bus_stop("1E", "forward").dwell_s = 45.0  # the boundary road east: its bay serves traffic leaving
    demand = [PairDemand("1W", "1E", 900, None, [], "Poisson", (0, 100)),
              PairDemand("1E", "1W", 600, None, [], "Poisson", (0, 100))]
    simulation = Simulation(network, demand, MODES, seed=1)
    bays = {link: [bay.stop for bay in link_bays] for link, link_bays in simulation.graph.bays.items()}
    assert bays == {"1,1-2,1>": ["BS1"], "1,1-2,1<": ["BS1"], "1E>": ["BS2"]}
    east = simulation.graph.bays["1,1-2,1>"][0]
    assert east.points[20][1] < east.lane.points[0][1] - 3  # out beside the kerb: north, driving east on the left
    buses: dict[str, str] = {}  # every bus seen on the road -> where it came from
    visited: dict[str, set] = {}  # bus -> (stop, link) of each bay it was in
    stood: dict[tuple, float] = {}  # (bus, stop) -> seconds standing in the bay
    passed = overlaps = 0
    while simulation.time < 1200:
        on_lane = {vehicle.id: vehicle.s for vehicle in east.lane.vehicles}
        simulation.step()
        for vehicle in simulation.vehicles:
            if vehicle.mode == "Bus":
                buses[vehicle.id] = vehicle.origin
            if isinstance(vehicle.segment, Bay):
                assert vehicle.mode == "Bus"  # only buses use the bays
                visited.setdefault(vehicle.id, set()).add((vehicle.segment.stop, vehicle.segment.link.id))
                if vehicle.speed < 0.1:
                    key = (vehicle.id, vehicle.segment.stop)
                    stood[key] = stood.get(key, 0.0) + 0.5
            elif vehicle.segment is east.lane and east.vehicles and on_lane.get(vehicle.id, 1e9) < east.exit_s <= vehicle.s:
                passed += 1  # drove past the bay while a bus stood in it
        for segment in {vehicle.segment for vehicle in simulation.vehicles}:
            for rear, front in zip(segment.vehicles, segment.vehicles[1:]):
                if front.s - front.length_m < rear.s - 1e-6:
                    overlaps += 1
    assert overlaps == 0 and passed > 20 and simulation.arrived > 400, (overlaps, passed, simulation.arrived)
    still_here = {vehicle.id for vehicle in simulation.vehicles}
    arrived = {bus: origin for bus, origin in buses.items() if bus not in still_here}
    east_calls = {("BS1", "1,1-2,1>"), ("BS2", "1E>")}
    assert sum(origin == "1W" for origin in arrived.values()) >= 7 and sum(origin == "1E" for origin in arrived.values()) >= 7
    for bus, origin in arrived.items():  # every bus that finished called at each stop on its way, and no other
        assert visited.get(bus) == (east_calls if origin == "1W" else {("BS1", "1,1-2,1<")}), (bus, visited.get(bus))
    dwell = {"BS1": 20.0, "BS2": 45.0}  # each stop's own, and a wait for a gap to merge
    standing = {(vehicle.id, vehicle.segment.stop) for vehicle in simulation.vehicles if isinstance(vehicle.segment, Bay)}
    assert all(dwell[stop] - 0.5 <= seconds <= dwell[stop] + 30 for (bus, stop), seconds in stood.items()
               if (bus, stop) not in standing), stood  # buses still in a bay when the run ends: not done yet
    assert any(stop == "BS2" and seconds >= 44.5 for (_, stop), seconds in stood.items())


def test_the_bay_is_on_the_kerb_side_of_its_traffic():
    for side, outward in (("left", -1), ("right", 1)):  # y points down: driving east on the left, the kerb is north
        network = generate_network(1, 2, driving_side=side)
        network.add_bus_stop(ROAD, "forward")
        bay = Simulation(network, [], MODES).graph.bays["1,1-2,1>"][0]
        kerb_lane_y = bay.lane.points[0][1]
        assert abs(bay.points[20][1] - (kerb_lane_y + outward * 3.25)) < 1e-6  # half a lane plus half the bay out
        assert abs(bay.points[0][1] - kerb_lane_y) < 1e-6 and abs(bay.points[-1][1] - kerb_lane_y) < 1e-6


def test_a_bus_stop_is_added_slid_nudged_and_undone_in_the_window():
    from traffic_simulator.main import MainWindow

    window = MainWindow()
    window.install_network(generate_network(1, 2))
    window._commit()
    panel, scene, view = window.properties, window.scene, window.view
    window.select("road", ROAD)
    assert panel.bus_stop_button.isVisibleTo(panel) and not panel.stop_fields.isVisibleTo(panel)
    assert [action.text() for action in panel.bus_stop_menu.actions()] == ["1,1 → 2,1", "2,1 → 1,1", "Both directions"]
    panel.bus_stop_menu.actions()[2].trigger()  # Both directions
    assert scene.selection == ("bus_stop", "BS1") and window.network.bus_stops["BS1"].direction == "both"
    assert panel.stop_fields.isVisibleTo(panel) and not panel.drag_button.isVisibleTo(panel)
    assert (panel.stop_from_label.text(), panel.stop_from.value(), panel.stop_to.value()) == ("From 1,1", 250.0, 250.0)
    labels = {item.toPlainText() for item in scene.items() if isinstance(item, QGraphicsTextItem)}
    assert {"250.0 m from 1,1", "250.0 m from 2,1"} <= labels  # distances from both nodes, on the map

    window.show()
    window.activateWindow()
    QTest.qWaitForWindowActive(window)
    view.resetTransform()
    view.scale(4, 4)  # 4 px a metre
    view.centerOn(250, 0)
    view.setFocus()
    stop = window.network.bus_stops["BS1"]
    QTest.keyClick(view, Qt.Key.Key_Right)  # the road runs east: right is towards 2,1
    QTest.keyClick(view, Qt.Key.Key_Right, Qt.KeyboardModifier.ShiftModifier)
    QTest.keyClick(view, Qt.Key.Key_Left)
    QTest.keyClick(view, Qt.Key.Key_Up)  # across the road: nothing to move
    assert stop.distance_m == 255.0 and panel.stop_from.value() == 255.0 and panel.stop_to.value() == 245.0
    window.undo()  # the nudges were one step...
    assert window.network.bus_stops["BS1"].distance_m == 250.0
    window.undo()  # ...and adding it another
    assert window.network.bus_stops == {}
    window.redo()
    window.redo()
    stop = window.network.bus_stops["BS1"]
    window.select("bus_stop", "BS1")

    grab = view.mapFromScene(QPointF(250.0, -8.5))  # in the eastbound bay, on the north kerb
    QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=view.mapFromScene(QPointF(255.0, -8.5)))
    QTest.mouseMove(view.viewport(), grab + QPoint(40, 0))  # 10 m east of where it was grabbed
    QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=grab + QPoint(40, 0))
    assert abs(stop.distance_m - 260.0) < 0.5 and scene.selection == ("bus_stop", "BS1"), stop.distance_m
    assert abs(panel.stop_from.value() - stop.distance_m) < 0.05  # the panel follows
    assert panel.stop_dwell.isVisibleTo(panel) and panel.stop_dwell.value() == 20.0  # clicked: its dwell to edit
    panel.stop_dwell.setValue(35.0)
    assert stop.dwell_s == 35.0
    window.undo()
    stop = window.network.bus_stops["BS1"]
    assert stop.dwell_s == 20.0 and panel.stop_dwell.value() == 20.0

    panel.stop_to.setValue(100.0)  # typed in the other box: 100 m from 2,1
    assert stop.distance_m == 400.0
    panel.stop_serves.group.button(0).click()  # 1,1 → 2,1 only
    assert stop.direction == "forward"
    view.setFocus()
    QTest.keyClick(view, Qt.Key.Key_Delete)
    assert window.network.bus_stops == {} and scene.selection is None
    window.undo()
    assert window.network.bus_stops["BS1"].distance_m == 400.0
    window.select("road", ROAD)
    panel.bus_stop_menu.actions()[2].trigger()  # a second stop: placed clear of the first
    second = window.network.bus_stops["BS2"]
    assert scene.selection == ("bus_stop", "BS2") and abs(second.distance_m - 400.0) >= BAY_LENGTH_M
    assert abs(panel.stop_from.value() - second.distance_m) < 0.05  # shown to 0.1 m
    window.close()


def test_passengers_set_each_buss_dwell_and_load():
    from traffic_simulator.experiments import bus_passengers  # noqa: PLC0415
    from traffic_simulator.simulation import (ALIGHT_S, BOARD_S, CAPACITY_PER_M, DOOR_S, SEATED_SHARE,  # noqa: PLC0415
                                              STANDING_BOARD_S)

    def run(boarding, alighting, minutes=30):
        network = generate_network(1, 2)
        first = network.add_bus_stop(ROAD, "forward")
        first.boarding_per_h, first.alighting_percent = boarding, alighting
        network.add_bus_stop("1E", "forward")  # a second stop downstream, with no passengers: its fixed dwell
        simulation = Simulation(network, [PairDemand("1W", "1E", 300, None, [], "Poisson", (0, 100))], MODES, seed=4)
        simulation.observe()
        while simulation.time < minutes * 60:
            simulation.step()
        return simulation, simulation.observations()

    simulation, observed = run(240, 30)
    rows = observed["boardings"]
    assert rows and all(stop == "BS1" for stop, *_ in rows)  # the stop without passengers isn't served this way
    capacity = round(CAPACITY_PER_M * 12.0)
    loads: dict = {}
    for stop, link, t, bus, boarded, alighted, left, load, dwell in rows:
        before = loads.get(bus, 1.0)  # at first its type's occupancy (1 by default)
        assert load == before - alighted + boarded and 0 <= load <= capacity  # passengers kept count of
        standing = load > SEATED_SHARE * capacity
        assert abs(dwell - round(DOOR_S + alighted * ALIGHT_S + boarded * (BOARD_S + (STANDING_BOARD_S if standing else 0)), 1)) < 0.11
        loads[bus] = load
    assert sum(r[4] for r in rows) > 0  # some boarded: 240 an hour, a bus every 2 minutes, about 8 a bus
    summary = bus_passengers(observed)
    assert summary[0]["stop"] == "BS1" and summary[0]["buses"] == len(rows)
    assert summary[0]["boarded"] == sum(r[4] for r in rows) and summary[0]["dwell_mean_s"] > DOOR_S
    _, crowded = run(5000, 0, 20)  # more than buses can hold: left behind, never over capacity
    assert max(r[6] for r in crowded["boardings"]) > 0 and max(r[7] for r in crowded["boardings"]) <= capacity
    network = generate_network(1, 2)
    network.add_bus_stop(ROAD, "forward").dwell_s = 33.0
    plain = Simulation(network, [], MODES).graph.bays["1,1-2,1>"][0]
    assert plain.boarding_per_h == 0 and plain.alighting_share == 0 and plain.dwell_s == 33.0  # no passengers: as before
    stop = network.bus_stops["BS1"]
    stop.boarding_per_h = -1
    try:
        network.validate()
    except ValueError as exc:
        assert "boarding 0 to" in str(exc)
    else:
        raise AssertionError("a negative boarding rate is refused")


def test_a_buss_first_stop_starts_from_its_occupancy():
    """A bus with no load yet carries its type's occupancy into its first stop: all of them may alight there."""
    from traffic_simulator.simulation import ALIGHT_S, DOOR_S  # noqa: PLC0415
    from traffic_simulator.vehicle import create_vehicle  # noqa: PLC0415

    network = generate_network(1, 2)
    stop = network.add_bus_stop(ROAD, "forward")
    stop.boarding_per_h, stop.alighting_percent = 0, 100
    bus_type = VehicleMode("Bus", PALETTE[0], occupancy=20.0)
    simulation = Simulation(network, [], [bus_type])
    simulation.observe()
    bus = create_vehicle(bus_type, "Bus1", simulation.graph.route("1W", "1E"))
    dwell = simulation._serve(bus, simulation.graph.bays["1,1-2,1>"][0])
    boarded, alighted, _, load, _ = simulation.observations()["boardings"][-1][4:]
    assert (boarded, alighted, load) == (0, 20, 0.0) and isclose(dwell, DOOR_S + 20 * ALIGHT_S)  # 20 + 0 − 20 = 0


def test_a_bus_is_known_by_its_class_not_its_name():
    """Bus stops serve a type whose vehicle_class is bus, whatever it is called; a type from before vehicle_class
    named Bus is a bus."""
    network = generate_network(1, 2)
    network.add_bus_stop(ROAD, "forward")

    def stops(mode: VehicleMode) -> int:
        simulation = Simulation(network, [], [mode])
        simulation._spawn(PairDemand("1W", "1E", 100, None, [], "Random", (100,)), 0)
        return len(next(vehicle for queue in simulation.pending.values() for vehicle in queue).stops)

    assert stops(VehicleMode("Bus", PALETTE[0])) == stops(VehicleMode("City Bus", PALETTE[0], vehicle_class="bus")) == 1
    assert stops(VehicleMode("Bus", PALETTE[0], vehicle_class="coach")) == 0  # the name alone no longer makes a bus


if __name__ == "__main__":
    for name, check in list(globals().items()):
        if name.startswith("test_"):
            check()
    print("ok")

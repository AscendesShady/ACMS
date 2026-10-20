"""Metro rail (Edit Network → Metro): stops and lines in the model, saved with the project, deployed from the map
(each click a stop, joined to the last; a right-click finishes), one undo step a line."""

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PySide6.QtCore import QPoint, Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from traffic_simulator.models import METRO_MIN_SPACING_M, MetroLine  # noqa: E402
from traffic_simulator.network_generator import generate_network  # noqa: E402
from traffic_simulator.network_io import load_network, save_network, saved_project  # noqa: E402

APP = QApplication.instance() or QApplication([])


def test_a_line_is_its_stops_in_order_and_refusals_change_nothing():
    network = generate_network(2, 2)
    line = network.add_metro_line((0.0, 0.0), (400.0, 0.0))
    network.add_metro_stop(line.id, 1000.0, 0.0)
    assert line.id == "ML1" and line.stops == ["M1", "M2", "M3"] and network.metro_length_m("ML1") == 1000.0
    m3 = network.metro_stops["M3"]
    for attempt in (lambda: network.add_metro_stop("ML1", m3.x + 100.0, m3.y),  # under 150 m from M3
                    lambda: network.add_metro_stop("ML1", float("nan"), 0.0),
                    lambda: network.add_metro_line((0.0, 900.0), (0.0, 900.0 + METRO_MIN_SPACING_M - 1))):
        try:
            attempt()
        except ValueError:
            pass
        else:
            raise AssertionError("refused")
    assert list(network.metro_stops) == ["M1", "M2", "M3"] and list(network.metro_lines) == ["ML1"]
    assert network.add_metro_line((0.0, 900.0), (0.0, 1200.0)).stops == ["M4", "M5"]  # a second line
    network.metro_demand = {"M1->M3": 1200.0, "M4->M5": 300.0}
    network.validate()
    for bad in ({"M1->M1": 10.0}, {"M1->M9": 10.0}, {"M1-M2": 10.0}, {"M1->M2": -1.0}):
        network.metro_demand = bad
        try:
            network.validate()
        except ValueError as exc:
            assert "Metro demand" in str(exc)
        else:
            raise AssertionError(bad)
    network.metro_demand = {"M1->M3": 1200.0, "M4->M5": 300.0}
    network.delete_metro_line("ML2")  # its stations and their demand go with it
    assert network.metro_demand == {"M1->M3": 1200.0} and "M4" not in network.metro_stops
    network.metro_lines["ML3"] = MetroLine("ML3", ["M1"])  # one stop, shared with ML1: refused
    try:
        network.validate()
    except ValueError as exc:
        assert "two stops" in str(exc)
    else:
        raise AssertionError("a one-stop line was accepted")


def test_a_line_curves_at_300_m_and_a_bend_too_sharp_is_drawn_in():
    from math import atan2, degrees, hypot

    from traffic_simulator.design_checks import _min_radius
    from traffic_simulator.models import METRO_MIN_RADIUS_M

    network = generate_network(2, 2)
    line = network.add_metro_line((0.0, 0.0), (400.0, 0.0))
    network.add_metro_stop(line.id, 400.0, 300.0)  # a right angle at M2: drawn in to a turn under 90 deg
    m3 = network.metro_stops["M3"]
    track = network.metro_track(line.id)
    assert hypot(m3.x - 400.0, m3.y - 300.0) > 100 and track[-1] == (m3.x, m3.y) and (400.0, 0.0) in track
    headings = [degrees(atan2(b[1] - a[1], b[0] - a[0])) for a, b in zip(track, track[1:])]
    assert all(abs(b - a) < 3 for a, b in zip(headings, headings[1:]))  # no corner anywhere, M2 included
    assert 0 < headings[-1] - headings[0] < 90  # it turns, by less than a right angle
    assert _min_radius(track) >= METRO_MIN_RADIUS_M - 0.5  # never tighter than 300 m
    assert abs(network.metro_length_m(line.id) - sum(hypot(b[0] - a[0], b[1] - a[1])
                                                    for a, b in zip(track, track[1:]))) < 1e-6
    leg, crossing = network.metro_leg(line.id, m3.x - 200.0, m3.y - 900.0)  # behind it: drawn in, nothing placed
    assert crossing is None and len(line.stops) == 3
    loop = network.add_metro_line((0.0, 2000.0), (1000.0, 2000.0))  # each click 80 deg to the right: a spiral...
    for x, y in ((1122.0, 2689.0), (620.0, 2861.0), (439.0, 2363.0)):
        network.add_metro_stop(loop.id, x, y)
    try:
        network.add_metro_stop(loop.id, 934.0, 2174.0)  # ...whose next leg would cross its first
    except ValueError as exc:
        assert "cross itself" in str(exc)
    else:
        raise AssertionError("a line crossed itself")
    assert len(loop.stops) == 5


def test_a_line_crossing_another_merges_at_an_interchange_where_riders_change():
    from traffic_simulator.metro import loads, ride
    from traffic_simulator.models import MetroCrossing

    network = generate_network(2, 2)
    across = network.add_metro_line((0.0, 500.0), (2000.0, 500.0))  # M1, M2
    try:
        network.add_metro_line((1000.0, 0.0), (1000.0, 1000.0))
    except MetroCrossing as crossing:
        assert crossing.line_id == "ML1"
    else:
        raise AssertionError("a crossing went unasked")
    assert list(network.metro_lines) == ["ML1"] and list(network.metro_stops) == ["M1", "M2"]  # nothing changed
    try:
        network.add_metro_line((1950.0, 0.0), (1950.0, 1000.0), merge=True)  # 50 m from M2: no room for one
    except ValueError as exc:
        assert "Too near M2" in str(exc)
    else:
        raise AssertionError("an interchange beside a station")
    down = network.add_metro_line((1000.0, 0.0), (1000.0, 1000.0), merge=True)  # ends at the crossing
    change = down.stops[-1]
    assert (network.metro_stops[change].x, network.metro_stops[change].y) == (1000.0, 500.0)
    assert across.stops == ["M1", change, "M2"] and network.metro_track(down.id)[-1] == (1000.0, 500.0)
    network.add_metro_stop(down.id, 1000.0, 1200.0)
    network.validate()  # a stop on two lines is an interchange
    end = down.stops[-1]
    trip = ride(network, "M1", end)  # across, then down: one change
    assert [leg["line"] for leg in trip["legs"]] == ["ML1", down.id] and abs(trip["km"] - 1.7) < 1e-9
    assert trip["wait_min"] == 5.0  # half of each line's 5 min headway
    report = {line["line"]: line for line in loads(network, [("M1", end, 100.0)])["lines"]}
    assert report["ML1"]["riders_per_h"] == 100 and report[down.id]["riders_per_h"] == 100
    assert report["ML1"]["stops"][change]["alighting"] == 100 and report[down.id]["stops"][change]["boarding"] == 100
    network.delete_metro_line(down.id)  # its stations go, the interchange off ML1 too
    assert across.stops == ["M1", "M2"] and change not in network.metro_stops


def test_deploying_shows_the_next_leg_and_asks_at_a_crossing():
    window = _window()
    window._accept_metro_line = lambda summary: {}
    window.metro_button.setChecked(True)
    for x in (0.0, 2000.0):
        window.metro_clicked(x, 500.0)
    window.view.right_clicked.emit()  # ML1 accepted
    window.metro_button.setChecked(True)
    window.metro_clicked(1000.0, 0.0)
    window._show_pointer(1000.0, 1000.0)  # the ghost follows the mouse, before a click
    ghost = window.scene._metro_ghost
    assert ghost is not None and not ghost.path().isEmpty()
    window._show_pointer(1050.0, 0.0)  # too near the first stop: red
    assert ghost.pen().color().name() == "#fca5a5"
    asked = []
    window._merge_or_discard = lambda line_id: (asked.append(line_id), "discard")[1]
    window.metro_clicked(1000.0, 1000.0)
    assert asked == ["ML1"] and list(window.network.metro_lines) == ["ML1"] and window.scene.metro_pending is None
    assert "discarded" in window.mode_hint.text()
    window.metro_clicked(1000.0, 0.0)
    window.metro_clicked(1000.0, 300.0)  # a line of its own first, then...
    line = window.metro_line
    window._show_pointer(1000.0, 900.0)
    assert window.scene._metro_ghost.pen().color().name() == "#fcd34d"  # amber: it would cross ML1
    window._merge_or_discard = lambda line_id: "merge"
    window.metro_clicked(1000.0, 900.0)  # ...crossing ML1: merged at an interchange
    change = window.network.metro_lines[line].stops[-1]
    assert change in window.network.metro_lines["ML1"].stops
    window.close()


def test_metro_is_saved_and_loaded_with_the_network():
    import json

    network = generate_network(2, 2)
    network.add_metro_line((0.0, 0.0), (500.0, 200.0)).cars_per_train = 8
    network.metro_demand = {"M1->M2": 750.0}
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "metro.json"
        save_network(network, path)
        loaded = load_network(path)
        document = json.loads(path.read_text(encoding="utf-8"))  # a line saved before carriages: carriages that hold it
        record = document["metro_lines"][0]
        record["train_capacity"] = 900
        del record["cars_per_train"], record["car_capacity"]
        path.write_text(json.dumps(document), encoding="utf-8")
        older = load_network(path).metro_lines["ML1"]
    assert loaded.metro_lines == network.metro_lines and loaded.metro_stops == network.metro_stops
    assert loaded.metro_demand == {"M1->M2": 750.0}
    assert (older.cars_per_train, older.car_capacity, older.train_capacity) == (3, 300, 900)  # 400 a carriage at most


def test_a_lines_service_is_checked_and_rides_go_station_to_station_on_one_line():
    from traffic_simulator import metro

    network = generate_network(2, 2)
    line = network.add_metro_line((0.0, 0.0), (1000.0, 0.0))
    network.add_metro_stop(line.id, 2000.0, 0.0)
    other = network.add_metro_line((0.0, 3000.0), (1000.0, 3000.0))
    assert (line.headway_min, line.speed_kmh, line.fare_cents) == (5.0, 35.0, 100.0)
    assert (line.cars_per_train, line.car_capacity, line.train_capacity) == (5, 200, 1000)  # carriages x passengers
    line.headway_min = 0.0
    try:
        network.validate()
    except ValueError as exc:
        assert "headway" in str(exc)
    else:
        raise AssertionError("a headway of 0 was accepted")
    line.headway_min = 5.0
    trip = metro.ride(network, "M3", "M1")  # either way along the line
    assert trip["line"] == "ML1" and trip["km"] == 2.0 and abs(trip["ivt_min"] - 2.0 / 35 * 60) < 1e-9
    assert trip["wait_min"] == 2.5 and trip["cost"] == 100.0
    assert metro.ride(network, "M1", "M1") is None and metro.ride(network, "M1", other.stops[0]) is None  # no transfers
    assert metro.door(network, 0.0, 700.0) == ("M1", 700.0) and metro.door(network, 0.0, 1500.0) is None


def test_only_institutions_draw_on_the_metro_from_the_stations_in_their_circle():
    from traffic_simulator import network_tools as tools
    from traffic_simulator.institutions import place, vehicle_split
    from traffic_simulator.mode_behavior import VehicleModes

    network, modes = generate_network(3, 3), VehicleModes().modes
    line = network.add_metro_line((-200.0, 1100.0), (600.0, 1100.0))
    network.add_metro_stop(line.id, 1400.0, 1100.0)
    pairs = [("1W", "1E")]
    try:  # road pairs never take the metro: it has its own OD matrix
        tools.mode_plan(network, pairs, {"alternatives": [{"mode": "Car"}, {"mode": "Metro"}]}, modes, {pairs[0]: 600})
    except ValueError as exc:
        assert "metro" in str(exc).lower()
    else:
        raise AssertionError("a road pair took the metro")
    near = place(network, "Office", 650.0, 1180.0, 2000, 1000, people=200, employees=300)  # by M2; M1, M3 in its circle
    percent, info = tools.institution_split(network, near, modes, {})
    assert info["metro"]["door"] == "M2" and sorted(info["metro"]["stations"]) == ["M1", "M3"]
    assert 0 < percent["Metro"] < 100 and info["metro"]["places"] == info["origins"] + 2  # its junctions, and M1, M3
    narrow = place(network, "Shop", 750.0, 1700.0, 1000, 700)  # M2 within the walk (618 m), no other station in its circle
    percent, info = tools.institution_split(network, narrow, modes, {})
    assert percent["Metro"] == 0 and info["metro"]["stations"] == []
    near.mode_shares = {"Walk": 20.0, "Car": 50.0, "Metro": 30.0}
    per_person, _, unknown = vehicle_split(near, modes)
    assert not unknown and abs(per_person - 0.5 / 1.5) < 1e-9  # walkers and metro riders: no vehicles


def test_passengers_load_the_lines_against_the_hcms_capacity():
    from traffic_simulator import metro

    network = generate_network(2, 2)
    line = network.add_metro_line((0.0, 0.0), (1000.0, 0.0))
    network.add_metro_stop(line.id, 2000.0, 0.0)
    other = network.add_metro_line((0.0, 3000.0), (1000.0, 3000.0))
    assert metro.capacity_per_h(line) == 60 / 5 * 1000 * 0.75  # HCM 2000 Eq 27-28: 9,000 an hour each way
    found = metro.loads(network, [("M1", "M3", 3000.0), ("M2", "M3", 1000.0), ("M3", "M1", 500.0),
                                  ("M1", other.stops[0], 200.0)])  # another line: no transfers, not served
    row = next(row for row in found["lines"] if row["line"] == "ML1")
    assert (row["riders_per_h"], row["peak_load_per_h"], row["peak_segment"]) == (4500, 4000, "M2 to M3")
    assert row["passenger_km_per_h"] == 3000 * 2 + 1000 + 500 * 2 and row["v_c"] == 0.44 and not row["over_capacity"]
    assert row["stops"]["M3"] == {"alighting": 4000, "boarding": 500} and found["unserved_per_h"] == 200
    busy = next(row for row in metro.loads(network, [("M1", "M3", 12000.0)])["lines"] if row["line"] == "ML1")
    assert busy["over_capacity"] and busy["v_c"] == 1.33


def test_the_metro_has_its_own_od_matrix_and_the_report_loads_it_with_the_buildings():
    from traffic_simulator.institutions import place

    window = _window()
    network = window.network
    line = network.add_metro_line((-200.0, 1100.0), (600.0, 1100.0))
    network.add_metro_stop(line.id, 1400.0, 1100.0)
    network.add_metro_line((-200.0, 2500.0), (600.0, 2500.0))
    window._commit()
    window.tabs.setCurrentWidget(window.demand_tab)
    window.od_panel.demand.setCurrentIndex(window.od_panel.demand.findData("metro"))
    assert window.workspace.currentWidget() is window.metro_matrix and window.metro_matrix.stations[:3] == ["M1", "M2", "M3"]
    table = window.metro_matrix.table
    steps = len(window.history.undo_stack)
    table.item(0, 2).setText("2500")  # M1 to M3
    table.item(2, 0).setText("abc")  # refused and put back
    table.item(0, 3).setText("100")  # M1 to M4, another line: no ride, refused
    assert network.metro_demand == {"M1->M3": 2500.0} and table.item(2, 0).text() == ""
    assert not table.item(0, 3).flags() & Qt.ItemFlag.ItemIsEditable and len(window.history.undo_stack) == steps + 1
    report = window.ai_mode._execute("metro_report", {})
    row = next(row for row in report["lines"] if row["line"] == "ML1")
    assert row["peak_load_per_h"] == 2500 and "at its busiest" in window.metro_summary.text()
    building = place(network, "Office", 650.0, 1180.0, 2000, 1000, people=200, employees=300)
    building.mode_shares = {"Car": 60.0, "Metro": 40.0}  # its metro riders between M2 and M1, M3, in equal shares
    report = window._metro_report()
    row = next(row for row in report["lines"] if row["line"] == "ML1")
    assert row["riders_per_h"] > 2500 and row["stops"]["M2"]["alighting"] > 0
    window.undo()
    assert window.network.metro_demand == {}
    window.close()


def test_a_leg_leaves_its_station_straight_eases_into_its_curve_and_can_be_aimed():
    from math import atan2, degrees, hypot

    from traffic_simulator.models import METRO_MIN_RADIUS_M, METRO_PLATFORM_M, METRO_TRANSITION_M

    def headings(points):
        return [degrees(atan2(b[1] - a[1], b[0] - a[0])) for a, b in zip(points, points[1:])
                if hypot(b[0] - a[0], b[1] - a[1]) > 0.5]

    network = generate_network(2, 2)
    line = network.add_metro_line((0.0, 0.0), (400.0, 0.0))
    network.add_metro_stop(line.id, 1200.0, 400.0)  # a plain click: the turn that meets it
    m3 = network.metro_stops["M3"]
    assert (m3.x, m3.y) == (1200.0, 400.0)
    track = network.metro_track(line.id)
    after = [point for point in track if 400.0 < point[0] <= 400.0 + METRO_PLATFORM_M / 2 + 1e-6]
    assert after and all(abs(y) < 1e-9 for _, y in after)  # M2's platform: straight on past it
    turns = [abs(b - a) for a, b in zip(headings(track), headings(track)[1:])]
    bend = [turn for turn in turns if turn > 1e-6]
    arc = 5.0 / METRO_MIN_RADIUS_M * 180 / 3.141592653589793  # a 5 m piece of the 300 m arc
    assert bend[0] < arc / 2 and max(bend) <= arc + 1e-3  # easing in (a transition), never tighter than the arc
    assert sum(abs(turn - arc) < 0.01 for turn in bend) * 5.0 < sum(bend) / arc * 5.0  # partly arc, partly transition
    (bx, by), (cx, cy) = track[-2:]
    assert hypot(cx - bx, cy - by) >= METRO_PLATFORM_M / 2  # M3 on straight track too: half a platform into it
    # Aimed (the pen tool's drag): the track runs through the stop that way
    network.add_metro_stop(line.id, 1500.0, 1300.0, heading=(0.0, 1.0))
    m4 = network.metro_stops["M4"]
    final = network.metro_track(line.id)[-2:]
    assert (m4.x, m4.y) == (1500.0, 1300.0) and abs(headings(final)[0] - 90.0) < 1e-6
    stops = len(line.stops)
    for x, y, aim, words in ((1700.0, 1300.0, (1.0, 0.0), "Aimed too sharply"),  # a right angle
                             (1600.0, 1800.0, (0.0, 1.0), "S-bend")):  # parallel, beside it: two bends
        try:
            network.add_metro_stop(line.id, x, y, heading=aim)
        except ValueError as exc:
            assert words in str(exc), str(exc)
        else:
            raise AssertionError(aim)
    assert len(line.stops) == stops and METRO_TRANSITION_M > 0


def test_deploying_aims_a_stop_by_dragging_it_shift_snapping_the_aim():
    from PySide6.QtCore import QEvent, QPointF
    from PySide6.QtWidgets import QGraphicsSceneMouseEvent

    window = _window()
    window._accept_metro_line = lambda summary: {}
    window.metro_button.setChecked(True)
    for x in (0.0, 600.0):
        window.metro_clicked(x, 0.0)
    scene, placed = window.scene, []
    scene.metro_clicked.connect(lambda *args: placed.append(args))

    def mouse(kind, x, y, screen, shift=False):
        event = QGraphicsSceneMouseEvent(kind)
        event.setScenePos(QPointF(x, y))
        event.setScreenPos(QPoint(*screen))
        event.setButtonDownScreenPos(Qt.MouseButton.LeftButton, QPoint(100, 100))
        event.setButton(Qt.MouseButton.LeftButton)
        event.setButtons(Qt.MouseButton.LeftButton if kind != QEvent.Type.GraphicsSceneMouseRelease else
                         Qt.MouseButton.NoButton)
        event.setModifiers(Qt.KeyboardModifier.ShiftModifier if shift else Qt.KeyboardModifier.NoModifier)
        {QEvent.Type.GraphicsSceneMousePress: scene.mousePressEvent, QEvent.Type.GraphicsSceneMouseMove: scene.mouseMoveEvent,
         QEvent.Type.GraphicsSceneMouseRelease: scene.mouseReleaseEvent}[kind](event)

    mouse(QEvent.Type.GraphicsSceneMousePress, 1400.0, 300.0, (100, 100))
    mouse(QEvent.Type.GraphicsSceneMouseMove, 1450.0, 360.0, (140, 120), shift=True)  # 50.2 deg: snapped to 45
    ghost = scene._metro_ghost
    assert ghost is not None and ghost.path().elementCount() > 10  # the aimed leg and its handle, both ways
    mouse(QEvent.Type.GraphicsSceneMouseRelease, 1450.0, 360.0, (140, 120), shift=True)
    (x, y, dx, dy), = placed
    assert (x, y) == (1400.0, 300.0) and abs(dx - 0.7071068) < 1e-6 and abs(dy - 0.7071068) < 1e-6
    m3 = window.network.metro_stops["M3"]
    assert (m3.x, m3.y) == (1400.0, 300.0)
    mouse(QEvent.Type.GraphicsSceneMousePress, 2200.0, 500.0, (100, 100))  # a click without a drag: not aimed
    mouse(QEvent.Type.GraphicsSceneMouseRelease, 2200.0, 500.0, (101, 101))
    assert placed[-1][2:] == (0.0, 0.0) and "M4" in window.network.metro_stops
    window.close()


def _window():
    from traffic_simulator.main import MainWindow

    window = MainWindow()
    window._ask_to_save = False
    window.tabs.setCurrentWidget(window.edit_tab)
    window.edit_tabs.setCurrentIndex(1)  # Edit Network → Metro
    return window


def test_deploying_places_a_stop_a_click_joined_to_the_last_and_a_right_click_finishes():
    window = _window()
    answers = []
    window._accept_metro_line = lambda summary: (answers.append(summary), answers_given.pop(0))[1]
    answers_given = [{"headway_min": 4.0, "speed_kmh": 40.0, "cars_per_train": 6, "car_capacity": 180,
                      "fare_cents": 150.0}, None]
    # the first line accepted with its service, the second deleted
    assert window.metro_summary.text() == "No metro lines yet" and not window.scene.metro_deploying
    steps = len(window.history.undo_stack)
    window.metro_button.setChecked(True)
    assert window.scene.metro_deploying and "first stop" in window.mode_hint.text()
    window.metro_clicked(0.0, 100.0)  # the first stop waits for the second: no line without track
    assert not window.network.metro_lines and window.scene.metro_pending == (0.0, 100.0)
    window.metro_clicked(60.0, 100.0)  # too near: refused, said why
    assert not window.network.metro_lines and "Too near" in window.mode_hint.text()
    for x in (600.0, 1200.0):
        window.metro_clicked(x, 100.0)
    assert window.network.metro_lines["ML1"].stops == ["M1", "M2", "M3"]
    assert window.metro_summary.text() == "1 line, 3 stops, 1.20 km of track" and "M3" in window.mode_hint.text()
    assert len(window.history.undo_stack) == steps  # no undo step until it is accepted
    window.view.right_clicked.emit()  # finishes: accepted
    assert answers == ["ML1: 3 stops, 1.20 km of track"] and not window.metro_button.isChecked()
    assert "accepted" in window.statusBar().currentMessage() and len(window.history.undo_stack) == steps + 1
    line = window.network.metro_lines["ML1"]
    assert (line.headway_min, line.speed_kmh, line.cars_per_train, line.car_capacity, line.fare_cents) == \
        (4.0, 40.0, 6, 180, 150.0) and line.train_capacity == 1080
    window.metro_button.setChecked(True)  # a new deployment is a new line: the accepted one is never extended
    for x in (0.0, 600.0):
        window.metro_clicked(x, 900.0)
    window.cancel()  # Esc finishes too: deleted, nothing left of it and no undo step
    assert list(window.network.metro_lines) == ["ML1"] and len(window.network.metro_stops) == 3
    assert "deleted" in window.statusBar().currentMessage() and len(window.history.undo_stack) == steps + 1
    window.metro_button.setChecked(True)
    window.metro_clicked(0.0, 900.0)
    window.metro_clicked(0.0, 1500.0)
    window.undo()  # while deploying, Ctrl+Z takes back the unfinished line only
    assert list(window.network.metro_lines) == ["ML1"] and not window.metro_button.isChecked()
    window.undo()  # then the accepted line goes, as one step
    assert not window.network.metro_lines and window.metro_summary.text() == "No metro lines yet"
    window.redo()
    assert window.network.metro_lines["ML1"].stops == ["M1", "M2", "M3"]
    window.metro_button.setChecked(True)
    window.metro_clicked(0.0, 900.0)
    window.edit_tabs.setCurrentIndex(0)  # another tab ends it; a lone first stop is dropped without asking
    assert not window.metro_button.isChecked() and len(window.network.metro_stops) == 3 and len(answers) == 2
    window.close()


def test_a_right_click_is_told_from_a_right_drag_pan():
    window = _window()
    window.resize(1200, 800)
    window.show()
    clicks = []
    window.view.right_clicked.connect(lambda: clicks.append(1))
    viewport = window.view.viewport()
    QTest.mouseClick(viewport, Qt.MouseButton.RightButton, pos=QPoint(400, 300))
    assert clicks == [1]
    QTest.mousePress(viewport, Qt.MouseButton.RightButton, pos=QPoint(400, 300))
    QTest.mouseMove(viewport, QPoint(460, 340))
    QTest.mouseRelease(viewport, Qt.MouseButton.RightButton, pos=QPoint(460, 340))
    assert clicks == [1]  # a pan, not a click
    window.close()


def test_overhead_metro_has_metre_clearance_platforms_and_full_height_train_previews():
    from traffic_simulator import metro_assets as assets
    from traffic_simulator.asset_mesh import expand
    for cars in (1, 5, 12):
        faces = assets.line_mesh([(0., 0.), (400., 0.), (500., 300.)], cars)
        assert faces and all(len(p) == 3 for f in faces for p in f.vertices)
        deck = [f for f in faces if f.ink == 0 and all(p[2] >= assets.UNDERSIDE_M for p in f.vertices)]
        assert deck and min(p[2] for f in deck for p in f.vertices) >= 8.
        rails = [p for f in faces if f.ink == 2 for p in f.vertices]
        assert min(p[2] for p in rails) >= assets.TRACK_M >= assets.UNDERSIDE_M
        train = [p for f in faces if f.ink in (3, 4, 6) for p in f.vertices]
        assert min(p[2] for p in train) == assets.TRAIN_BASE_M > assets.TRACK_M
        assert abs(max(p[2] for p in train) - min(p[2] for p in train) - assets.TRAIN_HEIGHT_M) < 1e-9
        assert abs(max(p[0] for p in train) - min(p[0] for p in train) - assets.train_length(cars)) < 1e-9
        assert max(p[2] for f in faces for p in f.vertices) == assets.CANOPY_M
        assert assets.CANOPY_M > assets.TRAIN_BASE_M + assets.TRAIN_HEIGHT_M
        carriage = assets.carriage()
        points = [expand(p, 2., 2.) for f in carriage for p in f.vertices]
        assert max(p[0] for p in points) - min(p[0] for p in points) == assets.CAR_LENGTH_M
        assert max(p[1] for p in points) - min(p[1] for p in points) == assets.CAR_WIDTH_M


def test_overhead_projection_preserves_saved_geometry_frames_and_flat_markers_during_a_run():
    from copy import deepcopy
    from PySide6.QtCore import QPointF
    from PySide6.QtWidgets import QGraphicsItem
    from traffic_simulator import metro_assets
    from traffic_simulator.network_scene import NetworkGraphicsScene, NetworkView, STILL, VEHICLE_Z, projection_lift
    from traffic_simulator.simulation import Frame
    network = generate_network(2, 2)
    network.add_metro_line((0., 100.), (400., 100.))
    network.add_metro_stop('ML1', 400., 500.)
    snapshot = deepcopy(network.as_records())
    scene = NetworkGraphicsScene(network)
    scene.rebuild()
    view = NetworkView(scene)
    view.resize(900, 700)
    frame = Frame(18., (), {}, 0, 0)
    scene.show_frame(.35, frame)
    bounds, item = scene.sceneRect(), scene._metro_items[0]
    zoom = view.projection_scale()
    invalidations = []
    scene.changed.connect(lambda *args: invalidations.append(1))
    try:
        for iso in (True, False, True, False):
            view.set_isometric(iso)
            APP.processEvents()
            assert scene.network.as_records() == snapshot and scene.frame is frame and scene.frame_alpha == .35
            assert scene.sceneRect() == bounds and view.projection_scale() == zoom
            assert (item.zValue() > VEHICLE_Z) == iso
            assert bool(item.data(STILL)) == (not iso)
            assert bool(item.flags() & QGraphicsItem.GraphicsItemFlag.ItemHasNoContents) == (not iso)
            for label in scene._metro_labels:
                x, y, height, preview = label.data(2)
                expected = QPointF(x, y) + (projection_lift() * height if iso else QPointF())
                assert (label.pos() - expected).manhattanLength() < 1e-9
                assert label.isVisible() == (iso or not preview)
                if not preview:
                    assert height == metro_assets.CANOPY_M
            scene.set_buildings_visible(not iso)
            assert item.isVisible()  # the Buildings toggle never removes transit infrastructure
        assert invalidations
        scene.set_hidden(True)
        view.set_isometric(True)
        assert all(not item.isVisible() for item in scene._metro_items + scene._metro_labels)
        scene.set_hidden(False)
        assert all(item.isVisible() for item in scene._metro_items + scene._metro_labels)
        scene.metro_pending = (900., 100.)
        scene.rebuild()
        assert len(scene._metro_items) == 2 and scene._metro_items[1].faces
        assert network.as_records() == snapshot
    finally:
        view.close()


def test_overhead_mesh_rotates_with_world_tracks_and_fit_reserves_all_extrusions():
    from math import hypot
    from PySide6.QtCore import QPointF
    from traffic_simulator import metro_assets
    from traffic_simulator.network_scene import NetworkGraphicsScene, projection_lift
    horizontal = metro_assets.line_mesh([(0., 0.), (400., 0.)], 5)
    vertical = metro_assets.line_mesh([(100., 200.), (100., 600.)], 5)
    for a, b in zip(horizontal, vertical):
        for (x, y, z), (rx, ry, rz) in zip(a.vertices, b.vertices):
            assert hypot(rx - (100 - y), ry - (200 + x)) < 1e-9 and z == rz
    network = generate_network(2, 2)
    network.add_metro_line((0., 0.), (400., 0.))
    scene = NetworkGraphicsScene(network)
    scene.rebuild()
    item = scene._metro_items[0]
    for face in item.faces:
        for x, y, z in face.vertices:
            assert scene.network_rect.contains(QPointF(x, y) + projection_lift() * z)


def test_overhead_scenery_renders_identically_when_the_run_background_is_cached():
    from traffic_simulator.network_scene import NetworkGraphicsScene, NetworkView
    from traffic_simulator.simulation import Frame
    network = generate_network(2, 2)
    network.add_metro_line((0., 180.), (700., 180.))
    scene = NetworkGraphicsScene(network)
    scene.rebuild()
    view = NetworkView(scene)
    view.resize(900, 650)
    view.show()
    view.set_isometric(True)
    view.fit_network()
    try:
        for _ in range(5):
            APP.processEvents()
        before = view.viewport().grab().toImage()
        scene.show_frame(.4, Frame(8., (), {}, 0, 0))
        for _ in range(5):
            APP.processEvents()
        after = view.viewport().grab().toImage()
        assert before == after  # overhead scenery is never pushed behind road traffic by caching
        view.set_isometric(False)
        for _ in range(5):
            APP.processEvents()
        assert view.viewport().grab().toImage() != after
        view.set_isometric(True)
        for _ in range(5):
            APP.processEvents()
        # Zoom/centre are preserved; integer scroll rounding may shift one pixel after toggles.
        assert scene._metro_items[0].zValue() > scene.vehicle_layer.zValue()
    finally:
        view.close()


def test_long_viaduct_faces_do_not_hide_train_windows_at_the_far_station():
    from PySide6.QtCore import QPointF
    from traffic_simulator import metro_assets
    from traffic_simulator.network_scene import NetworkGraphicsScene, NetworkView, projection_lift, projection_transform
    network = generate_network(2, 2)
    network.add_metro_line((0., 180.), (700., 180.))
    scene = NetworkGraphicsScene(network)
    scene.rebuild()
    view = NetworkView(scene)
    view.resize(900, 650)
    view.show()
    view.set_isometric(True)
    view.setTransform(projection_transform(True, 6.))
    view.centerOn(0., 180.)
    try:
        for _ in range(5):
            APP.processEvents()
        image = view.viewport().grab().toImage()
        # Centre carriage, first side window; the opaque long deck must be below it, not painted over it.
        point = view.mapFromScene(QPointF(-6.6, 180. - metro_assets.TRACK_OFFSET_M + metro_assets.CAR_WIDTH_M / 2)
                                 + projection_lift() * (metro_assets.TRAIN_BASE_M + 2.))
        patch = [image.pixelColor(point.x() + dx, point.y() + dy) for dx in (-1, 0, 1) for dy in (-1, 0, 1)]
        assert sum(c.green() < 70 and c.blue() > c.red() + 5 for c in patch) >= 5
    finally:
        view.close()


def test_curved_viaduct_joins_are_watertight_and_supports_follow_distance_not_vertices():
    from collections import Counter
    from math import ceil, hypot
    from traffic_simulator import metro_assets as assets
    from traffic_simulator.geometry import polyline_length
    network = generate_network(2, 2)
    line = network.add_metro_line((0., 0.), (400., 0.))
    network.add_metro_stop(line.id, 400., 300.)
    path = network.metro_track(line.id)
    before = [tuple(p) for p in path]
    sampled = assets.sampled(path)
    assert all(tuple(p) in sampled for p in path)
    faces = assets.sweep(sampled, -4.5, 4.5, 8., 9., 0)
    tops = [f for f in faces if all(p[2] == 9. for p in f.vertices)]
    assert len(tops) == len(sampled) - 1
    for a, b in zip(tops, tops[1:]):
        assert a.vertices[2][:2] == b.vertices[1][:2] and a.vertices[3][:2] == b.vertices[0][:2]
    # A closed ribbon has no unmatched mesh edge, including caps, even on the outside of the curve.
    edges = Counter(tuple(sorted((tuple(a[:3]), tuple(b[:3])))) for f in faces
                    for a, b in zip(f.vertices, f.vertices[1:] + f.vertices[:1]))
    # Bottom is deliberately open, as for all existing opaque scenery boxes.
    assert all(n == 2 or (n == 1 and all(p[2] == 8. for p in edge)) for edge, n in edges.items())
    stops = [(network.metro_stops[s].x, network.metro_stops[s].y) for s in line.stops]
    mesh = assets.line_mesh(stops, 5, path)
    columns = [f for f in mesh if all(abs(p[2] - 8.) < 1e-9 for p in f.vertices)
               and len(f.vertices) == 4 and all(abs(hypot(b[0]-a[0], b[1]-a[1])-1.4) < 1e-8
               for a, b in zip(f.vertices, f.vertices[1:] + f.vertices[:1]))]
    extension = assets.station_length(5) / 2 + 2.
    assert len(columns) == ceil((polyline_length(path) + 2 * extension) / assets.PIER_SPACING_M)
    assert len(assets.supports(path)) == len(assets.supports(sampled))
    assert path == before


def test_curved_shared_interchange_view_keeps_authoritative_tracks_and_passenger_state():
    from copy import deepcopy
    from PySide6.QtCore import QPointF
    from traffic_simulator.network_scene import NetworkGraphicsScene, NetworkView
    network = generate_network(2, 2)
    line = network.add_metro_line((0., 500.), (2000., 500.))
    network.add_metro_line((1000., 0.), (1000., 1000.), merge=True)
    network.add_metro_stop('ML2', 1200., 1200.)
    before = deepcopy(network.as_records())
    scene = NetworkGraphicsScene(network)
    scene.rebuild()
    view = NetworkView(scene)
    view.resize(900, 650)
    try:
        for item, line in zip(scene._metro_items, network.metro_lines.values()):
            path = network.metro_track(line.id)
            assert item.track.elementCount() == len(path)
            for index, point in enumerate(path):
                p = item.track.elementAt(index)
                assert (p.x, p.y) == tuple(point)
        for on in (True, False, True):
            view.set_isometric(on)
            view.fit_network()
            assert network.as_records() == before
        assert any(sum(stop in line.stops for line in network.metro_lines.values()) == 2 for stop in network.metro_stops)
    finally:
        view.close()


def test_curved_deployment_ghost_lifts_only_the_render():
    from copy import deepcopy
    from PySide6.QtCore import QPointF
    from traffic_simulator import metro_assets
    from traffic_simulator.network_scene import NetworkGraphicsScene, NetworkView, projection_lift
    scene = NetworkGraphicsScene(generate_network(2, 2))
    scene.rebuild()
    points = [(0., 0.), (20., 1.), (40., 4.)]
    before = deepcopy(points)
    view = NetworkView(scene)
    try:
        scene.show_metro_ghost(points, 'crossing')
        raw = scene._metro_ghost.path()
        view.set_isometric(True)
        assert scene._metro_ghost.path() == raw and points == before
        assert scene._metro_ghost.pos() == projection_lift() * metro_assets.TRACK_M
        view.set_isometric(False)
        assert scene._metro_ghost.pos() == QPointF() and points == before
    finally:
        view.close()


def test_station_render_matches_current_platform_minimum_and_has_a_pitched_roof():
    from traffic_simulator import metro_assets as assets
    from traffic_simulator.models import METRO_PLATFORM_M
    assert assets.station_length(5) == METRO_PLATFORM_M == 120.
    assert assets.station_length(12) >= assets.train_length(12) + 8.
    faces = assets.station_on_path(assets.sampled([(-60., 0.), (60., 0.)]))
    roof = [p for f in faces if f.ink == 5 for p in f.vertices]
    eaves = [p[2] for p in roof if abs(abs(p[1]) - 6.9) < 1e-9]
    assert eaves and max(eaves) <= assets.CANOPY_M - .5 + 1e-9
    assert max(p[2] for p in roof) == assets.CANOPY_M == 14.
    assert any(f.ink == 7 for f in faces)  # a distinct tactile edge, not an enlarged platform


def test_train_noses_slope_with_the_glass_and_only_terminal_carriages_have_cabs():
    from traffic_simulator import metro_assets as assets
    from traffic_simulator.asset_mesh import normal
    end = assets.carriage()
    cabs = [f for f in end if f.ink == 3 and abs(normal(f.vertices)[0]) > .5]
    assert len(cabs) == 2 and all(normal(f.vertices)[2] > .1 for f in cabs)
    middle = assets.carriage(front=False, back=False)
    assert not any(f.ink == 3 and abs(normal(f.vertices)[0]) > .5 for f in middle)
    for face in end:
        for x, y, z, *_ in face.vertices:
            assert abs(x) <= assets.CAR_LENGTH_M / 2 and abs(y) <= assets.CAR_WIDTH_M / 2
            assert assets.TRAIN_BASE_M <= z <= assets.TRAIN_BASE_M + assets.TRAIN_HEIGHT_M
    assert any(f.ink == 1 for f in end)  # livery is part of the hull, never floating beyond its sides


def verify_overhead_hardware():
    """The actual GPU QPainter engine draws the same elevated windows as raster painting."""
    from PySide6.QtCore import QPointF
    from PySide6.QtGui import QImage, QPainter, QTransform
    from traffic_simulator import metro_assets
    from traffic_simulator.network_scene import MetroItem, NetworkGraphicsScene, projection_lift, projection_transform
    from test_gpu import _gl_painter
    context, surface, fbo, device = _gl_painter(900, 700)
    scene = NetworkGraphicsScene()
    scene.isometric = True
    item = MetroItem(scene, [(0., 0.), (700., 0.)], 5)
    t = projection_transform(True, 6.)
    t = QTransform(t.m11(), t.m12(), t.m21(), t.m22(), 450., 350.)
    images = []
    for target in (QImage(900, 700, QImage.Format.Format_RGB32), device):
        if target is device:
            fbo.bind()
        painter = QPainter(target)
        painter.fillRect(0, 0, 900, 700, scene.backgroundBrush())
        painter.setWorldTransform(t)
        item.paint(painter, None)
        painter.end()
        images.append(fbo.toImage() if target is device else target)
    for along in (-43.8, -25.2, -6.6, 12., 30.6):
        p = t.map(QPointF(along, -metro_assets.TRACK_OFFSET_M + metro_assets.CAR_WIDTH_M / 2)
                  + projection_lift() * (metro_assets.TRAIN_BASE_M + 2.)).toPoint()
        for image in images:
            colour = image.pixelColor(p)
            assert colour.green() < 70 and colour.blue() > colour.red() + 5, (along, colour.name())
    context.doneCurrent()


if __name__ == "__main__":
    if '--gpu' in sys.argv:
        verify_overhead_hardware()
        print('Overhead metro raster/GPU window pixels: ok')
        raise SystemExit(0)
    for name, check in list(globals().items()):
        if name.startswith("test_"):
            check()
    print("ok")

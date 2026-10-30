"""OD matrix hourly and daily demand, through the real widgets (offscreen).
Run with `python tests/test_od_matrix.py` (or the test monitor)."""

import os
import subprocess
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from traffic_simulator.daily_profile import WEEKDAY  # noqa: E402
from traffic_simulator.demand import shares_from  # noqa: E402
from traffic_simulator.od_matrix import ODCellPanel, ODMatrix  # noqa: E402

APP = QApplication.instance() or QApplication([])


def setup():
    matrix = ODMatrix()
    panel = ODCellPanel(matrix)
    panel.resize(420, 900)
    panel.show()
    matrix.set_ends(["1W", "2W"], ["1E", "2E"])
    matrix.table.setCurrentCell(0, 0)  # 1W -> 1E
    APP.processEvents()
    return matrix, panel


def daily(panel):
    panel.period.group.button(1).click()  # the Demand switch


def hourly(panel):
    panel.period.group.button(0).click()


def test_a_demand_cut_by_its_check_leads_with_what_was_kept():
    from traffic_simulator.demand import lead_with_what_was_kept as _lead_with_what_was_kept

    # qwen3.5 told the user 6,420 veh/h when the check had kept 47 (Dhanmondi): the kept figure now comes first
    info = {"method": "gravity", "total_asked": 6503, "total_made": 6420, "junction_controls": {"N41": "uncontrolled",
            "N66": "signalised"}, "checked_in_a_run": {"kept": "6%"}}
    _lead_with_what_was_kept(info, 0.06, 47)
    assert list(info)[:2] == ["total_now", "kept_after_check"] and info["total_now"] == 47
    assert "6% of 6420" in info["kept_after_check"] and info["total_before_check"] == 6420 and "total_made" not in info
    assert "N41" in info["to_hold_more"] and "N66" not in info["to_hold_more"]  # only those that give way
    most = {"total_made": 1000, "junction_controls": {"N41": "uncontrolled"}}
    _lead_with_what_was_kept(most, 0.8, 800)
    assert most["total_now"] == 800 and "to_hold_more" not in most  # most of it held: no advice
    scaled = {"factor": 2.0, "total_before": 500, "total_after": 1000}  # scale_demand's keys, cut by its check
    _lead_with_what_was_kept(scaled, 0.5, 500)
    assert "50% of 1000" in scaled["kept_after_check"] and scaled["factor"] == 1.0 and "total_after" not in scaled


def test_hourly_and_daily_demand_are_kept_apart():
    matrix, panel = setup()
    assert [panel.demand.tabData(i) for i in range(panel.demand.count())] == \
        ["vehicles", "walk", "spawn", "institutions", "metro"]  # one switch over five tabs
    daily(panel)
    panel.demand.setCurrentIndex(panel.demand.findData("institutions"))  # its own panel, not the pair editor
    assert panel.institutions.isVisible() and not panel.pair_group.isVisible() and matrix.mode == "daily"
    assert panel.institutions.daily  # the switch holds on every tab
    hourly(panel)
    assert not panel.institutions.daily and matrix.mode == "hourly"
    panel.demand.setCurrentIndex(panel.demand.findData("vehicles"))
    assert not panel.institutions.isVisible() and panel.pair_group.isVisible() and matrix.mode == "hourly"
    panel.flow.setValue(300)
    assert matrix.cells[("1W", "1E")].flow == 300
    daily(panel)  # choosing daily shows its options: trips per day and the day's curve
    assert matrix.mode == "daily" and panel.flow.value() == -1 and panel.day.isVisible()
    assert "day" in panel.flow.suffix()
    panel.flow.setValue(1_500_000)  # more than an hourly box allows
    cell = matrix.cells[("1W", "1E")]
    assert (cell.flow, cell.daily) == (300, 1_500_000)
    hourly(panel)
    assert matrix.cells[("1W", "1E")].flow == 300 and panel.flow.value() == 300  # switching wrote nothing
    assert not panel.day.isVisible()
    daily(panel)
    panel.clear_cell()  # clears the daily demand only
    cell = matrix.cells[("1W", "1E")]
    assert (cell.flow, cell.daily) == (300, None)


def test_the_grid_zooms_and_pans_like_the_map():
    from PySide6.QtCore import QPointF
    from PySide6.QtGui import QWheelEvent

    matrix = ODMatrix()
    ends = [f"{n}{side}" for side in "NESW" for n in range(1, 6)]  # 20 x 20: bigger than the view
    matrix.set_ends(ends, ends)
    matrix.resize(700, 600)
    matrix.show()
    APP.processEvents()
    table, bars = matrix.table, (matrix.table.horizontalScrollBar(), matrix.table.verticalScrollBar())
    assert table.columnWidth(0) == 56 and table.rowHeight(0) == 40  # 100 %
    viewport = table.viewport()

    def wheel(at, steps):
        event = QWheelEvent(QPointF(at), QPointF(viewport.mapToGlobal(at)), QPoint(), QPoint(0, 120 * steps),
                            Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)
        QApplication.sendEvent(viewport, event)

    at = QPoint(300, 200)
    under = table.indexAt(at)
    wheel(at, 1)  # zooms in at the cursor: the same cell stays under it
    assert matrix.zoom > 1 and table.columnWidth(0) > 56 and table.indexAt(at) == under
    wheel(at, -1)
    assert abs(matrix.zoom - 1) < 1e-9
    start = [bar.value() for bar in bars]
    QTest.mousePress(viewport, Qt.MouseButton.RightButton, pos=QPoint(400, 300))
    QTest.mouseMove(viewport, QPoint(300, 250))
    QTest.mouseRelease(viewport, Qt.MouseButton.RightButton, pos=QPoint(300, 250))
    assert [bar.value() for bar in bars] == [start[0] + 100, start[1] + 50]  # dragged left and up: pans right, down
    assert not matrix.selected_pairs()  # a pan selects nothing
    matrix.fit()  # the whole grid shows: no scrolling left
    APP.processEvents()
    assert matrix.zoom < 1 and bars[0].maximum() == 0 and bars[1].maximum() == 0
    zoom_out, zoom_in, fit = (matrix.corner.itemAt(i).widget() for i in range(3))
    fitted = matrix.zoom
    zoom_in.click()
    assert abs(matrix.zoom - fitted * 1.25) < 1e-9 and bars[0].maximum() > 0
    for _ in range(20):
        zoom_out.click()
    assert matrix.zoom == 0.25 and table.columnWidth(0) == 14  # the limit
    fit.click()
    assert abs(matrix.zoom - fitted) < 0.01


def test_the_keyboard_moves_between_cells_and_types_flows():
    matrix, panel = setup()
    QTest.keyClick(matrix.table, "4")  # a number typed on the matrix starts that cell's flow...
    QTest.keyClicks(panel.flow, "50")  # ...and the rest goes into the Flow box
    assert matrix.cells[("1W", "1E")].flow == 450
    QTest.keyClick(panel.flow, Qt.Key.Key_Right)  # arrows in Flow move to the neighbouring cell
    assert matrix.current_pair() == ("1W", "2E") and matrix.selected_pairs() == [("1W", "2E")]
    QTest.keyClicks(panel.flow, "7")  # its "—" is selected, so typing replaces it
    QTest.keyClick(panel.flow, Qt.Key.Key_Down)
    QTest.keyClick(panel.flow, Qt.Key.Key_Left)
    assert matrix.current_pair() == ("2W", "1E") and matrix.cells[("1W", "2E")].flow == 7
    QTest.keyClick(panel.flow, Qt.Key.Key_Left)  # at the edge it stays
    assert matrix.current_pair() == ("2W", "1E") and panel.flow.value() == -1  # and Left never changed a value
    matrix.set_ends(["1W", "2W"], ["1E", "1W", "2E"])  # 1W -> 1W can't be a trip: the arrows hop it
    matrix.table.setCurrentCell(0, 0)
    QTest.keyClick(panel.flow, Qt.Key.Key_Right)
    assert matrix.current_pair() == ("1W", "2E")


def test_the_curve_is_shaped_by_dragging_and_by_equations():
    matrix, panel = setup()
    daily(panel)
    panel.flow.setValue(2400)
    curve = panel.day.curve
    peak = curve._screen(*WEEKDAY[3]).toPoint()  # the 08:00 point
    QTest.mousePress(curve, Qt.MouseButton.LeftButton, pos=peak)
    QTest.mouseMove(curve, peak + QPoint(0, 40))
    QTest.mouseRelease(curve, Qt.MouseButton.LeftButton, pos=peak + QPoint(0, 40))
    stored = matrix.cells[("1W", "1E")].profile
    assert abs(stored.points[3][0] - 8) < 0.1 and stored.points[3][1] < 0.9  # dragged down, about the same hour
    count = len(stored.points)
    QTest.mouseDClick(curve, Qt.MouseButton.LeftButton, pos=curve._screen(*stored.points[3]).toPoint())
    assert len(matrix.cells[("1W", "1E")].profile.points) == count - 1  # double-click removes it
    assert "Busiest hour" in panel.day.peak.text() and "veh/h" in panel.day.peak.text()

    day = panel.day
    day.kind.setCurrentIndex(day.kind.findData(3))
    day._kind_chosen()
    profile = matrix.cells[("1W", "1E")].profile
    assert profile.kind == "poly" and len(profile.coefficients) == 4
    assert day.coefficients[3].isVisibleTo(panel) and not day.coefficients[4].isVisibleTo(panel)
    day.coefficients[0].setValue(day.coefficients[0].value() + 0.5)
    assert abs(matrix.cells[("1W", "1E")].profile.coefficients[0] - day.coefficients[0].value()) < 1e-9

    panel.copy_choices["curve"].setChecked(True)
    panel.apply_to("row")  # 1W -> 2E gets the same curve
    assert matrix.cells[("1W", "2E")].profile == matrix.cells[("1W", "1E")].profile
    panel.day.reset.click()
    assert matrix.cells[("1W", "1E")].profile.points == WEEKDAY


def test_a_types_trips_can_end_part_way_along_the_pair():
    matrix, panel = setup()
    panel.router = lambda origin, destination: (1000.0, [(0.4, "J1"), (0.7, "J2")])  # the fastest route, 1 km
    panel.flow.setValue(600)
    modes, slider = panel.modes, panel.drop_slider
    zone = QPoint(round(modes._x((modes.cuts[0] + modes.cuts[1]) / 2)), modes.TRACK_TOP + 6)  # the middle of Car's zone
    split = modes.split()
    QTest.mouseClick(modes, Qt.MouseButton.LeftButton, pos=zone)
    assert modes.split() == split  # a zone, not a handle: nothing moves...
    assert panel.drop_box.isVisibleTo(panel) and panel.drop_heading.text() == "Drop-offs: Car"  # ...Car's drop-offs
    assert "100 % of these trips reach 1E" in panel.drop_hint.text() and not panel.drop_share_row.isVisibleTo(panel)
    track = lambda at: QPoint(round(slider._x(at)), slider.TRACK_Y)  # noqa: E731
    QTest.mouseClick(slider, Qt.MouseButton.LeftButton, pos=track(0.41))  # near J1: snapped to it
    assert matrix.cells[("1W", "1E")].drops == ((1, 0.4, 10),) and panel.drop_share.value() == 10
    panel.drop_share.setValue(25)  # typed: a quarter of the cars end at J1
    assert matrix.cells[("1W", "1E")].drops == ((1, 0.4, 25),) and "75 % of these trips" in panel.drop_hint.text()
    QTest.mousePress(slider, Qt.MouseButton.LeftButton, pos=track(0.4))
    QTest.mouseMove(slider, track(0.55))
    QTest.mouseRelease(slider, Qt.MouseButton.LeftButton, pos=track(0.55))  # dragged between the junctions
    (kind, at, share), = matrix.cells[("1W", "1E")].drops
    assert kind == 1 and abs(at - 0.55) < 0.01 and share == 25
    QTest.mouseClick(slider, Qt.MouseButton.LeftButton, pos=track(0.9))
    assert [point[2] for point in matrix.cells[("1W", "1E")].drops] == [25, 10]
    assert panel.drop_share.maximum() == 75  # the two can't end more than all the cars
    QTest.mouseClick(slider, Qt.MouseButton.RightButton, pos=track(0.9))  # removed
    assert len(matrix.cells[("1W", "1E")].drops) == 1
    read = panel._read_for_assistant()
    assert read["different"]["1W->1E"]["drop_offs"]["Car"][0]["percent"] == 25  # the assistant sees them
    matrix.vehicle_modes.delete(0)  # Bus goes: Car's points move down to its new index
    assert matrix.cells[("1W", "1E")].drops[0][0] == 0 and not panel.drop_box.isVisibleTo(panel)


def test_the_assistant_puts_drop_offs_at_junctions_on_the_pairs_route():
    matrix, panel = setup()
    panel.router = lambda origin, destination: (1000.0, [(0.4, "J1"), (0.7, "J2")])  # as the slider's route
    panel.command("set_od_demand", {"pairs": ["1W->1E"], "value": 600})
    done = panel.command("set_od_demand", {"pairs": ["1W->1E"], "drop_offs": [
        {"mode": "Bus", "at": "J2", "percent": 30}, {"mode": "car", "at": "0.25", "percent": 10}]})
    assert matrix.cells[("1W", "1E")].drops == ((0, 0.7, 30), (1, 0.25, 10))  # a stop at J2, as the slider snaps
    assert done["drop_offs"]["Bus"] == [{"at_share_of_trip": 0.7, "percent": 30}] and matrix.cells[("1W", "1E")].flow == 600
    panel.command("set_od_demand", {"pairs": ["1W->1E"], "drop_offs": [{"mode": "Bus", "at": "J1", "percent": 20}]})
    assert matrix.cells[("1W", "1E")].drops == ((0, 0.4, 20), (1, 0.25, 10))  # Bus's replaced, Car's kept
    for drops, why in (([{"mode": "Bus", "at": "J9", "percent": 5}], "J1 (40%), J2 (70%)"),  # names the route's stops
                       ([{"mode": "Bus", "at": "J1", "percent": 60}, {"mode": "Bus", "at": "J2", "percent": 50}], "more than 100 %"),
                       ([{"mode": "Tram", "at": "J1", "percent": 5}], "No vehicle type called")):
        try:
            panel.command("set_od_demand", {"pairs": ["1W->1E"], "drop_offs": drops}, check_only=True)
        except ValueError as exc:
            assert why in str(exc), exc
        else:
            raise AssertionError(f"{drops} must be refused")
    assert matrix.cells[("1W", "1E")].drops == ((0, 0.4, 20), (1, 0.25, 10))  # each refusal changed nothing
    panel.router = lambda origin, destination: None
    try:
        panel.command("set_od_demand", {"pairs": ["1W->1E"], "drop_offs": [{"mode": "Bus", "at": "0.5", "percent": 5}]})
    except ValueError as exc:
        assert "No route from 1W to 1E" in str(exc)
    else:
        raise AssertionError("a pair with no route has nowhere to drop off")
    panel.command("set_od_demand", {"pairs": ["1W->1E"], "drop_offs": []})
    assert matrix.cells[("1W", "1E")].drops == ()  # [] clears them


def test_the_assistant_works_the_same_controls():
    matrix, panel = setup()
    undo_steps = []
    panel.changed.connect(undo_steps.append)
    panel.command("set_od_demand", {"origins": ["1W"], "demand": "daily", "value": 1200, "arrival_pattern": "poisson",
                                    "mode_split": {"bus": 20, "car": 70, "bike": 10}})
    assert matrix.mode == "daily" and panel.period.value() == "daily"  # switched like the switch
    assert panel.demand.currentData() == "vehicles"
    assert matrix.selected_pairs() == [("1W", "1E"), ("1W", "2E")]  # selected in the grid
    for pair in (("1W", "1E"), ("1W", "2E")):
        cell = matrix.cells[pair]
        assert (cell.daily, cell.pattern, cell.split) == (1200, "Poisson", (20, 70, 10))
    assert panel.flow.value() == 1200 and panel.pattern.currentText() == "Poisson"  # the controls show it
    assert panel.modes.split() == (20, 70, 10) and [box.value() for box in panel.mode_shares] == [20, 70, 10]
    assert undo_steps == [None]  # one undo step for the whole change

    # Small models send lists and numbers as text: read the same way.
    try:  # the same values again: nothing would change, so it is refused, not reported as done
        panel.command("set_od_demand", {"origins": '["1W"]', "demand": "daily", "value": "1200",
                                        "mode_split": '{"bus": 20, "car": 70, "bike": 10}'}, check_only=True)
    except ValueError as exc:
        assert "Nothing would change" in str(exc)
    else:
        raise AssertionError("re-entering the same demand should be refused")
    assert matrix.cells[("1W", "2E")].daily == 1200 and undo_steps == [None] and matrix.mode == "daily"
    panel.command("shape_daily_curve", {"pairs": ["1W->1E"], "points": [[17, 90], [8, 100], [3, 5]]})
    profile = matrix.cells[("1W", "1E")].profile  # sorted, scaled to 0-1 and run from midnight to midnight
    assert profile.points == [(0.0, 0.05), (3.0, 0.05), (8.0, 1.0), (17.0, 0.9), (24.0, 0.9)]
    assert panel.day.profile == profile and panel.day.kind.currentData() == "points"
    panel.command("shape_daily_curve", {"pairs": ["1W to 2E"], "polynomial": [0, 4, -4]})
    assert matrix.cells[("1W", "2E")].profile.coefficients == [0.0, 4.0, -4.0]
    assert panel.day.kind.currentData() == 2 and panel.day.coefficients[2].value() == -4.0  # the equation shows
    assert matrix.cells[("1W", "1E")].profile == profile  # the other pair kept its curve

    read = panel.command("read_od_matrix", {})
    assert read["showing"] == "daily" and set(read["daily_trips"]) == {"1W->1E", "1W->2E"}
    assert read["usual"]["daily_curve"]["kind"] in ("points", "polynomial")  # shared once...
    assert set(read["different"]) <= {"1W->1E", "1W->2E"} and read["pairs_with_data_count"] == 2  # ...exceptions apart
    for args, message in (({"pairs": ["9X->1E"], "demand": "hourly", "value": 5}, "No such origin"),
                          ({"origins": ["1W"], "demand": "daily", "mode_split": {"bus": 50, "car": 60}}, "adding up to 100"),
                          ({"origins": ["1W"], "demand": "daily", "mode_split": [0.4, 0.6]}, "one each for"),
                          ({"pairs": ["1W->1E"], "demand": "hourly", "mode_split": []}, "Give a value"),  # [] is none
                          ({"pairs": ["1W->1E"], "demand": "hourly"}, "Give a value")):
        try:
            panel.command("set_od_demand", args)
        except ValueError as exc:
            assert message in str(exc), (message, str(exc))
        else:
            raise AssertionError(f"{args} should have been refused")
    assert undo_steps == [None, None, None]  # refusals changed nothing
    assert shares_from([0.3, 0.6, 0.1], panel.matrix.vehicle_modes.names()) == [30, 60, 10]  # fractions (llama3.2) read as percent
    for written in ("1W-1E", "1W->>1E", "1W => 1E", "1W - 1E"):  # pairs as small models write them
        assert panel._pairs_from({"pairs": [written]}) == [("1W", "1E")], written
    panel.command("shape_daily_curve", {"preset": "weekday"})  # no pairs named: every pair
    pairs = [(o, d) for o in matrix.origins for d in matrix.destinations if o != d]
    assert all(matrix.cells[pair].profile.points for pair in pairs) and len(undo_steps) == 4


def test_hourly_demand_takes_a_peak_hour_factor_or_quarters():
    matrix, panel = setup()
    panel.flow.setValue(400)  # 1W -> 1E
    assert matrix.cells[("1W", "1E")].quarters == () and panel.phf.value() == 1.0 and panel.phf.isVisible()
    panel.phf.setValue(0.8)  # the spin box: the busiest 15 minutes at 400 / 0.8 veh/h
    quarters = matrix.cells[("1W", "1E")].quarters
    assert abs(max(quarters) - 1 / 3.2) < 1e-12 and abs(sum(quarters) - 1) < 1e-12
    done = panel.command("set_od_demand", {"pairs": ["1W->1E"], "quarters": [100, 150, 100, 50]})
    assert done["quarters"] == [0.25, 0.375, 0.25, 0.125] and done["peak_hour_factor"] == round(1 / 1.5, 3)
    assert abs(panel.phf.value() - 0.67) < 1e-9  # the box shows the profile's PHF
    read = panel.command("read_od_matrix", {})
    assert read["usual"]["quarters"] == [0.25, 0.375, 0.25, 0.125]
    for args, message in (({"pairs": ["1W->1E"], "peak_hour_factor": 0.1}, "0.25 to 1"),
                          ({"pairs": ["1W->1E"], "quarters": [1, 2]}, "four numbers"),
                          ({"pairs": ["1W->1E"], "demand": "daily", "peak_hour_factor": 0.9}, "hourly demand only"),
                          ({"pairs": ["1W->2E"], "peak_hour_factor": 0.9}, "no hourly demand yet")):
        try:
            panel.command("set_od_demand", args)
        except ValueError as exc:
            assert message in str(exc), (message, str(exc))
        else:
            raise AssertionError(f"{args} should have been refused")
    panel.command("set_od_demand", {"pairs": ["1W->1E"], "peak_hour_factor": 1})
    assert matrix.cells[("1W", "1E")].quarters == ()  # 1: an even hour
    daily(panel)
    assert not panel.phf.isVisible()  # no PHF on a daily curve


def test_demand_tools_say_which_demand_a_run_uses_and_refuse_what_does_nothing():
    app = QApplication.instance() or QApplication([])
    matrix = ODMatrix()
    panel = ODCellPanel(matrix)
    ends = ["1W", "1E", "2W", "2E"]
    matrix.set_ends(ends, ends)
    done = panel.command("set_od_demand", {"pairs": ["1W->1E"], "value": 300})  # no demand given: the kind shown
    assert matrix.mode == "hourly" and matrix.cells[("1W", "1E")].flow == 300 and done["runs_use"] == "hourly demand"
    try:
        panel.command("set_od_demand", {"pairs": ["2W->2E"], "arrival_pattern": "Poisson"}, check_only=True)
    except ValueError as exc:
        assert "no hourly demand yet" in str(exc)  # a pattern alone for a pair with no demand
    else:
        raise AssertionError("a pattern for an empty pair should be refused")
    assert ("2W", "2E") not in matrix.cells
    before = dict(matrix.cells)
    assert panel.command("shape_daily_curve", {"preset": "flat"}, check_only=True) == {"ok": True}
    assert matrix.mode == "hourly" and matrix.cells == before  # the check tried it and put it all back
    done = panel.command("shape_daily_curve", {"preset": "flat"})
    assert done["runs_use"] == "daily demand" and "not the hourly demand" in done["note"]  # the switch is said


def test_shares_adding_up_to_100_are_kept_in_whole_percent():
    # Each share was rounded on its own: 12.5 + 82.5 + 5 made 99 (round half to even) and was refused, so were thirds.
    names = ["Bus", "Car", "Bike"]
    assert shares_from({"Bus": 12.5, "Car": 82.5, "Bike": 5}, names) == [13, 82, 5]
    assert sum(shares_from([33.3, 33.3, 33.4], names)) == 100 and sum(shares_from([1 / 3] * 3, names)) == 100
    assert shares_from([1 / 3] * 3, names) == [34, 33, 33]
    for bad in ([30, 30, 30], [0.5, 0.2, 0.2], [110, -5, -5], [50, 50]):
        try:
            shares_from(bad, names)
        except ValueError as exc:
            assert "adding up to 100" in str(exc), exc
        else:
            raise AssertionError(bad)


def test_demand_rules_need_no_qt():
    """The OD cells' rules (demand.py) without a widget: the split in effect, a tool's pairs and plan, the read-out and
    a run's pair demand; importing them loads no Qt."""
    loaded = subprocess.run([sys.executable, "-c", "import sys, traffic_simulator.demand; print(sorted(sys.modules))"],
                            capture_output=True, text=True, timeout=60, check=True).stdout
    assert "PySide6" not in loaded and "traffic_simulator.od_matrix" not in loaded
    from traffic_simulator import demand
    names, per_hour = ["Bus", "Car", "Bike"], {0: 6.0}  # a bus every 10 minutes
    origins, destinations = ["1W", "2W"], ["1E", "2E"]
    cells = {("1W", "1E"): demand.ODCell(flow=600, split=(10, 80, 10)), ("2W", "2E"): demand.ODCell(daily=4800),
             ("9X", "1E"): demand.ODCell(flow=50)}  # an end no longer on the network: left out
    assert demand.split_of(cells[("1W", "1E")], per_hour, "hourly") == (1, 88, 11)  # 6 buses of 600, the rest 80:10
    both = demand.ODCell(flow=60, daily=20_000, split=(10, 85, 5))  # a daily run fixes it against the trips a day
    assert demand.split_of(both, per_hour, "daily") == demand.split_of(demand.ODCell(daily=20_000, split=(10, 85, 5)),
                                                                       per_hour, "daily") == (1, 94, 5)
    assert demand.pair_demand({("1W", "1E"): both}, origins, destinations, "daily", per_hour)[0].split == (1, 94, 5)
    assert demand.split_of(both, per_hour, "hourly") == (10, 85, 5)  # 6 buses of 60 an hour
    assert demand.pairs_from({"pairs": "1W-1E"}, origins, destinations) == [("1W", "1E")]
    assert demand.demand_plan({"value": 12.6, "arrival_pattern": "poisson"}, "hourly", names) ==         ("hourly", 13, "Poisson", None, None)
    read = demand.read_od(cells, origins, destinations, "hourly", names, per_hour)
    assert read["hourly_vph"] == {"1W->1E": 600} and read["daily_trips"] == {"2W->2E": 4800}
    runs = demand.pair_demand(cells, origins, destinations, "hourly", per_hour)
    assert [(run.origin, run.destination, run.hourly, run.split) for run in runs] == [("1W", "1E", 600, (1, 88, 11))]


def test_pedestrians_an_hour_are_typed_between_boundary_ends():
    from traffic_simulator.models import MAX_WALK_DEMAND
    from traffic_simulator.network_generator import generate_network
    from traffic_simulator.od_matrix import PedestrianMatrix

    network, grid = generate_network(2, 2), PedestrianMatrix()
    grid.show_network(network)
    assert grid.stations == sorted(network.approach_nodes) and grid.table.rowCount() == len(network.approach_nodes)
    changes = []
    grid.changed.connect(lambda: changes.append(1))
    row, column = grid.stations.index("1W"), grid.stations.index("2E")
    grid.table.item(row, column).setText("450")
    assert network.walk_demand == {"1W->2E": 450.0} and changes == [1]
    grid.table.item(row, column).setText(str(MAX_WALK_DEMAND + 1))  # too many: put back
    assert network.walk_demand == {"1W->2E": 450.0} and grid.table.item(row, column).text() == "450"
    assert not grid.table.item(row, row).flags() & Qt.ItemFlag.ItemIsEditable  # the diagonal: nowhere to walk
    grid.table.item(row, column).setText("")
    assert network.walk_demand == {}


def test_pedestrians_an_hour_are_typed_between_spawn_points():
    from traffic_simulator.network_generator import generate_network
    from traffic_simulator.od_matrix import SpawnMatrix

    network, grid = generate_network(2, 2), SpawnMatrix()
    assert not grid.day.isVisibleTo(grid.side) and not grid.day_hint.isVisibleTo(grid.side)  # no curve before a pair
    grid.show_network(network)
    assert not grid.table.isVisibleTo(grid) and "No deploy points yet" in grid.empty.text()
    for x, y in ((10.0, 20.0), (300.0, 40.0)):
        network.add_spawn_point(x, y)
    grid.show_network(network)
    assert grid.stations == ["S1", "S2"]
    grid.table.item(0, 1).setText("120")
    assert network.spawn_demand == {"S1->S2": 120.0} and not network.walk_demand
    assert not grid.table.item(0, 0).flags() & Qt.ItemFlag.ItemIsEditable


def test_every_grids_diagonal_is_drawn_shut():
    # A cell nothing can be typed in (the diagonal: no trip from a place to itself) is theme.BLOCKED_CELL hatched with
    # BLOCKED_HATCH, in the vehicle OD grid and in the metro's, the pedestrians' and the spawn points' grids alike
    from PySide6.QtGui import QColor

    from traffic_simulator import theme
    from traffic_simulator.network_generator import generate_network
    from traffic_simulator.od_matrix import PedestrianMatrix, SpawnMatrix

    shut = {QColor(theme.BLOCKED_CELL).name(), QColor(theme.BLOCKED_HATCH).name()}

    def colours(table, row, column):
        rect = table.visualRect(table.model().index(row, column))
        image = table.viewport().grab().toImage()
        return {image.pixelColor(x, y).name() for x in range(rect.left() + 2, rect.right() - 1, 3)
                for y in range(rect.top() + 2, rect.bottom() - 1, 3)}

    matrix = ODMatrix()
    matrix.resize(420, 300)
    matrix.set_ends(["1W", "2W", "1E"], ["1W", "2W", "1E"])  # the same ends as rows and columns: a diagonal
    matrix.show()
    APP.processEvents()
    assert colours(matrix.table, 1, 1) <= shut and len(colours(matrix.table, 1, 1)) == 2  # hatched across
    assert not colours(matrix.table, 0, 1) & {QColor(theme.BLOCKED_CELL).name()}  # a pair to type in: open
    network = generate_network(2, 2)
    for x, y in ((0.0, 0.0), (300.0, 0.0)):
        network.add_spawn_point(x, y)
    for grid in (SpawnMatrix(), PedestrianMatrix()):
        grid.resize(400, 300)
        grid.show_network(network)
        grid.show()
        APP.processEvents()
        assert colours(grid.table, 1, 1) <= shut and not colours(grid.table, 0, 1) <= shut
        grid.close()


def test_a_stations_tooltip_names_its_lines_in_five_words_at_most():
    from types import SimpleNamespace

    from traffic_simulator.network_generator import generate_network
    from traffic_simulator.od_matrix import MetroMatrix, PedestrianMatrix

    network = generate_network(2, 2)
    grid = MetroMatrix()
    for count, tip in ((1, "Line ML1"), (4, "Lines ML1, ML2, ML3, ML4"), (5, "5 lines"), (12, "12 lines")):
        network.metro_lines = {f"ML{n}": SimpleNamespace(id=f"ML{n}", stops=["M1"]) for n in range(1, count + 1)}
        assert grid._tip(network, "M1") == tip and len(tip.split()) <= 5
    assert PedestrianMatrix()._tip(network, "1W") == "Boundary end"


if __name__ == "__main__":
    for name, check in list(globals().items()):
        if name.startswith("test_"):
            check()
    print("ok")

"""OD matrix hourly and daily demand, through the real widgets (offscreen).
Run with `python tests/test_od_matrix.py` (or the test monitor)."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from traffic_simulator.daily_profile import WEEKDAY  # noqa: E402
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
    panel.demand.setCurrentIndex(panel.demand.findData("daily"))  # the Demand drop-down


def test_a_demand_cut_by_its_check_leads_with_what_was_kept():
    from traffic_simulator.od_matrix import _lead_with_what_was_kept

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
    assert [panel.demand.itemData(i) for i in range(panel.demand.count())] == ["hourly", "daily", "institutions", "metro"]
    daily(panel)
    panel.demand.setCurrentIndex(panel.demand.findData("institutions"))  # its own panel, not the pair editor
    assert panel.institutions.isVisible() and not panel.pair_group.isVisible() and matrix.mode == "daily"
    panel.demand.setCurrentIndex(panel.demand.findData("hourly"))
    assert not panel.institutions.isVisible() and panel.pair_group.isVisible() and matrix.mode == "hourly"
    panel.flow.setValue(300)
    assert matrix.cells[("1W", "1E")].flow == 300
    daily(panel)  # choosing daily shows its options: trips per day and the day's curve
    assert matrix.mode == "daily" and panel.flow.value() == -1 and panel.daily_box.isVisible()
    assert "day" in panel.flow.suffix()
    panel.flow.setValue(1_500_000)  # more than an hourly box allows
    cell = matrix.cells[("1W", "1E")]
    assert (cell.flow, cell.daily) == (300, 1_500_000)
    panel.demand.setCurrentIndex(panel.demand.findData("hourly"))
    assert matrix.cells[("1W", "1E")].flow == 300 and panel.flow.value() == 300  # switching wrote nothing
    assert not panel.daily_box.isVisible()
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
    curve = panel.curve
    peak = curve._screen(*WEEKDAY[3]).toPoint()  # the 08:00 point
    QTest.mousePress(curve, Qt.MouseButton.LeftButton, pos=peak)
    QTest.mouseMove(curve, peak + QPoint(0, 40))
    QTest.mouseRelease(curve, Qt.MouseButton.LeftButton, pos=peak + QPoint(0, 40))
    stored = matrix.cells[("1W", "1E")].profile
    assert abs(stored.points[3][0] - 8) < 0.1 and stored.points[3][1] < 0.9  # dragged down, about the same hour
    count = len(stored.points)
    QTest.mouseDClick(curve, Qt.MouseButton.LeftButton, pos=curve._screen(*stored.points[3]).toPoint())
    assert len(matrix.cells[("1W", "1E")].profile.points) == count - 1  # double-click removes it
    assert "Busiest hour" in panel.peak.text() and "veh/h" in panel.peak.text()

    panel.curve_kind.setCurrentIndex(panel.curve_kind.findData(3))
    panel._kind_chosen()
    profile = matrix.cells[("1W", "1E")].profile
    assert profile.kind == "poly" and len(profile.coefficients) == 4
    assert panel.coefficients[3].isVisibleTo(panel) and not panel.coefficients[4].isVisibleTo(panel)
    panel.coefficients[0].setValue(panel.coefficients[0].value() + 0.5)
    assert abs(matrix.cells[("1W", "1E")].profile.coefficients[0] - panel.coefficients[0].value()) < 1e-9

    panel.copy_choices["curve"].setChecked(True)
    panel.apply_to("row")  # 1W -> 2E gets the same curve
    assert matrix.cells[("1W", "2E")].profile == matrix.cells[("1W", "1E")].profile
    panel.reset_curve.click()
    assert matrix.cells[("1W", "1E")].profile.points == WEEKDAY


def test_the_assistant_works_the_same_controls():
    matrix, panel = setup()
    undo_steps = []
    panel.changed.connect(undo_steps.append)
    panel.command("set_od_demand", {"origins": ["1W"], "demand": "daily", "value": 1200, "arrival_pattern": "poisson",
                                    "mode_split": {"bus": 20, "car": 70, "bike": 10}})
    assert matrix.mode == "daily" and panel.demand.currentData() == "daily"  # switched like the drop-down
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
    assert panel.curve.profile == profile and panel.curve_kind.currentData() == "points"
    panel.command("shape_daily_curve", {"pairs": ["1W to 2E"], "polynomial": [0, 4, -4]})
    assert matrix.cells[("1W", "2E")].profile.coefficients == [0.0, 4.0, -4.0]
    assert panel.curve_kind.currentData() == 2 and panel.coefficients[2].value() == -4.0  # the equation shows
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
    assert panel._shares_from([0.3, 0.6, 0.1]) == [30, 60, 10]  # fractions (llama3.2) read as percent
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


if __name__ == "__main__":
    for name, check in list(globals().items()):
        if name.startswith("test_"):
            check()
    print("ok")

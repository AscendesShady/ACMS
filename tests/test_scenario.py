"""The Scenario Builder (scenario.py): the form completed and checked, the plan in a study's order, the report;
in the window, build_scenario as one undo step; and a request offered the tools it needs (network_tools.tools_for),
the model planning and calling them itself, from a fake Ollama.
Run with `python tests/test_scenario.py` (or the test monitor)."""

import json
import os
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from traffic_simulator import network_tools as tools, scenario  # noqa: E402

APP = QApplication.instance() or QApplication([])
EMPTY = {"has_network": False, "has_demand": False, "bus_type": "Bus"}
BUILT = {"has_network": True, "has_demand": True, "bus_type": "Bus"}


def test_the_form_is_completed_and_checked_before_anything_runs():
    spec, assumed, problems = scenario.prepare({}, EMPTY)
    assert spec["grid"] == "3x3" and spec["traffic"] == "medium" and not problems and len(assumed) >= 2
    spec, assumed, problems = scenario.prepare({"what_if": [{"change": "add lane"}]}, BUILT)
    assert spec["intent"] == "test" and "traffic" not in spec  # the demand entered stays
    assert spec["what_if"][0]["where"] == "busiest" and spec["seeds"] == 3
    for form, reason in (({"place": "Paris", "grid": "2x2"}, "not both"), ({"grid": "40x2"}, "1 to 20"),
                         ({"grid": "big"}, "Can't read"), ({"what_if": [{"change": "speed limit"}]}, "km/h"),
                         ({"what_if": [{"change": "signal timing"}]}, "method"), ({"minutes": 999}, "minutes"),
                         ({"bus_every_min": 5}, "no bus type")):
        _, _, problems = scenario.prepare(form, {**BUILT, "bus_type": None} if "bus_every_min" in form else BUILT)
        assert any(reason in problem for problem in problems), (form, problems)
    assert scenario.grid_size("4 by 3") == (4, 3) and scenario.grid_size("5×2") == (5, 2)


def test_the_plan_follows_a_study_and_each_step_is_a_real_tool():
    spec, _, _ = scenario.prepare({"place": "Uttara, Dhaka", "junctions": "roundabouts", "traffic": "heavy",
                                   "whole_day": True, "mode_choice": True, "bus_every_min": 8, "route_choice": True,
                                   "report": ["safety"], "what_if": [{"change": "optimise signals"},
                                                                    {"change": "more traffic", "value": 10}]}, EMPTY)
    steps = scenario.plan(spec)
    order = [step["tool"] for step in steps]
    assert order == ["place_map", "network_from_background", "set_junction_control", "update_vehicle_type",
                     "generate_demand", "shape_daily_curve", "choose_modes", "assign_routes", "run_simulation",
                     "analyse_run", "analyse_safety", "optimise_signals", "run_experiment"]
    assert set(order) <= {spec["name"] for spec in tools.TOOLS}  # nothing but the assistant's own tools
    demand = steps[order.index("generate_demand")]["args"]
    assert demand == {"demand": "daily", "degree_of_saturation": scenario.LEVELS["heavy"]}
    text = scenario.describe(spec, steps, ["x"])
    assert text.count("<br>") >= len(steps) and "Assumed: x" in text

    def resolve(token):
        return {"@busiest_road": ["1,1-2,1"], "@busiest_junction": ["2,2"], "@junctions": ["2,2", "3,2"],
                "@signalised": ["2,2"], "@lanes:1,1-2,1": 2, "@optimised": [{"tool": "set_signal_timing", "args": {}}]}[token]

    lane = scenario.variant({"change": "add lane", "where": "busiest"}, resolve)
    assert lane["name"] == "add lane at 1,1-2,1" and lane["changes"][0]["args"]["lanes_per_direction"] == 3
    assert scenario.variant({"change": "more traffic", "value": 30}, resolve)["demand_scale"] == 1.3
    assert scenario.variant({"change": "bus priority"}, resolve)["extensions"] == [scenario.TSP]
    assert scenario.variant({"change": "roundabout", "where": "all"}, resolve)["changes"][0]["args"]["junctions"] == ["2,2", "3,2"]


def test_the_report_says_what_was_measured():
    done = [{"tool": "generate_grid", "why": "", "ok": True, "result": {}},
            {"tool": "run_simulation", "why": "", "ok": True, "result": {
                "simulated": "00:30:00", "seed": 1, "throughput_vph": 900, "mean_speed_kmh": 30.5, "delay_veh_h": 4.2,
                "mean_trip_s": 200, "tti": 1.4, "pti": 1.9, "conflicts": 3, "longest_queue": None, "file": "a.xlsx"}},
            {"tool": "run_experiment", "why": "", "ok": True, "result": {"runs": 6, "file": "e.xlsx", "comparison": [
                {"figure": "Delay", "scenario": "add lane", "diff": -1.0, "clear": True, "better": True,
                 "change_percent": -12.5, "replications_needed": 4},
                {"figure": "Delay", "scenario": "roundabout", "diff": 0.1, "clear": False, "replications_needed": 30}]}},
            {"tool": "analyse_safety", "why": "Report safety", "ok": False, "error": "no run"}]
    text = scenario.report({"grid": "3x3"}, done)
    assert "a 3x3 grid" in text and "900 veh/h" in text and "travel time index 1.4" in text
    assert "add lane: delay better (-12.5 %)" in text and "roundabout: no clear difference" in text
    assert "over 20 runs" in text and "Couldn't: report safety: no run" in text and "e.xlsx" in text


def test_the_window_builds_a_described_scenario_as_one_undo_step():
    from traffic_simulator.main import MainWindow

    window = MainWindow()
    window.results_folder = Path(tempfile.mkdtemp())
    window._ask_to_save = False
    form = {"grid": "3x2", "traffic": "medium", "minutes": 6, "seeds": 2, "report": ["network"],
            "what_if": [{"change": "add lane"}, {"change": "close road", "where": "9,9-8,8"}]}
    preview = window._runs_command("build_scenario", form, check_only=True)["preview"]
    assert "Build a 3 x 2 grid" in preview and "Test 2 what-ifs" in preview and window.history.undo_stack == []
    try:
        window._runs_command("build_scenario", {"grid": "3x2", "place": "Paris"}, check_only=True)
    except ValueError as exc:
        assert "not both" in str(exc)
    else:
        raise AssertionError("a place and a grid together")
    result = window._runs_command("build_scenario", form)
    steps = {step["tool"]: step for step in result["steps"]}
    assert all(step["ok"] for step in result["steps"]), result["steps"]
    assert len(window.network.nodes) == 6 and window._pair_demand() and len(window.history.undo_stack) == 1
    assert "Built** a 3x2 grid" in result["report"] and "Baseline run" in result["report"]
    assert "network_indices" in steps and "run_experiment" in steps  # the closed road was left out, the rest tested
    experiment = window.tests["E1"]["experiment"]
    assert [s["name"] for s in experiment["scenarios"]][0] == "As built" and len(experiment["scenarios"]) == 2
    window.undo()
    assert not window.network.nodes or len(window.network.nodes) != 6 or not window._pair_demand()  # all of it undone
    window.close()
    window = MainWindow()  # it opens on a 4 x 3 grid: asking for that grid keeps it and builds the rest
    window.results_folder = Path(tempfile.mkdtemp())
    window._ask_to_save = False
    result = window._runs_command("build_scenario", {"grid": "4x3", "traffic": "heavy", "minutes": 5})
    assert all(step["ok"] for step in result["steps"]), result["steps"]  # it once stopped the whole build
    assert "already** a 4x3 grid" in result["report"] and "Baseline run" in result["report"] and window._pair_demand()
    window.close()


class _Ollama(BaseHTTPRequestHandler):
    """A local model with tools: to a request it plans in one line and calls generate_grid, then answers from the
    result; anything else (the warm-up) it answers "ok". It keeps the tools each request offered."""
    offered: list = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.send_response(200)
        self.end_headers()
        if self.path == "/api/show":
            self.wfile.write(json.dumps({"capabilities": ["completion", "tools"]}).encode())
            return
        _Ollama.offered.append([tool["function"]["name"] for tool in body.get("tools") or []])
        last = body["messages"][-1]
        message = {"role": "assistant", "content": "ok"}
        if last["role"] == "user" and "grid" in last["content"]:
            message = {"role": "assistant", "content": "Plan: a 3 x 2 grid with generate_grid.",
                       "tool_calls": [{"function": {"name": "generate_grid", "arguments": {"horizontal": 2, "vertical": 3}}}]}
        elif last["role"] == "user" and "SCOOT" in last["content"]:  # the slip qwen3.5 4B made: scats for SCOOT
            message = {"role": "assistant", "content": "Plan: SCOOT at 1,1 -> set_signal_timing", "tool_calls": [
                {"function": {"name": "set_signal_timing", "arguments": {"junctions": ["1,1"], "method": "scats"}}}]}
        elif last["role"] == "tool" and "not_what_was_asked" in last["content"]:
            message = {"role": "assistant", "content": "", "tool_calls": [
                {"function": {"name": "set_signal_timing", "arguments": {"junctions": ["1,1"], "method": "scoot"}}}]}
        elif last["role"] == "tool":
            message = {"role": "assistant", "content": "Done: a 3 x 2 grid."}
        for event in ({"message": message, "done": False}, {"message": {"content": ""}, "done": True}):
            self.wfile.write((json.dumps(event) + "\n").encode())

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(json.dumps({"models": [{"name": "small:3b"}]}).encode())

    def log_message(self, *args):
        pass


def test_a_request_is_offered_the_tools_it_needs_and_planned_by_the_model():
    from traffic_simulator.main import MainWindow

    server = HTTPServer(("127.0.0.1", 0), _Ollama)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    before = os.environ.get("OLLAMA_HOST")
    os.environ["OLLAMA_HOST"] = f"http://127.0.0.1:{server.server_address[1]}"
    window = MainWindow()
    window.results_folder = Path(tempfile.mkdtemp())
    window._ask_to_save = False
    panel = window.ai_mode

    def settle(condition, limit=120.0):
        start = time.time()
        while not condition() and time.time() - start < limit:
            APP.processEvents()
            time.sleep(0.01)
        assert condition(), "timed out"

    try:
        panel.rescan()
        panel.local_models.setCurrentText("small:3b")
        panel.provider.setCurrentIndex(panel.provider.findData("local"))
        panel.provider.activated.emit(panel.provider.currentIndex())
        assert not hasattr(panel, "guided")  # no form mode: the model chooses its tools
        panel.start_button.click()
        settle(lambda: panel.prompt.isEnabled() and not panel._running)
        _Ollama.offered.clear()
        panel.prompt.setText("Build me a 3x2 grid")
        panel.send()
        settle(lambda: not panel.proposal.isHidden())
        first = _Ollama.offered[0]
        assert "generate_grid" in first and tools.FIND_TOOLS in first and len(first) < len(tools.TOOLS) // 2
        assert "schedule_emergency" not in first  # nothing in the request points to it
        panel.apply_button.click()
        settle(lambda: panel.prompt.isEnabled() and not panel._running)
        assert len(window.network.nodes) == 6 and panel.history[-1]["content"] == "Done: a 3 x 2 grid."
        panel.prompt.setText("thanks")  # a follow-up keeps the tools the conversation used
        panel.send()
        settle(lambda: panel.prompt.isEnabled() and not panel._running)
        assert "generate_grid" in _Ollama.offered[-1] and panel.history[-1]["content"] == "ok"
        steps, keep = [], panel._host.step
        panel._host.step = lambda event: steps.append(event) or keep(event)
        window.network.node("1,1").control = "signalised"
        panel.prompt.setText("use SCOOT at junction 1,1")  # the method asked for, not a look-alike
        panel.send()
        settle(lambda: not panel.proposal.isHidden())
        assert "scoot" in panel.proposal_text.text().lower() and "scats" not in panel.proposal_text.text().lower()
        panel.apply_button.click()
        settle(lambda: panel.prompt.isEnabled() and not panel._running and steps)
        assert window.network.node("1,1").signal_method == "scoot"
        decisions = [(c["tool"], c["decision"]) for c in steps[-1]["calls"]]
        assert decisions[0] == ("set_signal_timing", "refused: not what was asked") and decisions[-1][1] == "applied"
    finally:
        window.close()
        server.shutdown()
        if before is None:
            os.environ.pop("OLLAMA_HOST", None)
        else:
            os.environ["OLLAMA_HOST"] = before


if __name__ == "__main__":
    for name, check in list(globals().items()):
        if name.startswith("test_"):
            check()
    print("ok")

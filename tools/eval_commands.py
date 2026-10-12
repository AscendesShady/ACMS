"""How well local models turn requests into the right tool calls, against hand-written expectations.

Not a unit test (it needs Ollama and takes minutes): run `python tools/eval_commands.py [model ...]`.
Each case is a request as a user types it (the first three are requests guided mode got wrong in the Research
Log); the model chats with the app's own system prompt and the tools the app would offer it. Network tools run on
a real grid; the window's tools (runs, demand, institutions' trips) answer with a stand-in result, so a case shows
which tools the model chose and with what, not what they'd measure. A case passes when the calls include one of
its expected sets, none of its forbidden tools, and its check holds. Prints a table per model and writes
tools/eval_commands.json (not kept in git)."""

import json
import os
import sys
import time
from copy import deepcopy
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from traffic_simulator import assistant, network_tools  # noqa: E402
from traffic_simulator.network_generator import generate_network  # noqa: E402

MODELS = ["qwen3.5:4b", "llama3.2:3b", "gemma4:e4b"]
EDITOR = "[Editor update]\nSelected on the map: nothing\n[End of editor update]\n\n"
BUILD_PLACE = {"place_map", "network_from_background"}


def _grid_size(calls):
    """The extent of the last generate_grid call, (width, height) in metres, or None."""
    grids = [c["arguments"] for c in calls if c["name"] == "generate_grid"]
    if not grids:
        return None
    args = grids[-1]
    try:
        spacing = float(args.get("spacing_m") or 300)
        return (int(args["vertical"]) - 1) * spacing, (int(args["horizontal"]) - 1) * spacing
    except (KeyError, TypeError, ValueError):
        return None


def _about(size, width, height, slack=0.35):
    return size is not None and all(abs(a - b) <= slack * b for a, b in zip(sorted(size), sorted((width, height))))


CASES = [
    {"name": "city 2000x1500 from institutions",
     "request": "create a nice city replacing the existing grid. Keep some roads, demand should be made with the "
                "institution block. The city should be around 2000m x 1500m",
     "expect": [{"generate_grid", "deploy_institutions"}, {"generate_grid", "place_institution"}],
     "forbid": BUILD_PLACE,
     "check": lambda calls: _about(_grid_size(calls), 2000, 1500), "check_says": "grid about 2000 x 1500 m"},
    {"name": "busy city demand, hourly",
     "request": "Create a demand for busy city in hourly basis",
     "expect": [{"generate_demand"}, {"scale_demand"}, {"set_od_demand"}],
     "forbid": BUILD_PLACE,
     "check": lambda calls: any((c["arguments"].get("degree_of_saturation") or 0) >= 0.75 or c["arguments"].get("total")
                                for c in calls if c["name"] == "generate_demand"),
     "check_says": "a busy level (saturation 0.75+ or a total)"},
    {"name": "dense city of institutions, peak",
     "request": "create a dense city using the instituitions and thus simulate a peak hour traffic.",
     "expect": [{"deploy_institutions", "run_simulation"}, {"place_institution", "run_simulation"}],
     "forbid": BUILD_PLACE},
    {"name": "real place, heavy traffic",
     "request": "Build Dhanmondi in Dhaka and give it heavy traffic",
     "expect": [{"place_map", "network_from_background", "generate_demand"}, {"build_scenario"}],
     "forbid": {"generate_grid"}},
    {"name": "what-if lane",
     "request": "does a third lane on 2,2-3,2 cut delay?",
     "expect": [{"run_experiment"}, {"build_scenario"}],
     "forbid": {"update_roads"}},  # the open network stays as it is: a scenario on a copy
    {"name": "why a junction is slow",
     "request": "why is junction 2,2 so slow?",
     "expect": [{"diagnose_signal"}, {"junction_los"}]},
    {"name": "all signals SCOOT",
     "request": "switch every signal to SCOOT",
     "expect": [{"set_signal_timing"}, {"set_junction_control"}]},
    {"name": "concept question",
     "request": "what is a level of service?",
     "expect": [set()], "forbid": network_tools.EDIT_TOOLS},
]


RUN_READS = {"junction_los", "diagnose_signal", "analyse_run", "read_detectors", "emissions_report", "check_run_health",
             "analyse_safety", "read_results", "compare_runs", "read_decisions"}


def stand_in(name: str, args: dict, state: dict) -> dict:
    """What a window tool would say, roughly (its shape and plausible numbers): enough for the model to carry on."""
    if name == "run_simulation":
        state["runs"] += 1
        return {"run": f"R{state['runs']}", "simulated": f"00:{int(args.get('duration_min') or 30):02d}:00",
                "throughput_vph": 4200, "mean_speed_kmh": 31.5, "delay_veh_h": 60.2, "mean_trip_s": 210,
                "longest_queue": "14 veh on 2,2-3,2", "health": "fine", "file": "ACMS results R1.xlsx"}
    if name in RUN_READS and not state["runs"]:
        return network_tools.explain_error("No run to read yet: call run_simulation first.")
    if name == "diagnose_signal":
        return {"junction": args.get("junction") or "2,2", "method": "webster", "cycle_s": 90, "Y": 0.71, "Xc": 0.93,
                "approaches": [{"link": "1,2-2,2", "v_c": 1.04, "arrival_type": 2, "delay_s": 71, "los": "E"},
                               {"link": "2,1-2,2", "v_c": 0.62, "arrival_type": 3, "delay_s": 24, "los": "C"}],
                "causes": [{"cause": "greens not matching demand", "evidence": "1,2-2,2 at v/c 1.04 while Xc 0.93",
                            "fix": "set_signal_timing with greens by flow ratio, or optimise_signals"}]}
    if name == "junction_los":
        return {"junctions": [{"junction": "2,2", "control": "signal", "delay_s": 48.2, "los": "D",
                               "worst_approach": "1,2-2,2 (71 s, E)"}]}
    if name in RUN_READS:
        return {"run": f"R{state['runs']}", "throughput_vph": 4200, "mean_speed_kmh": 31.5, "delay_veh_h": 60.2,
                "busiest_roads": ["2,2-3,2", "1,2-2,2"], "longest_queues": ["14 veh on 2,2-3,2"]}
    if name == "generate_demand":
        return {"total_made": 5200, "pairs": 182, "busiest": [{"junction": "2,2", "degree_of_saturation":
                                                                args.get("degree_of_saturation") or 0.65}]}
    if name == "read_od_matrix":
        return {"mode": "hourly", "origins": ["1W", "2W", "1N", "2N"], "destinations": ["1E", "2E", "1S", "2S"],
                "pairs_with_demand": 0, "total_vph": 0}
    if name == "run_experiment":
        return {"experiment": "E1", "seeds": [1, 2, 3], "comparison": [
            {"figure": "Delay (veh·h)", "scenario": "third lane", "mean": 52.1, "baseline": 60.2, "diff": -8.1,
             "ci95": [-11.0, -5.2], "clear": True, "better": True}], "file": "ACMS experiment E1.xlsx"}
    if name == "build_scenario":
        return {"report": "Built and tested (stand-in): demand 5200 veh/h, baseline run 4200 veh/h through, 31.5 km/h."}
    if name == "note_plan":
        return {"noted": True}
    if name == "get_selection":
        return {"ids": []}
    if name == "read_traffic":
        return {"error": "No run is going: the user presses Run to start one.", "error_type": "refused"}
    return {"ok": True, "done": f"{name} (stand-in)"}


def run_case(model: str, case: dict) -> dict:
    network = generate_network(4, 4)
    state = {"runs": 0}
    calls: list[dict] = []
    request = case["request"]
    offered = network_tools.tools_for(request) if hasattr(network_tools, "tools_for") else network_tools.tool_specs()

    def run_tool(call: dict) -> dict:
        name, args = call["name"], call.get("arguments") or {}
        calls.append({"name": name, "arguments": args})
        if hasattr(network_tools, "expand_tools") and name == network_tools.FIND_TOOLS:
            return network_tools.expand_tools(args, offered)
        if name not in network_tools.UI_TOOLS:
            try:
                return network_tools.run_tool(network, name, deepcopy(args))
            except (network_tools.ToolError, ValueError) as exc:
                return network_tools.explain_error(str(exc), network)
        return stand_in(name, args, state)

    messages = [{"role": "system", "content": assistant.system_prompt(tools=True)},
                {"role": "user", "content": EDITOR + request}]
    before, started, error = len(assistant.USAGE), time.monotonic(), ""
    try:
        answer = assistant.converse("local", model, messages, "", offered, None, run_tool)
        reply = next((m.get("content") for m in reversed(answer) if m.get("role") == "assistant"), "") or ""
    except assistant.AssistantError as exc:
        reply, error = "", str(exc)
    usage = assistant.USAGE[before:]
    names = {c["name"] for c in calls}
    expected = any(want <= names for want in case["expect"]) if case["expect"] != [set()] \
        else not (names & case.get("forbid", set()))
    forbidden = sorted(names & case.get("forbid", set()))
    checked = case["check"](calls) if "check" in case else True
    return {"model": model, "case": case["name"], "pass": bool(expected and not forbidden and checked and not error),
            "expected": expected, "forbidden": forbidden, "checked": checked, "error": error,
            "calls": [f"{c['name']}({json.dumps(c['arguments'])[:120]})" for c in calls],
            "requests": len(usage), "input_tokens": sum(u.get("input_tokens") or 0 for u in usage),
            "seconds": round(time.monotonic() - started, 1), "tools_offered": len(offered), "reply": reply[:300]}


def main(models: list[str]) -> None:
    rows = []
    for model in models:
        for case in CASES:
            row = run_case(model, case)
            rows.append(row)
            print(f"{model:14} {case['name']:34} {'PASS' if row['pass'] else 'fail'}  {row['seconds']:6.1f}s "
                  f"{row['input_tokens']:7} tok  {len(row['calls'])} calls: {', '.join(c.split('(')[0] for c in row['calls'])}"
                  + (f"  ERROR {row['error'][:80]}" if row["error"] else ""), flush=True)
        passed = [r for r in rows if r["model"] == model]
        print(f"{model}: {sum(r['pass'] for r in passed)}/{len(passed)} passed, "
              f"{sum(r['seconds'] for r in passed):.0f} s, {sum(r['input_tokens'] for r in passed):,} input tokens\n",
              flush=True)
    out = Path(__file__).with_name(os.environ.get("EVAL_OUT", "eval_commands.json"))
    out.write_text(json.dumps(rows, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main(sys.argv[1:] or MODELS)

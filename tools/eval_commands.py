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
    if args.get("width_m") or args.get("height_m"):  # a size given in metres: the grid made to it
        return float(args.get("width_m") or args["height_m"]), float(args.get("height_m") or args["width_m"])
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
    # Everyday tasks (2026-10-13): each names what must be right, not only which tool
    {"name": "widen one road",
     "request": "Make road 2,2-3,2 three lanes each way",
     "expect": [{"update_roads"}], "forbid": {"run_experiment", "build_scenario"},
     "check": lambda calls: _args_any(calls, "update_roads", lambda a: "2,2-3,2" in str(a.get("roads"))
                                      and (_num(a.get("lanes_per_direction")) == 3 or _num(a.get("add_lanes")) == 1)),
     "check_says": "2,2-3,2 at 3 lanes"},
    {"name": "speed limit everywhere",
     "request": "Set the speed limit of every road to 40 km/h",
     "expect": [{"update_roads"}],
     "check": lambda calls: _args_any(calls, "update_roads", lambda a: _num(a.get("speed_limit_kmh")) == 40
                                      and ("all" in str(a.get("roads")).lower() or len(a.get("roads") or []) >= 24)),
     "check_says": "every road at 40"},
    {"name": "roundabout at a junction",
     "request": "Turn junction 3,3 into a roundabout",
     "expect": [{"set_junction_control"}],
     "check": lambda calls: _args_any(calls, "set_junction_control", lambda a: "3,3" in str(a.get("junctions"))
                                      and str(a.get("control")).lower() == "roundabout"),
     "check_says": "3,3 roundabout"},
    {"name": "webster timing",
     "request": "Retime the signal at 2,2 with Webster's method",
     "expect": [{"set_signal_timing"}],
     "check": lambda calls: _args_any(calls, "set_signal_timing", lambda a: "2,2" in str(a.get("junctions"))
                                      and str(a.get("method") or "webster").lower() == "webster"),
     "check_says": "2,2 webster"},
    {"name": "one OD flow",
     "request": "Add 600 vehicles per hour from 1W to 1E",
     "expect": [{"set_od_demand"}],
     "check": lambda calls: _args_any(calls, "set_od_demand", lambda a: _num(a.get("value")) == 600
                                      and ("1W" in str(a.get("pairs")) or "1W" in str(a.get("origins")))
                                      and "1E" in str(a.get("pairs")) + str(a.get("destinations"))),
     "check_says": "1W->1E at 600"},
    {"name": "run in heavy rain",
     "request": "Run the simulation for 30 minutes in heavy rain",
     "expect": [{"run_simulation"}],
     "check": lambda calls: _args_any(calls, "run_simulation", lambda a: _num(a.get("duration_min")) == 30
                                      and str(a.get("weather")).lower() == "heavy rain"),
     "check_says": "30 min, heavy rain"},
    {"name": "lane closed in a run",
     "request": "Close one lane of 2,2-3,2 for 10 minutes during a 30 minute run and tell me the effect",
     "expect": [{"run_simulation"}, {"run_experiment"}, {"build_scenario"}], "forbid": {"update_roads", "delete_objects"},
     "check": lambda calls: any("2,2-3,2" in json.dumps(c["arguments"]) and "incident" in json.dumps(c["arguments"])
                                for c in calls), "check_says": "an incident on 2,2-3,2"},
    {"name": "bus stop",
     "request": "Add a bus stop on road 1,2-2,2",
     "expect": [{"add_bus_stop"}],
     "check": lambda calls: _args_any(calls, "add_bus_stop", lambda a: str(a.get("road")) in ("1,2-2,2", "2,2-1,2")),
     "check_says": "on 1,2-2,2"},
    {"name": "signal vs give-way",
     "request": "Would junction 3,2 work better as a give-way than with its signal? Compare them.",
     "expect": [{"run_experiment"}, {"build_scenario"}], "forbid": {"set_junction_control"}},
    {"name": "count junctions",
     "request": "How many junctions does the network have?",
     "expect": [{"network_summary"}, {"list_junctions"}], "forbid": network_tools.EDIT_TOOLS,
     "check": lambda calls: True, "reply_has": "16"},
    {"name": "delete a road",
     "request": "Delete road 4,4-4,3",
     "expect": [{"delete_objects"}],
     "check": lambda calls: _args_any(calls, "delete_objects", lambda a: "4,4-4,3" in str(a.get("ids"))),
     "check_says": "4,4-4,3"},
    {"name": "show on map",
     "request": "Show me junction 2,3 on the map",
     "expect": [{"show_on_map"}], "forbid": network_tools.EDIT_TOOLS,
     "check": lambda calls: _args_any(calls, "show_on_map", lambda a: str(a.get("id")) == "2,3"), "check_says": "2,3"},
    {"name": "safety after a run",
     "request": "Run 20 minutes and check how safe the network is",
     "expect": [{"run_simulation", "analyse_safety"}]},
    {"name": "calibrate to a count",
     "request": "Run 30 minutes and check the model against a count of 800 veh/h on road 1,2-2,2",
     "expect": [{"run_simulation", "check_calibration"}],
     "check": lambda calls: _args_any(calls, "check_calibration", lambda a: "800" in json.dumps(a.get("counts"))
                                      and "1,2-2,2" in json.dumps(a.get("counts"))),
     "check_says": "count 800 on 1,2-2,2"},
]


def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _args_any(calls, name, test) -> bool:
    """Whether a call of `name` has arguments passing `test`."""
    return any(test(c["arguments"]) for c in calls if c["name"] == name and isinstance(c["arguments"], dict))


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
        """As AI Mode's call_tool and _tool_requested do: what the user asked checked, other tools' arguments and
        invented ends dropped, the arguments read as the schema gives; recorded as the app would run them."""
        name, args = call["name"], call.get("arguments") or {}
        if name == network_tools.FIND_TOOLS:
            calls.append({"name": name, "arguments": args})
            return network_tools.expand_tools(args, offered)
        against = network_tools.contradicts(name, args, request)
        if against:
            calls.append({"name": name, "arguments": args, "refused": True})
            return {"error": against, "error_type": "not_what_was_asked"}
        if isinstance(args, dict):
            args = dict(args)
            notes = [network_tools.fold_incident(name, args), network_tools.drop_strays(name, args),
                     network_tools.repair_ends(network, name, args, request)]
        else:
            notes = []
        try:
            args = network_tools.normalise(name, args)
        except network_tools.ToolError as exc:
            calls.append({"name": name, "arguments": call.get("arguments") or {}, "refused": True})
            return {"error": f"{exc} Nothing was run."}
        calls.append({"name": name, "arguments": args})
        if name not in network_tools.UI_TOOLS:
            try:
                result = network_tools.run_tool(network, name, deepcopy(args))
            except (network_tools.ToolError, ValueError) as exc:
                if str(exc).startswith("Nothing would change"):  # as the app: already so, an answer
                    return network_tools.already_so(str(exc))
                calls[-1]["refused"] = True
                return network_tools.explain_error(str(exc), network)
        else:
            result = stand_in(name, args, state)
        if any(notes) and isinstance(result, dict):
            result["read_as"] = "; ".join(filter(None, notes))
        return result

    messages = [{"role": "system", "content": assistant.system_prompt(tools=True)},
                {"role": "user", "content": EDITOR + request + network_tools.skills_for(request)}]
    before, started, error = len(assistant.USAGE), time.monotonic(), ""
    try:
        answer = assistant.converse("local", model, messages, "", offered, None, run_tool,
                                    edits=network_tools.EDIT_TOOLS)
        reply = next((m.get("content") for m in reversed(answer) if m.get("role") == "assistant"), "") or ""
    except assistant.AssistantError as exc:
        reply, error = "", str(exc)
    usage = assistant.USAGE[before:]
    ran = [c for c in calls if not c.get("refused")]  # a refused call did nothing: neither credit nor harm
    names = {c["name"] for c in ran}
    expected = any(want <= names for want in case["expect"]) if case["expect"] != [set()] \
        else not (names & case.get("forbid", set()))
    forbidden = sorted(names & case.get("forbid", set()))
    checked = (case["check"](ran) if "check" in case else True) and case.get("reply_has", "") in reply
    return {"model": model, "case": case["name"], "pass": bool(expected and not forbidden and checked and not error),
            "expected": expected, "forbidden": forbidden, "checked": checked, "error": error,
            "calls": [f"{c['name']}{' REFUSED' if c.get('refused') else ''}({json.dumps(c['arguments'], default=str)[:120]})" for c in calls],
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
    out = Path(os.environ["EVAL_OUT"]) if os.environ.get("EVAL_OUT") else Path(__file__).with_name("eval_commands.json")
    out.write_text(json.dumps(rows, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main(sys.argv[1:] or MODELS)

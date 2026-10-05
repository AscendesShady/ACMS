"""The Scenario Builder (no Qt): what the user describes, as a small form (SCHEMA), turned into a plan of the
assistant's own tools, then a report of what the runs measured.

It is one of the assistant's tools (`build_scenario`): the model decides when a request is a whole study and
passes what the user said; code plans the steps (ChatSUMO's fixed scripts; AgentSUMO's parameter sufficiency).
- `SCHEMA` is flat, with choices wherever it can be, as the tool's parameters.
- `prepare(spec, state)` fills what was left out with stated defaults (`assumptions`) and says what can't
  be done (`problems`), before anything runs.
- `plan(spec)` lists the steps in the order a traffic study takes them: network, junction control, signal
  timing, buses, demand (sized to a degree of saturation and checked in a run), mode choice, route choice,
  a baseline run with its statistics, then the what-ifs as one experiment (paired over common seeds).
  Targets such as "the busiest road" are resolved when the step runs (main.MainWindow._build_scenario).
- `report(spec, done)` writes what was built and measured in plain words, from the tools' own numbers.
"""

from __future__ import annotations

import re

from .models import SIGNAL_METHODS

# Traffic as people say it, as the degree of saturation of the busiest junction (flow / capacity)
LEVELS = {"light": 0.4, "medium": 0.65, "heavy": 0.85, "near capacity": 0.95, "over capacity": 1.1}
CHANGES = ["add lane", "remove lane", "speed limit", "roundabout", "signals", "signal timing", "close road",
           "more traffic", "less traffic", "bus priority", "optimise signals"]
REPORTS = ["reliability", "safety", "detectors", "vulnerability", "forecast", "network"]
TSP = "Transit Signal Priority (TSP)"

SCHEMA = {"type": "object", "properties": {
    "intent": {"type": "string", "enum": ["build", "test", "question"],
               "description": "build: make or change a network and its traffic; test: try what-ifs on the "
                              "network as it is; question: just asking something."},
    "place": {"type": "string", "description": "A real place to build from the map, e.g. \"Dhanmondi, Dhaka\"."},
    "area_km": {"type": "number", "description": "Size of the square area round the place, km (default 1.2)."},
    "grid": {"type": "string", "description": "A made-up grid instead of a place, columns x rows, e.g. \"4x3\"."},
    "block_m": {"type": "number", "description": "Grid block length, m."},
    "lanes": {"type": "integer", "description": "Lanes each way on a new grid."},
    "speed_kmh": {"type": "number", "description": "Speed limit on a new grid, km/h."},
    "driving_side": {"type": "string", "enum": ["left", "right"]},
    "junctions": {"type": "string", "enum": ["signals", "roundabouts", "give way", "keep"],
                  "description": "How every junction is controlled."},
    "signal_timing": {"type": "string", "enum": list(SIGNAL_METHODS)},
    "traffic": {"type": "string", "enum": list(LEVELS)},
    "vehicles_per_hour": {"type": "number", "description": "Total traffic entering, veh/h, instead of a level."},
    "whole_day": {"type": "boolean", "description": "A day of traffic with morning and evening peaks."},
    "trip_km": {"type": "number", "description": "Average trip length, km, if known."},
    "bus_every_min": {"type": "number", "description": "Buses run every this many minutes."},
    "mode_choice": {"type": "boolean", "description": "Let people choose car, bus or bike by time and cost."},
    "car_cost_per_km": {"type": "number", "description": "Cents per km by car (for mode choice)."},
    "bus_fare": {"type": "number", "description": "Bus fare in cents (for mode choice)."},
    "route_choice": {"type": "boolean", "description": "Drivers spread over several routes by travel time."},
    "what_if": {"type": "array", "description": "Changes to test against the network as built.",
                "items": {"type": "object", "required": ["change"], "additionalProperties": False, "properties": {
                    "change": {"type": "string", "enum": CHANGES},
                    "where": {"type": "string", "description": "A road or junction id, \"busiest\" or \"all\"."},
                    "value": {"type": "number", "description": "km/h, percent of traffic, or lanes."},
                    "method": {"type": "string", "enum": list(SIGNAL_METHODS)}}}},
    "report": {"type": "array", "items": {"type": "string", "enum": REPORTS}},
    "minutes": {"type": "number", "description": "Simulated minutes a run (default 30)."},
    "seeds": {"type": "integer", "description": "Runs a design, each with its own random seed (default 3)."}},
    "required": [], "additionalProperties": False}

ROAD_CHANGES = {"add lane", "remove lane", "speed limit", "close road"}
JUNCTION_CHANGES = {"roundabout", "signals", "signal timing", "optimise signals"}


def grid_size(text) -> tuple[int, int] | None:
    """"4x3", "4 by 3", "4 × 3" → (4, 3) (columns, rows: vertical and horizontal corridors)."""
    match = re.fullmatch(r"\s*(\d+)\s*(?:x|×|by|\*)\s*(\d+)\s*", str(text or ""), re.IGNORECASE)
    return (int(match.group(1)), int(match.group(2))) if match else None


def prepare(spec: dict, state: dict) -> tuple[dict, list[str], list[str]]:
    """The form completed with stated defaults: (spec, assumptions, problems). `state`: has_network,
    has_demand, junctions (ids), signalised (ids), bus_type (name or None). Problems stop the build."""
    spec = {key: value for key, value in (spec or {}).items() if value not in (None, "", [])}
    assumptions, problems = [], []
    if spec.get("intent") == "question":
        return spec, assumptions, problems
    building = any(key in spec for key in ("place", "grid"))
    if spec.get("place") and spec.get("grid"):
        problems.append("Give a place or a grid, not both.")
    if spec.get("grid") and not grid_size(spec["grid"]):
        problems.append(f"Can't read the grid {spec['grid']!r}: write it like \"4x3\" (columns x rows).")
    elif spec.get("grid"):
        columns, rows = grid_size(spec["grid"])
        if not (1 <= columns <= 20 and 1 <= rows <= 20):
            problems.append("A grid is 1 to 20 corridors each way.")
    if not building and not state.get("has_network"):
        spec["grid"] = "3x3"
        building = True
        assumptions.append("No network yet and no place given: a 3 x 3 grid of signalised junctions.")
    if spec.get("place"):
        spec.setdefault("area_km", 1.2)
        if not 0.2 <= spec["area_km"] <= 4:
            problems.append("area_km must be 0.2 to 4 km.")
        assumptions.append(f"The roads of {spec['area_km']:g} km x {spec['area_km']:g} km round {spec['place']} "
                           "from OpenStreetMap, signals where OSM has them.")
    spec["intent"] = spec.get("intent") or ("build" if building else "test")
    new_demand = building or not state.get("has_demand") or any(k in spec for k in ("traffic", "vehicles_per_hour"))
    if new_demand and "traffic" not in spec and "vehicles_per_hour" not in spec:
        spec["traffic"] = "medium"
        assumptions.append("Medium traffic: the busiest junction at 65 % of its capacity.")
    if "traffic" in spec:
        assumptions.append(f"{spec['traffic'].capitalize()} traffic means the busiest junction at "
                           f"{LEVELS[spec['traffic']]:.0%} of capacity; the demand is checked in a run and "
                           "backed off if it jams.")
    if spec.get("mode_choice") and not state.get("bus_type") and not spec.get("bus_every_min"):
        assumptions.append("Mode choice between car and bike (no bus type is timetabled).")
    if spec.get("bus_every_min") and not state.get("bus_type"):
        problems.append("There's no bus type to put on a timetable (Mode Behavior).")
    spec.setdefault("minutes", 30)
    if not 5 <= spec["minutes"] <= 240:
        problems.append("minutes must be 5 to 240.")
    spec.setdefault("seeds", 3)
    if not 1 <= spec["seeds"] <= 10:
        problems.append("seeds must be 1 to 10.")
    if spec.get("what_if") and spec["seeds"] < 3:
        assumptions.append("Fewer than 3 seeds: differences can't be shown with confidence.")
    for item in spec.get("what_if", []):
        change = item.get("change")
        if change not in CHANGES:
            problems.append(f"Unknown change {change!r}.")
            continue
        if change in ROAD_CHANGES | JUNCTION_CHANGES and not item.get("where"):
            item["where"] = "busiest" if change in ROAD_CHANGES or change == "optimise signals" else "all"
        if change == "speed limit" and not item.get("value"):
            problems.append("A speed limit change needs its value in km/h.")
        if change == "signal timing" and not item.get("method"):
            problems.append(f"A signal timing change needs its method: {', '.join(SIGNAL_METHODS)}.")
        if change in ("more traffic", "less traffic"):
            item.setdefault("value", 20)
    if spec.get("what_if") and len(spec["what_if"]) > 5:
        problems.append("At most 5 what-ifs at once.")
    return spec, assumptions, problems


def plan(spec: dict) -> list[dict]:
    """The steps, in order: {"tool", "args", "why"}. Ids written "@…" are resolved when the step runs:
    @junctions (every junction), @signalised, @busiest_road, @busiest_junction, @busiest_signalised."""
    steps = []

    def step(tool, args, why):
        steps.append({"tool": tool, "args": args, "why": why})

    if spec.get("place"):
        step("place_map", {"place": spec["place"]}, f"Find {spec['place']} on the map")
        metres = spec["area_km"] * 1000
        step("network_from_background", {"width_m": metres, "height_m": metres,
                                          "road_classes": "main" if spec["area_km"] > 2 else "all"},
             "Build its roads from OpenStreetMap")
    elif spec.get("grid"):
        columns, rows = grid_size(spec["grid"])
        args = {"horizontal": rows, "vertical": columns}
        for key, name in (("block_m", "spacing_m"), ("lanes", "lanes_per_direction"), ("speed_kmh", "speed_limit_kmh"),
                          ("driving_side", "driving_side")):
            if key in spec:
                args[name] = spec[key]
        step("generate_grid", args, f"Build a {columns} x {rows} grid")
    control = {"signals": "signalised", "roundabouts": "roundabout", "give way": "uncontrolled"}.get(spec.get("junctions"))
    if control:
        step("set_junction_control", {"junctions": "@junctions", "control": control}, f"Make every junction {spec['junctions']}")
    if spec.get("signal_timing"):
        step("set_signal_timing", {"junctions": "@signalised", "method": spec["signal_timing"]},
             f"Time the signals by {spec['signal_timing']}")
    if spec.get("bus_every_min"):
        step("update_vehicle_type", {"name": "@bus_type", "headway_min": round(spec["bus_every_min"])},
             f"Run a bus every {round(spec['bus_every_min'])} min on each route")
    if "traffic" in spec or "vehicles_per_hour" in spec:
        args = {"demand": "daily" if spec.get("whole_day") else "hourly"}
        if "vehicles_per_hour" in spec:
            args["total"] = spec["vehicles_per_hour"] * (24 if spec.get("whole_day") else 1)
        else:
            args["degree_of_saturation"] = LEVELS[spec["traffic"]]
        if spec.get("trip_km"):
            args["mean_trip_km"] = spec["trip_km"]
        step("generate_demand", args, "Generate the trips between the boundary ends (gravity model)")
        if spec.get("whole_day"):
            step("shape_daily_curve", {"preset": "weekday"}, "Spread them over a weekday")
    if spec.get("mode_choice"):
        alternatives = [{"mode": "Car", "cost_per_km": spec.get("car_cost_per_km", 25)}, {"mode": "Bike", "constant": -1.5}]
        if spec.get("bus_every_min") or spec.get("bus_fare"):
            alternatives.append({"mode": "@bus_type", "cost": spec.get("bus_fare", 150), "out_of_vehicle_min": 4})
        step("choose_modes", {"alternatives": alternatives}, "Split trips between modes by time and cost (logit)")
    if spec.get("route_choice"):
        step("assign_routes", {"iterations": 3, "duration_min": min(spec["minutes"], 20)},
             "Spread drivers over routes by travel time (path-size logit, successive averages)")
    step("run_simulation", {"duration_min": spec["minutes"]}, f"Run {spec['minutes']:g} simulated minutes")
    step("analyse_run", {}, "Measure reliability, queues, arrivals and buses")
    for name in spec.get("report", []):
        tool = {"reliability": None, "safety": "analyse_safety", "detectors": "read_detectors",
                "vulnerability": "critical_links", "forecast": "forecast", "network": "network_indices"}[name]
        if tool:
            step(tool, {"what": "throughput", "horizon": 10} if tool == "forecast" else {}, f"Report {name}")
    variants = []
    for item in spec.get("what_if", []):
        if item["change"] == "optimise signals":
            step("optimise_signals", {"junctions": "@busiest_signalised", "duration_min": min(spec["minutes"], 15),
                                      "seeds": [101, 102], "population": 4, "generations": 2},
                 "Search fixed-time plans for the busiest signals (genetic algorithm)")
        variants.append(item)
    if variants:
        step("run_experiment", {"what_if": variants, "seeds": spec["seeds"], "duration_min": spec["minutes"]},
             f"Test {len(variants)} what-if{'s' if len(variants) > 1 else ''} against the network as built, "
             f"{spec['seeds']} seeds each, paired comparison")
    return steps


def variant(item: dict, resolve) -> dict:
    """One what-if as an experiment scenario; `resolve(token)` gives ids and current values."""
    change, where, value = item["change"], item.get("where") or "", item.get("value")
    roads = resolve("@busiest_road") if where in ("", "busiest") else [where]
    junctions = (resolve("@busiest_junction") if where == "busiest" else resolve("@junctions") if where == "all" else [where])
    target = roads if change in ROAD_CHANGES else junctions
    name = f"{change}" + (f" {value:g}" if isinstance(value, (int, float)) else "") + (
        f" at {', '.join(target)}" if where and where != "all" and change in ROAD_CHANGES | JUNCTION_CHANGES else "")
    if change in ("add lane", "remove lane"):
        step = 1 if change == "add lane" else -1
        return {"name": name, "changes": [{"tool": "update_roads", "args": {
            "roads": [road], "lanes_per_direction": max(1, resolve(f"@lanes:{road}") + step * int(value or 1))}} for road in roads]}
    if change == "speed limit":
        return {"name": name, "changes": [{"tool": "update_roads", "args": {"roads": roads, "speed_limit_kmh": value}}]}
    if change == "close road":
        return {"name": name, "changes": [{"tool": "delete_objects", "args": {"ids": roads}}]}
    if change in ("roundabout", "signals"):
        return {"name": name, "changes": [{"tool": "set_junction_control", "args": {
            "junctions": junctions, "control": "roundabout" if change == "roundabout" else "signalised"}}]}
    if change == "signal timing":
        signalised = resolve("@signalised") if where in ("all", "") else junctions
        return {"name": f"{item['method']} signals", "changes": [{"tool": "set_signal_timing", "args": {
            "junctions": signalised, "method": item["method"]}}]}
    if change in ("more traffic", "less traffic"):
        scale = 1 + (value / 100 if change == "more traffic" else -value / 100)
        return {"name": name, "demand_scale": round(scale, 3)}
    if change == "bus priority":
        return {"name": name, "extensions": [TSP]}
    if change == "optimise signals":
        return {"name": "optimised signals", "changes": resolve("@optimised")}
    raise ValueError(f"Unknown change {change!r}.")


def describe(spec: dict, steps: list[dict], assumptions: list[str]) -> str:
    """The plan as the approval card shows it."""
    lines = [f"{index}. {step['why']}" for index, step in enumerate(steps, 1)]
    text = "<br>".join(lines)
    if assumptions:
        text += "<br><i>Assumed: " + " ".join(assumptions) + "</i>"
    return text


def _percent(value) -> str:
    return "" if value is None else f" ({value:+.1f} %)"


KEY_FIGURES = ("Delay", "Mean trip time", "Throughput", "Travel time index", "Rear-end conflicts (TTC < 1.5 s)")


def report(spec: dict, done: list[dict]) -> str:
    """Plain words from the results: what was built, the demand, the baseline run's figures, and each
    what-if's clear differences (paired over seeds), with what couldn't be done."""
    lines = []
    results = {step["tool"]: step.get("result") or {} for step in done if step.get("ok")}
    failed = [step for step in done if not step.get("ok")]
    if "network_from_background" in results:
        r = results["network_from_background"]
        lines.append(f"**Built** {spec.get('place')}: {r.get('junctions', '?')} junctions, {r.get('roads', '?')} roads "
                     f"from OpenStreetMap.")
    elif "generate_grid" in results:
        lines.append(f"**The network is already** a {spec.get('grid')} grid: kept." if results["generate_grid"].get("unchanged")
                     else f"**Built** a {spec.get('grid')} grid.")
    demand = results.get("generate_demand")
    if demand:
        busiest = demand.get("degree_of_saturation") or {}
        top = next(iter(busiest.items()), None)
        lines.append(f"**Demand**: {demand.get('total_made', '?')} {demand.get('unit', 'veh/h')} over "
                     f"{demand.get('pairs_with_demand', '?')} OD pairs, mean trip {demand.get('mean_trip_km', '?')} km"
                     + (f"; busiest junction {top[0]} at {top[1]:.0%} of capacity" if top else "")
                     + (f"; {demand['checked_in_a_run']['note'].rstrip('.')}" if isinstance(demand.get("checked_in_a_run"), dict)
                        and demand["checked_in_a_run"].get("note") else "") + ".")
    modes = results.get("choose_modes")
    if modes and modes.get("pairs"):
        shares = modes["pairs"][0]["people_percent"]
        lines.append("**Modes** (logit, first pair): " + ", ".join(f"{k} {v:.0f} %" for k, v in shares.items()) + ".")
    routes = results.get("assign_routes")
    if routes and routes.get("iterations"):
        gap = routes["iterations"][-1].get("relative_gap")
        lines.append(f"**Routes**: path-size logit, {len(routes['iterations'])} averaging runs"
                     + (f", relative gap {gap:.1%}" if gap is not None else "") + ".")
    run = results.get("run_simulation")
    if run:
        lines.append(f"**Baseline run** ({run.get('simulated')}, seed {run.get('seed')}): {run.get('throughput_vph')} veh/h "
                     f"through, mean speed {run.get('mean_speed_kmh')} km/h, delay {run.get('delay_veh_h')} veh·h, "
                     f"mean trip {run.get('mean_trip_s')} s, travel time index {run.get('tti')}, planning time index "
                     f"{run.get('pti')}" + (f", {run['conflicts']} rear-end conflicts" if run.get("conflicts") is not None else "")
                     + (f"; longest queue {run['longest_queue']['queue_max']} veh on {run['longest_queue']['road']}"
                        if run.get("longest_queue") else "") + ".")
    analysed = results.get("analyse_run")
    if analysed:
        spill = [q["road"] for q in analysed.get("queues_p95", []) if q.get("spills_back")]
        if spill:
            lines.append(f"**Queues** reach the end of {', '.join(spill[:5])} (95th percentile): spillback.")
        buses = [b for b in analysed.get("bus_stops", []) if b.get("los")]
        if buses:
            lines.append("**Buses**: headway adherence " + ", ".join(f"{b['stop']} {b['los']}" for b in buses[:4]) + ".")
    safety = results.get("analyse_safety")
    if safety and safety.get("ranked"):
        lines.append(f"**Safety**: {safety['conflicts']} conflicts ({safety['severe']} severe); most above expected on "
                     + ", ".join(s["road"] for s in safety["ranked"][:3]) + ".")
    critical = results.get("critical_links")
    if critical and critical.get("critical"):
        lines.append("**Most critical roads**: " + ", ".join(
            f"{c['road']} ({c['cut_off_vph']} veh/h cut off, +{c['extra_veh_h_per_h']} veh·h/h)" for c in critical["critical"][:3]) + ".")
    indices = results.get("network_indices")
    if indices:
        lines.append(f"**Network**: {indices['vertices']} nodes, {indices['edges']} roads, gamma {indices['gamma']}, "
                     f"detour index {indices['detour_index']}.")
    experiment = results.get("run_experiment")
    if experiment and experiment.get("comparison"):
        lines.append(f"**What-ifs** ({experiment.get('runs')} runs, paired over the same seeds):")
        by_scenario: dict = {}
        for row in experiment["comparison"]:
            if "diff" in row and row["figure"] in KEY_FIGURES:
                by_scenario.setdefault(row["scenario"], []).append(row)
        for name, rows in by_scenario.items():
            clear = [r for r in rows if r.get("clear")]
            if not clear:
                lines.append(f"- {name}: no clear difference from the network as built.")
                continue
            parts = [f"{r['figure'].lower()} {'better' if r.get('better') else 'worse' if r.get('better') is False else 'changed'}"
                     f"{_percent(r.get('change_percent'))}" for r in clear]
            lines.append(f"- {name}: " + "; ".join(parts) + ".")
        unstable = sorted({row["scenario"] for row in experiment["comparison"]
                           if row["figure"] in KEY_FIGURES and (row.get("replications_needed") or 0) > 20})
        if unstable:
            lines.append(f"  Caution: the key figures of {', '.join(unstable)} vary so much between seeds that over 20 "
                         "runs would be needed for ±5 %: run more seeds before relying on small differences.")
        if experiment.get("caution"):
            lines.append(f"  Caution: {experiment['caution']}")
    for step in failed:
        lines.append(f"Couldn't: {step['why'][:1].lower() + step['why'][1:]}: {step.get('error')}")
    timing = next((r["agent_timing"] for r in (run, experiment) if r and r.get("agent_timing")), None)
    if timing:
        lines.append(f"Note: {timing}")
    files = [r.get("file") for r in (run, experiment) if r and r.get("file")]
    if files:
        lines.append("Workbooks: " + "; ".join(files))
    return "\n".join(lines) or "Nothing was built."

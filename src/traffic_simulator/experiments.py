"""Network testing (no Qt): simulations run headless, as fast as the CPU allows, and experiments that
compare network variants across seeds. The AI Mode tools run_simulation and run_experiment use it
(main.MainWindow._runs_command), and so can a person.

- `run_headless(network, demand, modes, seed, duration_s, ...)`: a Simulation of copies of the inputs,
  stepped without the wall clock, sampled by a results.RunRecorder every SAMPLE_S, so its KPIs are the
  same as a live run's with that seed; it saves its workbook like any run.
- A **scenario** is the network changed by edit tools (network_tools.run_tool on a copy: set_signal_timing,
  update_roads, add_road, set_junction_control…, each checked, a refused one refusing the scenario), the
  demand scaled (`demand_scale`: every pair's flow × it, to stress the network), and extensions ticked
  (their signal overrides). An **experiment** runs every scenario with every seed (the first scenario is
  the baseline) and reports, per KPI, the mean, standard deviation, 95 % confidence interval (Student's t)
  and the replications needed for a given precision (FHWA Toolbox III), and each scenario's difference
  from the baseline: a paired t interval over the seeds they share (common random numbers; Welch's when
  they share none), at 95 % widened by Bonferroni for several scenarios, its p-value, and whether the
  interval leaves out zero. One seed can't show a difference: its interval is left open.
- `analyse_run(observed)`: one run's statistics (travel-time reliability, arrivals against Poisson, 95th
  percentile queues against storage); `check_calibration(kpis, counts)`: its flows against counts.
"""

from __future__ import annotations

import re
import statistics
import time
from collections import Counter, defaultdict
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import datetime
from math import log, sqrt
from pathlib import Path
from statistics import stdev

from . import stats
from .results import MINUTE_COLUMNS, SAMPLE_S, RunRecorder, clock
from .simulation import DT, Simulation
from .xlsx import write_xlsx

MAX_SCENARIOS, MAX_SEEDS, MAX_RUNS, MAX_MINUTES = 6, 20, 60, 240
UNSTABLE_RUNS = 20  # replications needed beyond this: an unstable model or gridlock (FHWA Toolbox III, p93)
# The KPIs an experiment compares: (key in results.kpis, label, unit, higher is better?)
MEASURES = [("throughput_vph", "Throughput", "veh/h", True), ("people_per_h", "Passenger throughput", "people/h", True),
            ("mean_speed_kmh", "Mean speed", "km/h", True), ("delay_veh_h", "Delay", "veh·h", False),
            ("delay_s_per_km", "Delay per km", "s/veh·km", False), ("mean_trip_s", "Mean trip time", "s", False),
            ("mean_trip_delay_s", "Mean delay per trip", "s", False), ("queued_max", "Most queued at once", "veh", False),
            ("arrived", "Trips completed", "veh", True),
            ("tti", "Travel time index", "trip time ÷ free-flow time", False),
            ("pti", "Planning time index", "95th percentile of trip ÷ free-flow time", False),
            ("bi", "Buffer index (mean-based)", "(95th percentile − mean) ÷ mean of trip ÷ free-flow time", False),
            ("mean_system_trip_s", "Mean system travel time", "s", False),
            ("unfinished_share", "Unfinished trips", "share of spawned", False),
            ("conflicts", "Rear-end conflicts (TTC < 1.5 s)", "events", False),
            ("mean_wait_s", "Mean wait per trip", "s", False), ("queued_mean", "Mean queued at once", "veh", False),
            ("people_delay_h", "Passenger delay", "person·h", False),
            ("mean_person_delay_s", "Mean delay per passenger", "s", False),
            ("co2_kg", "CO2 of cars and vans", "kg", False),
            ("vehicle_km", "Distance driven", "veh·km", None),
            ("bus_headway_cv", "Bus headway variation", "SD ÷ mean headway", False)]
OPTIONAL_MEASURES = ("conflicts", "co2_kg", "bus_headway_cv")  # observed in headless runs only: left out when absent
# What a run's extensions report for comparing (TSP and its agent: TransitSignalPriority.report)
AGENT_MEASURES = [("tsp_requests", "Bus requests", "requests", None), ("tsp_true", "Priority given (TRUE)", "requests", None),
                  ("tsp_green_held_s", "Green held for buses", "s", None),
                  ("tsp_greens_cut", "Greens cut short for buses", "times", None),
                  ("agent_latency_s", "Agent latency, mean", "s", False), ("agent_errors", "Agent errors", "requests", False),
                  ("agent_fell_back", "Fell back at the deadline", "requests", False),
                  ("agent_late", "Answered too late", "requests", False),
                  ("pace_achieved", "Pace achieved", "x real time", None),  # a paced batch run's
                  ("tsp_served", "Requests served (priority acted)", "requests", None),
                  ("tsp_not_needed", "Requests TRUE but not needed", "requests", None),
                  ("tsp_denied", "Requests TRUE but blocked or not feasible", "requests", None),
                  ("agent_rejected", "Answers rejected by the guard", "requests", False),
                  ("tsp_off_plan_s", "Time off the plan", "s", None),
                  ("agent_agrees_estimate", "Decisions agreeing with the person-delay estimate", "%", None),
                  ("tsp_back_to_plan_s", "Back to the plan after the bus, mean", "s", False),
                  # Test DEVL: its emergency vehicles (main.emergency_report) and DEVL's own figures
                  ("emergency_trips", "Emergency vehicles dispatched", "vehicles", None),
                  ("emergency_arrived", "Emergency vehicles arrived", "vehicles", True),
                  ("emergency_trip_s", "Emergency travel time, mean (dispatch to arrival)", "s", False),
                  ("devl_requests", "DEVL requests", "requests", None),
                  ("devl_true", "DEVL lane cleared (TRUE)", "requests", None),
                  ("devl_fell_back", "DEVL fell back at the deadline", "requests", False),
                  ("devl_cleared", "Lanes cleared", "times", None),
                  ("devl_lane_green_s", "Lane green, in all", "s", None),
                  ("devl_to_green_s", "Request to lane green, mean", "s", False),
                  ("devl_latency_s", "DEVL agent latency, mean", "s", False)]


def summary(values: list[float]) -> dict:
    """Mean, standard deviation, n and the 95 % confidence interval's half-width (None for one value)."""
    values = [float(v) for v in values if v is not None]
    n = len(values)
    if not n:
        return {"n": 0, "mean": None, "sd": None, "ci95": None}
    mean = sum(values) / n
    sd = sqrt(sum((v - mean) ** 2 for v in values) / (n - 1)) if n > 1 else None
    return {"n": n, "mean": mean, "sd": sd, "ci95": stats.t_quantile(0.95, n - 1) * sd / sqrt(n) if n > 1 else None}


def difference(base: dict, other: dict, confidence: float = 0.95) -> dict:
    """other − base with Welch's interval at `confidence`; `clear` when it leaves out zero."""
    if base["mean"] is None or other["mean"] is None:
        return {"diff": None, "ci": None, "clear": None, "percent": None, "p": None}
    diff = other["mean"] - base["mean"]
    percent = diff / base["mean"] * 100 if base["mean"] else None
    if base["sd"] is None or other["sd"] is None:
        return {"diff": diff, "ci": None, "clear": None, "percent": percent, "p": None}
    a, b = base["sd"] ** 2 / base["n"], other["sd"] ** 2 / other["n"]
    if a + b == 0:
        return {"diff": diff, "ci": 0.0, "clear": diff != 0, "percent": percent, "p": 0.0 if diff else 1.0}
    df = (a + b) ** 2 / (a ** 2 / (base["n"] - 1) + b ** 2 / (other["n"] - 1))
    half = stats.t_quantile(confidence, df) * sqrt(a + b)
    return {"diff": diff, "ci": half, "clear": abs(diff) > half, "percent": percent,
            "p": stats.t_p(diff / sqrt(a + b), df)}


def seeds_from(value) -> list[int]:
    """Seeds as given: [1, 2, 3], "1-5", "4, 7, 10-12" or one number."""
    if isinstance(value, int):
        return [value]
    if isinstance(value, list):
        return [int(v) for v in value]
    seeds: list[int] = []
    for part in str(value or "").split(","):
        part = part.strip()
        if not part:
            continue
        match = re.fullmatch(r"(\d+)\s*[-–]\s*(\d+)", part)
        if match:
            first, last = int(match.group(1)), int(match.group(2))
            if last < first or last - first >= MAX_SEEDS:
                raise ValueError(f"The seed range {part} runs backwards or is too long (at most {MAX_SEEDS}).")
            seeds += range(first, last + 1)
        elif part.isdigit():
            seeds.append(int(part))
        else:
            raise ValueError(f"Can't read the seed {part!r}; write seeds like 1-5 or 3, 8, 13.")
    return list(dict.fromkeys(seeds))


@dataclass
class Scenario:
    name: str
    changes: list = field(default_factory=list)  # [{"tool": name, "args": {...}}], network edit tools
    demand_scale: float = 1.0
    extensions: list = field(default_factory=list)  # names of extensions ticked in it
    incidents: list = field(default_factory=list)  # lanes closed or work zones in the run (incidents_from)
    weather: str = "clear"  # simulation.WEATHER
    random_incidents: dict | None = None  # {"per_hour", "for_min", "lanes"}: drawn for each seed (draw_incidents)
    meters: list = field(default_factory=list)  # ramp meters: [{"road", "target_occupancy_percent" | "rate_vph"}]
    assign_routes: dict | None = None  # route choice found for it before its runs: {"iterations", "until_gap"}
    lateral: bool = False  # lateral position and filtering in its runs (Simulation lateral)


INCIDENT_KEYS = ("road", "lanes", "at_min", "for_min", "from_m", "length_m", "direction", "speed_kmh")


def incidents_from(value, where: str = "") -> list[dict]:
    """Incidents and work zones as given: [{"road", "lanes" (closed, kerbside first; 0 with speed_kmh: a limit
    only), "at_min" (when it starts, default 0), "for_min" (how long; none: to the end), "from_m", "length_m"
    (default 50), "direction" (both, forward, back), "speed_kmh" (a work zone's limit)}]. Shapes checked here;
    the roads when the run is built (Simulation.close)."""
    if value in (None, ""):
        return []
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError(f"{where}incidents are a list like [{{\"road\": \"1,1-2,1\", \"lanes\": 1, \"at_min\": 5, "
                         "\"for_min\": 15}].")
    found = []
    for item in value:
        unknown = set(item) - set(INCIDENT_KEYS)
        if unknown or not item.get("road"):
            raise ValueError(f"{where}an incident takes {', '.join(INCIDENT_KEYS)} (road needed); "
                             f"unknown: {', '.join(sorted(unknown)) or 'none'}.")
        try:
            at = float(item.get("at_min") or 0)
            lasting = None if item.get("for_min") in (None, "") else float(item["for_min"])
            spec = {"road": str(item["road"]), "lanes": int(item.get("lanes", 1)), "start_s": at * 60,
                    "end_s": None if lasting is None else (at + lasting) * 60,
                    "from_m": None if item.get("from_m") in (None, "") else float(item["from_m"]),
                    "length_m": float(item.get("length_m") or 50), "direction": str(item.get("direction") or "both"),
                    "speed_kmh": None if item.get("speed_kmh") in (None, "") else float(item["speed_kmh"])}
        except (TypeError, ValueError):
            raise ValueError(f"{where}an incident's numbers (lanes, at_min, for_min, from_m, length_m, speed_kmh) "
                             "must be numbers.") from None
        if at < 0 or (lasting is not None and lasting <= 0):
            raise ValueError(f"{where}at_min can't be below 0 and for_min must be above 0.")
        found.append(spec)
    return found


def draw_incidents(network, seed: int, duration_s: float, spec: dict) -> list[dict]:
    """Random incidents for one seed (FHWA Toolbox III: incidents as a source of non-recurrent congestion): about
    `per_hour` an hour (Poisson), each on a road drawn by its length (boundary roads left out), at a uniform time
    in the run, closing `lanes` (default 1) for `for_min` (default 15) over 50 m in one direction. Drawn from their
    own stream (the seed's), so every scenario on a seed meets the same incidents and the traffic's random numbers
    are untouched (common random numbers)."""
    import random  # noqa: PLC0415

    rate, lasting, lanes = float(spec.get("per_hour") or 0), float(spec.get("for_min") or 15), int(spec.get("lanes") or 1)
    if not 0 < rate <= 20 or lasting <= 0:
        raise ValueError("random_incidents: per_hour 0-20 (above 0) and for_min above 0.")
    rng = random.Random(f"incidents {seed}")
    roads = [road for road in network.roads.values() if road.length_m > 120]
    if not roads:
        raise ValueError("random_incidents: no road between junctions is long enough (over 120 m).")
    found, t = [], 0.0
    while True:
        t += rng.expovariate(rate / 3600)
        if t >= duration_s:
            return found
        road = rng.choices(roads, weights=[r.length_m for r in roads])[0]
        direction = "forward" if road.direction != "two-way" or rng.random() < 0.5 else "back"
        found.append({"road": road.id, "lanes": min(lanes, road.lanes_per_direction), "start_s": round(t),
                      "end_s": round(t + lasting * 60), "from_m": None, "length_m": 50.0, "direction": direction,
                      "speed_kmh": None})


CONDITION_KEYS = ("name", "demand_scale", "weather", "incidents", "random_incidents", "weight")
MAX_CONDITIONS = 6


def conditions_from(value) -> list[dict]:
    """Conditions every scenario is run under (FHWA Toolbox III 2019, p87-91: alternatives tested across the
    conditions that matter, each weighted by how often it occurs): [{"name", "demand_scale" (× the scenario's),
    "weather", "incidents", "random_incidents", "weight" (default equal)}]."""
    from .simulation import WEATHER  # noqa: PLC0415

    if value in (None, ""):
        return []
    if not isinstance(value, list) or not 2 <= len(value) <= MAX_CONDITIONS or \
            not all(isinstance(item, dict) and item.get("name") for item in value):
        raise ValueError(f"conditions are a list of 2 to {MAX_CONDITIONS} like [{{\"name\": \"peak\", \"demand_scale\": "
                         "1.2}, {\"name\": \"rain\", \"weather\": \"heavy rain\"}].")
    found, names = [], set()
    for item in value:
        unknown = set(item) - set(CONDITION_KEYS)
        name = str(item["name"]).strip()[:30]
        if unknown or name.lower() in names:
            raise ValueError(f"Each condition needs its own name and takes {', '.join(CONDITION_KEYS)}; "
                             f"unknown: {', '.join(sorted(unknown)) or 'none'}.")
        names.add(name.lower())
        scale, weight = float(item.get("demand_scale") or 1.0), float(item.get("weight") or 1.0)
        weather = str(item["weather"]).lower() if item.get("weather") else None
        if not 0.05 <= scale <= 10 or weight <= 0 or (weather is not None and weather not in WEATHER):
            raise ValueError(f"Condition {name}: demand_scale 0.05-10, weight above 0, weather one of {', '.join(WEATHER)}.")
        found.append({"name": name, "demand_scale": scale, "weather": weather, "weight": weight,
                      "incidents": incidents_from(item.get("incidents"), f"Condition {name}: "),
                      "random_incidents": item.get("random_incidents") or None})
    return found


def under(scenario: Scenario, condition: dict) -> Scenario:
    """A scenario run under a condition: its demand scaled again, the condition's weather, incidents added."""
    return Scenario(f"{scenario.name} | {condition['name']}", scenario.changes,
                    scenario.demand_scale * condition["demand_scale"], scenario.extensions,
                    scenario.incidents + condition["incidents"], condition["weather"] or scenario.weather,
                    condition["random_incidents"] or scenario.random_incidents, scenario.meters, scenario.assign_routes,
                    scenario.lateral)


def factorial(factors) -> tuple[list[dict], list[str]]:
    """Scenarios for every combination of 1 to 3 factors' levels (a full factorial design; Snell, Applied Statistics,
    Examples J-N): `factors` [{"name": "signals", "levels": [{"label": "webster", "changes": [...]}, {"label":
    "transyt", "changes": [...]}]}, ...]; a level may also give demand_scale, extensions, weather, incidents, meters,
    lateral.
    Returns (the scenario specs, named "signals=webster, tsp=on"; the factors' names)."""
    if not isinstance(factors, list) or not 1 <= len(factors) <= 3 or not all(
            isinstance(f, dict) and f.get("name") and isinstance(f.get("levels"), list) and len(f["levels"]) >= 2
            and all(isinstance(level, dict) and level.get("label") for level in f["levels"]) for f in factors):
        raise ValueError("factors are 1 to 3 like [{\"name\": \"signals\", \"levels\": [{\"label\": \"webster\"}, "
                         "{\"label\": \"transyt\", \"changes\": [...]}]}]: each with 2 or more labelled levels.")
    specs = [{"name": "", "changes": [], "demand_scale": 1.0, "extensions": [], "incidents": [], "meters": []}]
    for factor in factors:
        grown = []
        for spec in specs:
            for level in factor["levels"]:
                grown.append({"name": (spec["name"] + ", " if spec["name"] else "") + f"{factor['name']}={level['label']}",
                              "changes": spec["changes"] + list(level.get("changes") or []),
                              "demand_scale": spec["demand_scale"] * float(level.get("demand_scale") or 1.0),
                              "extensions": spec["extensions"] + list(level.get("extensions") or []),
                              "incidents": spec["incidents"] + list(level.get("incidents") or []),
                              "meters": spec["meters"] + list(level.get("meters") or []),
                              **({"weather": level["weather"]} if level.get("weather") else
                                 {"weather": spec["weather"]} if spec.get("weather") else {}),
                              **({"lateral": level["lateral"]} if "lateral" in level else
                                 {"lateral": spec["lateral"]} if "lateral" in spec else {})})
        specs = grown
    return specs, [str(factor["name"]) for factor in factors]


def condition_summary(runs: list[dict], scenarios: list[str], conditions: list[dict], key: str,
                      higher: bool | None) -> dict:
    """Each design under each condition on one figure (`key`): its mean over the seeds, the winner, every design's
    regret (how much worse than that condition's winner), the weighted mean over the conditions (by their weights),
    the worst regret, and the robust choice (the least worst regret; Handbook of Transportation Science, robust
    design). `scenarios`: the designs' own names (before " | condition")."""
    better = (lambda a, b: a > b) if higher else (lambda a, b: a < b)
    table: dict = {}
    for condition in conditions:
        means = {}
        for name in scenarios:
            values = [run["measures"].get(key) for run in runs if run["scenario"] == f"{name} | {condition['name']}"]
            values = [v for v in values if v is not None]
            means[name] = statistics.fmean(values) if values else None
        known = {name: value for name, value in means.items() if value is not None}
        if not known:
            continue
        best = next(iter(known))
        for name, value in known.items():
            if better(value, known[best]):
                best = name
        table[condition["name"]] = {"means": means, "winner": best,
                                    "regret": {name: abs(value - known[best]) for name, value in known.items()}}
    total = sum(c["weight"] for c in conditions if c["name"] in table)
    designs = []
    for name in scenarios:
        regrets = [row["regret"][name] for row in table.values() if name in row["regret"]]
        weighted = [(c["weight"], table[c["name"]]["means"][name]) for c in conditions
                    if c["name"] in table and table[c["name"]]["means"].get(name) is not None]
        designs.append({"design": name, "weighted_mean": sum(w * v for w, v in weighted) / total if weighted else None,
                        "worst_regret": max(regrets) if regrets else None,
                        "wins": sum(1 for row in table.values() if row["winner"] == name)})
    robust = min((d for d in designs if d["worst_regret"] is not None), key=lambda d: d["worst_regret"], default=None)
    winners = {row["winner"] for row in table.values()}
    return {"by_condition": table, "designs": designs, "robust_choice": robust["design"] if robust else None,
            "same_winner_everywhere": len(winners) == 1,
            "note": "Regret: how much worse a design is than the best under that condition. The robust choice has the "
                    "least worst regret; when the winner changes with the condition, say under which it wins."}


def meters_from(value, where: str = "") -> list[dict]:
    """Ramp meters as given: [{"road": the on-ramp, "target_occupancy_percent": 20 (ALINEA) or "rate_vph": 600}]."""
    if value in (None, ""):
        return []
    if not isinstance(value, list) or not all(isinstance(item, dict) and item.get("road") for item in value):
        raise ValueError(f"{where}meters are a list like [{{\"road\": \"DI1.2\", \"target_occupancy_percent\": 20}}].")
    unknown = {key for item in value for key in item} - {"road", "target_occupancy_percent", "rate_vph", "detector_road"}
    if unknown:
        raise ValueError(f"{where}a meter takes road and target_occupancy_percent or rate_vph; unknown: "
                         f"{', '.join(sorted(unknown))}.")
    return [dict(item) for item in value]


def apply_conditions(simulation, incidents: list[dict], weather: str = "clear", meters=()) -> None:
    """Incidents (incidents_from, draw_incidents), weather and ramp meters onto a Simulation: ValueError names what
    can't be."""
    from math import inf  # noqa: PLC0415

    simulation.set_weather(weather)
    for spec in meters:
        try:
            simulation.meter(str(spec["road"]), spec.get("target_occupancy_percent"), spec.get("rate_vph"),
                             spec.get("detector_road"))
        except ValueError as exc:
            raise ValueError(f"Meter on {spec['road']}: {exc}") from None
    for spec in incidents:
        try:
            simulation.close(spec["road"], spec["lanes"], spec["from_m"], spec["length_m"], spec["start_s"],
                             inf if spec["end_s"] is None else spec["end_s"], spec["speed_kmh"], spec["direction"])
        except ValueError as exc:
            raise ValueError(f"Incident on {spec['road']}: {exc}") from None


def scenario_from(spec: dict, index: int) -> Scenario:
    if not isinstance(spec, dict):
        raise ValueError("Each scenario is an object: {\"name\": ..., \"changes\": [...], \"demand_scale\": ..., \"extensions\": [...]}.")
    changes = spec.get("changes") or []
    if not isinstance(changes, list) or not all(isinstance(c, dict) and isinstance(c.get("tool"), str) for c in changes):
        raise ValueError(f"Scenario {index + 1}: changes are [{{\"tool\": \"update_roads\", \"args\": {{...}}}}, ...].")
    scale = float(spec.get("demand_scale", 1.0) or 1.0)
    if not 0.05 <= scale <= 10:
        raise ValueError(f"Scenario {index + 1}: demand_scale must be 0.05 to 10.")
    extensions = spec.get("extensions") or []
    if not isinstance(extensions, list):
        raise ValueError(f"Scenario {index + 1}: extensions are a list of their names.")
    from .simulation import WEATHER  # noqa: PLC0415

    weather = str(spec.get("weather") or "clear").lower()
    if weather not in WEATHER:
        raise ValueError(f"Scenario {index + 1}: weather is one of {', '.join(WEATHER)}.")
    if not isinstance(spec.get("lateral", False), bool):
        raise ValueError(f"Scenario {index + 1}: lateral is true or false.")
    random_incidents = spec.get("random_incidents") or None
    if random_incidents is not None and not isinstance(random_incidents, dict):
        raise ValueError(f"Scenario {index + 1}: random_incidents is {{\"per_hour\": 2, \"for_min\": 15, \"lanes\": 1}}.")
    assign = spec.get("assign_routes") or None
    if assign is not None:
        if assign is True:
            assign = {}
        if not isinstance(assign, dict) or set(assign) - {"iterations", "until_gap"} \
                or not 1 <= int(assign.get("iterations", 4)) <= 8 or not 0 < float(assign.get("until_gap", 0.02)) <= 0.5:
            raise ValueError(f"Scenario {index + 1}: assign_routes is {{\"iterations\": 1-8, \"until_gap\": 0-0.5}}.")
        assign = {"iterations": int(assign.get("iterations", 4)), "until_gap": float(assign.get("until_gap", 0.02))}
    return Scenario(str(spec.get("name") or f"Scenario {index + 1}").strip()[:60], changes, scale, [str(e) for e in extensions],
                    incidents_from(spec.get("incidents"), f"Scenario {index + 1}: "), weather, random_incidents,
                    meters_from(spec.get("meters"), f"Scenario {index + 1}: "), assign, spec.get("lateral", False))


def scenario_network(network, scenario: Scenario, allowed: set[str]):
    """A copy of the network with the scenario's changes, each through run_tool (checked, all or none)."""
    from .network_tools import ToolError, run_tool  # noqa: PLC0415 - network_tools imports nothing from here

    copy = deepcopy(network)
    for step, change in enumerate(scenario.changes, 1):
        if change["tool"] not in allowed:
            raise ValueError(f"{scenario.name}, change {step}: {change['tool']} can't be part of a scenario; "
                             f"these can: {', '.join(sorted(allowed))}.")
        try:
            run_tool(copy, change["tool"], change.get("args") or {})
        except ToolError as exc:
            raise ValueError(f"{scenario.name}, change {step} ({change['tool']}): {exc}") from None
    return copy


def scaled(demand: list, scale: float) -> list:
    if scale == 1.0:
        return list(demand)
    return [replace(pair, hourly=None if pair.hourly is None else pair.hourly * scale,
                    daily=None if pair.daily is None else pair.daily * scale) for pair in demand]


def run_headless(network, demand, modes, seed: int, duration_s: float, override=None, recorder=None,
                 progress=None, cancelled=lambda: False, observe: bool = False, speed: float | None = None,
                 dispatches=(), incidents=(), weather: str = "clear", meters=(), on_frame=None,
                 step_s: float = DT, step_method: str = "euler", lateral: bool = False):
    """A run without drawing: step until `duration_s`, sampling a RunRecorder every SAMPLE_S, as fast as the
    CPU allows, or with `speed` in step with the wall clock (simulated seconds a real second, as a live run:
    an agent answering in the background then costs its real latency, and the run never pauses for it).
    `recorder` is one, or a function making it from the Simulation (so it can see the signal plans).
    `dispatches`: emergency vehicles sent at the start, (from, to, every seconds; 0: once[, seconds later])
    (Simulation.dispatch). `incidents`, `weather`: apply_conditions. `on_frame(simulation)`: after every step, on
    this (the worker's) thread, so it may take a frame safely (the window shows a batch's run on the map, taking one
    at most 10 times a real second). `step_s`, `step_method`: the step's length (DT, or 0.25 or 0.1 s to check
    the results don't depend on it) and Euler or ballistic update (Simulation). `lateral`: lateral position and
    filtering (Simulation lateral).
    Returns (the Simulation, the recorder); the Simulation's time says how far it got if cancelled."""
    simulation = Simulation(deepcopy(network), demand, modes, seed=seed, step_s=step_s, step_method=step_method,
                            lateral=lateral)
    apply_conditions(simulation, list(incidents), weather, list(meters))
    simulation.signal_override = override
    for origin, destination, every_s, *after_s in dispatches:
        simulation.dispatch(origin, destination, every_s, *after_s)
    if observe:
        simulation.observe()
    if callable(recorder) and not isinstance(recorder, RunRecorder):
        recorder = recorder(simulation)
    next_sample, started = SAMPLE_S, time.monotonic()
    while simulation.time < duration_s - 1e-9 and not cancelled():
        if speed:
            ahead = simulation.time / speed - (time.monotonic() - started)
            if ahead > 0:  # ahead of the clock: wait (a little at a time, so Stop is heard)
                time.sleep(min(ahead, 0.1))
                continue
        simulation.step()
        if recorder is not None and simulation.time + 1e-9 >= next_sample:
            recorder.sample(simulation.frame())
            next_sample += SAMPLE_S
        if progress is not None and abs(simulation.time - round(simulation.time)) < 1e-6:  # each simulated second: the
            progress(simulation.time)  # menu bar's clock ticks with it in a paced batch
        if on_frame is not None:
            on_frame(simulation)
    return simulation, recorder


# Does the network hold a demand? One headless run this long; it holds unless, over the last part, the
# vehicles in it (with those waiting to enter) grow by more than this share of the vehicles setting off,
# and by at least HOLD_MIN_PILE (a light demand's few vehicles swing by more than 5 % by chance).
# ponytail: one seed per check; a borderline demand can hold on one seed and jam on another
HOLD_MINUTES, HOLD_WINDOW_MINUTES, HOLD_BUILD_UP, HOLD_MIN_PILE, HOLD_STEPS = 45, 15, 0.05, 25, 4


def at_busiest_hour(demand: list) -> list:
    """Daily pairs as their veh/h in the network's busiest hour by their curves (hourly pairs as they are),
    so a short run tests the peak."""
    if all(pair.hourly is not None for pair in demand):
        return list(demand)
    hour = max(range(24), key=lambda h: sum(Simulation.rate(pair, h * 3600) for pair in demand))
    return [replace(pair, hourly=Simulation.rate(pair, hour * 3600), daily=None, shares=[]) for pair in demand]


def holds(network, demand, modes, seed: int, override=None, progress=None, cancelled=lambda: False) -> dict:
    """One HOLD_MINUTES headless run: whether vehicles pile up (see HOLD_BUILD_UP) and by how much."""
    simulation = Simulation(deepcopy(network), demand, modes, seed=seed)
    simulation.signal_override = override
    mark, end = None, HOLD_MINUTES * 60

    def in_network() -> int:
        return len(simulation.vehicles) + sum(len(queue) for queue in simulation.pending.values())

    while simulation.time < end - 1e-9 and not cancelled():
        simulation.step()
        if mark is None and simulation.time >= end - HOLD_WINDOW_MINUTES * 60:
            mark = (in_network(), simulation.spawned)
        if progress is not None and int(simulation.time / simulation.dt) % 120 == 0:
            progress(simulation.time)
    if mark is None:  # stopped early
        return {"holds": None}
    piled = in_network() - mark[0]
    build_up = piled / max(simulation.spawned - mark[1], 1)
    return {"holds": build_up <= HOLD_BUILD_UP or piled < HOLD_MIN_PILE, "build_up": round(build_up, 3),
            "in_network": in_network()}


def largest_holding(check, steps: int = HOLD_STEPS) -> tuple[float, list]:
    """The largest share (of 1) of a demand that holds, `check(share)` -> holds()'s answer: the whole of it
    if it holds, else by bisection (`steps` more runs). 0 if none tried held; None if stopped."""
    tried = [(1.0, check(1.0))]
    if tried[0][1]["holds"] is not False:
        return (1.0 if tried[0][1]["holds"] else None), tried
    low, high = 0.0, 1.0
    for _ in range(steps):
        share = (low + high) / 2
        result = check(share)
        tried.append((share, result))
        if result["holds"] is None:
            return None, tried
        low, high = (share, high) if result["holds"] else (low, share)
    return low, tried


def headline(kpis: dict) -> dict:
    """The KPIs a tool hands back: the network's figures and per type, without the per-road table."""
    keep = {key: kpis.get(key) for key, *_ in MEASURES}
    keep.update(time=clock(kpis["time_s"]), spawned=kpis["spawned"], on_road=kpis["on_road"],
                vehicle_km=kpis["vehicle_km"], longest_queue=kpis["longest_queue"],
                by_type=kpis["by_type"])
    return keep


def measures_of(kpis: dict) -> dict:
    """The compared figures of one run, per-type delays included (e.g. "Bus mean delay")."""
    values = {key: kpis.get(key) for key, *_ in MEASURES}
    for key in OPTIONAL_MEASURES:
        if values.get(key) is None:
            values.pop(key)  # not observed
    for kind, by in kpis.get("by_type", {}).items():
        values[f"{kind} mean delay (s)"] = by.get("mean_delay_s")
        values[f"{kind} mean wait (s)"] = by.get("mean_wait_s")
        values[f"{kind} people per hour"] = by.get("people_per_h")
    values.update(kpis.get("reported") or {})  # the run's extensions' figures (TSP, its agent)
    return values


def compare(runs: list[dict], scenarios: list[str], precision: float = 0.05) -> list[dict]:
    """Per figure and scenario: summary over its seeds, the runs needed for its mean within ±precision, and
    the difference from the first scenario: paired over the seeds both ran (common random numbers), else
    Welch's; at 95 %, Bonferroni-widened (1 − 0.05/k) for k scenarios compared with the baseline."""
    labels = {key: (label, unit, up) for key, label, unit, up in MEASURES + AGENT_MEASURES}
    keys = list(dict.fromkeys(key for run in runs for key in run["measures"]))
    confidence = 1 - 0.05 / max(len(scenarios) - 1, 1)
    rows = []
    for key in keys:
        label, unit, up = labels.get(key, (key, "", None))
        values = {name: [run["measures"].get(key) for run in runs if run["scenario"] == name] for name in scenarios}
        by_seed = {name: {run["seed"]: run["measures"].get(key) for run in runs if run["scenario"] == name and "seed" in run}
                   for name in scenarios}
        summaries = {name: summary(values[name]) for name in scenarios}
        base = scenarios[0]
        overall = stats.anova(by_seed) if len(scenarios) >= 3 else None
        for name in scenarios:
            row = {"figure": label, "unit": unit, "scenario": name, **summaries[name],
                   "replications_needed": stats.replications(values[name], precision)}
            if name != base:
                found = stats.paired(by_seed[base], by_seed[name], confidence)
                if found["n"] >= 2:
                    mean = summaries[base]["mean"]
                    found.update(percent=found["diff"] / mean * 100 if mean else None, test="paired t (shared seeds)")
                else:
                    found = {**difference(summaries[base], summaries[name], confidence), "test": "Welch"}
                found.pop("n", None)
                if found["test"].startswith("paired"):
                    shared = [seed for seed in by_seed[base] if seed in by_seed[name]
                              and None not in (by_seed[base][seed], by_seed[name][seed])]
                    found["wilcoxon_p"] = stats.signed_rank([by_seed[name][seed] - by_seed[base][seed] for seed in shared])
                row.update({f"vs_baseline_{k}": v for k, v in found.items()}, confidence=confidence)
                if key == "mean_speed_kmh" and summaries[base]["mean"] and summaries[name]["mean"]:
                    row["speed_risk"] = speed_risk(summaries[base]["mean"], summaries[name]["mean"])
                if row.get("vs_baseline_clear") and row["vs_baseline_diff"] is not None and up is not None:
                    row["better"] = (row["vs_baseline_diff"] > 0) == up
            if overall is not None:
                row["anova"] = {"design": overall["design"], "f": round(overall["f"], 2), "p": round(overall["p"], 4)}
            rows.append(row)
    return rows


def analyse_run(kpis: dict, every: bool = False) -> dict:
    """One run's statistics from its KPIs and observations (RunRecorder.observed(), Simulation.observations()):
    travel-time reliability (network and the least reliable OD pairs), the warm-up (MSER-5) and steady-state
    means by batch means, how concentrated flow is on few roads (Gini), each origin's arrivals a minute against
    Poisson, 95th-percentile queues against each road's length, and bus headway adherence at each stop.
    `every`: all pairs and roads (for the workbook), not the few worst."""
    observed = kpis["observed"]
    trips = observed["trips"]
    times = [trip[3] for trip in trips if trip[3] > 0]
    reliability = {"trips": len(times)}
    if len(times) >= 2:
        mean, sd = sum(times) / len(times), stdev(times)
        reliability.update(mean_s=round(mean, 1), sd_s=round(sd, 1), cv=round(sd / mean, 3),
                           median_s=round(stats.percentile(times, 50), 1), p85_s=round(stats.percentile(times, 85), 1),
                           p95_s=round(stats.percentile(times, 95), 1),
                           **stats.reliability([(trip[3], trip[4]) for trip in trips]))
        by_pair: dict = {}
        for origin, destination, _, trip, lost, _ in trips:
            by_pair.setdefault(f"{origin}->{destination}", []).append((trip, lost))
        pairs = [{"pair": pair, "trips": len(found), **stats.reliability(found)} for pair, found in by_pair.items()
                 if len(found) >= 5]
        reliability["least_reliable_pairs"] = sorted((row for row in pairs if row["pti"] is not None),
                                                     key=lambda row: -row["pti"])[:None if every else 8]
    rows = observed.get("minute_rows", [])
    steady = {}
    if len(rows) >= 10:
        column = {name: MINUTE_COLUMNS.index(name) for name in ("Vehicles on the road", "Throughput (veh/h)",
                                                               "Mean speed (km/h)")}
        warm = stats.mser([row[column["Vehicles on the road"]] for row in rows])
        after = rows[warm:]
        batches = 10 if len(after) >= 30 else max(3, len(after) // 3)
        steady = {"warm_up_min": warm, "minutes_after": len(after),
                  "throughput_vph": stats.batch_means([row[column["Throughput (veh/h)"]] for row in after], batches),
                  "mean_speed_kmh": stats.batch_means([row[column["Mean speed (km/h)"]] for row in after], batches)}
    minutes = observed["minutes"]  # whole minutes only: the last part-minute would count low
    origins = sorted({origin for origin, _ in observed["entries"]})
    arrivals = {origin: stats.dispersion([observed["entries"].get((origin, minute), 0) for minute in range(minutes)])
                for origin in origins}
    queues = []
    samples = observed["samples"]
    for road_id, counts in observed["queues"].items():
        rank, seen, q95 = 0.95 * samples, samples - sum(counts.values()), 0.0  # zeros: the samples with no queue
        for metres in sorted(counts, key=float):
            if seen >= rank:
                break
            seen += counts[metres]
            q95 = float(metres)
        length = observed["roads"].get(road_id, {}).get("length_m") or 0
        if q95 > 0 and length:
            queues.append({"road": road_id, "queue_p95_m": q95, "length_m": length, "ratio": round(q95 / length, 2),
                           "spills_back": q95 >= length})
    gini = stats.gini(row["flow_vph"] for row in kpis["roads"])
    return {"reliability": reliability, "steady_state": steady, "arrivals_per_minute": arrivals,
            "queues_p95": sorted(queues, key=lambda row: -row["ratio"])[:None if every else 10],
            "flow_gini": None if gini is None else round(gini, 3), "bus_stops": bus_stops(observed),
            "bus_passengers": bus_passengers(observed),
            "notes": ["Free-flow time is trip time less delay against the speed limit, so slow types (bikes) read high.",
                      "Arrivals are vehicles first seen on the road: queued entry evens them out (reads regular).",
                      "Queues are metres a lane on a road, both directions of a two-way road together.",
                      "Batch means: the steady state after the warm-up; a lag-1 autocorrelation far from 0 means the "
                      "batches are too short to trust the interval."]}


# HCM 2000 Exhibit 27-8: headway adherence LOS by cv = SD of headways / scheduled headway (6 or more buses/h)
HEADWAY_LOS = ((0.10, "A"), (0.20, "B"), (0.30, "C"), (0.40, "D"), (0.50, "E"))


def bus_stops(observed: dict) -> list[dict]:
    """Each stop's bus arrivals (timetabled types): headways, their cv against the scheduled headway and its
    LOS (HCM 2000 Exhibit 27-8, for 6 or more buses an hour), and the expected wait of a passenger arriving
    at random, E(h)(1 + cv²)/2 with cv against the mean headway (Handbook of Transportation Science p127)."""
    arrivals: dict = defaultdict(list)
    for stop, link, t, _, mode, headway_min, line in observed.get("stops", []):
        arrivals[(stop, link, line, mode, headway_min)].append(t)  # a line: one OD pair's timetable
    found = []
    for (stop, link, line, mode, headway_min), times in sorted(arrivals.items()):
        gaps = [b - a for a, b in zip(sorted(times), sorted(times)[1:])]
        if len(gaps) < 2:
            continue
        mean, sd = statistics.fmean(gaps), statistics.stdev(gaps)
        wait = mean * (1 + (sd / mean) ** 2) / 2
        row = {"stop": stop, "direction": link, "line": line, "type": mode, "arrivals": len(times), "mean_headway_min": round(mean / 60, 2),
               "sd_min": round(sd / 60, 2), "expected_wait_min": round(wait / 60, 2)}
        if headway_min:
            cv = sd / (headway_min * 60)
            # bunched: a bus closer than a quarter of the timetable's headway to the one before it (Handbook of
            # Transportation Science p127); excess wait: the expected wait over half the timetabled headway
            row.update(scheduled_min=headway_min, cv=round(cv, 3),
                       bunched_percent=round(100 * sum(gap < 0.25 * headway_min * 60 for gap in gaps) / len(gaps), 1),
                       excess_wait_min=round((wait - headway_min * 30) / 60, 2))
            if headway_min <= 10:
                row["los"] = next((grade for top, grade in HEADWAY_LOS if cv <= top), "F")
        found.append(row)
    return found


def bus_passengers(observed: dict) -> list[dict]:
    """Each stop with passengers (Simulation._serve): the buses that called, passengers boarded and alighted, those
    left behind (a bus full when it came), the dwell (mean, longest) and the heaviest load a bus left with."""
    found: dict = {}
    for stop, link, _t, _bus, boarded, alighted, left, load, dwell in observed.get("boardings", []):
        row = found.setdefault((stop, link), {"stop": stop, "direction": link, "buses": 0, "boarded": 0, "alighted": 0,
                                              "left_behind_max": 0, "dwells": [], "load_max": 0.0})
        row["buses"] += 1
        row["boarded"] += boarded
        row["alighted"] += alighted
        row["left_behind_max"] = max(row["left_behind_max"], left)
        row["dwells"].append(dwell)
        row["load_max"] = max(row["load_max"], load)
    rows = []
    for row in found.values():
        dwells = row.pop("dwells")
        rows.append({**row, "dwell_mean_s": round(statistics.fmean(dwells), 1), "dwell_max_s": max(dwells)})
    return sorted(rows, key=lambda row: (row["stop"], row["direction"]))


def headway_cv(observed: dict) -> float | None:
    """The run's bus headway variation: the mean over stops of SD ÷ mean headway (TSP bunching or evening buses out:
    lower is more regular); None without two headways at a stop."""
    found = [row["sd_min"] / row["mean_headway_min"] for row in bus_stops(observed) if row["mean_headway_min"]]
    return round(statistics.fmean(found), 3) if found else None


def series(kpis: dict, what: str, road: str | None = None, interval_min: int = 1) -> list[float]:
    """A run's values per interval: the network's throughput (veh/h), mean_speed (km/h), on_road and queued
    (per-minute rows); a road's flow (veh/h, both directions, at its detectors) or speed (space-mean km/h);
    or trip_time (mean seconds of the trips arriving in it). Whole intervals only."""
    observed = kpis["observed"]
    rows = observed.get("minute_rows", [])
    if interval_min < 1:
        raise ValueError("interval_min must be 1 or more.")
    networkwide = {"throughput": "Throughput (veh/h)", "mean_speed": "Mean speed (km/h)",
                   "on_road": "Vehicles on the road", "queued": "Queued now (veh)"}
    span = interval_min * 60
    count = int(observed.get("observed_s", kpis["time_s"]) // span)
    if what in networkwide:
        column = MINUTE_COLUMNS.index(networkwide[what])
        values = [row[column] for row in rows]
        return [statistics.fmean(values[i * interval_min:(i + 1) * interval_min]) for i in range(len(values) // interval_min)]
    if what == "trip_time":
        buckets: dict = defaultdict(list)
        for trip in observed["trips"]:
            buckets[int(trip[5] // span)].append(trip[3])
        return [statistics.fmean(buckets[i]) if buckets[i] else 0.0 for i in range(int(kpis["time_s"] // span))]
    if what not in ("flow", "speed"):
        raise ValueError("what is throughput, mean_speed, on_road, queued, trip_time, or a road's flow or speed.")
    passages = [p for p in observed.get("passages", []) if p[0] == road]
    if not road or road not in {info["road"] for info in observed.get("links", {}).values()}:
        raise ValueError(f"The run has no road called {road!r} (give road for flow or speed).")
    buckets = defaultdict(list)
    for passage in passages:
        buckets[int(passage[3] // span)].append(passage[4])
    if what == "flow":
        return [len(buckets[i]) * 3600 / span for i in range(count)]
    return [len(buckets[i]) / sum(1 / v for v in buckets[i]) * 3.6 if buckets[i] else 0.0 for i in range(count)]


def detectors(kpis: dict, road: str | None = None, limit: int = 12) -> dict:
    """Detector statistics at the middle of each link (one direction of a road; the busiest unless `road`):
    flow, time-mean and space-mean speed (harmonic; Edie), the 15th, 50th and 85th percentile speeds and their
    SD, headways per lane (mean, cv, shortest, share under 3 s: following), occupancy and density (flow over
    space-mean speed), and counts a minute against Poisson (HCM 2000 p85–86, p236; Green Book p119–121;
    Handbook of Transportation Science p162–170). With stop-line crossings, each signalised approach's queue
    discharge: saturation headway from the 5th queued vehicle, saturation flow, start-up lost time (HCM 2000
    p90–91, p124–125)."""
    observed = kpis["observed"]
    seconds = observed.get("observed_s") or kpis["time_s"]
    by_link: dict = defaultdict(list)
    for passage in observed.get("passages", []):
        if road is None or passage[0] == road:
            by_link[passage[1]].append(passage)
    if road is not None and not by_link:
        raise ValueError(f"No vehicle passed a detector on {road!r} in that run (or there is no such road).")
    rows = []
    for link_id, found in sorted(by_link.items(), key=lambda item: -len(item[1]))[:limit if road is None else None]:
        speeds = [p[4] * 3.6 for p in found if p[4] > 0.05]
        lanes: dict = defaultdict(list)
        for p in found:
            lanes[p[2]].append(p[3])
        gaps = [b - a for times in lanes.values() for a, b in zip(sorted(times), sorted(times)[1:])]
        sms = len(speeds) / sum(1 / v for v in speeds) if speeds else None
        flow = len(found) * 3600 / seconds
        row = {"link": link_id, "road": found[0][0], "vehicles": len(found), "flow_vph": round(flow),
               "time_mean_kmh": round(statistics.fmean(speeds), 1) if speeds else None,
               "space_mean_kmh": round(sms, 1) if sms else None,
               "occupancy_percent": round(100 * sum(p[5] / p[4] for p in found if p[4] > 0.05) / (seconds * len(lanes)), 1),
               "density_veh_km": round(flow / sms, 1) if sms else None,
               "counts_per_minute": stats.dispersion([sum(1 for p in found if int(p[3] // 60) == m)
                                                      for m in range(int(seconds // 60))])}
        if len(speeds) >= 2:
            row.update(v15_kmh=round(stats.percentile(speeds, 15), 1), v50_kmh=round(stats.percentile(speeds, 50), 1),
                       v85_kmh=round(stats.percentile(speeds, 85), 1), speed_sd_kmh=round(stdev(speeds), 1))
        if len(gaps) >= 2:
            mean = statistics.fmean(gaps)
            row["headways"] = {"mean_s": round(mean, 2), "cv": round(stdev(gaps) / mean, 2), "shortest_s": round(min(gaps), 2),
                               "under_3s_percent": round(100 * sum(g < 3 for g in gaps) / len(gaps), 1)}
        rows.append(row)
    return {"detectors": rows, "stop_lines": discharge(observed, road),
            "notes": ["A detector sits at the middle of each lane of a link (one direction of a road; '>' runs from its "
                      "from-node, '<' back). Headway cv near 1 is random flow; low is platooned or queued.",
                      "Saturation flow is measured on the commonest vehicle type only (the HCM's passenger cars); "
                      "the HCM asks for 15 cycles or more."]}


def discharge(observed: dict, road: str | None = None) -> list[dict]:
    """Queue discharge at signalised stop lines: in each green, the vehicles that had stopped on the
    approach, crossing one after another; the saturation headway is the mean headway from the 5th of them
    on, and the start-up lost time each cycle's first four headways' excess over it (h₁ from the green's start)."""
    crossings = [c for c in observed.get("stop_lines", []) if c[5] is not None and c[7] == "straight"
                 and (road is None or c[1].rstrip("<>") == road)]  # through movements, as the HCM measures
    if not crossings:
        return []
    common = Counter(c[6] for c in crossings).most_common(1)[0][0]
    cycles: dict = defaultdict(list)
    for node, link, lane, t, stopped, green, mode, _ in crossings:
        cycles[(node, link, lane, green)].append((t, stopped and mode == common))
    by_link: dict = defaultdict(lambda: {"saturation": [], "first_four": []})
    for (node, link, _, green), found in cycles.items():
        found.sort()
        queue = []
        for t, queued in found:
            if not queued or (len(queue) > 1 and t - queue[-1] > QUEUE_GAP_S):  # the standing queue has gone
                break
            queue.append(t)
        if len(queue) < 6:
            continue
        headways = [queue[0] - green] + [b - a for a, b in zip(queue, queue[1:])]
        by_link[(node, link)]["saturation"] += headways[4:]
        by_link[(node, link)]["first_four"].append(headways[:4])
    rows = []
    for (node, link), found in sorted(by_link.items()):
        h = statistics.fmean(found["saturation"])
        rows.append({"junction": node, "link": link, "cycles": len(found["first_four"]), "saturation_headway_s": round(h, 2),
                     "saturation_flow_vphpl": round(3600 / h), "vehicle_type": common,
                     "start_up_lost_s": round(statistics.fmean(sum(x - h for x in four) for four in found["first_four"]), 2)})
    return rows


QUEUE_GAP_S = 4.0  # a discharging queue ends at a gap longer than this
CONFLICT_TTC_S, SEVERE_TTC_S, SEVERE_DRAC = 1.5, 1.0, 3.35  # a severe conflict: TTC under 1 s, or braking over 3.35 m/s² needed (SSAM)


def safety(kpis: dict, ttc_s: float = CONFLICT_TTC_S, every: bool = False) -> dict:
    """Rear-end conflicts (TTC < 1.5 s, simulation.CONFLICT_TTC_S) as a safety surrogate: per road, counts,
    severe ones and the rate per million vehicle-km; a safety performance function fitted across roads,
    ln E[conflicts] = ln(km) + b0 + b1·ln(flow) (Poisson, with the NB2 α when overdispersed; Washington et al.
    p631–662), and the roads ranked by their Empirical Bayes excess over it (Hauer; Safer Roads p148–149)."""
    observed = kpis["observed"]
    if not 0.5 <= ttc_s <= 3.0:
        raise ValueError("ttc_s must be 0.5 to 3 seconds (1.5 is the usual threshold).")
    conflicts = [event for event in observed.get("conflicts", []) if event["ttc_min"] < ttc_s]
    lengths = {}
    for info in observed.get("links", {}).values():
        lengths[info["road"]] = info["length_m"]
    by_road: dict = defaultdict(list)
    for event in conflicts:
        by_road[event["road"]].append(event)
    roads = [row for row in kpis["roads"] if row.get("vehicle_km") and row["flow_vph"] > 0]
    sites = []
    for row in roads:
        events = by_road.get(row["road"], [])
        severe = sum(e["ttc_min"] < SEVERE_TTC_S or e["drac_max"] > SEVERE_DRAC for e in events)
        sites.append({"road": row["road"], "conflicts": len(events), "severe": severe, "flow_vph": row["flow_vph"],
                      "vehicle_km": row["vehicle_km"], "per_million_veh_km": round(len(events) / row["vehicle_km"] * 1e6)})
    result = {"ttc_threshold_s": ttc_s, "conflicts": len(conflicts), "severe": sum(s["severe"] for s in sites),
              "by_type": dict(Counter(f"{e['type']} behind {e['leader_type']}" for e in conflicts).most_common(6))}
    if conflicts:
        ttc = [e["ttc_min"] for e in conflicts]
        result["ttc_s"] = {"median": round(stats.percentile(ttc, 50), 2), "p15": round(stats.percentile(ttc, 15), 2)}
    usable = [s for s in sites if lengths.get(s["road"])]
    fit = None
    if len(usable) >= 4 and any(s["conflicts"] for s in usable):
        try:  # flows all alike (collinear) or a fit that won't settle: ranked by rate below instead
            fit = stats.poisson_regression([[log(s["flow_vph"])] for s in usable], [s["conflicts"] for s in usable],
                                           [log(lengths[s["road"]] / 1000) for s in usable])
        except (ValueError, ZeroDivisionError, OverflowError):
            fit = None
    if fit is not None:
        alpha = fit["alpha"] if fit["dispersion"] > 1 and fit["dispersion_p"] < 0.05 else 0.0
        for site, mu in zip(usable, fit["mu"]):
            site["expected"] = round(mu, 2)
            site["eb_estimate"] = round(stats.empirical_bayes(site["conflicts"], mu, alpha), 2)
            site["excess"] = round(site["eb_estimate"] - mu, 2)
        result["spf"] = {"form": "conflicts = km × exp(b0) × flow^b1", "b0": round(fit["coefficients"][0], 3),
                         "b1": round(fit["coefficients"][1], 3), "b1_se": round(fit["se"][1], 3),
                         "dispersion": round(fit["dispersion"], 2), "alpha": round(alpha, 3),
                         "model": "negative binomial (overdispersed)" if alpha else "Poisson"}
        result["ranked"] = sorted(usable, key=lambda s: -s["excess"])[:None if every else 10]
    else:
        result["ranked"] = sorted(sites, key=lambda s: -s["per_million_veh_km"])[:None if every else 10]
        result["note"] = ("Too few roads with conflicts (or flows too alike) for a safety performance function: "
                          "ranked by rate.")
    result["notes"] = ["Conflicts are rear-end only, between vehicles on a lane (not crossing movements in junctions).",
                       "A surrogate, not crashes: use it to compare designs, not to predict crash numbers.",
                       "The simulated drivers keep safe gaps (Krauss), so short TTCs are rare: a few a road an hour "
                       "means stop-start traffic, not reckless driving."]
    # Collisions (overlaps the drivers failed to avoid: a model fault to look into, not a crash rate) and emergency
    # braking (harder than comfortable), each by where; runs saved before these were recorded have neither
    for key in ("collisions", "emergency_braking"):
        if key in observed:
            events = observed[key]
            result[key] = {"count": len(events), "by_where": dict(Counter(e["where"] for e in events).most_common(6))}
            if key == "collisions":
                result[key]["by_kind"] = dict(Counter(e["kind"] for e in events))
    return result


def check_calibration(kpis: dict, counts=None, profiles=None) -> dict:
    """A run against field data: `counts` (road flows: GEH per count and stats.fit's scores) and/or
    `profiles` (FHWA Toolbox III 2019: a measure per interval on several observed days, the run's against the
    representative day's variation envelopes, Criteria I–IV)."""
    if not counts and not profiles:
        raise ValueError("Give counts like [{\"road\": \"1,1-2,1\", \"vph\": 800}] and/or profiles.")
    result = {}
    if counts:
        if not isinstance(counts, list) or not all(isinstance(count, dict) for count in counts):
            raise ValueError("Give counts like [{\"road\": \"1,1-2,1\", \"vph\": 800}].")
        flows = {row["road"]: row["flow_vph"] for row in kpis["roads"]}
        rows = []
        for count in counts:
            road, vph = str(count.get("road", "")), count.get("vph")
            if road not in flows:
                raise ValueError(f"The run has no road called {road!r}.")
            if not isinstance(vph, (int, float)) or vph < 0:
                raise ValueError(f"{road}: vph must be a count, not below zero.")
            rows.append({"road": road, "counted_vph": vph, "modelled_vph": flows[road],
                         "geh": round(stats.geh(flows[road], vph), 2)})
        result.update({**stats.fit([(row["modelled_vph"], row["counted_vph"]) for row in rows]), "counts": rows,
                       "note": "GEH under 5 for 85 % of counts is the usual target (UK DMRB; FHWA Toolbox III 2004)."})
    if profiles:
        checked = []
        for profile in profiles if isinstance(profiles, list) else [profiles]:
            if not isinstance(profile, dict) or profile.get("what") not in ("flow", "speed", "trip_time"):
                raise ValueError("Each profile is {\"what\": \"flow\" | \"speed\" | \"trip_time\", \"road\": id "
                                 "(flow, speed), \"interval_min\": 15, \"days\": [[one value an interval], ...]}.")
            simulated = series(kpis, profile["what"], profile.get("road"), int(profile.get("interval_min") or 15))
            kind = {"flow": "flow", "speed": "speed", "trip_time": "travel_time"}[profile["what"]]
            found = stats.envelopes(profile.get("days") or [], simulated, kind, profile.get("representative_day"))
            checked.append({"what": profile["what"], "road": profile.get("road"), "simulated": [round(v, 1) for v in simulated],
                            **found})
        result["profiles"] = checked
        result["envelope_note"] = ("FHWA Toolbox III (2019): calibrated when Criteria I-IV all hold, for each key "
                                   "measure. For flows the critical intervals are the two highest (the book uses "
                                   "bottleneck onset and dissipation).")
    return result


def speed_risk(before_kmh: float, after_kmh: float) -> dict:
    """The power model of speed and road safety (Nilsson; Handbook of Transportation Science p92–93): crashes
    change with (v₁/v₀), injury crashes with its square, fatal crashes with its fourth power."""
    ratio = after_kmh / before_kmh
    return {"crashes_percent": round(100 * (ratio - 1), 1), "injury_percent": round(100 * (ratio ** 2 - 1), 1),
            "fatal_percent": round(100 * (ratio ** 4 - 1), 1)}


def experiment_workbook(path, experiment: dict, inputs: list, journal: list[dict]) -> Path:
    """An experiment's workbook: Read me, Scenarios, Comparison, Runs, Planning journal, and the baseline's inputs."""
    def rounded(value, digits=2):
        return round(value, digits) if isinstance(value, float) else value

    read_me = [["ACMS network test", ""], ["Experiment", experiment["id"]], ["Started", experiment["started"]],
               ["Seeds", ", ".join(map(str, experiment["seeds"]))], ["Simulated each run", clock(experiment["duration_s"])],
               ["Question", experiment.get("question", "")],
               *([["AI agent timing", experiment["agent_timing"]]] if experiment.get("agent_timing") else []), ["", ""],
               ["Comparison", "Each figure per scenario: mean over the seeds, standard deviation, 95 % confidence "
                              "interval (Student's t), the runs needed for the mean within ±precision (FHWA Toolbox "
                              "III), and the difference from the baseline (the first scenario): paired t over the shared "
                              "seeds (else Welch), its p-value and interval at 95 %, Bonferroni-widened for several "
                              "scenarios (Confidence); 'clear' when that interval leaves out zero (one seed: open)."],
               ["Runs", "Every run: its scenario, seed and figures; each also has its own run workbook."],
               ["Scenarios", "What each scenario changed: edit tools on a copy of the network, demand scaling, extensions, "
                             "weather and incidents (lanes closed, work zones; random ones are drawn per seed)."],
               ["Planning journal", "The assistant's plans (note_plan) and tool calls with what the user decided, "
                                    "for studying how the agent plans."],
               ["Inputs", "The baseline network, demand and vehicle types as at the start (sheets after these)."]]
    scenario_rows = [["Scenario", "Demand scale", "Extensions", "Changes", "Weather", "Incidents", "Random incidents",
                      "Ramp meters"]]
    for scenario in experiment["scenarios"]:
        scenario_rows.append([scenario["name"], scenario["demand_scale"], ", ".join(scenario["extensions"]) or "none",
                              "; ".join(f"{c['tool']} {c.get('args', {})}" for c in scenario["changes"]) or "none (as built)",
                              scenario.get("weather", "clear"),
                              "; ".join(f"{i['road']}: {i['lanes']} lane(s) from {i['start_s'] / 60:g} min"
                                        + (f" for {(i['end_s'] - i['start_s']) / 60:g} min" if i.get("end_s") is not None else "")
                                        + (f", {i['speed_kmh']:g} km/h" if i.get("speed_kmh") else "")
                                        for i in scenario.get("incidents") or []) or "none",
                              str(scenario.get("random_incidents") or "none"),
                              "; ".join(f"{m['road']}: " + (f"ALINEA to {m['target_occupancy_percent']:g} %"
                                                           if m.get("target_occupancy_percent") is not None
                                                           else f"{m['rate_vph']:g} veh/h")
                                        for m in scenario.get("meters") or []) or "none"])
    comparison = comparison_sheet(experiment["comparison"])
    keys = list(dict.fromkeys(key for run in experiment["runs"] for key in run["measures"]))
    run_rows = [["Run", "Scenario", "Seed", "Workbook", *keys]]
    for run in experiment["runs"]:
        run_rows.append([run["id"], run["scenario"], run["seed"], run.get("file", ""),
                         *(rounded(run["measures"].get(key)) for key in keys)])
    journal_rows = [["Time", "Kind", "Tool", "What", "Outcome"]]
    journal_rows += [[entry.get("time"), entry.get("kind"), entry.get("tool", ""), entry.get("what", ""),
                      entry.get("outcome", "")] for entry in journal]
    return write_xlsx(path, [("Read me", read_me), ("Comparison", comparison), ("Scenarios", scenario_rows),
                             ("Runs", run_rows), ("Planning journal", journal_rows), *inputs])


def experiment_name(started: datetime) -> str:
    return f"ACMS experiment {started:%Y-%m-%d %H-%M-%S}.xlsx"


def _rounded(value, digits=2):
    return round(value, digits) if isinstance(value, float) else value


def _yes(value) -> str | None:
    return None if value is None else ("yes" if value else "no")


def comparison_sheet(comparison: list[dict]) -> list[list]:
    """compare()'s rows as a sheet: per figure and scenario, its summary and the difference from the baseline."""
    rows = [["Figure", "Unit", "Scenario", "Runs", "Mean", "SD", "95 % CI ±", "Runs needed", "Difference vs baseline",
             "Test", "Confidence", "Difference CI ±", "p", "Change (%)", "Clear?", "Better?", "Wilcoxon p",
             "ANOVA p (all scenarios)"]]
    for row in comparison:
        rows.append([row["figure"], row["unit"], row["scenario"], row["n"], _rounded(row["mean"]), _rounded(row["sd"]),
                     _rounded(row["ci95"]), row.get("replications_needed"), _rounded(row.get("vs_baseline_diff")),
                     row.get("vs_baseline_test"), _rounded(row.get("confidence"), 4), _rounded(row.get("vs_baseline_ci")),
                     _rounded(row.get("vs_baseline_p"), 4), _rounded(row.get("vs_baseline_percent"), 1),
                     _yes(row.get("vs_baseline_clear")), _yes(row.get("better")),
                     _rounded(row.get("vs_baseline_wilcoxon_p"), 4), (row.get("anova") or {}).get("p")])
    return rows


# --- Batch runs (the Batch Run extension): every seed x agent x repeat, compared with the first agent ------

# What a batch leads with, against its baseline: (key, the figure as the headline names it, higher is better?)
HEADLINE = [("people_per_h", "Passenger throughput increase", True),
            ("people_delay_h", "Passenger delay reduction", False),
            ("mean_person_delay_s", "Delay per passenger reduction", False),
            ("queued_mean", "Queue reduction (mean queued at once)", False),
            ("queued_max", "Queue reduction (most queued at once)", False),
            ("mean_wait_s", "Vehicle wait time reduction (per trip)", False),
            ("mean_trip_delay_s", "Vehicle delay reduction (per trip)", False),
            ("throughput_vph", "Vehicle throughput increase", True),
            ("emergency_trip_s", "Emergency travel time reduction", False)]  # with Test DEVL


def averaged(runs: list[dict]) -> list[dict]:
    """A batch's runs with each (scenario, seed)'s repeats made one: every figure's mean over them, so the
    comparison pairs seeds (common random numbers) and repeats only steady a model's varying answers."""
    groups: dict = {}
    for run in runs:
        groups.setdefault((run["scenario"], run["seed"]), []).append(run)
    merged = []
    for (scenario, seed), group in groups.items():
        means = {}
        for key in dict.fromkeys(key for run in group for key in run["measures"]):
            values = [run["measures"][key] for run in group if isinstance(run["measures"].get(key), (int, float))]
            means[key] = sum(values) / len(values) if values else None
        merged.append({"scenario": scenario, "seed": seed, "repeats": len(group), "measures": means})
    return merged


def headline_sheet(comparison: list[dict]) -> list[list]:
    """Each agent against the baseline (the first scenario) on the lead figures, as increases or reductions
    (positive: better), with every vehicle type's delay and wait reduction (the bus's among them)."""
    rows = [["Agent", "Figure", "Baseline mean", "Agent mean", "Increase or reduction (%)", "Difference",
             "Difference CI ±", "p", "Clear?", "Better?"]]
    if not comparison:
        return rows
    labels = {key: label for key, label, *_ in MEASURES + AGENT_MEASURES}
    figures = list(dict.fromkeys(row["figure"] for row in comparison))
    picks = [(labels[key], name, up) for key, name, up in HEADLINE]
    for ending, what in ((" mean delay (s)", "delay"), (" mean wait (s)", "wait time")):
        picks += [(figure, f"{figure[:-len(ending)]} {what} reduction", False) for figure in figures if figure.endswith(ending)]
    by = {(row["figure"], row["scenario"]): row for row in comparison}
    scenarios = list(dict.fromkeys(row["scenario"] for row in comparison))
    for scenario in scenarios[1:]:
        for figure, name, up in picks:
            row, base = by.get((figure, scenario)), by.get((figure, scenarios[0]))
            if row is None or base is None:
                continue
            percent = row.get("vs_baseline_percent")
            rows.append([scenario, name, _rounded(base["mean"]), _rounded(row["mean"]),
                         None if percent is None else round(percent if up else -percent, 1),
                         _rounded(row.get("vs_baseline_diff")), _rounded(row.get("vs_baseline_ci")),
                         _rounded(row.get("vs_baseline_p"), 4), _yes(row.get("vs_baseline_clear")), _yes(row.get("better"))])
    return rows


def batch_workbook(path, batch: dict, inputs: list) -> Path:
    """A batch's workbook: Read me, Headline (each agent's increases and reductions against the baseline),
    Comparison (every figure, the agents' LLM figures among them), Agents, Runs, and the inputs."""
    agents = batch["agents"]
    read_me = [["ACMS batch run", ""], ["Started", batch["started"]], ["Seeds", ", ".join(map(str, batch["seeds"]))],
               ["Times each", batch["times"]], ["Simulated each run", clock(batch["duration_s"])],
               ["Runs finished", f"{len(batch['runs'])} of {batch.get('planned', len(batch['runs']))}"
                                 + ("" if len(batch["runs"]) >= batch.get("planned", 0) else
                                    " (a checkpoint, rewritten after every run, or a batch cut short)")],
               ["Agents", ", ".join(agent["label"] for agent in agents)], ["Baseline", agents[0]["label"]],
               ["Decision deadline", f"{batch['deadline_s']:g} simulated s" if batch.get("tsp") else "none (TSP not ticked)"],
               ["Pace", f"{batch['speed']:g}x real time" if batch.get("speed") else "as fast as the CPU allows"],
               ["Runs at once", f"{batch.get('at_once', 1)}, never two on one model server"],
               ["Each run's workbook", batch.get("folder", "")], ["", ""],
               ["How it ran", "Every agent ran every seed, the times asked, headless, seed by seed (every agent on a seed before the next), so a batch cut short still compares on the seeds it finished. With TSP ticked, each run kept in step "
                              "with the wall clock at the pace above and never paused: the junctions published their "
                              "telemetry (JSON, each run's telemetry.jsonl beside its workbook), the agent answered each bus "
                              "request in the background, the soonest-arriving bus first, and an answer counted from when it "
                              "reached the controller; none by the deadline: the plan. The baselines: the controllers run with no "
                              "agent, Never preempt falling back to the plan on every request, Always preempt granting every one."],
               ["How it compares", "A seed's repeats are averaged, then each agent is compared with the baseline (the first "
                                   "agent) by a paired t over the seeds (common random numbers), at 95 % widened by "
                                   "Bonferroni for several agents; 'clear' when the interval leaves out zero (one seed: open)."],
               ["", ""], ["Sheet", "What it holds"],
               ["Headline", "Each agent against the baseline: passenger throughput increase, passenger delay reduction, "
                            "queue, vehicle wait time and delay reductions, each type's delay and wait (positive: better)."],
               ["Comparison", "Every figure per agent: mean over the seeds, SD, 95 % CI, runs needed, and the difference "
                              "from the baseline; the agents' figures (requests, TRUE, latency, errors) too."],
               ["Agents", "Who decided in each: system, provider, model, and their LLM figures over the runs."],
               ["Runs", "Every run: agent, seed, repeat, its workbook and its figures."],
               ["Inputs", "The network, demand and vehicle types as at the start (the sheets after these)."]]
    agent_keys = [key for key, *_ in AGENT_MEASURES]
    agent_rows = [["Agent", "System", "Provider", "Model", "Runs", *(label for _, label, *_ in AGENT_MEASURES)]]
    for agent in agents:
        mine = [run for run in batch["runs"] if run["scenario"] == agent["label"]]
        means = averaged([{**run, "seed": 0} for run in mine])
        values = means[0]["measures"] if means else {}
        agent_rows.append([agent["label"], {"1": "System 1", "2": "System 2"}.get(agent.get("system"), "none"),
                           agent.get("provider") or None, agent.get("model") or None, len(mine),
                           *(_rounded(values.get(key)) for key in agent_keys)])
    keys = list(dict.fromkeys(key for run in batch["runs"] for key in run["measures"]))
    run_rows = [["Run", "Agent", "Seed", "Repeat", "Workbook", *keys]]
    for run in batch["runs"]:
        run_rows.append([run["id"], run["scenario"], run["seed"], run["repeat"], run.get("file", ""),
                         *(_rounded(run["measures"].get(key)) for key in keys)])
    return write_xlsx(path, [("Read me", read_me), ("Headline", headline_sheet(batch["comparison"])),
                             ("Comparison", comparison_sheet(batch["comparison"])), ("Agents", agent_rows),
                             ("Runs", run_rows), *inputs])


def batch_name(started: datetime) -> str:
    return f"ACMS batch {started:%Y-%m-%d %H-%M-%S}.xlsx"


ANALYSIS_ROWS = 20_000  # rows a sheet of raw events keeps (conflicts): the rest are counted in its Read me line


def analysis_sheets(kpis: dict) -> list[list]:
    """What a run observed, analysed, as workbook sheets [name, rows, what it holds] (results.RunRecorder.add_analysis):
    its statistics, every OD pair's reliability, arrivals against Poisson, 95th-percentile queues, bus stops,
    detectors, link travel times, queue discharge at signals, conflicts and the safety ranking. Sheets with
    nothing to show are left out."""
    observed = kpis.get("observed") or {}
    if not observed:
        return []
    sheets = []
    try:
        _analysis_sheets(kpis, observed, sheets)
    except Exception as exc:  # noqa: BLE001 - the run's workbook matters more than any one analysis
        sheets.append(["Analysis note", [["What", "Why"], ["Some analysis sheets are missing", repr(exc)]],
                       "An analysis that couldn't be worked out, and why; the sheets before it are complete."])
    return sheets


def _analysis_sheets(kpis: dict, observed: dict, sheets: list) -> None:
    analysed = analyse_run(kpis, every=True)
    rel, steady = analysed["reliability"], analysed["steady_state"]
    rows = [["Figure", "Value", "Unit", "How"],
            ["Trips completed", rel.get("trips"), "trips", ""],
            ["Mean trip time", rel.get("mean_s"), "s", ""], ["Trip time SD", rel.get("sd_s"), "s", ""],
            ["Trip time CV", rel.get("cv"), "", "SD / mean"], ["Median trip time", rel.get("median_s"), "s", ""],
            ["85th percentile trip time", rel.get("p85_s"), "s", ""], ["95th percentile trip time", rel.get("p95_s"), "s", ""],
            ["Travel time index", rel.get("tti"), "", "mean trip time / mean free-flow time"],
            ["Planning time index", rel.get("pti"), "", "95th percentile of trip / free-flow time (FHWA)"],
            ["Buffer index (mean-based)", rel.get("bi"), "", "(95th percentile − mean) / mean of trip / free-flow time"],
            ["Flow concentration (Gini)", analysed["flow_gini"], "", "road flows: 0 even, towards 1 on few roads"]]
    if steady:
        rows += [["Warm-up", steady["warm_up_min"], "min", "MSER-5 on the vehicles on the road: measure from it "
                                                            "(measure_from_min) to leave it out of the figures"],
                 ["Minutes after warm-up", steady["minutes_after"], "min", ""]]
        for key, label in (("throughput_vph", "Steady throughput"), ("mean_speed_kmh", "Steady mean speed")):
            found = steady.get(key)
            if found:
                rows.append([label, found["mean"], "veh/h" if key == "throughput_vph" else "km/h",
                             f"± {found['ci95']} (95 %, {found['batches']} batch means of {found['batch_minutes']} min; "
                             f"lag-1 autocorrelation {found['lag1_autocorrelation']})"])
    sheets.append(["Run statistics", rows, "The run's trip-time reliability, warm-up, steady state and flow concentration."])
    if rel.get("least_reliable_pairs"):
        sheets.append(["Reliability by OD", [["OD pair", "Trips", "Travel time index", "Planning time index",
                                              "Buffer index (mean-based)"]] +
                       [[r["pair"], r["trips"], r["tti"], r["pti"], r["bi"]] for r in rel["least_reliable_pairs"]],
                       "Every OD pair with 5 trips or more: travel time, planning time and buffer indexes, least "
                       "reliable first."])
    if analysed["arrivals_per_minute"]:
        sheets.append(["Arrivals", [["Origin", "Minutes", "Mean a minute", "Variance", "Index of dispersion", "p (Poisson)",
                                     "Verdict"]] +
                       [[o, d.get("intervals"), d.get("mean"), d.get("variance"), d.get("index"), d.get("p_poisson"),
                         d.get("verdict")] for o, d in analysed["arrivals_per_minute"].items()],
                       "Vehicles first seen a minute at each origin against Poisson (index of dispersion, chi-square)."])
    if analysed["queues_p95"]:
        sheets.append(["Queues (95th pct)", [["Road", "95th percentile queue a lane (m)", "Length (m)", "Share of length",
                                             "Spills back"]] +
                       [[q["road"], q["queue_p95_m"], q["length_m"], q["ratio"], q["spills_back"]] for q in analysed["queues_p95"]],
                       "Each road's 95th-percentile queue against its length (storage), spillback where it reaches it."])
    if analysed["bus_stops"]:
        columns = ["stop", "direction", "line", "type", "arrivals", "mean_headway_min", "sd_min", "scheduled_min", "cv",
                   "los", "expected_wait_min"]
        sheets.append(["Bus headways", [["Stop", "Direction", "Line", "Type", "Arrivals", "Mean headway (min)", "SD (min)",
                                      "Scheduled (min)", "cv", "Headway adherence LOS", "Expected wait (min)"]] +
                       [[b.get(c) for c in columns] for b in analysed["bus_stops"]],
                       "Each line's buses at each stop: headways, adherence (HCM 2000 Exhibit 27-8), expected wait."])
    if analysed["bus_passengers"]:
        columns = ["stop", "direction", "buses", "boarded", "alighted", "left_behind_max", "dwell_mean_s", "dwell_max_s",
                   "load_max"]
        sheets.append(["Bus passengers", [["Stop", "Direction", "Buses", "Boarded", "Alighted", "Most left behind",
                                           "Mean dwell (s)", "Longest dwell (s)", "Heaviest load"]] +
                       [[row.get(c) for c in columns] for row in analysed["bus_passengers"]],
                       "Passengers at each stop with passengers: dwell = door time + alighting × 1.7 s + boarding × "
                       "2.6 s (HCM 2000 Eq 27-2, Exhibit 27-9); left behind when a bus came full."])
    if observed.get("passages"):
        found = detectors(kpis, None, limit=100_000)
        head = ["Link", "Road", "Vehicles", "Flow (veh/h)", "Time-mean speed (km/h)", "Space-mean speed (km/h)",
                "v15 (km/h)", "v50 (km/h)", "v85 (km/h)", "Speed SD (km/h)", "Occupancy (%)", "Density (veh/km)",
                "Mean headway (s)", "Headway cv", "Shortest headway (s)", "Headways under 3 s (%)",
                "Counts a minute: index", "Counts a minute: verdict"]
        rows = [head]
        for d in found["detectors"]:
            h, c = d.get("headways") or {}, d.get("counts_per_minute") or {}
            rows.append([d["link"], d["road"], d["vehicles"], d["flow_vph"], d["time_mean_kmh"], d["space_mean_kmh"],
                         d.get("v15_kmh"), d.get("v50_kmh"), d.get("v85_kmh"), d.get("speed_sd_kmh"), d["occupancy_percent"],
                         d["density_veh_km"], h.get("mean_s"), h.get("cv"), h.get("shortest_s"), h.get("under_3s_percent"),
                         c.get("index"), c.get("verdict")])
        sheets.append(["Detectors", rows, "A detector at the middle of each lane of each link: flow, speeds and their "
                                          "percentiles, headways, occupancy, density, counts against Poisson."])
        if found["stop_lines"]:
            sheets.append(["Queue discharge", [["Junction", "Link", "Cycles", "Saturation headway (s)",
                                                 "Saturation flow (veh/h a lane)", "Start-up lost time (s)", "Vehicle type"]] +
                           [[r["junction"], r["link"], r["cycles"], r["saturation_headway_s"], r["saturation_flow_vphpl"],
                             r["start_up_lost_s"], r["vehicle_type"]] for r in found["stop_lines"]],
                           "Signalised approaches: through vehicles' queue discharge each green (HCM 2000 p90-91)."])
    if observed.get("traversals"):
        links = observed.get("links", {})
        rows = [["Link", "Road", "From", "To", "Length (m)", "Free-flow time (s)", "Traversals", "Mean time (s)",
                 "Time / free flow"]]
        for link_id, (count, mean) in sorted(observed["traversals"].items()):
            info = links.get(link_id, {})
            free = info.get("free_s")
            rows.append([link_id, info.get("road"), info.get("from"), info.get("to"), info.get("length_m"), free, count,
                         round(mean, 1), round(mean / free, 2) if free else None])
        sheets.append(["Link travel times", rows, "Each link's measured traversal time (entering it to entering the "
                                                  "next, the junction included) against free flow."])
    conflicts = observed.get("conflicts") or []
    if conflicts:
        rows = [["Time (s)", "Road", "Link", "Type", "Behind", "Speed (km/h)", "Least TTC (s)", "Most DRAC (m/s²)"]]
        rows += [[e["t"], e["road"], e["link"], e["type"], e["leader_type"], e["speed_kmh"], round(e["ttc_min"], 2),
                  round(e["drac_max"], 2)] for e in conflicts[:ANALYSIS_ROWS]]
        sheets.append(["Conflicts", rows, f"Every rear-end conflict (time to collision under 3 s), {len(conflicts)} in all"
                                          + (f", the first {ANALYSIS_ROWS} listed" if len(conflicts) > ANALYSIS_ROWS else "") + "."])
        found = safety(kpis, every=True)
        rows = [["Road", "Conflicts", "Severe", "Flow (veh/h)", "Vehicle-km", "Per million veh-km", "Expected",
                 "Empirical Bayes", "Excess"]]
        rows += [[r["road"], r["conflicts"], r["severe"], r["flow_vph"], r["vehicle_km"], r["per_million_veh_km"],
                  r.get("expected"), r.get("eb_estimate"), r.get("excess")] for r in found["ranked"]]
        spf = found.get("spf")
        if spf:
            rows += [[], ["Safety performance function", spf["form"], f"b0 {spf['b0']}", f"b1 {spf['b1']} ± {spf['b1_se']}",
                          f"dispersion {spf['dispersion']}", f"alpha {spf['alpha']}", spf["model"]]]
        sheets.append(["Safety", rows, "Conflicts under 1.5 s per road, the safety performance function and the "
                                       "roads ranked by their Empirical Bayes excess (blackspots)."])

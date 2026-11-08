"""Answers about this session's finished headless runs and experiments (no Qt): read, compare, analyse, regress,
power, benefit-cost, rank, decisions, the compact comparison. Each takes the window's tests dict ({"R1": {...}}).
Cut from main.MainWindow's _runs_command and its helpers (docs/architecture/experiments.md)."""

from __future__ import annotations

import json
import math
import statistics
from collections import defaultdict

from . import analysis, experiments, network_tools, stats


def run_test(tests: dict, identifier: str) -> dict:
    """A headless run of this session by id (R1...), or the latest when none is given."""
    runs = [test for test in tests.values() if test["kind"] == "run"]
    latest = list(tests.values())[-1] if tests else None
    if not identifier and latest is not None and latest["kind"] != "run":  # "the latest" would be an older run
        raise ValueError(f"The latest test is {latest['id']}, an experiment: its runs aren't read one by one. Give "
                         f"a run id ({', '.join(t['id'] for t in runs) or 'none yet'}), or run_simulation the "
                         "design to analyse (read_results gives the experiment's comparison).")
    test = tests.get(identifier.upper()) if identifier else (runs[-1] if runs else None)
    if test is None or test["kind"] != "run":
        raise ValueError(f"No run called {identifier or '(the latest)'}: run_simulation first. This session's runs: "
                         f"{', '.join(t['id'] for t in runs) or 'none yet'}.")
    return test


def read_results(tests: dict, identifier: str, latest_results) -> dict:
    if not identifier:
        listed = []
        for test in tests.values():
            entry = {"id": test["id"], "kind": test["kind"], "what": test["what"], "file": test["file"]}
            if test["kind"] == "run":
                entry.update(seed=test["seed"], simulated=test["simulated"],
                             throughput_vph=test["kpis"]["throughput_vph"], delay_veh_h=test["kpis"]["delay_veh_h"])
            else:
                entry.update(seeds=test["seeds"], runs=len(test["experiment"]["runs"]))
            listed.append(entry)
        latest = latest_results()
        return {"tests": listed, "latest_run": None if latest is None else {
            "running": latest["running"], "file": latest["file"], **experiments.headline(latest["kpis"])}}
    test = tests.get(identifier.upper())
    if test is None:
        raise ValueError(f"No test called {identifier}; this session's: {', '.join(tests) or 'none yet'}.")
    if test["kind"] == "run":
        kpis = test["kpis"]
        return {"id": test["id"], "seed": test["seed"], "simulated": test["simulated"], "file": test["file"],
                **experiments.headline(kpis), "busiest_roads": kpis["roads"][:15],
                "longest_queues": sorted(kpis["roads"], key=lambda row: -row["queue_max"])[:10]}
    experiment = test["experiment"]
    return {"id": test["id"], "question": experiment["question"], "seeds": experiment["seeds"],
            "scenarios": experiment["scenarios"], "file": test["file"],
            "comparison": compact(experiment["comparison"], experiment.get("objective"))}


def compare_runs(session: dict, ids: list[str]) -> dict:
    tests = [session.get(i.upper()) for i in ids]
    if len(ids) < 2 or any(t is None or t["kind"] != "run" for t in tests):
        raise ValueError(f"Give two or more run ids (R1, R2...); this session's runs: "
                         f"{', '.join(k for k, t in session.items() if t['kind'] == 'run') or 'none yet'}.")
    first = experiments.measures_of(tests[0]["kpis"])
    rows = []
    for test in tests:
        values = experiments.measures_of(test["kpis"])
        rows.append({"run": test["id"], "seed": test["seed"], "figures": {
            key: {"value": value, "change": None if test is tests[0] or value is None or first.get(key) is None
                  else round(value - first[key], 2)} for key, value in values.items()}})
    return {"runs": rows, "note": "Each run is one sample (one seed): use run_experiment to tell designs apart."}


def analyse_run(tests: dict, tool: str, args: dict) -> dict:
    """analyse_run, read_detectors, analyse_safety, forecast, check_calibration: one run's figures."""
    test = run_test(tests, str(args.get("id") or ""))
    kpis, head = test["kpis"], {"run": test["id"], "seed": test["seed"], "simulated": test["simulated"]}
    if tool == "analyse_run":
        return {**head, **experiments.analyse_run(kpis)}
    if tool == "read_detectors":
        return {**head, **experiments.detectors(kpis, args.get("road") or None)}
    if tool == "analyse_safety":
        found = {**head, **experiments.safety(kpis, float(args.get("ttc_s") or experiments.CONFLICT_TTC_S))}
        change = args.get("speed_change_kmh")
        if change:
            if not isinstance(change, list) or len(change) != 2 or any(not isinstance(v, (int, float)) or v <= 0
                                                                       for v in change):
                raise ValueError("speed_change_kmh is [before, after] mean speeds in km/h, both above 0.")
            found["speed_change"] = {"before_kmh": change[0], "after_kmh": change[1],
                                     **experiments.speed_risk(float(change[0]), float(change[1])),
                                     "model": "Nilsson's power model: crashes x (v1/v0), injury crashes ^2, "
                                              "fatal crashes ^4 (Handbook of Transportation Science p92-93)"}
        return found
    if tool == "forecast":
        interval, horizon = int(args.get("interval_min") or 1), int(args.get("horizon") or 10)
        if not 1 <= horizon <= 60:
            raise ValueError("horizon must be 1 to 60 intervals.")
        values = experiments.series(kpis, str(args.get("what")), args.get("road") or None, interval)
        return {**head, "what": args.get("what"), "road": args.get("road"), "interval_min": interval,
                "recent": [round(v, 1) for v in values[-12:]], **stats.holt(values, horizon)}
    counts, profiles = (network_tools._parsed_json(v) if isinstance(v, str) else v
                        for v in (args.get("counts"), args.get("profiles")))
    return {**head, **experiments.check_calibration(kpis, counts, profiles)}


def junction_analysis(tests: dict, tool: str, args: dict) -> dict:
    """junction_los, diagnose_signal, check_run_health, emissions_report: one run's figures."""
    test = run_test(tests, str(args.get("id") or ""))
    kpis, head = test["kpis"], {"run": test["id"], "seed": test["seed"], "simulated": test["simulated"]}
    if tool == "junction_los":
        return {**head, **analysis.junction_los(kpis)}
    if tool == "diagnose_signal":
        return {**head, **analysis.diagnose_signal(kpis, str(args["junction"]) if args.get("junction") else None)}
    if tool == "check_run_health":
        return {**head, **analysis.run_health(kpis)}
    return {**head, **analysis.emissions_report(kpis)}


def resilience(tests: dict, args: dict) -> dict:
    """resilience: a disrupted run against the undisrupted one."""
    if not args.get("baseline") or not args.get("disrupted"):
        raise ValueError("Give the baseline run and the disrupted run (ids from read_results).")
    base, hit = (run_test(tests, str(args[key])) for key in ("baseline", "disrupted"))
    if base is hit:
        raise ValueError("Give two different runs: the undisrupted one and the disrupted one.")
    found = {"baseline": base["id"], "disrupted": hit["id"],
             **analysis.resilience(base["kpis"], hit["kpis"], str(args.get("measure") or "throughput"))}
    if base["seed"] != hit["seed"]:
        found["caution"] = (f"Different seeds ({base['seed']} and {hit['seed']}): part of the difference is chance. "
                            "Run the disrupted case on the baseline's seed.")
    return found


def regress(tests: dict, args: dict) -> dict:
    """regress: OLS of y on x over rows given, or over an experiment's runs (their figures and demand_scale)."""
    rows = network_tools._parsed_json(args["data"]) if isinstance(args.get("data"), str) else args.get("data")
    if args.get("experiment"):
        test = tests.get(str(args["experiment"]).upper())
        if test is None or test["kind"] != "experiment":
            raise ValueError(f"No experiment called {args['experiment']}.")
        scale = {sc["name"]: sc["demand_scale"] for sc in test["experiment"]["scenarios"]}
        rows = [{**run["measures"], "demand_scale": scale[run["scenario"]], "seed": run["seed"]}
                for run in test["experiment"]["runs"]]
    if not isinstance(rows, list) or not rows or not all(isinstance(row, dict) for row in rows):
        raise ValueError("Give data (a list of objects) or an experiment id.")
    def keyed(name: str) -> str:  # a comparison's label ("Delay") is its runs' key (delay_veh_h)
        if args.get("experiment") and not any(name in row for row in rows):
            try:
                return figure_key(name)
            except ValueError:
                return name
        return name

    y, xs = keyed(str(args.get("y"))), [keyed(str(x)) for x in network_tools._ids(args.get("x") or [])]
    logs = {keyed(name) for name in network_tools._ids(args.get("log") or [])}
    names = [y, *xs]
    missing = [name for name in names if not any(name in row for row in rows)]
    if missing:
        raise ValueError(f"No {', '.join(missing)} in the data; it has {', '.join(sorted({k for r in rows for k in r}))}.")
    table = []
    for row in rows:
        values = [row.get(name) for name in names]
        if any(not isinstance(v, (int, float)) or (name in logs and v <= 0) for v, name in zip(values, names)):
            continue
        table.append([math.log(v) if name in logs else float(v) for v, name in zip(values, names)])
    fit = stats.ols([row[1:] for row in table], [row[0] for row in table],
                    [f"ln {x}" if x in logs else x for x in xs])
    fit["y"] = f"ln {y}" if y in logs else y
    fit["rows_used"], fit["rows_skipped"] = len(table), len(rows) - len(table)
    return fit


def experiment_groups(tests: dict, identifier, figure) -> tuple[dict, str, bool | None]:
    """One figure of an experiment of this session: ({scenario: {seed: value}}, its label, higher is better)."""
    test = tests.get(str(identifier or "").upper())
    if test is None or test["kind"] != "experiment":
        raise ValueError(f"No experiment called {identifier or '(none given)'}; this session's: "
                         f"{', '.join(k for k, t in tests.items() if t['kind'] == 'experiment') or 'none yet'}.")
    experiment = test["experiment"]
    labels = {key: (label, up) for key, label, _unit, up in experiments.MEASURES + experiments.AGENT_MEASURES}
    keys = list(dict.fromkeys(key for run in experiment["runs"] for key in run["measures"]))
    wanted = str(figure or "").strip().lower()
    key = next((key for key in keys if wanted in (key.lower(), labels.get(key, (key,))[0].lower())), None)
    if key is None:
        raise ValueError(f"No figure {figure!r} in {test['id']}; it has: "
                         + ", ".join(labels.get(key, (key,))[0] for key in keys) + ".")
    groups = {spec["name"]: {} for spec in experiment["scenarios"]}
    for run in experiment["runs"]:
        groups[run["scenario"]][run["seed"]] = run["measures"].get(key)
    label, up = labels.get(key, (key, None))
    return groups, label, up


def baseline_of(names: list[str], scenario: str) -> str:
    """The baseline a scenario is compared with: the first design under the same condition ("B | rain" against
    "A | rain", not the first condition's "A | peak"), or the first scenario when there are no conditions."""
    if " | " in scenario and " | " in names[0]:
        return f"{names[0].split(' | ')[0]} | {scenario.split(' | ', 1)[1]}"
    return names[0]


def power_analysis(tests: dict, args: dict) -> dict:
    """power_analysis: seeds needed for a paired comparison, from an experiment's per-seed differences or from
    an sd and a difference given (stats.paired_power, seeds_for_power, detectable)."""
    power = float(args.get("power") or 0.8)
    if not 0.5 <= power <= 0.99:
        raise ValueError("power must be 0.5 to 0.99 (0.8 is usual).")
    found: dict = {}
    if args.get("experiment"):
        groups, label, _ = experiment_groups(tests, args["experiment"], args.get("figure") or "")
        names = list(groups)
        if len(names) < 2:
            raise ValueError("That experiment has one scenario: nothing to compare.")
        other = str(args.get("scenario") or names[1])
        baseline = baseline_of(names, other)
        if other not in groups or other == baseline or baseline not in groups:
            raise ValueError(f"scenario is one of {', '.join(names[1:])} (each is compared with {names[0]}"
                             f"{', under its own condition' if ' | ' in names[0] else ''}).")
        base = groups[baseline]
        shared = [s for s in base if s in groups[other] and None not in (base[s], groups[other][s])]
        if len(shared) < 2:
            raise ValueError("Fewer than 2 seeds ran both: run the experiment on 2 or more shared seeds first.")
        differences = [groups[other][s] - base[s] for s in shared]
        sd, observed = statistics.stdev(differences), statistics.fmean(differences)
        found.update(figure=label, baseline=baseline, scenario=other, seeds_now=len(shared),
                     observed_difference=round(observed, 3), sd_of_differences=round(sd, 3))
        difference = float(args["difference"]) if args.get("difference") is not None else observed
    else:
        if args.get("sd") is None or args.get("difference") is None:
            raise ValueError("Give an experiment (and figure), or sd and difference.")
        sd, difference = float(args["sd"]), float(args["difference"])
        if sd < 0:
            raise ValueError("sd can't be below zero.")
        found.update(sd_of_differences=sd)
    seeds = int(args.get("seeds") or found.get("seeds_now") or 0)
    found.update(target_difference=round(difference, 3), power=power,
                 seeds_needed=stats.seeds_for_power(sd, difference, power),
                 seeds_for_90_percent=stats.seeds_for_power(sd, difference, 0.9),
                 curve=[{"seeds": n, "power": round(stats.paired_power(sd, difference, n), 2)}
                        for n in (3, 5, 8, 10, 15, 20, 30) if difference])
    if seeds >= 2:
        found.update(seeds=seeds, power_with_them=round(stats.paired_power(sd, difference, seeds), 2),
                     smallest_detectable=round(stats.detectable(sd, seeds, power), 3))
    found["note"] = ("Paired t-test at 5 %, two-sided, on the per-seed differences (common random numbers). "
                     "Sized from the observed difference it is a pilot estimate: say the difference that matters.")
    if found["seeds_needed"] is None and difference:
        found["note"] += " Over 1,000 seeds: the difference is too small to find against this noise."
    return found


def benefit_cost(tests: dict, args: dict) -> dict:
    """benefit_cost: a scenario of an experiment against its baseline in money: per seed, the person-hours of delay,
    vehicle-km and CO2 saved each simulated hour, valued, over a year's hours and the appraisal years; net present
    value and benefit-cost ratio at each discount rate (7 % as NSW Treasury requires, 3.5 % the UK's, 4 % elsewhere:
    Hensher, Bus Transport, p358), with the 95 % interval of the yearly benefit over the seeds."""
    test = tests.get(str(args.get("experiment") or "").upper())
    if test is None or test["kind"] != "experiment":
        raise ValueError("Give the experiment (E1...) whose scenario to appraise.")
    experiment = test["experiment"]
    names = [spec["name"] for spec in experiment["scenarios"]]
    scenario = str(args.get("scenario") or (names[1] if len(names) > 1 else ""))
    baseline = baseline_of(names, scenario)
    if scenario not in names[1:] or scenario == baseline or baseline not in names:
        raise ValueError(f"scenario is one of {', '.join(names[1:]) or '(none: one scenario)'} (each against {names[0]}"
                         f"{', under its own condition' if ' | ' in names[0] else ''}).")
    try:
        capital = float(args["capital_cost"])
    except (KeyError, TypeError, ValueError):
        raise ValueError("Give capital_cost: what the change costs to build, in your currency.") from None
    values = {key: float(args.get(key) if args.get(key) is not None else default) for key, default in (
        ("annual_cost", 0.0), ("value_of_time_per_h", 15.0), ("cost_per_veh_km", 0.15), ("carbon_per_tonne", 100.0),
        ("hours_per_year", 500.0), ("years", 30.0))}
    rates = [float(r) for r in (args.get("discount_rates_percent") or (3.5, 4.0, 7.0))]
    if capital < 0 or values["hours_per_year"] <= 0 or not 1 <= values["years"] <= 100 or not rates \
            or any(not 0 <= r <= 20 for r in rates):
        raise ValueError("capital_cost 0 or more, hours_per_year above 0, years 1-100, discount rates 0-20 %.")
    hours = (experiment["duration_s"] - experiment.get("measure_from_s", 0.0)) / 3600  # the figures' window
    runs = {(run["scenario"], run["seed"]): run["measures"] for run in experiment["runs"]}
    yearly = []
    for seed in experiment["seeds"]:
        base, other = runs.get((baseline, seed)), runs.get((scenario, seed))
        if base is None or other is None:
            continue

        def saved(key, base=base, other=other):
            return ((base.get(key) or 0.0) - (other.get(key) or 0.0)) / hours  # a measured hour's worth

        # Everyone's delay, not only finished trips': on board (people_delay_h counts only trips that
        # ended, so a scenario letting more finish showed more delay) and waiting to get on the road
        people = "person_delay_on_board_h" if "person_delay_on_board_h" in base else "people_delay_h"
        occupancy = (base.get("people_per_h") or 0.0) / max(base.get("throughput_vph") or 0.0, 1.0) or 1.0
        hourly = ((saved(people) + saved("entry_wait_veh_h") * occupancy) * values["value_of_time_per_h"]
                  + saved("vehicle_km") * values["cost_per_veh_km"]
                  + saved("co2_kg") / 1000 * values["carbon_per_tonne"])
        yearly.append(hourly * values["hours_per_year"])
    if not yearly:
        raise ValueError("No seed ran both the baseline and that scenario.")
    mean = statistics.fmean(yearly)
    half = experiments.summary(yearly)["ci95"]

    def appraise(benefit, rate):
        factor = values["years"] if rate == 0 else (1 - (1 + rate / 100) ** -values["years"]) / (rate / 100)
        costs = capital + values["annual_cost"] * factor
        return {"rate_percent": rate, "pv_benefits": round(benefit * factor), "pv_costs": round(costs),
                "npv": round(benefit * factor - costs), "bcr": round(benefit * factor / costs, 2) if costs else None}

    by_rate = [appraise(mean, rate) for rate in rates]
    ranges = None
    if half is not None:
        ranges = [{"rate_percent": rate, "bcr_low": appraise(mean - half, rate)["bcr"],
                   "bcr_high": appraise(mean + half, rate)["bcr"]} for rate in rates]
    flips = len({row["npv"] > 0 for row in by_rate}) > 1
    served = [statistics.fmean(runs[(name, seed)].get("arrived") or 0.0 for seed in experiment["seeds"]
                               if (name, seed) in runs) for name in (baseline, scenario)]
    caution = None
    if served[0] and abs(served[1] - served[0]) > 0.05 * served[0]:  # more or fewer trips, not the same trips faster
        caution = (f"{scenario} completes {(served[1] - served[0]) / served[0]:+.0%} trips against {baseline}: its "
                   "vehicle-km and delay differ partly because it serves a different number of trips, not only "
                   "because each is faster or slower.")
    return {"experiment": test["id"], "scenario": scenario, "baseline": baseline, "seeds": len(yearly),
            **({"caution": caution} if caution else {}),
            "yearly_benefit": round(mean), "yearly_benefit_ci95": None if half is None else round(half),
            "by_discount_rate": by_rate, "bcr_interval_over_seeds": ranges, "decision_flips_with_rate": flips,
            "assumptions": {**values, "discount_rates_percent": rates},
            "note": "Benefits are person-hours of delay (everyone on board, finished or not, and waiting to enter), "
                    "vehicle-km and car CO2 saved over the measured hours, after any warm-up (conflicts aren't "
                    "crashes, so "
                    "they aren't valued). value_of_time_per_h, cost_per_veh_km, carbon_per_tonne and "
                    "hours_per_year (default 500: two peak hours, 250 days) are assumptions: give your own."}


def rank_scenarios(tests: dict, args: dict) -> dict:
    """rank_scenarios: all of an experiment's scenarios on one figure (stats.anova, friedman, all_pairs)."""
    groups, label, up = experiment_groups(tests, args.get("experiment"), args.get("figure"))
    if len(groups) < 2:
        raise ValueError("That experiment has one scenario: nothing to rank.")
    overall = stats.anova(groups)
    ranked = stats.all_pairs(groups, higher_is_better=up)
    friedman = stats.friedman(groups)
    r = lambda value: round(value, 4) if isinstance(value, float) else value  # noqa: E731
    answer = {"figure": label, "higher_is_better": up,
              "anova": None if overall is None else {"design": overall["design"], "f": r(overall["f"]),
                                                     "p": r(overall["p"])},
              "friedman": None if friedman is None else {k: r(v) if not isinstance(v, dict) else v
                                                         for k, v in friedman.items()}}
    if friedman is None:
        answer["kruskal_wallis"] = stats.kruskal_wallis({name: list(values.values()) for name, values in groups.items()})
    if ranked:
        answer["ranked"] = [{**row, "mean": r(row["mean"])} for row in ranked["ranked"]]
        answer["pairs"] = [{k: r(v) for k, v in pair.items()} for pair in ranked["pairs"]]
        answer["confidence_each"] = ranked["confidence_each"]
    answer["note"] = ("Scenarios sharing a letter can't be told apart. Friedman ranks within each seed, so it holds "
                      "when the figure is skewed or the seeds few; trust it over ANOVA when they disagree.")
    return answer


def seconds(value) -> float | None:
    """A sheet's time: seconds, or a clock "HH:MM:SS"."""
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value.count(":") == 2:
        hours, minutes, seconds = value.split(":")
        return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    return None


def decision_rows(test: dict) -> list[dict]:
    """A run's agent decisions (TSP's and DEVL's sheets) as dicts, each with its extension and when it was asked."""
    found = []
    for name, rows in (test["kpis"].get("decision_sheets") or {}).items():
        for row in rows[1:]:
            item = dict(zip(rows[0], row))
            asked = item.get("Asked at", item.get("Asked"))
            found.append({"extension": "DEVL" if name.startswith("DEVL") else "TSP",
                          "asked_s": seconds(asked), **item})
    return found


def read_decisions(tests: dict, args: dict) -> dict:
    """read_decisions: the agent decisions of a run of this session, filtered by junction and time."""
    test = run_test(tests, str(args.get("id") or ""))
    rows = decision_rows(test)
    if not rows:
        return {"run": test["id"], "decisions": [], "note": "No agent decisions in that run: tick TSP or DEVL "
                                                             "(set_extension) before run_simulation."}
    junction = str(args.get("junction") or "")
    start = float(args["from_min"]) * 60 if args.get("from_min") is not None else None
    end = float(args["to_min"]) * 60 if args.get("to_min") is not None else None
    picked = [row for row in rows if (not junction or row.get("Junction") == junction)
              and (start is None or (row["asked_s"] or 0) >= start) and (end is None or (row["asked_s"] or 0) <= end)]
    limit = max(1, min(int(args.get("limit") or 20), 100))
    shown = []
    for row in picked[:limit]:
        item = {key: value for key, value in row.items() if value not in (None, "")}
        if isinstance(item.get("Telemetry read"), str):
            item["Telemetry read"] = json.loads(item["Telemetry read"])
        shown.append(item)
    return {"run": test["id"], "decisions": shown, "matching": len(picked), "in_run": len(rows),
            "note": "Outcome is what came of each request (served, not needed, blocked...): a hindsight check of "
                    "the answer. TSP's rows carry the telemetry the agent read and the person-delay estimate."}


def figure_key(figure: str) -> str:
    """A compared figure's key from its key or label (experiments.MEASURES)."""
    wanted = str(figure).strip().lower()
    for key, label, *_ in experiments.MEASURES:
        if wanted in (key.lower(), label.lower()):
            return key
    raise ValueError(f"objective is one of: {', '.join(label for _, label, *_ in experiments.MEASURES)}.")


def figure_label(key: str) -> tuple[str, bool | None]:
    return next(((label, up) for k, label, _unit, up in experiments.MEASURES if k == key), (key, None))


def factorial(runs, designs, factor_names, conditions, key, label) -> dict:
    """Two factors' main effects and interaction on the objective (stats.two_way_anova), each seed a replicate;
    per condition when there are conditions."""
    def level(name: str, factor: str) -> str:
        return dict(part.split("=", 1) for part in name.split(", "))[factor]

    found = {}
    for condition in conditions or [None]:
        cells: dict = defaultdict(list)
        for design in designs:
            scenario = design.name if condition is None else f"{design.name} | {condition['name']}"
            cells[(level(design.name, factor_names[0]), level(design.name, factor_names[1]))] += [
                run["measures"].get(key) for run in runs if run["scenario"] == scenario]
        result = stats.two_way_anova(dict(cells))
        if result is not None:
            result = {factor_names[0] if k == "A" else factor_names[1] if k == "B" else k: v for k, v in result.items()}
        found[condition["name"] if condition else "all"] = result
    return {"objective": label, "factors": factor_names, "anova": found,
            "note": "An interaction p under 0.05: one factor's effect depends on the other's level, so compare the "
                    "cell means, not the factors one at a time."}


LEAD_FIGURES = ("Delay", "Mean trip time", "Throughput", "Unfinished trips")  # an experiment's answer leads with


def compact(comparison: list[dict], objective: str | None = None) -> list[dict]:
    """An experiment's comparison, rounded, for the model: the objective's figure and LEAD_FIGURES, each with its
    unit (some 100 rows of every figure buried the one asked about for a small model; the workbook has them all).
    A difference is called better or worse only when clear; else "not told apart"."""
    def r(value):
        return round(value, 2) if isinstance(value, float) else value
    lead = [objective, *LEAD_FIGURES] if objective else list(LEAD_FIGURES)
    kept = [row for row in comparison if row["figure"] in lead]
    kept.sort(key=lambda row: lead.index(row["figure"]))
    rows = []
    for row in kept or comparison:
        item = {"figure": row["figure"], "unit": row.get("unit"), "scenario": row["scenario"], "mean": r(row["mean"]),
                "ci95": r(row["ci95"]), "n": row["n"], "replications_needed": row.get("replications_needed")}
        if "vs_baseline_diff" in row:
            clear = row["vs_baseline_clear"]
            item.update(diff=r(row["vs_baseline_diff"]), diff_ci=r(row["vs_baseline_ci"]),
                        p=None if row["vs_baseline_p"] is None else round(row["vs_baseline_p"], 4),
                        interval_level=round(row["confidence"], 4), test=row["vs_baseline_test"],
                        change_percent=r(row["vs_baseline_percent"]), clear=clear,
                        **({"better": row.get("better")} if clear and row.get("better") is not None
                           else {"verdict": "not told apart"} if clear is False else {}))
            if row.get("vs_baseline_wilcoxon_p") is not None:
                item["wilcoxon_p"] = round(row["vs_baseline_wilcoxon_p"], 4)
            if row.get("vs_baseline_p_holm") is not None:  # adjusted across the ~20 figures tested at once
                item["p_adjusted"] = round(row["vs_baseline_p_holm"], 4)
            if row.get("speed_risk"):
                item["speed_risk"] = row["speed_risk"]
        if row.get("anova"):
            item["anova_p"] = row["anova"]["p"]
        rows.append(item)
    return rows

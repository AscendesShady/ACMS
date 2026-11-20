"""MainWindow's experiments: the assistant's run tools, dispatched by _runs_command (argument checks, headless tests
and experiments on the window's current network and demand, and the shape of their answers). Cut out of main.py's
MainWindow as a plain mixin: a file to work in, not a decoupled module. Its state (tests, last_results ...) is made
in MainWindow.__init__, and it calls the rest of the window through self (_pair_demand, _snapshot, _ai_edited,
_one_run, _quick_run, _compliance ...)."""

from __future__ import annotations

import json
import statistics
from collections import defaultdict
from copy import deepcopy
from dataclasses import replace
from datetime import datetime

from traffic_simulator import (accessibility, analysis, design_checks, experiments, results, run_reports, searches,
                               stats, tool_args, tool_schemas, weaving)
from traffic_simulator.lane_graph import Graph
from traffic_simulator.models import route_choice_problem
from traffic_simulator.network_io import MAX_SEED
from traffic_simulator.simulation import Simulation
from traffic_simulator.demand import PairDemand


def _rounded(value, digits: int = 3):
    """Floats rounded throughout a result (dicts, lists), for the model to read."""
    if isinstance(value, float):
        return round(value, digits)
    if isinstance(value, dict):
        return {key: _rounded(item, digits) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_rounded(item, digits) for item in value]
    return value


class ExperimentsMixin:
    """The assistant's run tools (_runs_command) and the window side of headless tests and experiments."""

    # What it uses of the window and doesn't define: MainWindow's state and the other mixins' methods
    # (tests/test_docs.py keeps this exact, so a new dependency shows in the diff)
    NEEDS = ('_ai_edited', '_build_scenario', '_compliance', '_deciding', '_decision_sensitivity',
             '_emergency_fleet', '_extension', '_has_buses', '_in_background', '_institution_split', '_journal_sheet',
             '_metro_report', '_one_run', '_pair_demand', '_pooled', '_quick_run', '_refresh_summary', '_results',
             '_run_settings', '_schedule_emergency', '_snapshot', '_testing', '_waiting_agent', 'ai_mode',
             'extensions', 'last_kpis', 'last_results', 'network', 'od_matrix', 'replay_run', 'results_folder',
             'runner', 'seed_box', 'tests', 'vehicle_modes')

    def _check_demand(self, pairs: list) -> tuple[float | None, dict]:
        """The Demand Generator's demand, just sized to a degree of saturation, run headless (its busiest hour,
        the Seed box's seed, the ticked extensions): the share of `pairs`' demand that holds, the whole of it
        or found by bisection (experiments.largest_holding), and a report. None: not checked. Raises
        ValueError if none tried holds."""
        if self.runner is not None or self._testing:
            return None, {"checked": False, "note": "Not checked in a run: one is going. Stop it, then check with "
                                                    "run_simulation."}
        scope, seed = set(pairs), self.seed_box.value()
        demand = experiments.at_busiest_hour(self._pair_demand())
        network, modes = deepcopy(self.network), deepcopy(self.vehicle_modes.modes)
        override = self.extensions.signal_override(agent=self._waiting_agent())
        ran = [experiments.hold_minutes(demand)]  # how long each share's check runs (its busiest quarter can move)

        def check(share, progress, cancelled):
            sized = [replace(pair, hourly=pair.hourly * share) if (pair.origin, pair.destination) in scope else pair
                     for pair in demand]
            minutes = experiments.hold_minutes(sized)
            ran.append(minutes)
            return experiments.holds_by_majority(  # a borderline verdict confirmed on two more seeds
                lambda s: experiments.holds(network, sized, modes, s, override, cancelled=cancelled, progress=lambda t: progress(
                    f"{share:.0%} of the demand, seed {s}, {results.clock(t)} of {results.clock(minutes * 60)}")), seed)

        share, tried = self._in_background(lambda progress, cancelled: experiments.largest_holding(
            lambda s: check(s, progress, cancelled)), "Checking the demand in a run")
        report = {"simulated_min": max(ran), "seed": seed, "tried": [
            {"share": f"{s:.0%}", "held": r["holds"], "build_up": r.get("build_up")} for s, r in tried]}
        if share is None:
            return None, {**report, "checked": False, "note": "Stopped by the user: not checked."}
        if share == 0:
            controls = [node.control for node in self.network.nodes.values()]
            giving_way = controls.count("uncontrolled")
            advice = (f" {giving_way} of its {len(controls)} junctions only give way (an OpenStreetMap network marks few "
                      "signals): signalising the busiest (set_junction_control, control 'signalised'), or main roads "
                      "only (network_from_background, road_classes 'main'), may let demand hold."
                      if giving_way > len(controls) / 2 else "")
            raise ValueError(f"The simulation jams even at {tried[-1][0]:.0%} of that demand: vehicles pile up."
                             f"{advice} Look at the network (run_simulation, read_traffic) before sizing demand.")
        note = ("It held in the run." if share == 1 else
                f"At that size the simulation jammed (vehicles piled up), so {share:.0%} of it is kept: the largest "
                "share that held. The capacity model's saturation figures are for the size before this check.")
        return share, {**report, "checked": True, "kept": f"{share:.0%}", "note": note}

    # run_reports' answers on this session's runs and experiments: each (tests, args), named as its tool
    _REPORTS = ("resilience", "power_analysis", "rank_scenarios", "read_decisions", "benefit_cost", "regress")
    _RUN_ANALYSES = ("analyse_run", "read_detectors", "analyse_safety", "forecast", "check_calibration")
    _JUNCTION_ANALYSES = ("junction_los", "diagnose_signal", "check_run_health", "emissions_report")
    # The tools answered at once, a run going on or not: tool -> (window, args, check_only) -> its answer
    _ANSWERS = {
        "institution_mode_split": lambda self, args, check_only: self._institution_split(args, check_only),
        "read_results": lambda self, args, check_only: run_reports.read_results(
            self.tests, str(args.get("id") or ""), self._results),
        "compare_runs": lambda self, args, check_only: run_reports.compare_runs(
            self.tests, tool_args._ids(args.get("runs") or [])),
        "preflight_check": lambda self, args, check_only: self._preflight(args, check_only),
        "test_agent_compliance": lambda self, args, check_only: self._compliance(args, check_only),
        "decision_sensitivity": lambda self, args, check_only: self._decision_sensitivity(args, check_only),
        "critical_links": lambda self, args, check_only: self._critical_links(args),
        "design_audit": lambda self, args, check_only: design_checks.audit(self.network, self.vehicle_modes.modes),
        "weaving_los": lambda self, args, check_only: self._weaving_los(args),
        "metro_report": lambda self, args, check_only: self._metro_tool(),
        "accessibility_report": lambda self, args, check_only: self._accessibility(args),
        "set_extension": lambda self, args, check_only: self._set_extension(args, check_only),
        "read_extensions": lambda self, args, check_only: self._read_extensions(),
        "schedule_emergency": lambda self, args, check_only: self._schedule_emergency(args, check_only),
        "replay_run": lambda self, args, check_only: self._replay(args, check_only),
        "start_batch": lambda self, args, check_only: self._start_batch(args, check_only),
    }
    # The headless tests, once nothing else runs and the duration holds: tool -> its method (args, demand, modes,
    # duration in minutes, check_only); any other name is run_experiment's
    _TESTS = {"assign_routes": "_assign_routes", "optimise_signals": "_optimise_signals",
              "parameter_sensitivity": "_parameter_sensitivity", "run_simulation": "_run_simulation"}

    def _runs_command(self, tool: str, args: dict, check_only: bool = False) -> dict:
        """AI Mode's network-testing tools: headless runs and experiments, their results, the extensions."""
        if tool in self._ANSWERS:
            return self._ANSWERS[tool](self, args, check_only)
        if tool in self._REPORTS:
            return getattr(run_reports, tool)(self.tests, args)
        if tool in self._RUN_ANALYSES:
            return run_reports.analyse_run(self.tests, tool, args)
        if tool in self._JUNCTION_ANALYSES:
            return run_reports.junction_analysis(self.tests, tool, args)
        if self.runner is not None or self._testing:
            raise ValueError("A run is going on: stop it first (the menu bar's Stop), then test.")
        duration = float(args.get("duration_min") or (30 if tool in ("assign_routes", "build_scenario") else
                                                      20 if tool == "parameter_sensitivity" else 0))
        if not 1 <= duration <= experiments.MAX_MINUTES:
            raise ValueError(f"duration_min must be 1 to {experiments.MAX_MINUTES} simulated minutes.")
        if not 0 <= float(args.get("measure_from_min") or 0) < duration:
            raise ValueError("measure_from_min must be 0 or more and less than duration_min.")
        if tool == "build_scenario":
            return self._build_scenario(args, check_only)
        modes = deepcopy(self.vehicle_modes.modes)
        # A test (at most MAX_MINUTES) of daily demand runs its busiest hour, as the demand check sizes it: from
        # midnight it would see the night's few trips and report no congestion
        demand = experiments.at_busiest_hour(self._pair_demand())
        return getattr(self, self._TESTS.get(tool, "_experiment"))(args, demand, modes, duration, check_only)

    def _critical_links(self, args: dict) -> dict:
        demand = experiments.at_busiest_hour(self._pair_demand())
        flows = {(pair.origin, pair.destination): pair.hourly for pair in demand}
        area = tool_args._parsed_json(args["area"]) if isinstance(args.get("area"), str) else args.get("area")
        return design_checks.critical_links_plan(self.network, flows, args.get("limit") or 10, area)

    def _metro_tool(self) -> dict:
        if not self.network.metro_lines:
            raise ValueError("No MRT lines: deploy one first (Edit Network > MRT).")
        return self._metro_report()

    def _accessibility(self, args: dict) -> dict:
        measured = None
        if args.get("id"):
            observed = run_reports.run_test(self.tests, str(args["id"]))["kpis"]["observed"]
            measured = {link: mean for link, (count, mean) in observed.get("traversals", {}).items() if count >= 3}
        budgets = tuple(float(b) for b in (args.get("budgets_min") or (15, 30)))
        return accessibility.report(self.network, measured, budgets, float(args.get("beta_per_min") or 0.1),
                                    str(args["close_road"]) if args.get("close_road") else None)

    def _set_extension(self, args: dict, check_only: bool) -> dict:
        extension = self.extensions.find(str(args.get("name", "")))
        settings = args.get("settings") or {}
        if isinstance(settings, str):
            settings = tool_args._parsed_json(settings)
        self.extensions.set_settings(extension, settings if isinstance(settings, dict) else {}, check_only=True)
        if check_only:
            return {"ok": True}
        extension.check.setChecked(tool_args._bool(args.get("on")))
        self.extensions.set_settings(extension, settings if isinstance(settings, dict) else {})
        return {"extension": extension.name, "ticked": extension.on, "settings": dict(self.extensions.settings())[extension.name],
                "description": getattr(extension.module, "DESCRIPTION", "") or (extension.module.__doc__ or "").split("\n\n")[0]}

    def _read_extensions(self) -> dict:
        devl = self._extension("dynamic_emergency_lane")  # its Dispatch Emergency Vehicle section's schedules
        return {"extensions": self.extensions.describe(),
                "emergency_schedules": [{"from": origin, "to": destination, "every_min": every, "after_min": after}
                                        for origin, destination, every, after in (devl.module.dispatch.fleet() if devl else [])]}

    def _replay(self, args: dict, check_only: bool) -> dict:
        # A workbook in the results folder (a name alone: there); whether a file is anywhere else isn't told
        given, folder = str(args.get("path") or ""), self.results_folder.resolve()
        path = (folder / given).resolve()
        if not path.is_relative_to(folder) or not path.is_file():
            raise ValueError(f"No workbook at {given} in the results folder ({folder}): give the path of a batch "
                             "run's workbook (.xlsx).")
        return {"ok": True} if check_only else self.replay_run(path)

    def _start_batch(self, args: dict, check_only: bool) -> dict:
        batch = self._extension("batch_run")
        if batch is None:
            raise ValueError("The Batch Run extension didn't load: see the Extensions tab.")
        tests = [kind for kind in ("TSP", "DBL", "DEVL") if tool_args._bool(args.get(f"test_{kind.lower()}"))]
        compare = tool_args._bool(args["compare_default"]) if args.get("compare_default") is not None else None
        if check_only:  # what run_batch checks, before the approval card
            if self.runner is not None or self._testing:
                raise ValueError("A run or test is going on: stop it first (the menu bar's Stop), then start the batch.")
            if not self._pair_demand():
                raise ValueError("There's no demand to run: generate or enter some first.")
            if "DEVL" in tests:
                self._emergency_fleet()
            if "DBL" in tests and not self._has_buses():
                raise ValueError("Test DBL clears a lane for buses, and no vehicle type is a bus: add one first.")
        try:
            minutes = int(float(args.get("duration_min") or 0))
        except (TypeError, ValueError):
            raise ValueError("duration_min is a whole number of minutes.") from None
        seeds = args.get("seeds") or ""  # a list as the schema asks, or text like "1-4" as the panel takes
        seeds = ", ".join(map(str, experiments.seeds_from(seeds))) if isinstance(seeds, list) else str(seeds)
        return batch.module.start(seeds, minutes, tests, check_only, default=compare)

    def _run_simulation(self, args: dict, demand, modes, duration: float, check_only: bool) -> dict:
        peak_hour = experiments.busiest_hour(self._pair_demand())
        if not demand:
            raise ValueError(f"There's no demand to test with: the OD matrix has no {self.od_matrix.mode} demand "
                             "(a Run uses the kind it shows). Enter or generate some first.")
        seed = int(args["seed"]) if args.get("seed") is not None else self.seed_box.value()
        if not 0 <= seed <= MAX_SEED:
            raise ValueError(f"seed must be 0 to {MAX_SEED:,}.")
        incidents = experiments.incidents_from(tool_args._parsed_json(args["incidents"])
                                               if isinstance(args.get("incidents"), str) else args.get("incidents"))
        weather = str(args.get("weather") or "clear").lower()
        meters = experiments.meters_from(tool_args._parsed_json(args["meters"])
                                         if isinstance(args.get("meters"), str) else args.get("meters"))
        lateral = args.get("lateral") or False
        if not isinstance(lateral, bool):
            raise ValueError("lateral must be true or false.")
        experiments.apply_conditions(Simulation(self.network, [], modes), incidents, weather, meters)  # checked
        if check_only:
            return {"ok": True}
        network, override = deepcopy(self.network), self.extensions.signal_override(agent=self._waiting_agent())
        model = self._deciding()[0]
        settings = self._run_settings(None, seed=seed, extra=[("Kind", "headless test (run_simulation, by the assistant)"),
                                                                ("Duration (simulated)", results.clock(duration * 60)),
                                                                ("AI model deciding TSP", model),
                                                                ("Weather", weather),
                                                                ("Lateral position (sublane)", "on" if lateral else "off"),
                                                                ("Incidents", json.dumps(incidents) if incidents else "none"),
                                                                ("Ramp meters", json.dumps(meters) if meters else "none")])
        kpis, path, simulated = self._in_background(
            lambda progress, cancelled: self._one_run(network, demand, modes, seed, duration * 60, override, settings,
                                                      self._journal_sheet(), "Testing the network", progress, cancelled,
                                                      model=model, incidents=incidents, weather=weather,
                                                      meters=meters, lateral=lateral,
                                                      measure_from_s=float(args.get("measure_from_min") or 0) * 60),
            "Testing the network")
        run_id = f"R{sum(key.startswith('R') for key in self.tests) + 1}"
        self.tests[run_id] = {"id": run_id, "kind": "run", "what": "the network as built", "seed": seed,
                              "simulated": results.clock(simulated), "kpis": kpis, "file": str(path or "")}
        if path:
            self.last_results, self.last_kpis = path, kpis
        self._refresh_summary()
        answer = {"run": run_id, "seed": seed, "simulated": results.clock(simulated), "file": str(path or ""),
                  **experiments.headline(kpis), **({"agent_timing": experiments.AGENT_TIMING} if override is not None else {})}
        health = analysis.run_health(kpis)  # a broken run said at once, with what to do (ChatSUMO's recovery loop)
        answer["health"] = "fine" if health["healthy"] else health["problems"]
        if peak_hour is not None:
            answer["demand_simulated"] = (f"daily demand at its busiest hour, {peak_hour:02d}:00-{peak_hour + 1:02d}:00, "
                                          "held for the whole run")
        if simulated < duration * 60 - 1:
            answer["note"] = "Stopped by the user before the end: the figures are of the part simulated."
        return answer

    def _assign_routes(self, args, demand, modes, duration, check_only) -> dict:
        """assign_routes: route choice on the network, its link times by the method of successive averages over
        headless runs (Handbook of Transportation Science p391–392): each iteration runs with the times so far,
        measures each link's traversal time (and each move's: Graph.move_time), and moves the times 1/n of the way
        to it."""
        if tool_args._bool(args.get("off", False)):
            if check_only:
                if not self.network.route_choice:
                    raise ValueError("Nothing would change: route choice is off already.")
                return {"ok": True}
            self.network.route_choice = {}
            self._ai_edited("assign_routes")
            return {"route_choice": "off", "note": "Every vehicle takes the fastest route again."}
        iterations = int(args.get("iterations", 4) if args.get("iterations") is not None else 4)
        choice = {"theta_per_min": float(args.get("theta_per_min") or 0.5), "routes": int(args.get("routes") or 3),
                  "link_times_s": {}}
        problem = None if 0 <= iterations <= 10 else "iterations must be 0 to 10."
        problem = problem or route_choice_problem(choice)
        if problem:
            raise ValueError(problem)
        if not 1 <= duration <= experiments.MAX_MINUTES:
            raise ValueError(f"duration_min must be 1 to {experiments.MAX_MINUTES} simulated minutes.")
        if iterations and not demand:
            raise ValueError("There's no demand to assign: enter or generate some first (or give iterations 0).")
        if check_only:
            return {"ok": True}
        seed = int(args["seed"]) if args.get("seed") is not None else self.seed_box.value()
        until_gap = float(args["until_gap"]) if args.get("until_gap") is not None else None
        if until_gap is not None and not 0 < until_gap <= 0.5:
            raise ValueError("until_gap must be above 0 and at most 0.5.")
        override = self.extensions.signal_override(agent=self._waiting_agent())
        network, before = deepcopy(self.network), self._snapshot()
        choice, history = self._in_background(
            lambda progress, cancelled: searches.equilibrium(network, demand, modes, seed, duration, iterations, choice,
                                                             override, progress, cancelled, until_gap,
                                                             quick_run=self._quick_run), "Assigning routes")
        if self._snapshot() != before:  # edited, undone or another project opened while it ran
            raise ValueError("The network or demand changed while routes were assigned, so nothing was applied: "
                             "assign them again.")
        rates = {(pair.origin, pair.destination): pair.hourly for pair in experiments.at_busiest_hour(demand)}
        self.network.route_choice = choice
        self._ai_edited("assign_routes")
        graph = Graph(self.network)
        busiest = sorted(rates, key=lambda pair: -rates[pair])[:5]
        shares = {f"{o}->{d}": [{"share": round(share, 3), "via": " ".join([o] + [link.end for link in path])}
                                for share, path in graph.route_shares(o, d)] for o, d in busiest}
        gaps = [step["relative_gap"] for step in history if step["relative_gap"] is not None]
        return {"route_choice": {k: v for k, v in choice.items() if k not in ("link_times_s", "turn_times_s")},
                "link_times_measured": len(choice["link_times_s"]),
                "turn_times_measured": sum(map(len, choice.get("turn_times_s", {}).values())), "iterations": history, "busiest_pairs": shares,
                **({"converged": bool(gaps) and gaps[-1] <= until_gap} if until_gap is not None else {}),
                "note": "Every run now draws each vehicle's route by these shares (saved with the network; Ctrl+Z "
                        "undoes it). The relative gap is the share of the flow off its logit share: towards 0, the routes are settling "
                        "(stochastic user equilibrium)."}

    OBJECTIVES = {"delay": ("delay_veh_h", False), "trip_delay": ("mean_trip_delay_s", False),
                  "throughput": ("throughput_vph", True), "conflicts": ("conflicts", False)}

    def _optimise_signals(self, args, demand, modes, duration, check_only) -> dict:
        """optimise_signals: a genetic algorithm over fixed-time settings (Explainable AI for ITS p146–158:
        signal settings searched with the simulation as fitness). A genome per junction: its cycle and phase
        shares; greens = minimum green + share × (cycle − phases × (minimum green + amber + all-red)). Tournament
        selection, blend crossover, Gaussian mutation, one elite; every candidate scored on the same seeds."""
        import random  # noqa: PLC0415
        from traffic_simulator.signal_timing import ALL_RED_S, AMBER_S, MIN_GREEN_S  # noqa: PLC0415

        junctions = tool_args._ids(args.get("junctions") or [])
        for node_id in junctions:
            node = self.network.nodes.get(node_id)
            if node is None or node.control != "signalised":
                raise ValueError(f"{node_id} isn't a signalised junction.")
        if not junctions:
            raise ValueError("Give the signalised junctions to optimise.")
        seeds = experiments.seeds_from(args.get("seeds") or "1-3")
        population, generations = int(args.get("population") or 6), int(args.get("generations") or 3)
        objective = str(args.get("objective") or "delay")
        if objective not in self.OBJECTIVES:
            raise ValueError(f"objective must be one of {', '.join(self.OBJECTIVES)}.")
        if not 4 <= population <= 10 or not 1 <= generations <= 5 or not 1 <= len(seeds) <= 5:
            raise ValueError("population 4-10, generations 1-5, seeds 1-5.")
        runs = (population * generations + 1) * len(seeds)
        if runs > 120:
            raise ValueError(f"That is up to {runs} runs; at most 120: fewer seeds, a smaller population or fewer generations.")
        if not demand:
            raise ValueError("There's no demand to test with: enter or generate some first.")
        if check_only:
            return {"ok": True}
        key, higher = self.OBJECTIVES[objective]
        base = deepcopy(self.network)  # the search's own copy: an edit meanwhile never reaches its thread
        plans = Simulation(deepcopy(base), demand, modes).signal_plans
        nodes = {node_id: base.nodes[node_id] for node_id in junctions}
        rng, override = random.Random(seeds[0]), self.extensions.signal_override(agent=self._waiting_agent())

        def bounds(node):
            phases = node.signal_phases
            low = max(node.cycle_min_s, phases * (MIN_GREEN_S + AMBER_S + ALL_RED_S) + 5)
            return low, max(low + 1, node.cycle_max_s)

        def greens_of(node, cycle, shares):
            phases, spare = node.signal_phases, cycle - node.signal_phases * (MIN_GREEN_S + AMBER_S + ALL_RED_S)
            total = sum(shares) or 1.0
            return [round(MIN_GREEN_S + spare * share / total, 1) for share in shares[:phases]]

        def start(node_id):
            node, plan = nodes[node_id], plans.get(node_id, {})
            greens = list(plan.get("greens_s") or plan.get("green_times_s") or node.green_times_s)[:node.signal_phases]
            while len(greens) < node.signal_phases:
                greens.append(greens[-1] if greens else 20.0)
            low, high = bounds(node)
            cycle = min(max(sum(greens) + node.signal_phases * (AMBER_S + ALL_RED_S), low), high)
            return [cycle] + [max(g - MIN_GREEN_S, 0.5) for g in greens]

        def changes(genome):
            return [{"tool": "set_signal_timing", "args": {"junctions": [node_id], "method": "fixed",
                                                           "green_times_s": greens_of(nodes[node_id], gene[0], gene[1:])}}
                    for node_id, gene in zip(junctions, genome)]

        cache, values_of = {}, {}

        def token_of(genome) -> str:
            return repr([[round(g, 1) for g in gene] for gene in genome])

        def network_of(genome):
            return experiments.scenario_network(base, experiments.Scenario("candidate", changes(genome)),
                                                tool_schemas.SCENARIO_TOOLS) if genome else deepcopy(base)

        def keep(token, per_seed) -> None:
            values = [v for v in per_seed.values() if v is not None]
            mean = statistics.fmean(values) if values else None
            cache[token] = None if mean is None else (-mean if higher else mean)
            values_of[token] = per_seed

        def together(genomes, progress, cancelled, label) -> None:
            """The candidates not scored yet, every seed of each, side by side, a process each (plain runs:
            _pooled); score() then finds them. With an extension deciding, or no processes: nothing, and score()
            runs them one by one."""
            fresh = {token_of(genome): genome for genome in genomes}
            fresh = {token: genome for token, genome in fresh.items() if token not in cache}
            jobs = [{"network": network, "demand": demand, "modes": modes, "seed": seed, "duration_s": duration * 60,
                     "label": f"{label}, seed {seed}"}
                    for network in map(network_of, fresh.values()) for seed in seeds]
            if override is not None or len(jobs) < 2:
                return
            made = self._pooled(jobs, progress, cancelled, work=experiments.quiet_run)
            if made is None or cancelled() or any(one is None for one in made):
                return
            for index, token in enumerate(fresh):
                keep(token, {seed: made[index * len(seeds) + at].get(key) for at, seed in enumerate(seeds)})

        def score(genome, progress, cancelled, label):
            token = token_of(genome)
            if token not in cache:
                network, per_seed = network_of(genome), {}
                for seed in seeds:
                    if cancelled():
                        return None
                    per_seed[seed] = self._quick_run(network, demand, modes, seed, duration * 60, override, progress,
                                                     cancelled, f"{label}, seed {seed}").get(key)
                keep(token, per_seed)
            return cache[token]

        def mutate(genome):
            out = []
            for node_id, gene in zip(junctions, genome):
                low, high = bounds(nodes[node_id])
                cycle = gene[0] + (rng.gauss(0, (high - low) * 0.15) if rng.random() < 0.4 else 0)
                shares = [max(0.5, g + (rng.gauss(0, 5) if rng.random() < 0.4 else 0)) for g in gene[1:]]
                out.append([min(max(cycle, low), high)] + shares)
            return out

        def work(progress, cancelled):
            baseline = score([], progress, cancelled, "Current plans")
            first = [start(node_id) for node_id in junctions]
            people = [first] + [mutate(first) for _ in range(population - 1)]
            history = []
            for generation in range(1, generations + 1):
                scored = []
                together(people, progress, cancelled, f"Generation {generation}")
                for index, genome in enumerate(people, 1):
                    value = score(genome, progress, cancelled, f"Generation {generation}, candidate {index}")
                    if value is None:
                        return baseline, scored, history
                    scored.append((value, genome))
                scored.sort(key=lambda item: item[0])
                history.append({"generation": generation, "best": round(abs(scored[0][0]), 3)})
                if generation == generations:
                    return baseline, scored, history

                def pick():
                    a, b = rng.sample(scored, 2)
                    return min(a, b, key=lambda item: item[0])[1]

                children = [scored[0][1]]
                while len(children) < population:
                    mother, father = pick(), pick()
                    weight = rng.random()
                    child = [[weight * m + (1 - weight) * f for m, f in zip(gm, gf)] for gm, gf in zip(mother, father)]
                    children.append(mutate(child))
                people = children
            return baseline, [], history

        baseline, scored, history = self._in_background(work, "Optimising signals")
        if not scored or baseline is None:
            raise ValueError("Stopped before any candidate was scored.")
        best_value, best = scored[0]
        token = repr([[round(g, 1) for g in gene] for gene in best])
        compared = stats.paired(values_of[repr([])], values_of[token])
        apply = [change["args"] for change in changes(best)]
        return {"objective": key, "higher_is_better": higher, "candidates_scored": len(cache) - 1,
                "runs": len(cache) * len(seeds), "seeds": seeds,
                "current": round(abs(baseline), 3), "best": round(abs(best_value), 3),
                "difference": None if compared["diff"] is None else {"diff": round(compared["diff"], 3),
                                                                     "ci95": round(compared["ci"], 3), "p": compared["p"],
                                                                     "clear": compared["clear"]},
                "best_plans": {a["junctions"][0]: {"green_times_s": a["green_times_s"],
                                                   "cycle_s": round(sum(a["green_times_s"]) + len(a["green_times_s"]) * (AMBER_S + ALL_RED_S), 1)}
                               for a in apply},
                "apply_with": [{"tool": "set_signal_timing", "args": a} for a in apply], "history": history,
                "note": "Apply only if the difference is clear and better; check with run_experiment on new seeds "
                        "(the search fits these seeds). The open network is unchanged.",
                **({"agent_timing": experiments.AGENT_TIMING} if override is not None else {})}

    def _weaving_los(self, args: dict) -> dict:
        """weaving_los: the HCM 2000 Ch 24 on every weaving segment, its movements from the demand (the busiest hour,
        each pair's vehicles in PCU by its split, at its peak quarter: V / PHF, Eq 24-1) on the routes a Run takes; a
        run's own speed and density beside it when one is given."""
        modes = self.vehicle_modes.modes
        rates: dict = defaultdict(float)
        for pair in experiments.at_busiest_hour(self._pair_demand()):
            pcu = sum(share / 100 * float(getattr(mode, "pcu", 1.0)) for share, mode in zip(pair.split, modes)) or 1.0
            peak = 4 * max(pair.quarters) if getattr(pair, "quarters", ()) else 1.0
            rates[(pair.origin, pair.destination)] += (pair.hourly or 0.0) * pcu * peak
        rows = weaving.report(Graph(self.network), dict(rates))
        found = {"segments": rows, "method": "HCM 2000 Chapter 24 (Eqs 24-1 to 24-6, Exhibits 24-2, 24-5 to 24-8)",
                 "flows": "pc/h at each pair's peak 15 minutes, the busiest hour, on the routes a Run takes"}
        if not rows:
            found["note"] = ("No weaving segments: no uncontrolled merge followed within 750 m by a diverge, with the "
                             "second road joining and leaving on the kerb side.")
        if args.get("id"):
            test = run_reports.run_test(self.tests, str(args["id"]))
            kpis, observed = test["kpis"], test["kpis"].get("observed", {})
            hours = float(observed.get("observed_s") or kpis.get("time_s") or 0.0) / 3600
            for row in rows:
                row.update(weaving.measured(row, observed, hours))
            found["run"] = test["id"]
        return found

    def _equilibrate_demand(self, flows: dict, args: dict, check_only: bool = False) -> tuple[dict, dict]:
        """equilibrate_demand's numbers: elastic demand (induced or suppressed traffic; Rodrigue, Geography of Transport
        Systems, and the HCM's demand elasticity): each pair's veh/h moved towards q0 × (t / t_ref)^elasticity, t its
        mean trip time in a headless run with the demand so far, t_ref in the reference run (before the change), by
        successive averages (q += (target − q) / n) until every pair is within 2 % of its target (the residual, not the
        step, which shrinks as 1/n by itself). Trips still on the road at the end count with their time so far (only
        finished trips understated a jam's times). Returns (plan, report)."""
        elasticity = float(args.get("elasticity", -0.5))
        iterations = int(args.get("iterations") or 3)
        duration = float(args.get("duration_min") or 30)
        if not -2.0 <= elasticity < 0:
            raise ValueError("elasticity must be below 0 and at least -2 (travel time elasticity of demand: about -0.3 "
                             "to -0.8 is usual; -0.5 by default).")
        if not 1 <= iterations <= 6 or not 5 <= duration <= experiments.MAX_MINUTES:
            raise ValueError(f"iterations 1-6, duration_min 5-{experiments.MAX_MINUTES}.")
        if not args.get("reference"):
            raise ValueError("Give reference: the run (R1...) of the network before the change, whose trip times the "
                             "demand you have was made for.")
        reference = run_reports.run_test(self.tests, str(args["reference"]))
        if self.runner is not None or self._testing:
            raise ValueError("A run is going on: stop it first.")
        before = searches.trip_times(reference["kpis"]["observed"])
        reference_s = {pair: statistics.fmean(found) for pair, found in before.items() if len(found) >= 2}
        if not any(pair in reference_s for pair in flows):
            raise ValueError(f"{reference['id']} has no trips of these pairs (2 or more each): run the reference again.")
        if check_only:
            return {}, {"ok": True}
        pairs = {(p.origin, p.destination): p for p in self._pair_demand()}
        network, modes = deepcopy(self.network), deepcopy(self.vehicle_modes.modes)
        override = self.extensions.signal_override(agent=self._waiting_agent())
        seed = reference["seed"]

        def work(progress, cancelled):
            q, history = dict(flows), []
            for n in range(1, iterations + 1):
                if cancelled():
                    break
                demand = [replace(pairs[pair], hourly=value) if pair in pairs else
                          PairDemand(pair[0], pair[1], value, None, [], "Random", (100,)) for pair, value in q.items()]
                kpis = self._quick_run(network, demand, modes, seed, duration * 60, override, progress, cancelled,
                                       f"Elastic demand, iteration {n} of {iterations}")
                now, moved, gap = searches.trip_times(kpis["observed"]), 0.0, 0.0
                for pair, base in flows.items():
                    if pair in reference_s and len(now.get(pair, ())) >= 2:
                        target = base * (statistics.fmean(now[pair]) / reference_s[pair]) ** elasticity
                        step = (target - q[pair]) / n
                        gap = max(gap, abs(target - q[pair]) / max(q[pair], 1.0))
                        moved = max(moved, abs(step) / max(q[pair], 1.0))
                        q[pair] += step
                history.append({"iteration": n, "total_vph": round(sum(q.values())), "largest_change": round(moved, 3), "largest_gap": round(gap, 3),
                                "mean_trip_s": kpis["mean_trip_s"]})
                if gap <= 0.02:  # at equilibrium: its runs' times give back the demand they were run with
                    break
            return q, history

        q, history = self._in_background(work, "Elastic demand")
        plan = {pair: round(value) for pair, value in q.items()}
        total, start = sum(plan.values()), round(sum(flows.values()))
        return plan, {"elasticity": elasticity, "reference": reference["id"], "iterations": history,
                      "total_vph_before": start, "total_vph_after": total,
                      "changed_percent": round(100 * (total - start) / start, 1) if start else None,
                      "settled": bool(history) and history[-1]["largest_change"] <= 0.02,
                      "note": "Pairs whose trips got faster than in the reference gain demand (induced traffic); slower "
                              "ones lose it. Test the design again with this demand; Ctrl+Z puts the demand back."}

    def _preflight(self, args: dict, check_only: bool) -> dict:
        """preflight_check: a run at a tenth of the demand for links slower than free flow with no signal to blame
        (analysis.slow_links), and one at half the demand checked for gridlock and blocked entries (run_health)."""
        duration = float(args.get("duration_min") or 15)
        if not 5 <= duration <= 60:
            raise ValueError("duration_min must be 5 to 60.")
        demand = experiments.at_busiest_hour(self._pair_demand())  # daily: its busiest hour, as run_simulation
        if not demand:
            raise ValueError("There's no demand to check with: enter or generate some first.")
        if self.runner is not None or self._testing:
            raise ValueError("A run is going on: stop it first, then check.")
        if check_only:
            return {"ok": True}
        seed = int(args["seed"]) if args.get("seed") is not None else self.seed_box.value()
        network, modes = deepcopy(self.network), deepcopy(self.vehicle_modes.modes)
        override = self.extensions.signal_override(agent=self._waiting_agent())

        def work(progress, cancelled):
            return [self._quick_run(network, experiments.scaled(demand, scale), modes, seed, duration * 60, override,
                                    progress, cancelled, f"Pre-flight at {scale:.0%} of the demand") for scale in (0.1, 0.5)]

        low, half = self._in_background(work, "Pre-flight check")
        slow = analysis.slow_links(low)
        health = analysis.run_health(half)
        return {"ok": not slow and health["healthy"], "seed": seed,
                "at_10_percent": {"slow_links": slow[:15], "verdict": "no link is slow without a signal to explain it"
                                  if not slow else f"{len(slow)} link(s) slower than free flow with almost no traffic: "
                                  "look at them (describe, read_traffic) for a fault: a tight curve, a short link, a "
                                  "junction that makes vehicles wait for nothing."},
                "at_50_percent": health}

    def _parameter_sensitivity(self, args: dict, demand, modes, duration: float, check_only: bool) -> dict:
        """parameter_sensitivity: the network and demand as they are, run with each calibration parameter 10 %
        either side (experiments.parameter_sensitivity), in the background."""
        if not demand:
            raise ValueError(f"There's no demand to test with: the OD matrix has no {self.od_matrix.mode} demand. "
                             "Enter or generate some first.")
        names = tool_args._ids(args.get("parameters") or list(experiments.SENSITIVITY_PARAMETERS))
        unknown = [name for name in names if name not in experiments.SENSITIVITY_PARAMETERS]
        if unknown:
            raise ValueError(f"parameters are some of {', '.join(experiments.SENSITIVITY_PARAMETERS)}.")
        seeds = experiments.seeds_from(args.get("seeds")) or [self.seed_box.value(), self.seed_box.value() + 1]
        if len(seeds) > 5 or not all(0 <= seed <= MAX_SEED for seed in seeds):
            raise ValueError(f"seeds: one to five, each 0 to {MAX_SEED:,}.")
        step = float(args.get("step") or experiments.SENSITIVITY_STEP)
        if not 0.05 <= step <= 0.3:
            raise ValueError("step must be 0.05 to 0.3 (0.1 = 10 % either side).")
        if check_only:
            return {"ok": True, "runs": (1 + 2 * len(names)) * len(seeds)}
        network, measure_from_s = deepcopy(self.network), float(args.get("measure_from_min") or 0) * 60
        found = self._in_background(
            lambda progress, cancelled: experiments.parameter_sensitivity(
                network, demand, modes, seeds, duration * 60, names, step, measure_from_s, progress, cancelled),
            "Parameter sensitivity")
        if found is None:
            raise ValueError("Stopped before every run was done: no elasticities.")
        return {**found, "duration_min": duration,
                "note": "Elasticity: the percent change in a figure for a 1 % change in the parameter. Beyond about 0.5 "
                        "(either sign) the results lean on that parameter, so calibrate it to local counts first; near "
                        "0 it hardly matters here. An interval wider than its value means more seeds are needed."}

    def _experiment(self, args, demand, modes, duration, check_only):
        parsed = {key: tool_args._parsed_json(args[key]) if isinstance(args.get(key), str) else args.get(key)
                  for key in ("scenarios", "factors", "conditions")}
        specs, factor_names = parsed["scenarios"], []
        if parsed["factors"]:
            if specs:
                raise ValueError("Give scenarios or factors, not both (factors make the scenarios).")
            specs, factor_names = experiments.factorial(parsed["factors"])
        if not isinstance(specs, list) or not 1 <= len(specs) <= (12 if factor_names else experiments.MAX_SCENARIOS):
            raise ValueError(f"Give 1 to {experiments.MAX_SCENARIOS} scenarios (the first is the baseline), or factors "
                             "making 12 or fewer.")
        designs = [experiments.scenario_from(spec, i) for i, spec in enumerate(specs)]
        if len({s.name.lower() for s in designs}) != len(designs):
            raise ValueError("Each scenario needs a name of its own.")
        conditions = experiments.conditions_from(parsed["conditions"])
        scenarios = [experiments.under(s, c) for c in conditions for s in designs] if conditions else designs
        objective = run_reports.figure_key(args.get("objective") or "delay_veh_h")
        seeds = experiments.seeds_from(args.get("seeds"))
        if not 1 <= len(seeds) <= experiments.MAX_SEEDS or any(not 0 <= seed <= MAX_SEED for seed in seeds):
            raise ValueError(f"Give 1 to {experiments.MAX_SEEDS} seeds, each 0 to {MAX_SEED:,}.")
        precision = float(args.get("precision_percent") or 5)
        if not 0.5 <= precision <= 50:
            raise ValueError("precision_percent must be 0.5 to 50.")
        total = len(scenarios) * len(seeds)
        if total > experiments.MAX_RUNS:
            raise ValueError(f"That is {total} runs; at most {experiments.MAX_RUNS}: fewer scenarios or seeds.")
        networks = [experiments.scenario_network(self.network, s, tool_schemas.SCENARIO_TOOLS) for s in scenarios]
        overrides = [self.extensions.overrides_for(s.extensions, agent=self._waiting_agent())  # unknown names refused here
                     for s in scenarios]
        incidents = {}  # (scenario, seed) -> its incidents: given, and those drawn for the seed
        for scenario, network in zip(scenarios, networks):
            for seed in seeds:
                drawn = experiments.draw_incidents(network, seed, duration * 60, scenario.random_incidents) \
                    if scenario.random_incidents else []
                incidents[(scenario.name, seed)] = scenario.incidents + drawn
            try:  # every road and lane checked before anything runs
                experiments.apply_conditions(Simulation(network, [], modes), scenario.incidents, scenario.weather,
                                             scenario.meters)
            except ValueError as exc:
                raise ValueError(f"{scenario.name}: {exc}") from None
        if not demand:
            raise ValueError("There's no demand to test with: enter or generate some first.")
        if check_only:
            return {"ok": True}
        started = datetime.now()
        experiment_id = f"E{sum(key.startswith('E') for key in self.tests) + 1}"
        runs_folder = self.results_folder / f"{experiments.experiment_name(started)[:-5]} runs"

        planned = []  # every run's settings read here, on the UI thread (they read the window's widgets)
        for scenario, network, override in zip(scenarios, networks, overrides):
            for seed in seeds:
                planned.append((scenario, network, override, seed, self._run_settings(None, seed=seed, network=network, extra=[
                    ("Kind", f"experiment {experiment_id} (run_experiment, by the assistant)"),
                    ("Scenario", scenario.name), ("Demand scale", scenario.demand_scale),
                    ("Extensions in it", ", ".join(scenario.extensions) or "none"),
                    ("Weather", scenario.weather), ("Lateral position (sublane)", "on" if scenario.lateral else "off"),
                    ("Incidents", json.dumps(incidents[(scenario.name, seed)]) if incidents[(scenario.name, seed)] else "none"),
                    ("Duration (simulated)", results.clock(duration * 60))])))

        def conditions_of(scenario, seed) -> dict:
            return {"incidents": incidents[(scenario.name, seed)], "weather": scenario.weather, "meters": scenario.meters,
                    "measure_from_s": float(args.get("measure_from_min") or 0) * 60, "lateral": scenario.lateral}

        def work(progress, cancelled):
            runs, assigned, made = [], {}, {}
            # The plain runs (no extension deciding, no route search first) side by side, a process each; the window's
            # own functions as they are (a test replaces them to see a run fail)
            plain = [count for count, (scenario, _network, override, _seed, _settings) in enumerate(planned, 1)
                     if override is None and not scenario.assign_routes]
            if len(plain) > 1:
                jobs = [{"network": network, "demand": experiments.scaled(demand, scenario.demand_scale),
                         "modes": modes, "seed": seed, "duration_s": duration * 60, "settings": settings,
                         "label": f"{scenario.name}, seed {seed} ({count}/{total})", "folder": runs_folder,
                         **conditions_of(scenario, seed)}
                        for count, (scenario, network, _override, seed, settings) in enumerate(planned, 1)
                        if count in plain]
                made = dict(zip(plain, self._pooled(jobs, progress, cancelled) or []))
            for count, (scenario, network, override, seed, settings) in enumerate(planned, 1):
                if count in made:
                    if made[count] is not None and made[count][2] >= duration * 60 - 1:
                        kpis, path, _simulated = made[count]
                        runs.append({"id": f"{experiment_id}.{count}", "scenario": scenario.name, "seed": seed,
                                     "measures": experiments.measures_of(kpis), "file": str(path or "")})
                    continue
                if cancelled():
                    break
                if scenario.assign_routes:  # its route choice found first (once a scenario), then every seed uses it
                    if scenario.name not in assigned:
                        found = deepcopy(network)
                        searches.equilibrium(found, experiments.scaled(demand, scenario.demand_scale), modes, seeds[0],
                                             min(duration, 30), scenario.assign_routes["iterations"],
                                             {"theta_per_min": 0.5, "routes": 3, "link_times_s": {}}, override,
                                             progress, cancelled, scenario.assign_routes["until_gap"],
                                             f"{scenario.name}: routes", quick_run=self._quick_run)
                        assigned[scenario.name] = found
                    network = assigned[scenario.name]
                kpis, path, simulated = self._one_run(
                    network, experiments.scaled(demand, scenario.demand_scale), modes, seed, duration * 60, override,
                    settings, [], f"{scenario.name}, seed {seed} ({count}/{total})", progress, cancelled, runs_folder,
                    model=self._deciding(on=override is not None and any(
                        extension.path.stem == "transit_signal_priority" for extension in override.extensions))[0],
                    **conditions_of(scenario, seed))
                if simulated >= duration * 60 - 1:
                    runs.append({"id": f"{experiment_id}.{count}", "scenario": scenario.name, "seed": seed,
                                 "measures": experiments.measures_of(kpis), "file": str(path or "")})
            return runs

        runs = self._in_background(work, f"Experiment {experiment_id}")
        names = [s.name for s in scenarios]
        experiment = {"id": experiment_id, "started": started.isoformat(timespec="seconds"), "seeds": seeds,
                      "duration_s": duration * 60, "question": str(args.get("question") or ""),
                      "measure_from_s": float(args.get("measure_from_min") or 0) * 60,  # its runs' warm-up
                      "scenarios": [{"name": s.name, "demand_scale": s.demand_scale, "extensions": s.extensions,
                                     "changes": s.changes, "weather": s.weather, "incidents": s.incidents,
                                     "random_incidents": s.random_incidents, "meters": s.meters,
                                     "assign_routes": s.assign_routes, "lateral": s.lateral} for s in scenarios],
                      "runs": runs, "comparison": []}
        groups = [[f"{s.name} | {c['name']}" for s in designs] for c in conditions] if conditions else [names]
        for group in groups:  # each condition's designs against its own baseline (the first design under it)
            experiment["comparison"] += experiments.compare([r for r in runs if r["scenario"] in group], group,
                                                            precision / 100) if runs else []
        label, higher = run_reports.figure_label(objective)
        if conditions and runs:
            experiment["conditions"] = {"objective": label, **experiments.condition_summary(
                runs, [s.name for s in designs], conditions, objective, higher)}
        if len(factor_names) == 2 and runs:
            experiment["factorial"] = run_reports.factorial(runs, designs, factor_names, conditions, objective, label)
        inputs = results.input_sheets(networks[0], demand, modes, {})
        self.results_folder.mkdir(parents=True, exist_ok=True)
        if any(override is not None for override in overrides):
            experiment["agent_timing"] = experiments.AGENT_TIMING  # in its Read me, its answer and the Scenario Builder's report
        path = experiments.experiment_workbook(self.results_folder / experiments.experiment_name(started), experiment,
                                               inputs, self.ai_mode.journal)
        self.tests[experiment_id] = {"id": experiment_id, "kind": "experiment", "what": experiment["question"] or
                                     " vs ".join(names), "seeds": seeds, "experiment": experiment, "file": str(path)}
        experiment["objective"] = label
        shown = run_reports.compact(experiment["comparison"], label)
        answer = {"experiment": experiment_id, "file": str(path), "runs": len(runs), "of": total, "objective": label,
                  **({"agent_timing": experiments.AGENT_TIMING} if experiment.get("agent_timing") else {}),
                  "comparison": shown}
        if len(shown) < len(experiment["comparison"]):
            answer["more"] = (f"{len(experiment['comparison']) - len(shown)} more rows (every figure, each vehicle "
                              "type's) are in the workbook; rank_scenarios ranks any figure.")
        if len(designs) == 1 and not conditions:  # one design and nothing to set it against
            answer["note"] = ("One scenario: nothing was compared. Put the network as it is first "
                              "({\"name\": \"baseline\"}), then the change.")
        for key in ("conditions", "factorial"):
            if key in experiment:
                answer[key] = _rounded(experiment[key])
        if len(runs) < total:
            answer["note"] = "Stopped by the user: only the finished runs are compared." + (
                " " + answer["note"] if answer.get("note") else "")
        if len(seeds) < 3:
            answer["caution"] = "Fewer than 3 seeds: the intervals are wide or open; don't call a difference from this."
        unstable = sorted({f"{row['figure']} ({row['scenario']})" for row in experiment["comparison"]
                           if (row.get("replications_needed") or 0) > experiments.UNSTABLE_RUNS})
        if unstable:
            answer["unstable"] = (f"Over {experiments.UNSTABLE_RUNS} runs needed for ±{precision:g} %: "
                                  + ", ".join(unstable[:8]) + ". The model may be unstable or gridlocked there.")
        return answer

    LEAD_FIGURES = run_reports.LEAD_FIGURES  # an experiment's answer leads with (run_reports.compact)


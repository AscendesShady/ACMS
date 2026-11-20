"""MainWindow's agents: who decides the signals. Asking the chosen model (_ask_agent), testing agents (compliance,
decision sensitivity) and the window side of the agent extensions (Batch Run, replay, Dispatch Emergency Vehicle,
the controllers' settings: TSP, DEVL, DBL). Cut out of main.py's MainWindow as a plain mixin: a file to work in, not a decoupled module. Its
state is made in MainWindow.__init__, and it calls the rest of the window through self (_pair_demand, _snapshot,
_research_step, _one_run, _quick_run, _in_background ...)."""

from __future__ import annotations

import json
import os
import re
import itertools
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait as wait_for
from copy import deepcopy
from datetime import datetime
from pathlib import Path

from traffic_simulator import analysis, assistant, experiments, results, run_reports, tool_args
from traffic_simulator.lane_graph import Graph
from traffic_simulator.signal_agent import INSTRUCTIONS, SignalAgent
from traffic_simulator.xlsx import read_xlsx


MAX_AT_ONCE = 8  # a batch's runs going at once, at most (TSP keeps the state of twice as many)
HEADLESS_WAIT_S = 30.0  # a headless test's deadline for each agent answer (Batch Run sets its own): missed, the plan
# The AI controllers Batch Run tests, in its order: kind -> (extension file stem, full name)
CONTROLLERS = {"TSP": ("transit_signal_priority", "Transit Signal Priority"),
               "DBL": ("dynamic_bus_lane", "Dynamic Bus Lane"),
               "DEVL": ("dynamic_emergency_lane", "Dynamic Emergency Vehicle Lane")}
# The batch's one baseline: the network as configured, no controller acting (its signal plans: Webster's from the
# demand, or timings entered from a survey, set where the network is made)
DEFAULT = {"label": "Default configuration", "system": "", "provider": "", "model": "", "name": "default",
           "parts": {}, "servers": set()}


class AgentsMixin:
    """Who decides the signals: the model asked, agent tests, and the agent extensions' window side."""

    # What it uses of the window and doesn't define: MainWindow's state and the other mixins' methods
    # (tests/test_docs.py keeps this exact, so a new dependency shows in the diff)
    NEEDS = ('_headless_seconds', '_in_background', '_one_run', '_pair_demand', '_pooled', '_quick_run',
             '_refresh_summary', '_research_step', '_run_settings', '_snapshot', '_testing', 'ai_mode', 'extensions',
             'network', 'od_matrix', 'results_folder', 'runner', 'tests', 'vehicle_modes')

    def _compliance(self, args: dict, check_only: bool) -> dict:
        """test_agent_compliance: the extension (TSP, DEVL or DBL), decided as its tab says (its model), run headless on a
        fixed battery of conditions (the network as it is, 30 % more demand, heavy rain, a lane closed on the busiest
        road) and seeds, each run checked against the invariants (analysis.invariants); with DEVL, emergency vehicles
        sent across the network every 4 minutes; DBL needs buses in the demand. The pass rate, and each failure with
        its evidence."""
        name = str(args.get("extension") or "")
        extension = self.extensions.find(name)  # ValueError: names the extensions
        if extension.path.stem not in [stem for stem, _name in CONTROLLERS.values()]:
            raise ValueError("extension is TSP, DEVL or DBL (the ones whose agent decides the signals).")
        if extension.path.stem == "dynamic_bus_lane" and not self._has_buses():
            raise ValueError("DBL clears a lane for buses, and no vehicle type is a bus: add one to the demand first.")
        seeds = experiments.seeds_from(args.get("seeds") or [1, 2])
        duration = float(args.get("duration_min") or 10)
        if not 1 <= len(seeds) <= 5 or not 3 <= duration <= 60:
            raise ValueError("seeds: 1 to 5; duration_min: 3 to 60.")
        demand = experiments.at_busiest_hour(self._pair_demand())  # daily: its busiest hour, as run_simulation
        if not demand:
            raise ValueError("There's no demand to test with: enter or generate some first.")
        if self.runner is not None or self._testing:
            raise ValueError("A run is going on: stop it first.")
        if check_only:
            return {"ok": True}
        devl = extension.path.stem == "dynamic_emergency_lane"
        dispatches = []
        if devl:
            ends = sorted(self.network.approach_nodes)
            graph = Graph(self.network)
            pairs = [(a, b) for a in ends for b in ends if a != b and graph.route(a, b)]
            if not pairs:
                raise ValueError("No two boundary ends are joined: emergency vehicles have nowhere to go.")
            origin, destination = max(pairs, key=lambda pair: len(graph.route(*pair)))
            dispatches = [(origin, destination, 240.0, 30.0), (destination, origin, 240.0, 150.0)]
        busiest = max(self.network.roads.values(), key=lambda road: road.length_m, default=None)
        battery = [("as it is", 1.0, "clear", []), ("30 % more demand", 1.3, "clear", []),
                   ("heavy rain", 1.0, "heavy rain", [])]
        if busiest is not None and busiest.lanes_per_direction >= 2 and busiest.length_m > 120:
            battery.append(("a lane closed", 1.0, "clear", experiments.incidents_from(
                [{"road": busiest.id, "lanes": 1, "at_min": 1, "length_m": 50}])))
        network, modes = deepcopy(self.network), deepcopy(self.vehicle_modes.modes)
        agent = self._waiting_agent()
        model = self._deciding()[0]

        def work(progress, cancelled):
            found = []
            for label, scale, weather, incidents in battery:
                for seed in seeds:
                    if cancelled():
                        return found
                    override = self.extensions.overrides_for([extension.name], agent=agent)
                    kpis = self._quick_run(network, experiments.scaled(demand, scale), modes, seed, duration * 60, override,
                                           progress, cancelled, f"Compliance: {label}, seed {seed}",
                                           dispatches=dispatches, incidents=incidents, weather=weather)
                    reported = {k: v for r in self.extensions.reports(override) for k, v in r.get("measures", {}).items()}
                    found.append({"condition": label, "seed": seed, "broken": analysis.invariants(kpis, reported),
                                  "decisions": reported.get("tsp_requests") or reported.get("devl_requests")
                                                 or reported.get("dbl_requests")})
            return found

        runs = self._in_background(work, f"Testing {extension.name}")
        passed = [run for run in runs if not run["broken"]]
        return {"extension": extension.name, "decided_by": model or "its tab's choice", "runs": len(runs),
                "passed": len(passed), "pass_rate": round(len(passed) / len(runs), 3) if runs else None,
                "conditions": [condition[0] for condition in battery],  # a lane closed is left out on a short road
                "failures": [run for run in runs if run["broken"]][:12],
                "invariants": [f"no signal arm with traffic left without a green over {analysis.STARVE_S:g} s",
                               f"no emergency vehicle standing still over {analysis.EMERGENCY_STANDSTILL_S:g} s at once",
                               "no gridlock", "no agent errors"],
                "note": "A fixed battery, the same for every model: compare pass rates before a long batch. With nobody "
                        "deciding (a baseline's rule) the extension's own rule runs."}

    SENSITIVITY = ("no_passengers", "exit_full", "no_cross_queues", "arrives_on_green", "no_estimate", "route_blocked")

    def _decision_sensitivity(self, args: dict, check_only: bool) -> dict:
        """decision_sensitivity: TSP decisions of a run re-asked with one fact of their telemetry changed at a time
        (tsp_agent.perturbed), to the model deciding now; how often each change flips the answer, and how often the
        same question asked again keeps it (Explainable AI for ITS Ch 7: occlusion; ZORRO-style perturbation)."""
        from traffic_simulator import agent_transit_signal_priority as tsp_agent  # noqa: PLC0415

        test = run_reports.run_test(self.tests, str(args.get("id") or ""))
        rows = [row for row in run_reports.decision_rows(test) if row["extension"] == "TSP"
                and isinstance(row.get("Telemetry read"), str) and row.get("System")]
        if not rows:
            raise ValueError("That run has no TSP decisions with their telemetry: tick TSP, run_simulation, then try.")
        changes = tool_args._ids(args.get("perturbations") or list(self.SENSITIVITY))
        unknown = [change for change in changes if change not in self.SENSITIVITY]
        if unknown:
            raise ValueError(f"perturbations are some of {', '.join(self.SENSITIVITY)}.")
        sample = max(1, min(int(args.get("sample") or 8), 30))
        if check_only:
            return {"ok": True}
        picked = rows[:: max(1, len(rows) // sample)][:sample]

        def work(progress, cancelled):
            found = []
            for index, row in enumerate(picked, 1):
                if cancelled():
                    break
                message = json.loads(row["Telemetry read"])
                system = "1" if row["System"].endswith("1") else "2"

                def ask(telemetry):
                    reply = self._ask_agent(tsp_agent.question_for(system, telemetry))
                    return bool(reply.get("answer")) if isinstance(reply, dict) else str(reply).strip().upper() == "TRUE"

                progress(f"Re-asking decision {index} of {len(picked)}")
                base, again = ask(message), ask(message)
                found.append({"base": base, "same_again": base == again,
                              **{change: ask(tsp_agent.perturbed(message, change)) != base for change in changes}})
            return found

        try:
            answers = self._in_background(work, "Decision sensitivity")
        except RuntimeError as exc:
            raise ValueError(f"The model couldn't be asked: {exc}") from None
        if not answers:
            raise ValueError("Stopped before any decision was re-asked.")
        return {"run": test["id"], "decisions_re_asked": len(answers),
                "same_answer_again_percent": round(100 * sum(a["same_again"] for a in answers) / len(answers), 1),
                "flips_percent": {change: round(100 * sum(a[change] for a in answers) / len(answers), 1) for change in changes},
                "note": "A change that flips many answers is a fact the model relies on: passengers and a full exit lane "
                        "should; one that flips none is ignored. Read the flips against the same-again rate: a model that "
                        "changes its answer to the same question isn't relying on anything."}

    def _ask_agent(self, question, choice: dict | None = None):
        """The signal agent's question (runs in its thread). TSP's ({"system": "1" | "2", ...}: System 1's state and
        typed question, or System 2's messages) goes to that system: `choice` names the model (a batch run's agent),
        else AI Mode's own (System 1's Use; System 2's started model); the answer comes back with what the model
        reported (model, tokens, P(yes)). Plain text (another extension's yes/no) goes to System 1 when one is
        chosen (one yes/no in an encoder pass), else to System 2's started model."""
        if not isinstance(question, dict):
            fast = self.ai_mode.system_one_for_agents()
            if fast is not None:
                provider, key, model = fast
                return "TRUE" if assistant.system_one_true(provider, question, key, model) else "FALSE"
            connection = self.ai_mode.model_for_agents()
            if connection is None:
                raise RuntimeError("Nothing is chosen in AI Mode (System 1 or a started System 2 model), "
                                   "so every answer is FALSE.")
            provider, model, key = connection
            return assistant.chat(provider, model, [{"role": "system", "content": INSTRUCTIONS},
                                                    {"role": "user", "content": question}], key)
        choice = choice or question.get("choice")  # a batch's agent, else the model picked in the extension
        system = choice["system"] if choice else question.get("system")  # TSP's question has both forms
        if system == "1":
            fast = (choice["provider"], self.ai_mode.key_for(choice["provider"]), choice["model"]) if choice else \
                self.ai_mode.system_one_for_agents()
            if fast is None:
                raise RuntimeError("AI Mode's System 1 is Off, so every answer is FALSE (the plan).")
            provider, key, model = fast
            reply = assistant.system_one(provider, question["state"], question["questions"], key, model=model)
            usage = reply.get("usage") or {}
            found = {"provider": provider, "system": "1", "model": reply.get("model") or provider,
                     "input_tokens": usage.get("input_tokens"), "output_tokens": usage.get("output_tokens")}
            each = {name: (reply["answers"].get(name) or {}).get("noul") for name in question["questions"]}
            for name, p_yes in each.items():  # the guard: a probability for every question, or the plan
                if not isinstance(p_yes, (int, float)) or isinstance(p_yes, bool) or not 0 <= p_yes <= 1:
                    return {**found, "answer": False, "guard": f"rejected: P(yes) for {name} was {p_yes!r}"}
            lowest = min(each.values())  # TRUE when every one is yes: the lowest decides
            return {**found, "answer": lowest >= 0.5, "p_yes": round(float(lowest), 4), "guard": "accepted",
                    "reason": ", ".join(f"{name} {p_yes:.2f}" for name, p_yes in each.items())}
        connection = (choice["provider"], choice["model"], self.ai_mode.key_for(choice["provider"])) if choice else \
            self.ai_mode.model_for_agents()
        if connection is None:
            raise RuntimeError("No model is started in AI Mode's System 2, so every answer is FALSE (the plan).")
        provider, model, key = connection
        tools = question.get("tools")
        try:  # one request: the decision comes as a tool call (data), or failing that in words
            reply = assistant.stream_chat(provider, model, question["messages"], key, tools)
        except assistant.ToolsUnsupported:  # a model that can't call tools (phi3...): TRUE or FALSE in words
            reply = assistant.stream_chat(provider, model, question["messages"], key)
        usage = assistant.last_usage()
        found = {"provider": provider, "model": model, "system": "2", "input_tokens": usage.get("input_tokens"),
                 "output_tokens": usage.get("output_tokens"), "reply": reply.get("content", "").strip()}
        name, argument = question.get("answer_from") or (None, None)
        call = next((call for call in reply.get("tool_calls", []) if call["name"] == name), None)
        # The guard: only a decision is one. Anything else is the plan, counted as rejected, never read into.
        if call is None:  # no tool call: a bare TRUE or FALSE, and nothing else, will do
            word = re.fullmatch(r"\W*(true|false)\W*", found["reply"], re.IGNORECASE)
            if word is None:
                return {**found, "answer": False, "via": "text",
                        "guard": f"rejected: no {name} call, and the reply isn't a bare TRUE or FALSE"}
            return {**found, "answer": word.group(1).lower() == "true", "via": "text", "guard": "accepted"}
        value = call["arguments"].get(argument)  # a boolean, or as small models send it, the word
        if isinstance(value, str) and value.strip().lower() in ("true", "false"):
            value = value.strip().lower() == "true"
        decided = {**found, "via": "tool", "reason": str(call["arguments"].get("reason") or "")[:300] or None,
                   "reply": found["reply"] or json.dumps(call["arguments"])}
        if not isinstance(value, bool):
            return {**decided, "answer": False, "guard": f"rejected: {argument} was {call['arguments'].get(argument)!r}"}
        return {**decided, "answer": value, "guard": "accepted"}

    def _warm_up(self, choice: dict) -> None:
        """One throwaway request, so a model's loading isn't charged to its first run's answers; a failure is
        left for the runs to report."""
        provider, key = choice["provider"], self.ai_mode.key_for(choice["provider"])
        try:
            if choice["system"] == "1":
                assistant.system_one(provider, "warm-up", {"ready": {"type": "noul", "instructions": "Is this a test?"}},
                                     key, model=choice["model"])
            else:
                assistant.chat(provider, choice["model"], [{"role": "user", "content": "Reply with one word: OK."}], key)
        except assistant.AssistantError:
            pass

    def _waiting_agent(self, choice: dict | None = None, wait_s: float = HEADLESS_WAIT_S) -> SignalAgent:
        """A headless run's signal agent: it waits for each answer, up to `wait_s` (missed: the plan). `choice`: a
        batch run's agent (a baseline: nobody asked, its rule answers); None: AI Mode's own choices, as a live run's."""
        if choice is not None and not choice.get("system"):
            return SignalAgent(None, wait_s=wait_s, rule=choice.get("rule"))
        return SignalAgent(lambda question: self._ask_agent(question, choice), wait_s=wait_s)

    def _tsp(self):
        """The TSP extension, if it loaded."""
        return self._extension("transit_signal_priority")

    def _extension(self, stem: str):
        """The extension from extensions/<stem>.py, if it loaded."""
        return next((extension for extension in self.extensions.extensions
                     if extension.path.stem == stem and extension.module is not None), None)

    def _tick_extension(self, stem: str) -> bool:
        """Tick the extension from extensions/<stem>.py in the Extensions tab (Batch Run's Test DEVL ticks Dispatch
        Emergency Vehicle, so its fleet is there to set); False if it didn't load."""
        extension = self._extension(stem)
        if extension is not None:
            extension.check.setChecked(True)
        return extension is not None

    def places(self) -> list[str]:
        """Where an emergency vehicle can be sent from or to: the boundary ends, then the junctions."""
        return sorted(self.network.approach_nodes) + sorted(self.network.nodes)

    def dispatch_emergency(self, origin: str, destination: str, every_min: float = 0, check_only: bool = False,
                           after_s: float = 0.0) -> None:
        """Dispatch Emergency Vehicle: an emergency vehicle into the live run from `origin` to `destination` (ends or
        junctions), `after_s` seconds from now (0: now) and every `every_min` minutes after (0: once;
        Simulation.dispatch). ValueError says why not."""
        if self.runner is None:
            raise ValueError("Start a run first (Run in the menu bar): emergency vehicles join a run.")
        for place in (origin, destination):
            if place not in self.places():
                raise ValueError(f"No end or junction called {place or '(blank)'}.")
        if origin == destination or not self.runner.simulation.graph.route(origin, destination):
            raise ValueError(f"No route from {origin} to {destination}.")
        if not check_only:
            self.runner.simulation.dispatch(origin, destination, float(every_min) * 60, float(after_s))

    def controller_plan(self, kind: str) -> dict:
        """A controller's tab settings for Batch Run (_agent_plan): `kind` one of CONTROLLERS (TSP, DEVL, DBL)."""
        stem, name = CONTROLLERS[kind]
        return self._agent_plan(self._extension(stem), kind, name)

    def _has_buses(self) -> bool:
        """Whether any vehicle type is a bus (what TSP and DBL act for)."""
        return any(getattr(mode, "vehicle_class", mode.name.lower()) == "bus" for mode in self.vehicle_modes.modes)

    def _agent_plan(self, extension, short: str, name: str) -> dict:
        """An extension's who-decides settings for Batch Run (its batch_settings(), agent_choice.py): the models
        picked (Multiple's, or System 1's or 2's), else the model on in AI Mode for its System, as agents (labels of
        agent_options) with their servers, and its deadline. None of them: no agents (run_batch refuses to test it)."""
        if extension is None:
            raise ValueError(f"The {name} extension didn't load: see the Extensions tab.")
        settings = dict(extension.module.batch_settings())
        system = settings.pop("system", None)
        if system:
            fast, connection = self.ai_mode.system_one_for_agents(), self.ai_mode.model_for_agents()
            option = next((option for option in self.ai_mode.agent_options() if option["system"] == system and (
                (system == "1" and fast and option["model"] == fast[2]) or
                (system == "2" and connection and option["model"] == connection[1]))), None)
            settings.update(agents=[option["label"]] if option else [],
                            servers={option["label"]: option["provider"]} if option else {})
        return settings

    def _deciding(self, on: bool | None = None) -> tuple[str, str]:
        """Who decides TSP, by the TSP extension's System 1 / System 2 and AI Mode: (the model's name, for the
        workbook's; a warning when nobody can answer). `on`: TSP in the run (default: ticked). No TSP: "no-ai"."""
        tsp = self._tsp()
        if tsp is None or not (tsp.on if on is None else on):
            return "no-ai", ""
        picked = getattr(tsp.module.tsp_agent, "model", None)
        if picked and picked.get("system") == tsp.module.tsp_agent.system:
            return picked["name"], ""
        if tsp.module.tsp_agent.system == "1":
            fast = self.ai_mode.system_one_for_agents()
            return (fast[2] if fast[0] == "local" else fast[0], "") if fast else (
                "no-llm", "TSP asks System 1, but AI Mode's System 1 is Off: every bus request falls back to the plan.")
        connection = self.ai_mode.model_for_agents()
        return (connection[1], "") if connection else (
            "no-llm", "TSP asks System 2, but no model is started in AI Mode's System 2: every bus request falls back "
                      "to the plan.")

    def run_batch(self, seeds: list[int], minutes: float, times: int = 1, tests: dict | None = None,
                  speed: float = 1.0, at_once: int = 4, default: bool = True) -> dict:
        """Batch Run's Start: every seed x agent x time, headless (Stop ends it), each run saving its workbook (named
        by its seed and model) and its telemetry feed in a folder of the batch's, then the batch's workbook
        comparing each agent with the first (experiments.batch_workbook). `tests` maps each AI controller tested (a
        kind of CONTROLLERS: TSP, DBL, DEVL), run whether ticked in the Extensions tab or not, to its plan:
        {"agents": labels of AIModePanel.agent_options(), the models to test (at least one), "deadline_s": each
        answer's deadline}. The universal logic: every combination of the controllers' models is an agent, each run
        `times` times on every seed (a model's answers vary from run to run; the repeats show by how much). `default`
        (Compare with default): the one baseline, DEFAULT, runs first on each seed, once (no model in it: a seed
        always runs the same), and every agent is compared with it seed by seed; without it, with the first agent.
        Each run has one signal agent per controller; it keeps in step with the wall clock at `speed`,
        never pausing: its agents answer in the background, and a request not answered within its deadline (in
        simulated seconds) falls back to the plan. Up to `at_once` runs go side by side, but never two on one model
        server (a provider: Ollama, Jev...), so no run's answers wait behind another's; each records the pace it
        achieved. The runs are paced only where a model answers (the default never: as fast as the CPU allows).
        With DEVL each run, the default's too, dispatches DEVL's Dispatch Emergency Vehicle fleet and a random emergency vehicle of
        its seed's, and its workbook gets an Emergency vehicles sheet. TSP and DBL act for buses: none in the
        demand, the answer says so. No tests: the default alone, plain runs as fast as the CPU allows. Everything
        is checked first: ValueError says what can't run."""
        tests = {kind: tests[kind] for kind in CONTROLLERS if kind in (tests or {})}  # in CONTROLLERS' order
        if self.runner is not None or self._testing:
            raise ValueError("A run or test is going on: stop it first (the menu bar's Stop), then start the batch.")
        unknown = [str(kind) for kind in tests if kind not in CONTROLLERS]
        if unknown:
            raise ValueError(f"No controller called {', '.join(unknown)}: tests are some of {', '.join(CONTROLLERS)}.")
        deadlines = {kind: float((tests[kind] or {}).get("deadline_s", 30)) for kind in CONTROLLERS if kind in tests}
        if not seeds or not 1 <= int(times) <= 100 or not 0 < float(minutes) <= 24 * 60 \
                or not all(1 <= deadline <= 280 for deadline in deadlines.values()) \
                or not 1 <= speed <= 60 or not 1 <= int(at_once) <= MAX_AT_ONCE:
            raise ValueError(f"A batch needs seeds, 1 to 100 times each, up to 24 h a run, a deadline of 1 to 280 s, "
                             f"a pace of 1x to 60x and 1 to {MAX_AT_ONCE} runs at once.")
        demand = self._pair_demand()
        if not demand:
            raise ValueError(f"There's no demand to run: the OD matrix has no {self.od_matrix.mode} demand (a run uses "
                             "the kind it shows). Enter or generate some first.")
        extensions = {}  # kind -> its extension, in CONTROLLERS' order (the override chain keeps its own)
        for kind in deadlines:
            stem, name = CONTROLLERS[kind]
            extensions[kind] = self._extension(stem)
            if extensions[kind] is None:
                raise ValueError(f"The {name} extension didn't load: see the Extensions tab.")
        options = {option["label"]: option for option in self.ai_mode.agent_options()}

        def resolve(kind: str, labels) -> list[dict]:
            """The models `labels` name, checked: at least one."""
            labels = labels or []
            if not labels:
                raise ValueError(f"Test {kind} has no model to test: pick one in the {CONTROLLERS[kind][1]} tab "
                                 "(Multiple's Models, or System 1 or 2 with its model on in AI Mode).")
            unknown = [label for label in labels if label not in options]
            if unknown:
                raise ValueError(f"Not found on this PC now: {', '.join(unknown)}. Rescan in AI Mode, then pick again.")
            found = [option for label, option in options.items() if label in labels]
            missing = [option["label"] for option in found
                       if option["provider"] in ("jev", "grok", "gemini") and not self.ai_mode.key_for(option["provider"])]
            if missing:
                raise ValueError(f"No key on this PC for {', '.join(missing)}: save it (see README), then Rescan PC.")
            return found

        deciders = {kind: resolve(kind, (tests[kind] or {}).get("agents")) for kind in deadlines}
        if not deciders and not default:
            raise ValueError("Nothing to run: tick Compare with default, or a test with a model to test.")

        def merged(parts: dict) -> dict:
            """One agent of the batch: who decides each controller (kind -> option), as one label."""
            asking = [part for part in parts.values() if part["system"]]
            return {"label": " · ".join(f"{kind} {part['label']}" for kind, part in parts.items())
                    if len(parts) > 1 else next(iter(parts.values()))["label"],
                    "system": asking[0]["system"] if asking else "",
                    "provider": "+".join(dict.fromkeys(part["provider"] for part in asking)),
                    "model": " + ".join(dict.fromkeys(part["model"] for part in asking if part["model"])),
                    "name": "-".join(dict.fromkeys(part["name"] for part in parts.values())),
                    "parts": parts, "servers": {part["provider"] for part in asking}}

        chosen = ([DEFAULT] if default else []) + ([merged(dict(zip(deciders, combo)))
                                                    for combo in itertools.product(*deciders.values())] if deciders else [])
        # Only a run a model answers in keeps in step with the wall clock (its latency then costs what it would on the
        # street); the default and its emergency fleet depend on nothing but the seed: as fast as the CPU
        paced = any(option["servers"] for option in chosen)
        fleet, _devl, random_emergency = self._emergency_fleet() if "DEVL" in deadlines else ([], None, None)
        no_buses = [kind for kind in ("TSP", "DBL") if kind in deadlines and not self._has_buses()]
        times, duration = int(times), float(minutes) * 60
        # Test DEVL: each seed's own random emergency vehicle, the same for every agent on that seed (within the run)
        randoms = {seed: random_emergency(seed, duration) for seed in seeds} if random_emergency else {}
        started = datetime.now()
        folder = self.results_folder / f"{experiments.batch_name(started)[:-5]} runs"
        planned = []  # every run's settings read here, on the UI thread (they read the window's widgets)
        # Seed by seed (every agent on a seed before the next seed): a batch cut short has every agent on the seeds it
        # finished, so they still compare, pair by pair
        for seed in seeds:
            for option in chosen:
                for time_ in range(1, (times if option["servers"] else 1) + 1):  # the default: one run is all
                    planned.append((option, seed, time_, self._run_settings(None, seed=seed, extra=[
                        ("Kind", "batch run (the Batch Run extension)"), ("Agent", option["label"]),
                        *(row for kind in CONTROLLERS for row in (
                            (f"AI model deciding {kind}", (option["parts"].get(kind) or {}).get("model") or "none"),
                            (f"{kind} decision deadline (simulated s)", deadlines.get(kind, "none")))),
                        ("Pace (simulated s a real s)", speed if option["servers"] else "as fast as the CPU allows"),
                        ("Repeat", f"{time_} of {times}"),
                        ("Emergency vehicles", "; ".join(f"{a} to {b}" + (f" every {every:g} s" if every else "")
                                                         + (f" at {results.clock(after)}" if after else "")
                                                         for a, b, every, after in fleet) or "none"),
                        ("Random emergency vehicle (from the seed)", (lambda a, b, every, after: f"{a} to {b} at "
                                                                      f"{results.clock(after)}")(*randoms[seed])
                         if seed in randoms else "none"),
                        ("Duration (simulated)", results.clock(duration))])))
        network, modes = deepcopy(self.network), deepcopy(self.vehicle_modes.modes)
        at_once = int(at_once) if paced else 1
        path, inputs = self.results_folder / experiments.batch_name(started), results.input_sheets(network, demand, modes, {})
        names = [option["label"] for option in chosen]

        def summary(runs: list) -> dict:
            return {"started": started.isoformat(timespec="seconds"), "seeds": list(seeds), "times": times,
                    "duration_s": duration, "deadlines": deadlines, "agents": chosen,
                    "speed": speed if paced else None, "at_once": at_once, "planned": len(planned),
                    "runs": runs, "folder": str(folder),
                    "comparison": experiments.compare(experiments.averaged(runs), names) if runs else []}

        def save(runs: list) -> Path:
            """The batch's workbook as it stands, swapped in whole: a crash mid-write keeps the last one."""
            self.results_folder.mkdir(parents=True, exist_ok=True)
            saving = path.with_name(f"{path.stem} (saving).xlsx")
            experiments.batch_workbook(saving, summary(runs), inputs)
            os.replace(saving, path)
            return path
        done: list = []  # the runs finished, for the progress line

        def label_of(option, seed, time_) -> str:
            return f"{option['label']}, seed {seed}" + (f", time {time_}" if times > 1 else "")

        def entry_of(count, option, seed, time_, kpis, book) -> dict:
            return {"id": f"B{count}", "scenario": option["label"], "seed": seed, "repeat": time_,
                    "measures": experiments.measures_of(kpis), "file": str(book or "")}  # with the pace it achieved

        def keep(entry) -> None:
            done.append(entry)
            try:  # a checkpoint after every run: a crash or power cut keeps what finished
                save(sorted(done, key=lambda entry: int(entry["id"][1:])))
            except OSError:
                pass  # open in Excel, say: the next run's checkpoint tries again

        def one(count, option, seed, time_, settings, progress, cancelled):
            """One run of the batch, on a worker thread: its entry for the comparison, or None if cut short."""
            agents = {}  # each extension's own agent and state: it never waits, and never meets another run's vehicles
            for kind, deciding in option["parts"].items():
                agents[extensions[kind].name] = SignalAgent(
                    (lambda question, deciding=deciding: self._ask_agent(question, deciding)) if deciding["system"] else None,
                    deadline_s=deadlines[kind], rule=deciding.get("rule"))
            override = self.extensions.overrides_for(list(agents), agent=agents) if agents else None
            label = label_of(option, seed, time_)
            try:
                kpis, path, simulated = self._one_run(network, demand, modes, seed, duration, override, settings, [],
                                                      label, progress, cancelled, folder, model=option["name"],
                                                      speed=speed if option["servers"] else None,
                                                      dispatches=fleet + ([randoms[seed]] if seed in randoms else []))
            finally:
                for asked in agents.values():
                    asked.close()
            return None if simulated < duration - 1 else entry_of(count, option, seed, time_, kpis, path)

        def work(progress, cancelled):
            if not extensions and len(planned) > 1:  # plain runs: side by side, a process each
                jobs = [{"network": network, "demand": demand, "modes": modes, "seed": seed, "duration_s": duration,
                         "settings": settings, "label": label_of(option, seed, time_), "folder": folder,
                         "model": option["name"], "dispatches": fleet + ([randoms[seed]] if seed in randoms else [])}
                        for option, seed, time_, settings in planned]

                def each(index, made) -> None:
                    if made is not None and made[2] >= duration - 1:
                        option, seed, time_, _settings = planned[index]
                        keep(entry_of(index + 1, option, seed, time_, made[0], made[1]))

                if self._pooled(jobs, progress, cancelled, each=each) is not None:
                    return sorted(done, key=lambda entry: int(entry["id"][1:]))
                done.clear()  # no processes here: one after another, below
            models = {part["label"]: part for option in chosen for part in option["parts"].values()}
            for part in models.values():  # loaded before the clock runs: a deployed model would be
                if part["system"] and not cancelled():
                    progress(f"warming up {part['label']}")
                    self._warm_up(part)
            failed = threading.Event()

            def halted():
                return cancelled() or failed.is_set()

            began = time.monotonic()

            def line(text):  # one status line for all the runs going, with the time left at the pace so far
                simulated = len(done) * duration + self._headless_seconds()
                left = ""
                if simulated > 0:
                    rest = (time.monotonic() - began) * (len(planned) * duration - simulated) / simulated
                    left = f" · about {results.clock(rest)} left"
                progress(f"{len(done)} of {len(planned)} done, {len(going)} going · {text}{left}")

            waiting, going = list(enumerate(planned, 1)), {}  # going: future -> its model servers
            with ThreadPoolExecutor(max_workers=at_once, thread_name_prefix="batch run") as pool:
                while waiting or going:
                    busy = set().union(*going.values())
                    for item in list(waiting) if not halted() else []:
                        count, (option, seed, time_, settings) = item
                        if len(going) >= at_once:
                            break
                        if option["servers"] & busy:
                            continue  # one run a model server at a time: none waits behind another's questions
                        waiting.remove(item)
                        busy |= option["servers"]
                        going[pool.submit(one, count, option, seed, time_, settings, line, halted)] = option["servers"]
                    if halted() and not going:
                        break
                    finished, _ = wait_for(list(going), timeout=0.2, return_when=FIRST_COMPLETED)
                    for future in finished:
                        going.pop(future)
                        try:
                            entry = future.result()
                        except Exception:
                            failed.set()  # the others stop at their next step; this run's error is the batch's
                            raise
                        if entry is not None:
                            keep(entry)
            return sorted(done, key=lambda entry: int(entry["id"][1:]))

        before, began = (self._snapshot() if self.extensions.wants("log") else None), time.time()
        asked = {"seeds": list(seeds), "minutes": float(minutes), "times": times,
                 "tests": {kind: {"agents": [option["label"] for option in deciders[kind]], "deadline_s": deadline}
                           for kind, deadline in deadlines.items()},
                 "agents": [option["label"] for option in chosen], "speed": speed if paced else None, "at_once": at_once, "default": bool(default)}
        what = (f"Batch run: {len(planned)} runs, {minutes:g} min each, seeds {', '.join(map(str, seeds))}; "
                + "; ".join(option["label"] for option in chosen))
        try:
            runs = self._in_background(work, "Batch run")
        except Exception as exc:
            self._log_step("batch", what, "run_batch", asked, {"error": str(exc)}, began, before, str(exc))
            raise
        save(runs)
        self._refresh_summary()
        answer = {"file": str(path), "runs": len(runs), "of": len(planned), "folder": str(folder)}
        behind = [run["id"] for run in runs if run["measures"].get("pace_achieved", speed) < 0.9 * speed]
        notes = [f"{len(behind)} run(s) fell behind the clock (under 90 % of {speed:g}x): fewer at once or a slower "
                 "pace keeps answers' latency honest."] if behind else []
        if no_buses:
            notes.append(f"No buses in the demand: {'/'.join(no_buses)} had nothing to decide.")
        if notes:
            answer["note"] = " ".join(notes)
        workbooks = sorted(str(book) for book in folder.glob("*.xlsx")) if folder.is_dir() else []
        self._log_step("batch", what, "run_batch", asked, {**answer, "files": workbooks}, began, before)
        return answer

    def _log_step(self, kind: str, what: str, tool: str, arguments: dict, result: dict, started: float, before,
                  error: str = "") -> None:
        """Something done outside AI Mode (a batch run) as a step of the ticked logging extensions (the Research
        Log: its folder, the files it wrote with their SHA-256 and the activity since the step
        before): the same record as an AI Mode step, with no model."""
        if before is None or not self.extensions.wants("log"):
            return
        ended = time.time()
        reply = error or "; ".join(f"{key}: {value}" for key, value in result.items() if key != "files")
        self._research_step({
            "kind": kind, "prompt": what, "editor_update": "", "provider": None, "model": None, "plan": "",
            "tools_offered": [], "started": started, "ended": ended, "messages": [], "reply": reply, "error": error,
            "usage": [], "before": before,
            "calls": [{"tool": tool, "arguments": arguments, "what": what, "decision": "failed" if error else "applied",
                       "result": result, "at": started, "proposed_at": started, "decided_at": started,
                       "compute_s": round(ended - started, 3)}]})

    def _route_check(self, no: str = "No"):
        """can_go(origin, destination): ValueError ("<no> route from ... to ...") unless a route joins them on the
        network as it is, no run needed."""
        graph = Graph(self.network)

        def can_go(origin: str, destination: str) -> None:
            if origin == destination or not graph.route(origin, destination):
                raise ValueError(f"{no} route from {origin} to {destination}.")

        return can_go

    def _schedule_emergency(self, args: dict, check_only: bool = False) -> dict:
        """The assistant's schedule_emergency: DEVL's Dispatch Emergency Vehicle schedules replaced (set_fleet),
        each checked first (an end or junction each, a route between them, every or after 0-120 min, not both),
        DEVL ticked (its section is used only while ticked, as Test DEVL ticks it); with dispatch_now also sent
        into the live run (dispatch_emergency). ValueError: why not, nothing changed."""
        devl = self._extension("dynamic_emergency_lane")
        if devl is None:
            raise ValueError("The Dynamic Emergency Vehicle Lane extension didn't load: see the Extensions tab.")
        dispatch = devl.module.dispatch  # its Dispatch Emergency Vehicle section
        rows = args.get("schedules")
        if isinstance(rows, str):
            rows = tool_args._parsed_json(rows)
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise ValueError("schedules is a list of {from, to, every_min or after_min}.")
        places, can_go, most = self.places(), self._route_check(), dispatch.MAX_EVERY_MIN
        fleet = []
        for row in rows:
            origin, destination = (str(row.get(key) or "").strip() for key in ("from", "to"))
            for place in (origin, destination):
                if place not in places:
                    raise ValueError(f"No end or junction called {place or '(blank)'}.")
            can_go(origin, destination)
            try:
                every, after = (int(float(row.get(key) or 0)) for key in ("every_min", "after_min"))
            except (TypeError, ValueError):
                raise ValueError("every_min and after_min are whole minutes.") from None
            if not (0 <= every <= most and 0 <= after <= most):
                raise ValueError(f"every_min and after_min are 0 to {most}.")
            if every and after:
                raise ValueError(f"{origin} → {destination}: a schedule repeats (every_min) or waits (after_min), not both.")
            fleet.append((origin, destination, every, after))
        now = tool_args._bool(args.get("dispatch_now"))
        for origin, destination, every, _ in fleet if now else []:
            self.dispatch_emergency(origin, destination, every, check_only=True)  # no run: refused here
        if check_only:
            return {"ok": True}
        self._tick_extension("dynamic_emergency_lane")
        dispatch.set_fleet(fleet)
        for origin, destination, every, after in fleet if now else []:
            self.dispatch_emergency(origin, destination, every, after_s=after * 60)
        return {"schedules": [{"from": origin, "to": destination, "every_min": every, "after_min": after}
                              for origin, destination, every, after in fleet], "dispatched_now": now}

    def _emergency_fleet(self) -> tuple[list, object, object]:
        """Test DEVL: DEVL's Dispatch Emergency Vehicle schedules as (from, to, every s, s in), each checked; the
        DEVL extension (its signal override clears their lanes); and `random_for(seed, duration_s)`, a seed's own
        random emergency vehicle as (from, to, 0, seconds in), within the run. ValueError says what's missing."""
        devl = self._extension("dynamic_emergency_lane")
        if devl is None:
            raise ValueError("Test DEVL needs the Dynamic Emergency Vehicle Lane extension: see the Extensions tab.")
        dispatch = devl.module.dispatch  # its Dispatch Emergency Vehicle section
        fleet, places, can_go = dispatch.fleet(), self.places(), self._route_check("Dispatch Emergency Vehicle: no")

        for origin, destination, *_ in fleet:
            if origin not in places or destination not in places:
                raise ValueError(f"Dispatch Emergency Vehicle: no end or junction called "
                                 f"{(origin if origin not in places else destination) or '(blank)'}.")
            can_go(origin, destination)

        def random_for(seed: int, duration_s: float) -> tuple:
            start, end, delay = dispatch.random_emergency(seed, places, can_go,
                                                          min(dispatch.MAX_DELAY_MIN * 60, int(duration_s)))
            return start, end, 0.0, float(delay)

        return [(origin, destination, every * 60.0, after * 60.0) for origin, destination, every, after in fleet], devl,             random_for

    def replay_run(self, path: str | Path) -> dict:
        """Replay a TSP run from its workbook (`path`) and the telemetry feed beside it: the same seed and
        simulated time, headless, each bus request getting the decision the feed recorded at the simulated time
        it reached the controller (tsp_agent.Replay), no model asked. With the same network and demand open it
        runs exactly as it did; its workbook ("<model>-replay") is compared with the original's Summary:
        {"file", "same", "differences": [[figure, original, replay]]}. ValueError says what can't run."""
        path = Path(path)
        feed = path.with_name(f"{path.stem} telemetry.jsonl")
        if self.runner is not None or self._testing:
            raise ValueError("A run or test is going on: stop it first (the menu bar's Stop), then replay.")
        if not path.is_file() or not feed.is_file():
            raise ValueError(f"A replay needs a run's workbook and its telemetry feed beside it ({feed.name}).")
        tsp = self._tsp()
        if tsp is None:
            raise ValueError("The Transit Signal Priority extension didn't load: see the Extensions tab.")
        demand = self._pair_demand()
        if not demand:
            raise ValueError("There's no demand to run: open the network and demand the run had, then replay.")
        sheets = read_xlsx(path)
        settings = {row[0]: row[1] for row in sheets.get("Run", [])[1:] if len(row) > 1}
        original = {row[0]: row[1] for row in sheets.get("Summary", [])[1:] if len(row) > 1}
        read_me = {row[0]: row[1] for row in sheets.get("Read me", [])[1:] if len(row) > 1}
        try:
            seed = int(settings["Random seed"])
            hours, minutes, seconds = (int(part) for part in str(original["Simulated time"]).split(":"))
        except (KeyError, ValueError) as exc:
            raise ValueError(f"{path.name} isn't a run's workbook (no seed or simulated time).") from exc
        duration = hours * 3600 + minutes * 60 + seconds
        agent = tsp.module.tsp_agent.Replay([json.loads(line) for line in feed.read_text(encoding="utf-8").splitlines()
                                             if line.strip()])
        override = self.extensions.overrides_for([tsp.name], agent=agent)
        model = f"{read_me.get('AI model') or 'run'}-replay"
        run_settings = self._run_settings(None, seed=seed, extra=[("Kind", f"replay of {path.name}"),
                                                                  ("Duration (simulated)", results.clock(duration))])
        network, modes = deepcopy(self.network), deepcopy(self.vehicle_modes.modes)
        _, replayed, _ = self._in_background(lambda progress, cancelled: self._one_run(
            network, demand, modes, seed, duration, override, run_settings, [], f"Replay of seed {seed}", progress,
            cancelled, model=model), "Replay")
        self._refresh_summary()
        if replayed is None:
            raise ValueError("The replay recorded nothing.")
        again = {row[0]: row[1] for row in read_xlsx(replayed).get("Summary", [])[1:] if len(row) > 1}
        differences = [[figure, value, again.get(figure)] for figure, value in original.items()
                       if isinstance(value, float) and again.get(figure) != value]
        return {"file": str(replayed), "same": not differences, "differences": differences}

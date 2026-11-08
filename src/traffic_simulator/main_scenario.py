"""MainWindow's Scenario Builder: a scenario.py plan run on the window, each step's @targets resolved, run through
the ordinary tool path, known failures recovered, all as one undo step (scenario.py stays its pure planning half).
Cut out of main.py's MainWindow as a plain mixin. Its _building flag is made in MainWindow.__init__ and read by
main.py's _commit and _wait_for_build; it calls the rest of the window through self (_runs_command, _od_command,
_types_command, _ai_edited, _pair_demand)."""

from __future__ import annotations

from collections import defaultdict

from traffic_simulator import experiments, network_tools, scenario


class ScenarioMixin:
    """build_scenario on the window: plan, resolve, run, recover."""

    # --- The Scenario Builder: a described scenario, built and tested in one go -----------------------

    def _scenario_state(self) -> dict:
        names = self.vehicle_modes.names()
        return {"has_network": bool(self.network.nodes and self.network.approach_nodes),
                "has_demand": bool(self._pair_demand()), "bus_type": next((n for n in names if "bus" in n.lower()), None)}

    def _build_scenario(self, args: dict, check_only: bool) -> dict:
        """build_scenario (scenario.py): the form completed with stated defaults, planned, and (applied) every
        step run through the ordinary tools, in order, as one undo step. A step that fails is recovered where a
        known fix exists (demand that can't be sized by saturation: sized by flow; a what-if that can't be
        built: left out), else reported; nothing after a failed network step runs."""
        spec, assumptions, problems = scenario.prepare(args, self._scenario_state())
        if spec.get("intent") == "question":
            raise ValueError("That is a question, not something to build: answer it instead.")
        if problems:
            raise ValueError(" ".join(problems))
        steps = scenario.plan(spec)
        if check_only:
            return {"ok": True, "preview": scenario.describe(spec, steps, assumptions)}
        done: list[dict] = []
        self._building = True
        editors = (self.workspace, self.create_tab, self.edit_tab, self.behavior_tab, self.demand_tab)
        for editor in editors:  # the map, grids and editing tabs wait: an edit during a step's test would join the build
            editor.setEnabled(False)
        try:
            for step in steps:
                try:
                    resolved = self._resolve(step, done)
                    result = self._scenario_step(step["tool"], resolved)
                    done.append({**step, "args": resolved, "ok": True, "result": result})
                except (ValueError, network_tools.ToolError, KeyError) as exc:
                    fixed = self._recover(step, exc, done, spec)
                    done.append(fixed or {**step, "ok": False, "error": str(exc)})
                    if not fixed and step["tool"] in ("place_map", "network_from_background", "generate_grid",
                                                      "generate_demand", "run_simulation"):
                        break  # nothing after it can work
        finally:
            self._building = False
            for editor in editors:
                editor.setEnabled(True)
            self._ai_edited("generate_grid" if any(s["tool"] in ("generate_grid", "network_from_background") and s.get("ok")
                                                   for s in done) else "build_scenario")
        files = [s["result"]["file"] for s in done if s.get("ok") and isinstance(s.get("result"), dict)
                 and s["result"].get("file")]
        return {"report": scenario.report(spec, done), "assumptions": assumptions, "files": files,
                "steps": [{"tool": s["tool"], "why": s["why"], "ok": s["ok"], **({"error": s["error"]} if not s["ok"] else {})}
                          for s in done],
                "note": "Built as one undo step (Ctrl+Z undoes all of it). The numbers are the tools' own; read the "
                        "workbooks for the detail."}

    def _scenario_step(self, tool: str, args: dict) -> dict:
        """One planned step through the tool's own path (the Demand Generator, Mode Behavior, the run tools,
        or a checked network edit)."""
        args = network_tools.normalise(tool, args)
        if tool in network_tools.OD_TOOLS:
            return self._od_command(tool, args)
        if tool in network_tools.TYPE_TOOLS:
            return self._types_command(tool, args)
        if tool in network_tools.RUN_TOOLS:
            return self._runs_command(tool, args)
        network_tools.prepare(self.network, tool, args)
        result = network_tools.run_tool(self.network, tool, args)
        self._ai_edited(tool)
        return result

    def _busiest(self, done: list[dict]) -> tuple[list[str], list[str]]:
        """(roads, junctions) by delay in the build's baseline run, busiest first; before it, the junctions
        the demand loads most."""
        run = next((s["result"] for s in reversed(done) if s["tool"] == "run_simulation" and s.get("ok")), None)
        if run:
            rows = self.tests[run["run"]]["kpis"]["roads"]
            roads = [r["road"] for r in sorted(rows, key=lambda r: -r["delay_veh_h"]) if r["road"] in self.network.roads]
            at: dict = defaultdict(float)
            for r in rows:
                road = self.network.roads.get(r["road"]) or self.network.approach_roads.get(r["road"])
                for node in (road.from_node, road.to_node) if road else ():
                    if node in self.network.nodes:
                        at[node] += r["delay_veh_h"]
            return roads, sorted(at, key=lambda n: -at[n])
        demand = next((s["result"] for s in done if s["tool"] == "generate_demand" and s.get("ok")), {})
        return list(self.network.roads)[:1], list((demand.get("degree_of_saturation") or {}) or self.network.nodes)

    def _resolve(self, step: dict, done: list[dict]) -> dict:
        """The step's arguments with its @ targets filled in from the network and the build so far."""
        nodes = self.network.nodes

        def token(value: str):
            roads, junctions = self._busiest(done)
            if value == "@junctions":
                return list(nodes)
            if value == "@signalised":
                return [n for n, node in nodes.items() if node.control == "signalised"]
            if value == "@busiest_road":
                return roads[:1] or list(self.network.roads)[:1]
            if value == "@busiest_junction":
                return junctions[:1] or list(nodes)[:1]
            if value == "@busiest_signalised":
                return [n for n in junctions if nodes[n].control == "signalised"][:3] or \
                    [n for n, node in nodes.items() if node.control == "signalised"][:1]
            if value == "@bus_type":
                return self._scenario_state()["bus_type"]
            if value.startswith("@lanes:"):
                return self.network.road(value[7:]).lanes_per_direction
            if value == "@optimised":
                found = next((s["result"] for s in done if s["tool"] == "optimise_signals" and s.get("ok")), None)
                if not found:
                    raise ValueError("The signal search didn't run, so there's no optimised plan to test.")
                return [{"tool": c["tool"], "args": c["args"]} for c in found["apply_with"]]
            raise ValueError(f"Unknown target {value}.")

        def fill(value):
            if isinstance(value, str) and value.startswith("@"):
                return token(value)
            if isinstance(value, dict):
                return {k: fill(v) for k, v in value.items()}
            if isinstance(value, list):
                return [fill(v) for v in value]
            return value

        if step["tool"] == "run_experiment":  # the what-ifs as scenarios against the network as built
            args = step["args"]
            variants = [scenario.variant(item, token) for item in args["what_if"]]
            return {"scenarios": [{"name": "As built"}, *variants], "seeds": list(range(1, args["seeds"] + 1)),
                    "duration_min": args["duration_min"], "question": "What-ifs from the Scenario Builder"}
        return fill(step["args"])

    def _recover(self, step: dict, exc: Exception, done: list[dict], spec: dict) -> dict | None:
        """A known fix for a failed step, run now; None if there is none (the step is reported failed)."""
        tool, error = step["tool"], str(exc)
        if error.startswith("Nothing would change"):  # already as asked (the grid on screen is that grid): done
            return {**step, "ok": True, "result": {"unchanged": error}}
        if tool == "generate_demand" and "degree_of_saturation" in step["args"]:
            # Nothing to size by (no junction where traffic waits): a flow per boundary end by the level instead
            per_end = {"light": 150, "medium": 300, "heavy": 450, "near capacity": 550, "over capacity": 700}[spec["traffic"]]
            args = {k: v for k, v in step["args"].items() if k != "degree_of_saturation"}
            args["total"] = per_end * len(self.network.approach_nodes) * (24 if args.get("demand") == "daily" else 1)
            try:
                result = self._scenario_step(tool, args)
            except (ValueError, network_tools.ToolError):
                return None
            result["recovered"] = f"Sized by flow ({per_end} veh/h an end) because {error}"
            return {**step, "args": args, "ok": True, "result": result}
        if tool == "run_experiment":  # leave out the what-ifs that can't be built, and test the rest
            args = self._resolve(step, done)
            kept = [sc for sc in args["scenarios"][1:] if self._buildable(sc)]
            if kept and len(kept) < len(args["scenarios"]) - 1:
                try:
                    result = self._scenario_step(tool, {**args, "scenarios": [args["scenarios"][0], *kept]})
                except (ValueError, network_tools.ToolError):
                    return None
                result["recovered"] = f"Left out what-ifs that couldn't be built ({error})"
                return {**step, "args": args, "ok": True, "result": result}
        return None

    def _buildable(self, spec: dict) -> bool:
        try:
            experiments.scenario_network(self.network, experiments.scenario_from(spec, 0), network_tools.SCENARIO_TOOLS)
            self.extensions.overrides_for(spec.get("extensions") or [])
        except ValueError:
            return False
        return True

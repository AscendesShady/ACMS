"""MainWindow's runs: starting, pacing, stopping and recording simulation runs, live or headless (the
background-thread runner, the clock and frame drawing, exporting results). Cut out of main.py's MainWindow as a
plain mixin: a file to work in, not a decoupled module. Its state (runner, recorder, _testing, _headless ...) is
made in MainWindow.__init__, and it calls the rest of the window through self (_pair_demand, _refresh_summary,
_show_saved, _deciding)."""

from __future__ import annotations

import threading
import time
from copy import deepcopy
from pathlib import Path

from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QFileDialog, QMessageBox

from traffic_simulator import experiments, results, tool_args, tool_reports
from traffic_simulator.clock import STEP_MS
from traffic_simulator.simulation import DT, Simulation
from traffic_simulator.physics_process import ProcessRunner
from traffic_simulator.runner import Runner
from traffic_simulator.theme import STATUS
from traffic_simulator.walking import walk_pairs

LIVE_OBSERVED = 400_000  # a live run keeps its latest passages, conflicts and stop-line crossings for the analysis


class RunsMixin:
    """Run, Play, Pause, Stop, the headless runs' thread and the run's workbook: MainWindow's run bar."""

    # What it uses of the window and doesn't define: MainWindow's state and the other mixins' methods
    # (tests/test_docs.py keeps this exact, so a new dependency shows in the diff)
    NEEDS = ('_busy_what', '_cancel', '_close_after_test', '_deciding', '_drawn_at', '_headless', '_headless_frames',
             '_headless_lock', '_institution_notes', '_pair_demand', '_refresh_summary', '_replaced',
             '_seed_box_action', '_seed_overridden_action', '_show_saved', '_testing', 'ai_mode', 'clock',
             'clock_label', 'close', 'extensions', 'last_kpis', 'last_results', 'network', 'network_path',
             'od_matrix', 'pause_action', 'physics_process', 'play_action', 'properties', 'recorder',
             'results_folder', 'run_action', 'runner', 'scene', 'seed_box', 'show_runs', 'signal_agent',
             'speed_combo', 'statusBar', 'vehicle_modes', 'view')

    def _run(self) -> None:
        """Run: a fresh simulation of the network and demand as they are now, from 00:00:00, its
        physics on its own thread in step with the wall clock (simulation.Runner)."""
        if self._testing:  # Run and Play are off meanwhile; this catches any other way in
            self.statusBar().showMessage(f"{self.busy()} is going on: Stop ends it, then Run.", 8000)
            return
        self._stop_runner()
        demand = self._pair_demand()
        # Its own copy: an edit stops the run, but only after it has changed the network, and the run's workbook
        # describes the network and types it drove (the physics thread never reads one half-edited)
        network, modes = deepcopy(self.network), deepcopy(self.vehicle_modes.modes)
        simulation = Simulation(network, demand, modes, seed=self.seed_box.value(),
                                walkers=walk_pairs(network, modes, self.od_matrix.mode == "daily"))  # one switch for all
        notes = []
        webster = [plan["cycle_s"] for plan in simulation.signal_plans.values() if plan["method"] == "webster"]
        if webster:
            notes.append(f"Webster timing from the demand at {len(webster)} junction{'s' if len(webster) > 1 else ''}: "
                         f"cycles {min(webster):g}–{max(webster):g} s.")
        groups = {plan["method"]: plan for plan in simulation.signal_plans.values() if "group" in plan}
        for method, plan in groups.items():
            name = {"transyt": "TRANSYT", "synchro": "Synchro", "scats": "SCATS", "scoot": "SCOOT"}[method]
            adapts = " to start, adapting from its detectors" if method in ("scats", "scoot") else ""
            notes.append(f"{name}: {plan['group']} junctions on a {plan['cycle_s']:g} s cycle{adapts}.")
        self.properties.show_plans(dict(simulation.signal_plans))
        if not simulation.demand:
            notes.append("No demand yet: enter flows in the Demand Generator. The signals run meanwhile.")
        notes += self._institution_notes
        if simulation.unreachable:
            notes.append(f"No route for {', '.join(simulation.unreachable[:5])}: those pairs spawn nothing.")
        model, warning = self._deciding()
        if warning:
            notes.append(warning)
        if notes:
            self.statusBar().showMessage(" ".join(notes), 8000)
        self.signal_agent.reset()
        simulation.observe(keep=LIVE_OBSERVED)  # detectors, link times, conflicts…: for the workbook's analysis sheets
        simulation.signal_override = self.extensions.signal_override()  # None unless TSP is ticked
        self.recorder = results.RunRecorder(network, modes,
                                            name=f"{len(simulation.demand)} OD pairs with demand",
                                            inputs=self._run_inputs(simulation, demand), seed=self.seed_box.value(),
                                            model=model, run=self._run_settings(simulation, extra=[
                                                ("AI model deciding TSP", model)]))
        self.runner = self._runner_for(simulation)
        self.runner.play()
        self.clock.run()

    def _runner_for(self, simulation: Simulation) -> Runner:
        """The run's physics: in a process of its own, unless an extension decides in the run (its override is this
        window's own code and agents, and can't go there) or the process won't start; then on a thread here."""
        speed = float(self.speed_combo.currentData())
        if self.physics_process and simulation.signal_override is None:
            try:
                return ProcessRunner(simulation, speed, recorder=self.recorder)
            except Exception as exc:  # noqa: BLE001 - whatever stopped it, the run goes as it always did
                self.statusBar().showMessage(f"The run's physics stays in this window ({exc}).", 8000)
        return Runner(simulation, speed, recorder=self.recorder)

    def _stop_pressed(self) -> None:
        self._cancel = True  # a headless test stops at its next step
        self._stop_run()

    def _in_background(self, work, what: str):
        """Run work(progress, cancelled) on its own thread, the window kept alive (a local event loop) and
        the status bar saying how far it is; Stop cancels it. Returns its result, or raises its error."""
        if self.runner is not None or self._testing:  # never two at once, whichever way it was asked for
            raise ValueError(f"{self.busy()} is going on: stop it first (the menu bar's Stop).")
        state = {"done": False, "result": None, "error": None, "progress": ""}
        self._cancel, self._testing, self._busy_what = False, True, what
        self._show_busy()

        def target():
            try:
                state["result"] = work(lambda text: state.__setitem__("progress", text), lambda: self._cancel)
            except Exception as exc:  # noqa: BLE001 - handed back to the caller
                state["error"] = exc
            finally:
                state["done"] = True

        threading.Thread(target=target, name="network test", daemon=True).start()
        loop, timer = QEventLoop(), QTimer()

        def poll():
            shown = self._show_headless()
            self.statusBar().showMessage(f"{what}: {state['progress']}{f' · {shown}' if shown else ''} (Stop ends it)")
            self.extensions.report_failures()  # a run's override raised: switched off, saying why
            if state["done"]:
                loop.quit()

        timer.timeout.connect(poll)
        timer.start(100)
        loop.exec()
        timer.stop()
        self._testing, self._busy_what = False, ""
        self._show_busy()
        self.statusBar().clearMessage()
        failed = self.extensions.report_failures()
        if failed:  # the runs went on without it from then: their workbooks' Extensions dropped sheet says so
            self.statusBar().showMessage(f"{', '.join(failed)} raised during {what.lower()} and was switched off: "
                                         "see the Extensions tab.", 15000)
        with self._headless_lock:
            self._headless.clear()
            self._headless_frames.clear()
        if self.runner is None:
            self.scene.show_frame(1.0, None)  # the test's vehicles leave the map
        self.clock_label.setToolTip("Simulation time")
        self.clock_label.setText(results.clock(self.clock.time_s))
        if self._close_after_test:
            QTimer.singleShot(0, self.close)  # after the caller has had its result
        if state["error"] is not None:
            raise state["error"]
        return state["result"]

    def busy(self) -> str:
        """What is using the simulator ("Batch run", "A live run"), or "" when nothing: for a control that
        would start another (Batch Run's Start), so it can say why it waits."""
        if self._testing:
            return self._busy_what
        return "A live run" if self.runner is not None else ""

    def _show_busy(self) -> None:
        """While a test runs, Run and Play wait, their tooltips naming it (the status bar says how far it is)."""
        busy = self._busy_what if self._testing else ""
        for action, tip in ((self.run_action, "Run from 00:00:00"), (self.play_action, "Play")):
            action.setEnabled(not busy)
            action.setToolTip(" ".join(f"Busy: {busy}".split()[:5]) if busy else tip)

    def _show_headless(self) -> str:
        """While a test runs headless: the menu bar's clock shows its run's simulated time (the least advanced of
        those going side by side), its tooltip saying so in five words; that run by name, for the status bar
        ("Default configuration, seed 1: 00:12:34 of 00:30:00"), or "" with none going."""
        with self._headless_lock:
            going = list(self._headless.items())
        if not going:
            return ""
        label, (seconds, of) = min(going, key=lambda item: item[1][0])
        self.clock_label.setText(results.clock(seconds))
        with self._headless_lock:
            frame = self._headless_frames.get(label) if self.show_runs else None
        if frame is not self.scene.frame:  # the same run the clock shows, drawn as a live run is (or none)
            self.scene.show_frame(1.0, frame)
        self.clock_label.setToolTip("Slowest test run's time")
        return f"{label}: {results.clock(seconds)} of {results.clock(of)}"

    def _headless_seconds(self) -> float:
        """The simulated seconds the headless runs going have done so far, together."""
        with self._headless_lock:
            return sum(t for t, _ in self._headless.values())

    def _pooled(self, jobs: list, progress, cancelled, **more) -> list | None:
        """experiments.run_pool for plain runs (no extension deciding), each on the menu bar's clock and, while Show
        runs on map is on, on the map, as a thread's run is. None when there are no processes here, or a test has
        replaced the window's run functions: the caller then makes the runs one by one."""
        if self._observed_kpis is not experiments.observed_kpis or self._end_recording is not experiments.end_recording \
                or self._quick_run is not experiments.quick_run:
            return None

        def watch(label, simulated, of, frame) -> None:
            with self._headless_lock:
                if simulated is None:  # it ended
                    self._headless.pop(label, None)
                    self._headless_frames.pop(label, None)
                else:
                    self._headless[label] = (simulated, of)
                    if frame is not None:
                        self._headless_frames[label] = frame

        try:
            return experiments.run_pool(jobs, progress, cancelled, watch=watch, frames=self.show_runs, **more)
        finally:
            with self._headless_lock:
                for job in jobs:
                    self._headless.pop(job.get("label"), None)
                    self._headless_frames.pop(job.get("label"), None)

    def _one_run(self, network, demand, modes, seed, duration_s, override, settings, extra_inputs, label, progress, cancelled,
                 folder: Path | None = None, model: str = "", speed: float | None = None, dispatches=(),
                 incidents=(), weather: str = "clear", meters=(), measure_from_s: float = 0.0, lateral: bool = False):
        """One headless run on the worker thread: its recorder, its workbook (named by its seed and `model`, the AI
        deciding TSP; its extensions' reports in it); (kpis, path, simulated seconds). kpis["reported"]: their figures.
        `speed`: in step with the wall clock at this pace (a batch's), else as fast as the CPU allows. `dispatches`:
        emergency vehicles sent at the start (their trips: the Emergency vehicles sheet)."""
        for agent in ((override.agent.values() if isinstance(override.agent, dict) else [override.agent])
                      if override is not None else []):
            if hasattr(agent, "reset"):
                agent.reset()  # its own run's answers only

        def stepped_to(t: float) -> None:  # the menu bar's clock follows it (_show_headless)
            with self._headless_lock:
                self._headless[label] = (t, duration_s)
            progress(f"{label}, {results.clock(t)} of {results.clock(duration_s)}")

        drawn = [0.0]

        def framed(simulation) -> None:  # on the run's thread: a frame for the map, while it shows runs
            now = time.monotonic()
            if not self.show_runs or now - drawn[0] < 0.1:
                return
            drawn[0] = now
            frame = simulation.frame()
            with self._headless_lock:
                self._headless_frames[label] = frame

        def headless_over() -> None:  # the clock and the map let go of it, before its workbook is written
            with self._headless_lock:
                self._headless.pop(label, None)
                self._headless_frames.pop(label, None)

        def end(*args):
            headless_over()
            return self._end_recording(*args)

        try:  # the run itself is experiments.recorded_run (a pool's process makes a plain one the same way)
            return experiments.recorded_run(
                network, demand, modes, seed, duration_s, settings, label, folder or self.results_folder,
                override=override, extra_inputs=extra_inputs, model=model, speed=speed, dispatches=dispatches,
                incidents=incidents, weather=weather, meters=meters, measure_from_s=measure_from_s, lateral=lateral,
                progress=stepped_to, cancelled=cancelled, on_frame=framed,
                reports=lambda: self.extensions.reports(override), observed=self._observed_kpis, end=end)
        finally:
            headless_over()

    _observed_kpis = staticmethod(experiments.observed_kpis)  # in experiments; tests replace them on the window
    _end_recording = staticmethod(experiments.end_recording)
    _quick_run = staticmethod(experiments.quick_run)

    def _journal_sheet(self) -> list:
        rows = [["Time", "Kind", "Tool", "What", "Arguments", "Outcome"]]
        rows += [[e["time"], e["kind"], e["tool"], e["what"], e["arguments"], e["outcome"]] for e in self.ai_mode.journal]
        return [("Planning journal", rows)]

    def _run_settings(self, simulation: Simulation | None, seed: int | None = None, network=None,
                      extra: list | None = None) -> list[tuple[str, object]]:
        """A run's settings, for its workbook: enough for someone else to repeat and check it."""
        network = network or self.network
        seed = self.seed_box.value() if seed is None else seed
        background = network.background
        where = {"colour": f"colour {background.colour}", "map": f"OpenStreetMap at {background.lat:.6f}, "
                 f"{background.lon:.6f} (under ({background.anchor_x:.1f}, {background.anchor_y:.1f}) m), turned "
                 f"{background.map_rotation_deg:g}°", "image": f"image {background.image}, {background.width_m:g} m wide"}
        rows = [("Application", "ACMS (Agentic Context-Aware Mobility System) 0.1.0"),
                ("Run started", time.strftime("%Y-%m-%d %H:%M:%S")),
                ("Random seed", seed), ("Time step (s)", simulation.dt if simulation else DT),
                ("Step method", simulation.step_method if simulation else "euler"),
                *((("Lateral position (sublane)", "on" if simulation.lateral else "off"),) if simulation else ()),
                ("Sampled every (s)", results.SAMPLE_S),
                ("Speed (simulated s a real s)", float(self.speed_combo.currentData())),
                ("Demand used", "daily (trips/day, by each pair's curve)" if self.od_matrix.mode == "daily" else "hourly (veh/h)"),
                *((("OD pairs with demand", len(simulation.demand)),
                   ("Pairs with no route", ", ".join(simulation.unreachable) or "none")) if simulation else ()),
                ("Network file", str(self.network_path) if self.network_path else "not saved"),
                ("Network", f"{len(network.nodes)} junctions, {len(network.roads)} roads, "
                            f"{len(network.approach_nodes)} boundary ends"),
                ("Driving side", network.driving_side), ("Background", where.get(background.kind, background.kind)),
                ("Queued below (km/h)", results.QUEUE_KMH)]
        rows += [(f"Extension: {name}", state) for name, state in self.extensions.settings()]
        return rows + list(extra or [])

    def _run_inputs(self, simulation: Simulation, demand) -> list:
        return results.input_sheets(self.network, demand, self.vehicle_modes.modes, dict(simulation.signal_plans))

    def _apply_hooks(self) -> None:
        """An extension that overrides signals was ticked or unticked: the run follows at once (the ones it had
        still report in its workbook, with any that were dropped)."""
        if self.runner is not None:
            override = self.extensions.signal_override()
            if override is not None and isinstance(self.runner, ProcessRunner):
                self.runner = self.runner.come_home()  # an extension decides in it from here: on a thread, here
            self._replaced.append(self.runner.simulation.signal_override)
            self.runner.simulation.signal_override = override

    def _override_seed(self, overridden: bool) -> None:
        """The menu bar's Seed box, or "Overridden" in its place while Batch Run (whose seeds a batch uses) is ticked."""
        self._seed_box_action.setVisible(not overridden)
        self._seed_overridden_action.setVisible(overridden)

    def _show_runs_on_map(self, on: bool) -> None:
        """Batch Run's Show runs on map: a headless test's run drawn on the map as it goes (the one the clock
        shows), or not (the map shows the network alone and the runs go a little faster)."""
        self.show_runs = bool(on)
        if not on and self.runner is None:
            self.scene.show_frame(1.0, None)

    def _play(self) -> None:
        if self.runner is None:
            self._run()
            return
        self.runner.play()
        self.clock.play()

    def _pause(self) -> None:
        if self.runner is not None:
            self.runner.pause()  # returns with its last step published
        self.clock.pause()
        self._frame(self.clock.time_s)  # clock, map and data all show that last step

    def _set_speed(self) -> None:
        self._show_saved()
        speed = float(self.speed_combo.currentData())
        self.clock.speed = speed
        if self.runner is not None:
            self.runner.set_speed(speed)

    def _stop_runner(self) -> None:
        """End the run, whatever ended it (Stop, a new Run, a change to the network, closing, its physics failing):
        its results become a workbook in the results folder."""
        if self.runner is not None:
            runner, recorder, replaced = self.runner, self.recorder, self._replaced
            self.runner, self.recorder, self._replaced = None, None, []
            self._end_run(runner, recorder, replaced)

    def _end_run(self, runner, recorder, replaced: list = ()) -> None:
        """_stop_runner's run, finished once its physics thread has ended: a step still going (a slow hook) is
        asked again shortly, the window carrying on meanwhile (closing then leaves it to results.recover).
        `replaced`: the overrides it had before its last (_apply_hooks), reported with it."""
        if not runner.stop(0.5):
            self.statusBar().showMessage("Stopping the run: its last step is still going.")
            QTimer.singleShot(200, self, lambda: self._end_run(runner, recorder, replaced))
            return
        self.extensions.report_failures()  # one that raised in the run's last steps too
        simulation = runner.simulation
        if recorder is not None:
            self.last_kpis = None

            def extras() -> None:
                recorder.add_sheets(self.extensions.reports([*replaced, simulation.signal_override]))  # TSP: every decision
                self.last_kpis = self._observed_kpis(simulation, recorder)
                recorder.add_analysis(experiments.analysis_sheets(self.last_kpis))

            try:
                path, _ = self._end_recording(recorder, simulation, self.results_folder, extras, runner.error)
            except Exception as exc:  # noqa: BLE001 - never in the way of Stop or closing; results.recover tries again
                self.statusBar().showMessage(f"Couldn't save the run's results: {exc}", 15000)
                return
            self.extensions.report_failures()  # and one whose report() raised
            if path is not None:
                self.last_results = path
                incomplete = f" (incomplete: {recorder.error})" if recorder.error else ""
                self.statusBar().showMessage(f"The run's results are saved{incomplete}: {path}", 15000)
        self._refresh_summary()

    def _export(self) -> None:
        """Export: a workbook of the run so far (it carries on), or of the last run."""
        if self.recorder is None and self.last_results is None:
            self._export_network()
            return
        recorder = self.recorder
        name = results.file_name(recorder.started, seed=recorder.seed, model=recorder.model) if recorder is not None \
            else self.last_results.name
        path, _ = QFileDialog.getSaveFileName(self, "Export Results", str(self.results_folder / name),
                                              "Excel workbook (*.xlsx)")
        if not path:
            return
        if recorder is None and Path(path).resolve() == self.last_results.resolve():  # the dialog's own suggestion
            self.statusBar().showMessage(f"The last run's workbook is already saved there: {path}", 8000)
            return
        try:
            if recorder is not None:
                if self.runner is not None:
                    try:  # TSP's figures so far: its sheet, the agent's decisions, latency and tokens
                        recorder.add_sheets(self.extensions.reports([*self._replaced, self.runner.simulation.signal_override]))
                    except RuntimeError:  # the run changed them while they were read: export without
                        pass
                    for _ in range(3):  # what it has observed so far, analysed (read while the physics carries on)
                        try:
                            recorder.add_analysis(experiments.analysis_sheets(
                                self._observed_kpis(self.runner.snapshot(), recorder)))
                            break
                        except RuntimeError:
                            continue
                recorder.export(path)
            else:
                results.write_whole(path, Path(self.last_results).read_bytes())  # a full disk keeps the old file
        except OSError as exc:
            QMessageBox.critical(self, "Export failed", str(exc))
            return
        self.statusBar().showMessage(f"Exported {path}", 8000)

    def _export_network(self) -> None:
        """Export with no run yet: the network for analysis (its City sheet, junctions, roads with their geometry
        and curves, ends, demand, vehicle types, bus stops, institutions) and its GeoJSON beside it."""
        name = f"ACMS network {time.strftime('%Y-%m-%d %H-%M-%S')}.xlsx"
        path, _ = QFileDialog.getSaveFileName(self, "Export Network", str(self.results_folder / name),
                                              "Excel workbook (*.xlsx)")
        if not path:
            return
        try:
            results.network_workbook(path, self.network, self._pair_demand(), self.vehicle_modes.modes,
                                     self._run_settings(None))
        except OSError as exc:
            QMessageBox.critical(self, "Export failed", str(exc))
            return
        self.statusBar().showMessage(f"Exported the network (no run yet): {path} and its .geojson", 8000)

    def _results(self) -> dict | None:
        """View Summary's Results page: the run going on, or the last one."""
        if self.recorder is not None:
            return {"kpis": self.recorder.kpis(), "running": True, "file": "", "error": self.recorder.error}
        if self.last_kpis is not None:
            return {"kpis": self.last_kpis, "running": False, "file": str(self.last_results or ""), "error": ""}
        return None

    def _stop_run(self, note: str = "") -> None:
        """Stop: end the run, clear its vehicles and put the clock back to 00:00:00."""
        self._stop_runner()
        self.clock.stop()
        self.scene.show_frame(1.0, None)
        if note:
            self.statusBar().showMessage(note, 8000)

    def _frame(self, seconds: float) -> None:
        """Each display frame: draw the run's latest published frame. The physics never waits for
        this; frames it published in between are skipped. The clock shows the physics' time."""
        runner = self.runner
        if runner is not None:
            self.extensions.report_failures()  # an override raised in the physics thread: switched off, saying why
            if runner.error:  # the physics raised: the run ends there, its workbook saying why (_end_run)
                self._stop_run()
                return
            frame = runner.frame
            seconds = frame.time
            self.clock.hold(seconds)
            alpha = min(max((runner.target() - frame.time) / DT, 0.0), 1.0) if runner.running else 1.0
            behind = runner.behind()
            now = time.monotonic()
            # Behind, its physics on a thread here: draw at most 10 frames a second, so drawing takes less time from
            # it. (In a process of its own, drawing takes nothing from it.)
            if not (behind and runner.shares_interpreter) or now - self._drawn_at >= 0.1:
                self.scene.show_frame(alpha, frame)
                self._drawn_at = now
            self.properties.show_plans(frame.plans)  # SCATS and SCOOT change theirs as they go
            tip = f"Behind: running at {runner.speed_now:.0f}×" if behind else "Simulation time"
            colour = f"color: {STATUS['waiting']};" if behind else ""
            if self.clock_label.toolTip() != tip:  # set when they change: a style sheet set again is worked out again
                self.clock_label.setToolTip(tip)
            if self.clock_label.styleSheet() != colour:
                self.clock_label.setStyleSheet(colour)
        if not self._testing:  # a headless test's runs have the clock meanwhile (_show_headless)
            self.clock_label.setText(results.clock(seconds))

    def _power_saver(self, on: bool) -> None:
        """Power Saver: the map draws a run 30 times a second instead of 60, and on a GPU without multisampling.
        The physics is untouched (it never waits for drawing)."""
        self.clock.timer.setInterval(2 * STEP_MS if on else STEP_MS)  # each step covers the time since the last
        self.view.set_power_saver(on)

    def _traffic(self, args: dict) -> dict:
        """The live run for the assistant (read_traffic): its latest published frame."""
        if self.runner is None:
            return {"error": "No live run is going (the user presses Run, ▷ ↻ in the menu bar corner, to start one). "
                             "run_simulation's runs are headless: read them with read_results or analyse_run."}
        road = str(args.get("road") or "") or None
        known = {*self.network.roads, *self.network.approach_roads, *self.network.nodes, *self.network.approach_nodes}
        if road and road not in known:  # a wrong id once matched nothing and said so only as "matching 0"
            own = tool_args._road_from_other_end(self.network, road)
            if own is None:
                return tool_reports.explain_error(f"Unknown id '{road}'.", self.network)
            road = own
        return self.runner.frame.records(road, int(args.get("limit") or 100))

    def _show_clock_state(self, running: bool) -> None:
        self.pause_action.setVisible(running)
        self.play_action.setVisible(not running)

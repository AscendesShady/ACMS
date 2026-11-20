"""Batch run, an extension with its own controls (panel(), see src/traffic_simulator/extensions.py).

Tests the AI controllers, each a signal override whose agent answers TRUE or FALSE, with different models over
different seeds and times, and exports the results. Its rows, in order:
- Seeds, typed like pages to print: "45, 34, 1-45" (each seed once, in the order written); Duration; Pace (how fast
  a model's runs go against the wall clock, for they never pause for an answer); Runs at once (never two on one
  model server); Times each (each model's runs repeated on every seed: a model's answers vary from run to run, and
  the repeats show by how much). One setting each for every controller.
- Compare with default (on at first): the one baseline, the default configuration: the network as configured, no
  controller acting, its signal plans as made with the network (Webster's from the demand, or timings entered from a
  survey). It runs once a seed (a seed always runs the same), and every agent is compared with it.
- Test TSP (Transit Signal Priority: priority for buses), Test DBL (Dynamic Bus Lane: a lane cleared for a bus) and
  Test DEVL (Dynamic Emergency Vehicle Lane: a lane cleared for an emergency vehicle), in that order, each run ticked
  in the Extensions tab or not, with its tab's choice (`app.controller_plan(kind)`, MainWindow.controller_plan, from
  the extension's batch_settings()): System 1 or System 2 (the model on in AI Mode), or Multiple, its ticked models,
  with its Decide within (each answer's deadline, in simulated seconds; missed: the plan). A ticked test needs a model.
  Every combination of the ticked controllers' models is an agent, run on every seed, Times each times; each run saves
  its workbook and telemetry feed, and the batch workbook compares every agent with the default.
- Test DEVL also dispatches DEVL's Dispatch Emergency Vehicle fleet at the start of each run, the default's too (each
  line on its own frequency), and records each emergency vehicle's trip; ticking it ticks DEVL, so its dispatch
  section is there to set (`app.tick_extension`). Test TSP and Test DBL need buses in the demand.
The line above Start counts the runs and their time as a sum, then, with models answering, divided by the runs at
once and the pace (each model server's runs one after another).
Show runs on map (the pin beside Start, on at first) draws the batch's run on the map as it goes, the one the menu
bar's clock shows (`app.show_runs`); off, the map shows the network alone. While a batch runs, the menu bar's clock
shows its runs' simulated time (its tooltip names each run going) and the status bar how many are done and about
how long is left.
Start Batch hands it all to the window (`app.run_batch`, MainWindow.run_batch). Replay a Run (the icon beside it)
runs one of those workbooks again from its telemetry feed, each decision as recorded (`app.replay_run`).
"""

import re

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QCheckBox, QComboBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QSpinBox, QWidget

from traffic_simulator.agent_choice import PACES
from traffic_simulator.widgets import ChoiceButtons, hint_label, icon_button, styled_button
from traffic_simulator.theme import SECONDARY_TEXT, STATUS

NAME = "Batch Run"
ORDER = 1
DESCRIPTION = "Test the AI controllers (TSP, DBL, DEVL) with their models over seeds and times, and export the results."

MAX_SEED = 2_147_483_647
MAX_RUNS = 10_000
TESTS = (("TSP", "Transit Signal Priority"), ("DBL", "Dynamic Bus Lane"), ("DEVL", "Dynamic Emergency Vehicle Lane"))
AT_ONCE = (1, 8, 4)  # Runs at once: least, most, at first
MAX_TIMES = 100
DURATIONS = [("5 min", 5), ("10 min", 10), ("15 min", 15), ("30 min", 30), ("1 hour", 60), ("Other…", None)]
app = None  # the window's services (run_batch, controller_plan, replay_run), set by the Extensions tab
_ui: dict = {}  # the panel's start() and its Other… minutes box, while it exists


def state() -> dict:
    """What the project file keeps beyond the labelled settings: Duration's Other… minutes (the box beside it)."""
    return {"other_min": _ui["custom"].value()} if "custom" in _ui else {}


def set_state(saved: dict, check_only: bool = False) -> None:
    """A project's Other… minutes back in its box (ValueError says why not)."""
    minutes = saved.get("other_min") if isinstance(saved, dict) else None
    if minutes is not None and not (type(minutes) is int and 1 <= minutes <= 24 * 60):
        raise ValueError(f"Batch Run: Other… minutes must be 1 to {24 * 60}.")
    if minutes is not None and not check_only and "custom" in _ui:
        _ui["custom"].setValue(minutes)


def start(seeds: str, minutes: int, tests: list[str], check_only: bool = False, pace: int | None = None,
          at_once: int | None = None, times: int | None = None, default: bool | None = None) -> dict:
    """The assistant's start_batch: these settings into the panel, then Start Batch (MainWindow.run_batch), `tests`
    the controllers' kinds (TSP, DBL, DEVL), their tabs giving the models; pace, at_once, times and default (Compare
    with default) left out: the panel's. `check_only`: checked, nothing changed. ValueError says what can't run."""
    if "start" not in _ui:
        raise ValueError("Batch Run's panel isn't open: see the Extensions tab.")
    return _ui["start"](seeds, minutes, tests, check_only, pace, at_once, times, default)


def span(minutes: float) -> str:
    """A length of time: "45 min", or "1.5 h" from an hour on."""
    return f"{round(minutes)} min" if minutes < 60 else f"{minutes / 60:.1f} h"


def parse_seeds(text: str) -> list[int]:
    """Seeds from a list like "45, 34, 1-45": numbers and ranges separated by commas, each seed
    once, in the order written. Raises ValueError saying what is wrong."""
    seeds: dict[int, None] = {}  # insertion-ordered set
    for part in filter(None, (part.strip() for part in text.split(","))):
        match = re.fullmatch(r"(\d+)\s*(?:[-–]\s*(\d+))?", part)
        if not match:
            raise ValueError(f"Can't read {part!r}: use numbers and ranges, like 45, 34, 1-45.")
        first, last = int(match[1]), int(match[2] or match[1])
        if last < first:
            raise ValueError(f"{part}: a range runs low to high, like {last}-{first}.")
        if last > MAX_SEED:
            raise ValueError(f"Seeds go up to {MAX_SEED:,}.")
        seeds.update(dict.fromkeys(range(first, min(last, first + MAX_RUNS) + 1)))  # a huge range stops early
        if len(seeds) > MAX_RUNS:
            raise ValueError(f"Up to {MAX_RUNS:,} runs in a batch.")
    if not seeds:
        raise ValueError("Enter at least one seed, like 45, 34, 1-45.")
    return list(seeds)


def panel():
    widget = QWidget()
    form = QFormLayout(widget)
    form.setContentsMargins(0, 0, 0, 0)
    seeds = QLineEdit("1-10")
    seeds.setPlaceholderText("e.g. 45, 34, 1-45")
    seeds.setToolTip("Seeds, e.g. 1-45")
    count = QLabel()
    count.setWordWrap(True)
    duration = QComboBox()
    for text, minutes in DURATIONS:
        duration.addItem(text, minutes)
    duration.setCurrentIndex(3)  # 30 min
    duration.setToolTip("How long each run simulates.")
    custom = QSpinBox()
    custom.setRange(1, 24 * 60)
    custom.setValue(45)
    custom.setSuffix(" min")
    custom.setToolTip("Duration in minutes")
    custom.hide()
    _ui["custom"] = custom
    length = QHBoxLayout()
    length.addWidget(duration, 1)
    length.addWidget(custom, 1)
    ticks = {}  # kind -> its Test tick
    for kind, _name in TESTS:
        ticks[kind] = QCheckBox()
        ticks[kind].setToolTip(f"{kind} tab's models")
    pace = ChoiceButtons([(f"{factor}×", factor) for factor in PACES])
    pace.setToolTip("Simulated seconds per second")
    at_once = QSpinBox()
    at_once.setRange(*AT_ONCE[:2])
    at_once.setValue(AT_ONCE[2])
    at_once.setToolTip("Runs going side by side")
    times = QSpinBox()
    times.setRange(1, MAX_TIMES)
    times.setToolTip("Runs per seed and model")
    default = QCheckBox()
    default.setChecked(True)
    default.setToolTip("Network's own signal plans")
    start = styled_button("Start Batch", "primary", "Run every seed and agent")
    replay = icon_button(0xE81C, "Replay a run")  # History
    show = icon_button(0xE707, "Show runs on map")  # a map pin: the batch's run drawn as it goes, or not
    show.setCheckable(True)
    show.setChecked(True)
    show.toggled.connect(lambda on: app is not None and hasattr(app, "show_runs") and app.show_runs(on))
    status = hint_label()

    def minutes() -> int:
        return duration.currentData() or custom.value()

    def plans(kinds: list[str] | None = None) -> dict:
        """The ticked controllers' tab settings ({kind: plan}, TESTS' order), or those of `kinds`. ValueError says
        what's wrong (a controller with no model picked: there's nothing of it to test)."""
        found = {}
        for kind, _name in TESTS:
            if not (ticks[kind].isChecked() if kinds is None else kind in kinds):
                continue
            if app is None or not hasattr(app, "controller_plan"):
                raise ValueError(f"Test {kind} reads the {kind} tab's models in the ACMS window.")
            found[kind] = app.controller_plan(kind)
            if not found[kind].get("agents"):
                raise ValueError(f"Test {kind}: pick a model to test in its tab (Multiple's Models, or System 1 or 2 "
                                 "with its model on in AI Mode).")
        return found

    def check() -> None:
        """Say how many runs the seeds, the tabs' models and times make and about how long they take, or what's
        wrong; Start needs valid choices."""
        custom.setVisible(duration.currentData() is None)
        try:
            found = plans()
            combos = [set()] if found else []  # each agent's model servers: every ticked controller's models crossed
            for plan in found.values():
                combos = [done | {plan["servers"].get(label, label)} for done in combos for label in plan["agents"]]
            seed_count, repeats, speed, side = len(parse_seeds(seeds.text())), times.value(), pace.value(), at_once.value()
            baseline = seed_count if default.isChecked() else 0  # the default: once a seed
            answered = seed_count * len(combos) * repeats
            runs = baseline + answered
            if not runs:
                raise ValueError("Nothing to run: tick Compare with default, or a test.")
            text = (f"{runs} run{'s' if runs > 1 else ''} of {minutes()} min = {span(runs * minutes())}: "
                    + "; ".join(part for part in (
                        f"the default once on each of {seed_count} seed{'s' if seed_count > 1 else ''}" if baseline else "",
                        f"{len(combos)} agent{'s' if len(combos) > 1 else ''} × {seed_count} seed{'s' if seed_count > 1 else ''}"
                        + (f" × {repeats} times" if repeats > 1 else "") if answered else "") if part))
            if answered:  # paced: wall-clock time, one run a server at a time
                by_server: dict = {}
                for servers in combos:
                    for server in servers:
                        by_server[server] = by_server.get(server, 0) + seed_count * repeats
                side_by_side = -(-answered // side)
                rounds = max([side_by_side] + list(by_server.values()))
                text += (f"; {side} at once at {speed}×: about {span(rounds * minutes() / speed)}"
                         + (" (one run a model server at a time)" if rounds > side_by_side else "")
                         + ("; the default as fast as the CPU allows" if baseline else ""))
            else:
                text += "; plain runs go one at a time as fast as the CPU allows, so under that"
            colour = SECONDARY_TEXT
        except ValueError as exc:
            runs, text, colour = 0, str(exc), STATUS["failed"]
        busy = app.busy() if app is not None and hasattr(app, "busy") else ""
        if busy and runs:  # a live run, this batch or another test: Start waits for it
            text += f". {busy} is going on: Start waits for it (Stop in the menu bar ends it)."
        count.setText(text)
        count.setStyleSheet(f"color: {colour};")
        start.setEnabled(bool(runs) and not busy)
        replay.setEnabled(not busy)

    # ponytail: the controllers' tabs' choices and whether a run or test is going on are read once a second while
    # the panel shows; signals from those tabs and the window if Start ever needs to follow them at once.
    follow = QTimer(widget)
    follow.setInterval(1000)
    follow.timeout.connect(lambda: widget.isVisible() and check())
    follow.start()

    def run() -> dict:
        """Start Batch: the panel's batch (MainWindow.run_batch), its outcome in the status line. ValueError: why not."""
        start.setEnabled(False)
        status.setStyleSheet("")
        status.setText("Running… (Stop in the menu bar ends it)")
        try:
            if app is None:
                raise ValueError("Batch Run starts runs from the ACMS window.")
            tests = {kind: {"agents": plan.get("agents") or [], "deadline_s": plan.get("deadline_s", 30)}
                     for kind, plan in plans().items()}
            done = app.run_batch(parse_seeds(seeds.text()), minutes(), times.value(), tests=tests, speed=pace.value(),
                                 at_once=at_once.value(), default=default.isChecked())
        except ValueError as exc:
            status.setStyleSheet(f"color: {STATUS['failed']};")
            status.setText(str(exc))
            raise
        else:
            stopped = f" Stopped: {done['runs']} of {done['of']} finished." if done["runs"] < done["of"] else ""
            note = f" {done['note']}" if done.get("note") else ""
            status.setText(f"Saved {done['file']}; each run's workbook in {done['folder']}.{stopped}{note}")
            return done
        finally:
            check()

    def started() -> None:
        try:
            run()
        except ValueError:
            pass  # run() says why in the status line

    def start_with(seed_text: str, length_min: int, kinds: list[str], check_only: bool, speed=None, side=None,
                   repeats=None, compare=None) -> dict:
        """start(): checked first, then into the panel and run."""
        parse_seeds(str(seed_text))
        if not 1 <= int(length_min) <= custom.maximum():
            raise ValueError(f"A run lasts 1 to {custom.maximum()} minutes.")
        unknown = [str(kind) for kind in kinds if kind not in ticks]
        if unknown:
            raise ValueError(f"No controller called {', '.join(unknown)}: tests are some of {', '.join(ticks)}.")
        if speed is not None and int(speed) not in PACES:
            raise ValueError(f"Pace is one of {', '.join(f'{factor}×' for factor in PACES)}.")
        for value, box, name in ((side, at_once, "Runs at once"), (repeats, times, "Times each")):
            if value is not None and not box.minimum() <= int(value) <= box.maximum():
                raise ValueError(f"{name} is {box.minimum()} to {box.maximum()}.")
        plans(kinds)
        if not kinds and compare is False:
            raise ValueError("Nothing to run: compare with the default, or test a controller.")
        if check_only:
            return {"ok": True}
        seeds.setText(str(seed_text))
        listed = duration.findData(int(length_min))
        duration.setCurrentIndex(listed if listed >= 0 else duration.count() - 1)  # else Other…, in minutes
        custom.setValue(int(length_min))
        for kind, tick in ticks.items():
            tick.setChecked(kind in kinds)
        if speed is not None:
            pace.set_value(int(speed))
        if side is not None:
            at_once.setValue(int(side))
        if repeats is not None:
            times.setValue(int(repeats))
        if compare is not None:
            default.setChecked(bool(compare))
        check()
        return run()

    _ui["start"] = start_with

    def replayed(path: str = "") -> None:
        """Replay a Run: a run's workbook, run again from its telemetry feed (`path`: the file dialog's choice)."""
        path = path or QFileDialog.getOpenFileName(widget, "Replay a run", "", "Run workbooks (*.xlsx)")[0]
        if not path or app is None:
            return
        status.setStyleSheet("")
        status.setText("Replaying… (Stop in the menu bar ends it)")
        try:
            done = app.replay_run(path)
        except ValueError as exc:
            status.setStyleSheet(f"color: {STATUS['failed']};")
            status.setText(str(exc))
            return
        differs = "; ".join(f"{figure} {was:g} then, {now} now" for figure, was, now in done["differences"][:3])
        status.setText(f"Saved {done['file']}: " + ("the same as the original." if done["same"] else
                                                    f"differs from the original ({differs}). Same network and demand open?"))

    replay.clicked.connect(lambda: replayed())
    seeds.textChanged.connect(check)
    duration.currentIndexChanged.connect(check)
    custom.valueChanged.connect(check)
    for tick in ticks.values():
        tick.toggled.connect(check)
    pace.changed.connect(check)
    at_once.valueChanged.connect(check)
    times.valueChanged.connect(check)
    default.toggled.connect(check)
    # Test DEVL dispatches DEVL's Dispatch Emergency Vehicle fleet: ticking it ticks DEVL, so its dispatch section
    # is there to set (unticking leaves it as it is)
    ticks["DEVL"].toggled.connect(lambda on: on and app is not None and hasattr(app, "tick_extension")
                                  and app.tick_extension("dynamic_emergency_lane"))
    start.clicked.connect(started)
    form.addRow("Seeds", seeds)
    form.addRow("Duration", length)
    form.addRow("Pace", pace)
    form.addRow("Runs at once", at_once)
    form.addRow("Times each", times)
    form.addRow("Compare with default", default)
    form.addRow(hint_label("The default: the network's own signal plans (Webster's from the demand, or timings "
                           "entered from a survey), set where the network is made."))
    for kind, _name in TESTS:
        form.addRow(f"Test {kind}", ticks[kind])
    form.addRow(count)  # full width: a wrapped label beside an empty title gets a stray blank line
    buttons = QHBoxLayout()
    buttons.addWidget(start, 1)
    buttons.addWidget(show)
    buttons.addWidget(replay)
    form.addRow(buttons)
    form.addRow(status)
    check()
    return widget

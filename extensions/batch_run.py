"""Batch run, an extension with its own controls (panel(), see src/traffic_simulator/extensions.py).

Runs the simulation headless once per seed (and model, and time), for a chosen duration, so a batch can be
repeated exactly (an LLM's answers aside) and its agents compared. Seeds are typed like pages to print:
"45, 34, 1-45" (each seed once, in the order written). Two ticks say what the runs test:
- Test TSP runs Transit Signal Priority (ticked in the Extensions tab or not) with the TSP tab's choice: System 1
  or System 2 (the model on in AI Mode), or Multiple, its ticked models, read from the tab (`app.tsp_batch`,
  MainWindow.tsp_batch, from the extension's batch_settings()) with its Decide within (each answer's deadline,
  in simulated seconds; missed: the plan), Pace (how fast the runs go against the wall clock, for they never
  pause for an answer), Runs at once (never two on one model server) and Times each. The baselines, Never
  preempt (general traffic: the plan) and Always preempt, always run beside them, each agent compared with Never.
- Test DEVL dispatches the Dispatch Emergency Vehicle tab's fleet at the start of each run (each line on its own
  frequency) with the DEVL tab's choice deciding its lane signals (`app.devl_batch`; the two baselines beside
  them), and records each emergency vehicle's trip, how long each lane signal was on and every decision. Both
  ticked: every TSP agent runs with every DEVL agent (the tabs must agree on pace, runs at once and times each).
The line under the seeds counts the runs and their time as a sum: minutes × seeds × agents × times, then, with
models answering, divided by the runs at once and the pace (each model server's runs one after another).
Show runs on map (the pin beside Start, on at first) draws the batch's run on the map as it goes, the one the menu
bar's clock shows (`app.show_runs`); off, the map shows the network alone. Ticking Test DEVL ticks the Dispatch
Emergency Vehicle extension (`app.tick_extension`), whose fleet each run
dispatches. While a batch runs, the menu bar's clock shows its runs' simulated time (its tooltip names each run going,
Never preempt the baseline) and the status bar how many are done and about how long is left.
Start Batch hands it all to the window (`app.run_batch`, MainWindow.run_batch): each run saves its own workbook
and telemetry feed, and the batch one compares the agents with the first. Replay a Run (the icon beside it) runs
one of those workbooks again from its telemetry feed, each decision as recorded (`app.replay_run`).
"""

import re

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QCheckBox, QComboBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QSpinBox, QWidget

from traffic_simulator.properties_panel import hint_label, icon_button, styled_button
from traffic_simulator.theme import SECONDARY_TEXT, STATUS

NAME = "Batch Run"
ORDER = 1
DESCRIPTION = "Run the simulation once per seed for a set time, testing TSP's models or emergency vehicles."

MAX_SEED = 2_147_483_647
MAX_RUNS = 10_000
DURATIONS = [("5 min", 5), ("10 min", 10), ("15 min", 15), ("30 min", 30), ("1 hour", 60), ("Other…", None)]
app = None  # the window's services (run_batch, tsp_batch, replay_run), set by the Extensions tab
_ui: dict = {}  # the panel's start(), while it exists


def start(seeds: str, minutes: int, test_tsp: bool, test_devl: bool, check_only: bool = False) -> dict:
    """The assistant's start_batch: these settings into the panel, then Start Batch (MainWindow.run_batch), the TSP
    and DEVL tabs giving the models. `check_only`: checked, nothing changed. ValueError says what can't run."""
    if "start" not in _ui:
        raise ValueError("Batch Run's panel isn't open: see the Extensions tab.")
    return _ui["start"](seeds, minutes, test_tsp, test_devl, check_only)


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
    length = QHBoxLayout()
    length.addWidget(duration, 1)
    length.addWidget(custom, 1)
    test_tsp = QCheckBox()
    test_tsp.setToolTip("TSP tab's models")
    test_devl = QCheckBox()
    test_devl.setToolTip("DEVL tab's models")
    start = styled_button("Start Batch", "primary", "Run every seed and agent")
    replay = icon_button(0xE81C, "Replay a run")  # History
    show = icon_button(0xE707, "Show runs on map")  # a map pin: the batch's run drawn as it goes, or not
    show.setCheckable(True)
    show.setChecked(True)
    show.toggled.connect(lambda on: app is not None and hasattr(app, "show_runs") and app.show_runs(on))
    status = hint_label()

    def minutes() -> int:
        return duration.currentData() or custom.value()

    def plans(tsp_on: bool | None = None, devl_on: bool | None = None) -> dict:
        """The TSP and DEVL tabs' batch settings for the ticked tests ({"TSP": plan, "DEVL": plan}), or those
        asked for (a test with no model picked runs its baselines alone); ValueError says what's wrong (both
        ticked: the same pace, runs at once and times each)."""
        found = {}
        for kind, on, service in (("TSP", test_tsp.isChecked() if tsp_on is None else tsp_on, "tsp_batch"),
                                  ("DEVL", test_devl.isChecked() if devl_on is None else devl_on, "devl_batch")):
            if not on:
                continue
            if app is None or not hasattr(app, service):
                raise ValueError(f"Test {kind} reads the {kind} tab's models in the ACMS window.")
            found[kind] = getattr(app, service)()
        if len([plan for plan in found.values() if plan.get("agents")]) == 2:
            differ = [name for key, name in (("speed", "Pace"), ("at_once", "Runs at once"), ("times", "Times each"))
                      if found["TSP"][key] != found["DEVL"][key]]
            if differ:
                raise ValueError(f"The TSP and DEVL tabs set different {', '.join(differ)}: make them the same.")
        return found

    def check() -> None:
        """Say how many runs the seeds, the tabs' models and times make and about how long they take, or what's
        wrong; Start needs valid choices."""
        custom.setVisible(duration.currentData() is None)
        try:
            found = plans()
            plan = next(iter(found.values()), {})
            per_test = [["", ""] + [plan["servers"].get(label, label) for label in plan["agents"]] for plan in found.values()]
            combos = [set()]  # each agent's model servers: every TSP agent with every DEVL agent, both baselines first
            for servers in per_test:
                combos = [done | ({server} if server else set()) for done in combos for server in servers]
            seed_count, times = len(parse_seeds(seeds.text())), plan.get("times", 1)
            runs = seed_count * len(combos) * times
            text = (f"{runs} run{'s' if runs > 1 else ''}: {minutes()} min × {seed_count} seed{'s' if seed_count > 1 else ''}"
                    + (f" × {len(combos)} agents (with both baselines)" if len(combos) > 1 else "")
                    + (f" × {times} times" if times > 1 else "") + f" = {span(runs * minutes())}")
            alone = [kind for kind, test in found.items() if not test.get("agents")]
            if alone:  # no model picked: its baselines alone, as a batch always runs them
                text += "; " + " and ".join(alone) + " decided by the baselines only (never and always preempt): pick a model in its tab to test one"
            if any(test.get("agents") for test in found.values()):  # paced: wall-clock time, one run a server at a time
                by_server: dict = {}
                for servers in combos:
                    for server in servers:
                        by_server[server] = by_server.get(server, 0) + seed_count * times
                answered = seed_count * times * sum(1 for servers in combos if servers)  # the baselines aren't paced
                side_by_side = -(-answered // plan["at_once"])
                rounds = max([side_by_side] + list(by_server.values()))
                text += (f"; {plan['at_once']} at once at {plan['speed']}×: about {span(rounds * minutes() / plan['speed'])}"
                         + (" (one run a model server at a time)" if rounds > side_by_side else "")
                         + "; the baselines as fast as the CPU allows")
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

    # ponytail: the TSP and DEVL tabs' choices and whether a run or test is going on are read once a second while
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
            found = plans()
            tsp, devl = found.get("TSP", {}), found.get("DEVL", {})
            plan = tsp or devl
            done = app.run_batch(parse_seeds(seeds.text()), minutes(), plan.get("times", 1), agents=tsp.get("agents"),
                                 deadline_s=tsp.get("deadline_s", 30), speed=plan.get("speed", 1),
                                 at_once=plan.get("at_once", 4), test_tsp=bool(tsp), test_devl=bool(devl),
                                 devl_agents=devl.get("agents"), devl_deadline_s=devl.get("deadline_s", 30))
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

    def start_with(seed_text: str, length_min: int, tsp_on: bool, devl_on: bool, check_only: bool) -> dict:
        """start(): checked first, then into the panel and run."""
        parse_seeds(str(seed_text))
        if not 1 <= int(length_min) <= custom.maximum():
            raise ValueError(f"A run lasts 1 to {custom.maximum()} minutes.")
        plans(tsp_on, devl_on)
        if check_only:
            return {"ok": True}
        seeds.setText(str(seed_text))
        listed = duration.findData(int(length_min))
        duration.setCurrentIndex(listed if listed >= 0 else duration.count() - 1)  # else Other…, in minutes
        custom.setValue(int(length_min))
        test_tsp.setChecked(tsp_on)
        test_devl.setChecked(devl_on)
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
    test_tsp.toggled.connect(check)
    test_devl.toggled.connect(check)
    # Test DEVL dispatches the Dispatch Emergency Vehicle tab's fleet: ticking it ticks that extension, so its
    # schedules are there to set (unticking leaves it as it is)
    test_devl.toggled.connect(lambda on: on and app is not None and hasattr(app, "tick_extension")
                              and app.tick_extension("dispatch_emergency"))
    start.clicked.connect(started)
    form.addRow("Seeds", seeds)
    form.addRow(count)  # full width: a wrapped label beside an empty title gets a stray blank line
    form.addRow("Duration", length)
    form.addRow("Test TSP", test_tsp)
    form.addRow("Test DEVL", test_devl)
    buttons = QHBoxLayout()
    buttons.addWidget(start, 1)
    buttons.addWidget(show)
    buttons.addWidget(replay)
    form.addRow(buttons)
    form.addRow(status)
    check()
    return widget

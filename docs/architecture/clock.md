# clock.py: simulation time (Qt)

- `SimulationClock`: `run()` starts again from 00:00:00, `pause()` holds, `play()` carries on,
  `stop()` halts and goes back to 00:00:00. While running, a precise 16 ms timer (one step per
  display frame, 60 Hz) advances `time_s` by the real time elapsed × `speed` (measured each step, so
  late ticks lose nothing) and emits `ticked(seconds)`; `state_changed(running)` follows Pause/Play.
  During a run it only paces the display: `hold(seconds)` sets it to the physics' time each frame.
  `timer.setInterval(ms)` changes the step (Power Saver: 32 ms, 30 frames a second).
- **Run** builds a fresh `Simulation` from the network, the OD matrix as the Demand Generator shows
  it (hourly or daily, with timetabled shares fixed) and the vehicle types, and starts its `Runner`
  (the physics thread) at the chosen speed; **Play** carries on (or runs, if nothing has); **Pause**
  holds the physics; **Stop** ends the thread and clears the vehicles (its workbook is written once the thread
  has ended: a step still going, a slow hook, is asked again every 0.2 s, the window carrying on; a physics that
  raised ends the run at the next display frame, its workbook marked Incomplete); the speed choice applies at
  once. A change to the network or the vehicle types (`_commit`, undo or redo) stops the run (an action that left the
  snapshot as `History.current` had it, such as re-clicking a checked option, changes nothing and stops nothing), with a
  note: it drives the network as it was, a copy of it and of the types taken at Run (an edit changes the
  network before it stops the run, so the run's workbook still describes what it drove, and the physics thread
  never reads a half-edited network). A demand change doesn't: the OD matrix's edits, the
  assistant's included, and undoing or redoing them (`_demand_changed`, `_restore` when only the
  demand differs) hand the new demand to the run (`Simulation.set_demand`), which carries on.
  Closing the window ends the thread and stops the clock (no frame, `draw()` or `algorithm()` after it). One thing at a time: while a test runs (a batch, an experiment, the
  assistant's checks: `_in_background`) **Run** and **Play** are off, their tooltips naming it ("Busy: Batch
  run"; `busy()`, `_show_busy`), `_run` refuses any other way in, and a second test is refused naming the first.
  Closing the window mid-test stops the test (as Stop) and closes once it has stopped, so its caller still gets
  its result and its workbooks are written whole.
- `MainWindow._frame` (each display frame) draws the runner's latest published frame: vehicles part
  way between their last two steps, signals lit. The physics never waits for it; frames published
  in between are simply skipped, and if the physics is behind (a speed the CPU can't manage) the map
  draws at most 10 frames a second so drawing takes less time from it, and the clock turns amber
  with the speed really achieved in its tooltip. The clock shows the physics' time.
- Measured on the real Windows platform (OpenGL view), default grid: at 60× with 800 and with 1,100
  vehicles the physics ran at 60.0× and the map at 60 frames a second (worst frame 34 ms). Offscreen, an
  8 × 8 grid at 500 veh/h an end (992 OD pairs) kept pace at 1× to 10× (626 vehicles at 10×); at 60× with
  1,500 vehicles it reached about 40× (the physics alone, headless, about 66×; `observe` costs about 8%).
- Controls in the menu bar's top-right corner, VS Code style (`MainWindow._build_sim_bar`): 14 px
  icons with their names in tooltips, 8 px clear of the window edge. **Run** (restart arrow), **Play**
  or **Pause** in one spot (Pause shows while running, Play while not), **Stop** (square), the clock
  (hh:mm:ss; during a headless test, the least advanced of its runs' time, `_show_headless`: its tooltip "Slowest test
  run's time", that run by name and time in the status bar), a speed choice (1×, 2×, 5×, 10×, 60×), "Seed" and its box (while
  Batch Run is ticked, "Overridden" in the box's place, `_override_seed`: a batch uses its own seeds), **Export** (the run so far as
  an Excel workbook with its GeoJSON, or the network alone before any run, results.py; the last run's workbook is
  copied to a `.part` beside the chosen file and moved into place, so a failed copy leaves that file as it was). **Stop** also ends a test the assistant is running (experiments.py).
- **Watch my edits** (the eye, last in the menu bar; `MainWindow.watch_action`;
  activity.py's `ActivityLog`, `MainWindow._watch`): while on, an app-wide event filter and the focus changes log
  every button click (a tick box's new state), menu choice and the window's actions (shortcuts such as Ctrl+Z;
  the eye's own on and off once each, as "watch" lines),
  drop-down choice, slider position, entry (a text or number field's final value as it loses focus or Enter is
  pressed; a password "(hidden)"), tab opened, map click (metres) and selection (`select`), and each undo step's
  changes (`_log_edits`, `_state_changes`, from `_commit` and undo/redo), each with its time, seconds since it
  began, where (the open tab or a dialog's title), what (the control's text, form label, placeholder or tooltip)
  and value, to `activity.csv` in a dated folder, `ACMS activity <date time>`, in the results folder (as the Research Log's
  and a batch's are), flushed line by line. One gesture seen two
  ways within 0.4 s is logged once. A click or a choice also logs what it changed besides itself (`_state` when it is
  pressed, `_changes` after): a field or list the app filled ("set": a Random button's pick), a box it ticked, a
  message it showed ("shown") and the status bar's ("message"); read-only text and the clock (`activity_skip`) aside. Each Research Log step takes the lines since the one before (`take`,
  `_research_step`: its `activity.csv`); with none taking them, `pending` keeps the last `KEEP_PENDING` (5,000). A logger only: it never asks the assistant anything.
  A line it can't write (a full disk: `OSError`) turns it off (`failed`, `MainWindow._watch_failed`): the eye unticks
  and the status bar says why, once.
- Extensions animate from the clock too, redrawn up to 10 times a second while it runs.

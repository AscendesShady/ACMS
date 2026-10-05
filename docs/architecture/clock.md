# clock.py: simulation time (Qt)

- `SimulationClock`: `run()` starts again from 00:00:00, `pause()` holds, `play()` carries on,
  `stop()` halts and goes back to 00:00:00. While running, a precise 16 ms timer (one step per
  display frame, 60 Hz) advances `time_s` by the real time elapsed × `speed` (measured each step, so
  late ticks lose nothing) and emits `ticked(seconds)`; `state_changed(running)` follows Pause/Play.
  During a run it only paces the display: `hold(seconds)` sets it to the physics' time each frame.
- **Run** builds a fresh `Simulation` from the network, the OD matrix as the Demand Generator shows
  it (hourly or daily, with timetabled shares fixed) and the vehicle types, and starts its `Runner`
  (the physics thread) at the chosen speed; **Play** carries on (or runs, if nothing has); **Pause**
  holds the physics; **Stop** ends the thread and clears the vehicles; the speed choice applies at
  once. A change to the network or the vehicle types (`_commit`, undo or redo) stops the run, with a
  note: it drives the network as it was. A demand change doesn't: the OD matrix's edits, the
  assistant's included, and undoing or redoing them (`_demand_changed`, `_restore` when only the
  demand differs) hand the new demand to the run (`Simulation.set_demand`), which carries on.
  Closing the window ends the thread.
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
  (hh:mm:ss), a speed choice (1×, 2×, 5×, 10×, 60×), the Seed box and **Export** (the run so far as
  an Excel workbook with its GeoJSON, or the network alone before any run, results.py). **Stop** also ends a test the assistant is running (experiments.py).
- Extensions animate from the clock too, redrawn up to 10 times a second while it runs.

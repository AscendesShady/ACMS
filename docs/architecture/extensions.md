# extensions.py: the Extensions tab (Qt)

- An extension is one file `extensions/<name>.py` (names starting with `_` are skipped) with at
  least one hook, plus optional `NAME`, `DESCRIPTION` and `ORDER` (its place in the tab, then by file
  name). `DESCRIPTION` is for the assistant only (read_extensions, set_extension; without one, the docstring's first
  paragraph): the tab shows no text under a tick, so each extension's settings stand alone. A row with settings is
  as wide as they are open, folded or not, so the sidebar is sized for them and opening one never clips:
  - `draw(network, t)`: Qt graphics items in metres, drawn after every map redraw
    (`NetworkGraphicsScene.redrawn`) and up to 10 times a second while the clock runs; `t` is the
    simulation time. The items it returned last are removed first. During a run they draw over the
    network's cached still layer whatever their z (network_scene.md).
  - `panel()`: a QWidget of its settings, in a **collapsible section**: a flat ▸/▾ arrow beside its
    checkbox shows or hides it (collapsed at first; ticking the box opens it), and it can be used
    only while ticked. Rows without settings keep the arrow's width, so every checkbox lines up.
  - `signal_override(junction, arm, state, context, agent)`: inside the signals (see simulation.py,
    Signal override); returns "G", "Y" or "R".
  - `algorithm(frame)`: up to 10 times a second during a run, with the run's latest `Frame`.
  - `log(event)`: once each AI Mode answer ends (a step), on the UI thread (`ExtensionsPanel.log`; one that
    raises is switched off). The event (`AIModePanel._end_step`, completed by `MainWindow._research_step`): `kind`
    (prompt), `prompt`, `editor_update` (what was sent before it), `provider`,
    `model`, `started`/`ended`, `plan` (the model's Plan: line), `tools_offered`, `calls` (each tool call: `tool`, `arguments`, `what`, `plan` (the approval
    card's), `decision` (read, applied, skipped, not decided, refused: arguments, refused: check), `result`, `at`,
    `proposed_at`/`decided_at`, `compute_s`, `check_s`: an edit's check before its card, an OpenStreetMap
    download included; compute counts both), `messages` (the model's, thinking included), `reply`, `error`,
    `usage` (each model request's tokens and seconds, `assistant.USAGE`, those tagged with this step's start by
    `assistant.USAGE_TAG`: not another thread's that ended meanwhile), `changes` (the step's changes to the
    network, demand and vehicle types, `MainWindow._state_changes`), `map_png` (the map view as it is after it,
    `_map_png`; only when the network changed), `demand_png` (the OD matrix after it, `_demand_png`; only when the
    demand changed), `results_folder`, `project_file` and `network`. The window takes the state before a step and builds
    the event only while a ticked extension defines `log` (`ExtensionsPanel.wants`).
  - `report(agent)` (optional, beside one of those): when a run whose overrides it supplied ends (and at
    Export), `{"sheets": [(name, rows, what it holds)], "measures": {key: value}}`: its sheets go in the
    run's workbook after Trips (named in its Read me), its measures in a batch's comparison.
    `ExtensionsPanel.reports(overrides)` calls them with that run's signal agent; one that raises gives a
    sheet saying so.
  - `app` (optional, a module variable): the tab sets it to the window's services, `run_batch`, `replay_run`,
    `tsp_batch`, `agent_options`, `dispatch_emergency`, `places`, `signal_agent` and `busy` (what is using the
    simulator, "" when nothing) (`MainWindow`, `AIModePanel`),
    on load and whenever they're given.
- Nothing runs until its box is ticked, and unticking makes it neutral again: with no ticked
  extension defining `signal_override` the simulation is handed none and calls nothing.
  `ExtensionsPanel.signal_override()` wraps the ticked ones (`SignalOverrides`); `hooks_changed`
  tells the window, which hands it to the run at once (`MainWindow._apply_hooks`) and at each Run.
- **Can't break the editor:** `network` is a deep copy, made once per map redraw, so changes to it
  never reach the real network. Loading and every hook run inside try/except (including
  `SystemExit`): a failure switches that extension off and shows the error with its line in the tab.
  A `signal_override` that raises (or returns anything but G/Y/R) in the physics thread is dropped at
  once, the plan standing (its arm state and lanes are both checked before either counts, so it leaves no
  lane green behind, even for that step), and listed in `SignalOverrides.failed` and the tab's own list of failures from
  every run, live or headless (a batch's, an experiment's, a replay's). `report_failures()` switches each off,
  saying why (`fail`): on the live run's next frame and when it stops, and while a test runs (its 100 ms poll)
  and as it ends, the status bar then naming them. The run's workbook gets an **Extensions dropped** sheet
  (`reports`, from `SignalOverrides.dropped`): the run went on without it from then, so its figures aren't
  that extension's alone.
- The tab is only the ticks (and the settings of an extension that has them): the files are read when
  the app starts (`reload()`, which a test still calls; its box's tooltip names the folder). No Reload or
  Open Folder button.
- `extensions/transit_signal_priority.py`, **Transit Signal Priority (TSP)** (a collapsible section:
  who decides, **Agent choice** below; System 1 or 2 sets the agent's `system` and `model` (`tsp_agent.model`, sent
  as each question's `choice`), the Run sheet records it; `batch_settings()` gives Batch Run what to test): ticked, each signalised junction **shows its own coordinated plan, exactly,
  unless priority is acting for a bus** (`_Junction.mode`: "plan", "tsp", "recovering"), whatever its
  timing method, so with nothing granted coordination is untouched (a run is the plain run, vehicle for
  vehicle). Each junction has its own `TSPController` (below), built from the method's plan the first time
  the junction is seen (a new run, or a gap over 2 s, starts afresh), following it as it changes
  (`_follow`, every step: each phase's green is the method's current one, SCATS and SCOOT adapting theirs,
  and the longest red it allows grows with them), and stepped once a second, on the whole second:
  - **plan**: before each step the controller is put in the plan's state (`_sync`, `TSPController.sync`:
    the interval running just before the second, from `plan_at`; a green ending at its last whole second, so
    a hold is decided before the plan would end it; when each other phase's green last ended, from
    `plan_greens`) and offered the buses. If it acts (HOLD, TRUNCATE or SKIP logged that step) it takes over
    from exactly where the plan is, an episode beginning; otherwise what it would have done is dropped.
  - **tsp**: the controller runs the junction while a bus's request is on.
  - **recovering**, once the last bus has passed (no request on): back to the plan the shorter way round
    (`_recover`): at each green's onset the controller ends that green with the plan's same green if it can
    (`set_green`), cut to its minimum when behind the plan, lengthened by up to `RECOVER_LONGER` (a fifth of
    its plan green) when ahead; the plan takes over again as soon as both show the same phase's green for at
    least its minimum green, the green carrying on. A new request on the way goes back to tsp.
  `episodes` holds each: [taken over, last bus passed, plan back]. Phases P1…Pn are the plan's, with its
  greens, amber and a minimum green of 7 s (or the green, if shorter); the controller's **all-red** is the
  plan's, lengthened only where amber and all-red together can't let a bus that entered as the green ended
  clear the junction: max(plan, ceil((`box_m` + 12 m) / 8 m/s − amber)), the plan's 2 s on the default grid.
  (It was the bus's whole crossing time, 5 s there: +3 s at every change while off the plan cost the junctions
  about a sixth of their capacity, so giving priority made traffic worse than the plan; a bus given priority is
  held in green until it has cleared anyway.) Each arm's check-in distance is its
  speed limit × the longest time its phase can take to turn green (+1 m); a bus (its `class` in `BUS_TYPES`: its
  type's `vehicle_class`, whatever the type is called) on the
  arm within it, or held in a queue further out (slower than `QUEUED_KMH`, 5 km/h, behind a vehicle in its
  lane), checks in (with the vehicles ahead of it in its lane), stays in however far the queue then carries it,
  is updated each second, and checks out once it is neither on the arm nor crossing the junction. The controller steps once a
  second; its (phase, interval) becomes each arm's G/Y/R (all-red: R). Its **gate** is the agent's
  TRUE/FALSE for the active bus request, read once a second while a bus is checked in (`priority(junction,
  view, request, controller, agent)`, the agent being the run's signal agent): TRUE lets the controller
  hold the green (up to 15 s) or bring it forward, within its limits; FALSE: the plan. A TRUE is refused when
  the lead bus's exit lane has no room for it (`_room`: under `ROOM_NEEDED_M`, 14.5 m, the bus and its gap, by
  the view's `exit_room_m`; logged BLOCKED, `TSPController.note`): no change to the signals could get it
  through. Nothing agent-side lives in the extension. `W_max` is the
  plan's longest red plus a hold and the recovery credit (15 s + 10 s per other phase).
  - `report(agent)`: the run's **TSP** sheet (who decided, the model, bus requests, answered in time, too
    late (the answer reached the controller after its buses had checked out), fell back at the deadline,
    TRUE and FALSE, the TRUE share, errors, latency mean/median/95th percentile/most, how many simulated
    seconds after the request the answer counted, tokens in and out, System 1's mean P(yes), seconds of
    green held and greens cut short, and the same by junction) and its **Agent decisions** sheet (every
    request: when, junction, approach, phase, buses, system, model, answer, when it counted, latency,
    P(yes), tokens, error, System 2's reply, what was observed); measures `tsp_requests`, `tsp_true`,
    `tsp_green_held_s`, `tsp_greens_cut`, `agent_latency_s`, `agent_errors`, `agent_fell_back`, `agent_late`.
    What came of each request (`outcome`, `OUTCOMES`, from the answer and what the controller logged for the
    request's phase until its release): **served** (TRUE, and it held or brought forward the bus's green),
    **not needed** (TRUE, the plan served it), **blocked** (TRUE, no room downstream), **not feasible** (TRUE,
    no extension or early green in time), **refused**, **rejected** (by the guard), **fell back**, **too
    late**, **withdrawn**, **no answer**, **no LLM** (Never preempt, the baseline: nobody to ask, the plan from the request
    on, `SignalAgent.asks`; Always preempt's requests are TRUE and go on to served, not needed...): counted in the TSP sheet and given per request in Agent decisions
    (with its guard verdict), measures `tsp_served`, `tsp_not_needed`, `tsp_denied`, `agent_rejected`; and
    **off the plan**: the times priority took over, the time off the plan, how long back to the plan took
    after the last bus (mean, most), any not back by the end (`tsp_off_plan_s`, `tsp_back_to_plan_s`).
    The TSP sheet also counts requests withdrawn before they were asked (the bus had gone) and System 2's
    answers by the tool; Agent decisions has each answer's Via (tool or text), Reason and the telemetry
    read; **Agent per minute** (`per_minute`) has each simulated minute's requests raised, answers reaching
    the controllers (TRUE, FALSE, fell back), requests withdrawn, latency mean and 95th percentile, how long
    after their requests the answers counted, and the most questions waiting in the agent's queue (sampled
    each second, `_backlog`). Its file `telemetry.jsonl` (the feed, a JSON message a line) goes beside the
    run's workbook.
  - **Watching it** while a run goes: `draw` rings each junction with a bus request on (`halo`: the active
    request's state; none once the clock has moved more than 5 s from the run, e.g. after Stop), in the
    theme's status colours (`HALOS`): amber and dashed, asking the agent; green, priority granted; grey,
    refused; red, fell back to the plan; blue, returning to the plan after a bus; its tooltip names it. The
    panel's line also counts the junctions off the plan. `algorithm` keeps a line under the panel's
    buttons (`live_text`): bus requests, questions waiting, answered, fell back, TRUE, latency median and
    95th percentile, errors.
- **TSP agent** (src/traffic_simulator/agent_transit_signal_priority.py): everything the agent does
  for TSP. `system` ("1" or "2") is who decides. The agent reads only **telemetry**: each second a
  request is on, `priority` publishes the junction's JSON message (`telemetry`, schema `SCHEMA`
  `acms-tsp-telemetry/1`, kept in the run's `feed`): `request` as a bus would send it (approach, phase,
  buses, distance to the stop line, speed, `eta_s`, `arrives_on` (red or green: `eta_s` against when the bus's
  green starts or ends, as the signals stand), vehicles ahead, `passengers` on board by its type's
  occupancy, `exit_room_m` on the lane the lead bus leaves by and `exit_has_room`; after SAE J2735's signal request),
  `spat` (the current phase and interval, the bus's phase and its state, when its green ends or starts
  by the controller's plan; after J2735's SPaT) and `approaches` (each one's queued and approaching
  vehicles, and `people_queued`); `person_delay` (`person_delay(message, controller)`, HCM 2000 Ch 14's person
  weighting: the bus's passengers × the red priority would save them against the people queued across × the green
  taken from them, from the plan; none without a controller plan), kept as the request's `estimate`; and, with
  the extension's **Look ahead** set (`lookahead_s`, seconds; tooltip "Rollout horizon"), `lookahead`
  (`look_ahead`: the next seconds played twice on forks, `Simulation.rollout`, priority for up to E_max then the
  plan, against the plan alone: person-seconds of delay in each, how far its buses get, which it favours;
  approximate, the run untouched). The TSP report and sheet add each decision's estimate and whether the answer
  agreed with it (`agent_agrees_estimate`, an experiment measure). `perturbed(message, change)` gives the
  telemetry with one fact changed (no_passengers, exit_full, no_cross_queues, arrives_on_green, no_estimate) for
  AI Mode's decision_sensitivity. Each request (`key_of`: "junction/phase/check-in time") is asked once (`question_for`):
  - System 1 (AI Mode's System 1: Jev or Laya in Ollama): an encoder reads words well but neither compares numbers nor
    weighs factors in one yes/no (tried on Laya, see sources.md), so its state is the telemetry's facts in
    words (`facts`: `exit_lane` clear or full, `more_people_on` bus or cross streets, the bus's passengers
    against the people queued on the other approaches), each read with one noul (`SYSTEM_1_QUESTIONS`:
    exit_clear, more_on_bus) in one request; TRUE when both are yes (the lowest P(yes) at 0.5 or more; its Reason gives each). TRUE is permission for
    the whole approach: the controller holds or brings forward the green only if and when the bus needs it;
  - System 2: zero-shot and role-based (`SYSTEM_2_ROLE`): told it is the traffic signal engineer
    supervising TSP at the junction, what the telemetry holds, that it decides once for the bus's approach and
    the controller acts only if and when the bus needs it, and what to weigh;
    given the telemetry as JSON, it answers by calling the boolean tool **`decide_tsp(activate, reason)`**
    (`DECIDE_TOOL`); a model that can't call tools (`ToolsUnsupported`) replies TRUE or FALSE in words.
  **The guard** (`MainWindow._ask_agent`): only a decision is one: a decide_tsp call whose `activate` is a
  boolean (or the word true or false, as small models send it), or with no call a reply that is a bare TRUE or
  FALSE; System 1's P(yes) a number in 0–1 for each of its questions. Anything else is the plan, its record saying "rejected: …" (never
  read into: "It is not true that…" is no TRUE); every answer's record has its `guard` verdict.
  It goes through the run's signal agent with `urgency` = when the bus reaches its stop line and `once`:
  the soonest bus is asked first, with the freshest telemetry, and a request whose buses have gone
  before it was asked is withdrawn (`release`, from the extension after each controller step). The
  answer stands for the request and counts from when it reaches the controller: a run never pauses for
  it. Past the agent's `deadline_s` (simulated s; a batch's Decide within) the request falls back to the
  plan (`fell_back`), and so does an answer that came later. The assistant's unpaced headless tests wait
  instead (`wait_s`), each answer counting its latency later in simulated time, as if the run went at 1×.
  Every decision is in the feed too (`tsp_decision`: the exact simulated time it reached the controller, and
  what the agent reported, `AGENT_REPORTED`), and so is each withdrawn question (`tsp_withdrawn`); the report
  adds the answers that came too late to count (`tsp_late_answer`). That makes the feed a replay's input:
  **`Replay(lines)`** is an agent that asks nobody and gives each request the decision recorded for it, at
  its recorded time, so the run comes out as it did (`MainWindow.replay_run`, Batch Run's Replay a Run). Written `decisions[junction]` win (here, or from
  any thread during a run). Each run's record is its own (`Run`: requests, feed, open questions; `RUN`, with
  `requests` and `feed` by name, when none is given; `reset`), and every question carries both systems'
  forms (System 1's state and noul, System 2's messages and tool) with `system` the extension's choice:
  whoever answers takes theirs (`MainWindow._ask_agent`: a batch agent's own system, else that choice), so
  runs with different agents go side by side. The extension re-imports it on each load (at start).
- **Per-run state** (the TSP extension): each run's controllers, last time, backlog and the agent's record
  (`_Run`) are found by the run's signal agent (`_runs`, a run without one under `NOBODY`; a new run when its
  time goes back or jumps, or its agent was reset since (`SignalAgent.generation`: the window's one agent is reset
  at every Run, so Stop then Run at once is a new run too; DEVL's `_run_for` the same); the last `KEEP_RUNS` 16 kept, twice a batch's most at once; created under a lock,
  as a batch's runs start side by side). The map and the panel show the window's live run (`app.signal_agent`,
  the Extensions tab's services), else the one stepped last (`_shown`); `report(agent)` reads the agent's run.
- **TSP controller** (src/traffic_simulator/transit_signal_priority.py, stdlib only, no simulator
  imports): `TSPController(NodeCfg, gate)` for one isolated node, dt = 1 s. `on_checkin`,
  `on_update`, `on_checkout` feed detections before each `step(t) -> (phase_id, interval)`; interval
  is "G", "Y" or "AR". A fixed single-ring phase sequence as the base plan, plus green extension
  (hold, up to `E_max`), red truncation (other greens cut to their minimum, latched until the bus's
  phase turns green), merged requests per phase and a queue, request timeout, and recovery: green
  lost to TSP is owed back as credit (up to `C_max` a cycle) and the priority phase pays it back.
  Validation refuses unsafe settings (amber + all-red below the bus's clearance, check-in too close, a base
  red over `W_max`); asserts guard minimum greens, `E_max`, `W_max` and one active request. Every
  decision is in `log` as (t, event, info): CHECKIN, CHECKOUT, HOLD, END_EXT, TRUNCATE, SKIP,
  RELEASE, RELEASE_END, TIMEOUT. Alongside a plan it doesn't own: `sync(t, phase, interval, began, green,
  red_since)` puts it in that plan's state before a tick (requests kept, its own changes cleared), so it can
  take over exactly where the plan is; `set_green(seconds)` sets the green running (at least its minimum, no
  credit or debt: a transition back to the plan); `note(t, event)` logs the caller's events (BLOCKED). Its 17
  tests run under `__main__`.
- **Agent choice** (src/traffic_simulator/agent_choice.py, `AgentChoice`: the rows TSP's and DEVL's panels share):
  **Decided by**, radio buttons, System 1, System 2 or Multiple (`ChoiceButtons`). With System 1 or 2, **Model**, a
  dropdown of that system's models from AI Mode's `agent_options()` (System 1: Jev and Ollama's decision models, as Laya; System 2: every Ollama chat
  model, Grok and Gemini when their keys are on the PC), listed afresh as it opens, the first at first: it answers
  live runs (the question's `choice`, which `MainWindow._ask_agent` uses before AI Mode's own) and a batch's. None
  listed: AI Mode's own choice (System 1's Use, System 2's started model; the window finds it, `_agent_plan`).
  **Multiple** is for Batch Run: **Models**, a dropdown of ticks (`CheckList`, widgets.py: a combo box of
  checkable items that stays open while ticking and shows what is ticked; a plain list popup, not Fusion's menu-like one, so a long list shows 10 rows and scrolls by its bar or the wheel; a click toggles its row once, on release, the popup never seeing it) listing the same, but for the baselines
  (Never and Always preempt, which always run); **Decide within**, a slider of 1–280 s (30 s at first: each answer's deadline in
  simulated seconds; missed, the extension's fallback); **Pace**, radio buttons 1×, 2×, 5×, 10× (simulated seconds a
  real second: a batch's runs where a model decides keep in step with the wall clock and never pause for an answer,
  so a model's latency costs pace × its seconds; its baselines run as fast as the CPU allows); **Runs at once** (1–8, 4 at first; never two on one model server); **Times each**
  (1–100: runs per seed and model, an LLM's answers varying). A live run asks the System chosen last.
  `batch_settings()`: deadline_s, speed, at_once, times and agents (labels) with their model servers, or `system` when
  no model is listed to pick. All set by label too (`set_settings`, the assistant's set_extension).
- `extensions/dynamic_emergency_lane.py`, **Dynamic Emergency Vehicle Lane (DEVL)** (a collapsible section: who
  decides, Agent choice above, and a live figures line): a signal override like TSP's, run after it for each arm of
  each signalised junction every step. Each junction has its own controller (`_Junction`; each run's found by its
  signal agent, `_run_for`, as TSP's), showing the state it is given (the plan, or TSP's) unless it is clearing:
  - **detection**, like TSP's check-in: an emergency vehicle (dispatched, or a type in `EMERGENCY_TYPES`) on an arm
    within `DETECT_S` (30 s) of the stop line at the arm's speed limit, or held in a queue further out (`in_queue`:
    slower than `QUEUED_KMH` behind a vehicle in its lane), checks in, and stays in until it leaves the arm; its request (key
    "devl/junction/arm/vehicle") is asked once through the run's signal agent, the soonest to arrive first; it checks
    out once it has left the arm. Telemetry (`telemetry`, schema `acms-devl-telemetry/1`, kept in the run's feed):
    the vehicle (distance, speed, `eta_s`, lane, `in_the_lane`), the arm's signal, `lane_ahead` (vehicles ahead in
    the kerbside lane, how many queued under 5 km/h) and `approach_queued`. Both forms (`question_for`): System 1 the
    facts in words (`traffic_in_lane`: queued, moving or none; the signal) with one noul, "Is traffic in the
    emergency vehicle's lane?"; System 2 zero-shot and role-based (`SYSTEM_2_ROLE`, the engineer supervising the
    lane) calling **`decide_devl(activate, reason)`** (`DECIDE_TOOL`), TSP's guard (`MainWindow._ask_agent`).
    Nobody to ask (no signal agent): TRUE, the rule; a batch's baselines: `SignalAgent.rule`, Never preempt FALSE (general traffic), Always preempt TRUE. TSP's answer rules: it counts from when it
    reaches the controller; past the deadline, FALSE (fell back).
  - **clearance**, on TRUE (`update`, `state`): "to_red", every arm ends its green after its minimum green (as TSP
    never cuts one shorter), then amber, then all-red; "clear", only the **kerbside lane** of the vehicle's arm
    (lane 0, kerbside, either driving side) shows green, (the arm's "R", {lane: "G"}), every other lane and arm red, so the queue ahead of
    the vehicle (which keeps to that lane, Simulation's `EMERGENCY_LANE_M`) clears and it goes through; once it has
    left the arm (or after `CLEAR_MAX_S`, 60 s) "lane_amber" ({lane: "Y"}) and "all_red", then the plan again, or
    straight on to the next granted request. One that left before its lane went green ends at the all-red.
  - Its lane signal (`draw`): a square head behind each arm's own, on the kerb, its lamp flashing green
    (`FLASH_PERIOD_S`) while that arm's kerbside lane is cleared (`lane_state`), dark otherwise; the panel's line
    counts requests, TRUE, fell back, lanes cleared and junctions clearing now (`live_text`).
  - `report(agent)`: the run's **DEVL** sheet (requests, granted, refused, fell back, decided by the rule, lanes
    cleared and their green in all, request to lane green, agent latency) and **DEVL decisions** (each request:
    asked, junction, approach, lane, vehicle, system, model, answer, answered, fell back, lane green, checked out,
    latency, guard, reason, error), measures `devl_requests`, `devl_true`, `devl_fell_back`, `devl_cleared`,
    `devl_lane_green_s`, `devl_to_green_s`, `devl_latency_s`, and its feed as **devl telemetry.jsonl** beside the
    workbook.
  The overrides (`SignalOverrides`) pass each the state the one before left and keep the last lanes set; a
  batch run gives TSP and DEVL each their own signal agent (`agent` a dict by extension name, `agent_of`).
- `extensions/dispatch_emergency.py`, **Dispatch Emergency Vehicle** (a collapsible section): on top, **Add
  Schedule** (+) and **Delete Schedule** (−, removes the ticked rows), with a **seed** box and **Random Emergency** (a
  lightning icon) sending the seed's own emergency vehicle (`random_emergency`: from `Random(seed)`, a delay of 0 to
  `MAX_DELAY_MIN`, 10 min, then two places it can go between) into the live run that long after the click, the same
  seed giving the same one. Then a row per schedule: a tick to select it; **From** and **To** (editable dropdowns,
  their names as placeholders, of the network's boundary ends and junctions, `app.places`, listed as they open), each
  after a small flat **Random** button (role "mini": just its shuffle glyph; a random end or junction, never the other
  box's); **Every** or **After** and its minutes (0–120): every so many minutes until the run ends (0: once now), or
  once that many minutes from now. Under the rows, **Dispatch Selected** sends the ticked rows and **Dispatch Fleet**
  every row into the live run (`app.dispatch_emergency`, MainWindow.dispatch_emergency, Simulation.dispatch), after
  checking them all (one that can't go sends none). A line under it says what was sent, or in red why not (no run,
  nothing ticked, an unknown place, no route). `fleet()` gives the rows, (from, to, every min, after min), for Batch
  Run's Test DEVL; `set_fleet(schedules)` replaces them as + would add them (the assistant's schedule_emergency).
- `extensions/batch_run.py`, **Batch Run** (a collapsible section of settings): one headless run per
  seed (and model, and time). Only time, seeds and two ticks:
  - **Seeds** are typed like pages to print, `45, 34, 1-45` (`parse_seeds`: numbers and ranges,
    each seed once in the order written, up to 10,000 runs). A line below counts the runs (seeds ×
    models × times), or says in red what is wrong, and Start Batch waits for a valid choice. It waits too
    while a live run, a batch or another test is going on (`app.busy()`), the line saying which; Replay a Run
    with it. The panel looks again once a second while it shows.
  - **Duration** of each run: 5, 10, 15 or 30 min, 1 hour, or Other… (a minutes box, up to 24 h).
  - **Test TSP** (a tick box, off at first): runs Transit Signal Priority whether or not it is ticked in the
    tab, with the TSP tab's choice (`app.tsp_batch`, MainWindow.tsp_batch): its model (System 1 or 2), or Multiple's
    ticked models, with their Decide within, Pace, Runs at once and Times each. The baselines, **Never preempt** (TSP's
    controllers with nobody to ask: the coordinated plan, exactly) and **Always preempt** (every bus request
    TRUE), always run too; every agent is compared with Never preempt, seed by seed, and Always preempt brackets them.
  - **While it runs**: the menu bar's clock shows the batch's runs' simulated time (`MainWindow._one_run` records
    each run's time as it steps, once a simulated second, in `_headless`; `_show_headless` shows the least
    advanced of those going), its tooltip each run by name (Never preempt (baseline), seed 1: 00:12:34 of 00:30:00), and
    the status bar how many are done and about how long is left at the pace so far; the same for an experiment
    or run_simulation. Afterwards the clock is the live run's again. **Show runs on map** (the pin beside Start,
    on at first; `app.show_runs` → `MainWindow._show_runs_on_map`): the map draws that same run as it goes, as a
    live run is drawn: each run's thread takes a frame at most 10 times a real second (`run_headless`'s
    `on_frame`, `_headless_frames`) and the window shows the one the clock follows; off, or once the batch ends,
    the map shows the network alone.
  - **Test DEVL** (a tick box; ticking it ticks the Dispatch Emergency Vehicle extension, `app.tick_extension` →
    `MainWindow._tick_extension`, so its fleet is there to set): each run dispatches that tab's fleet at its start
    (`MainWindow._emergency_fleet`: every row checked first; none is fine) and its own **random emergency
    vehicle**, drawn from the run's seed (`random_emergency`, its delay within the run: the same for every agent
    on that seed, so they're compared on the same one; the Run sheet names it) with DEVL's override clearing their lanes, the DEVL
    tab's choice deciding (`app.devl_batch`; DEVL's two baselines, Never and Always preempt, beside them), with its own signal agent; its
    workbook gets an **Emergency vehicles** sheet (`emergency_report`: each emergency vehicle's trip, from, to,
    dispatched, arrived, travel time; "on the road" if it hadn't arrived when the run ended) and DEVL's sheets,
    measures `emergency_trips`, `emergency_arrived`, `emergency_trip_s` (the mean of those that arrived) and DEVL's,
    labelled in the batch's Comparison (`AGENT_MEASURES`: emergency travel time lower is better) and in its
    Headline as the emergency travel time reduction. An emergency vehicle that reaches the end of its route (an
    end, or a junction's stop line) leaves the network then, as any vehicle does (`Simulation._move`).
  - Both ticked: every TSP agent runs with every DEVL agent ("TSP <model> · DEVL <model>", the two baselines
    first), the tabs agreeing on Pace, Runs at once and Times each (else it says which differ). With no model picked (or
    none ticked under Multiple, or none on in AI Mode), a test runs its two baselines alone (Never and Always
    preempt), as fast as the CPU allows, and the line under the seeds says so.
  - The line under the seeds counts the runs and their time as a sum: minutes × seeds × agents (with both baselines) ×
    times = the simulated total; with models answering, divided by the runs at once and the pace ("about …"), each
    model server's runs one after another (said when that makes it longer); plain runs go one at a time as fast as
    the CPU allows, so under the total. It follows the tabs (read once a second while a test is ticked).
  - **Start Batch** calls `app.run_batch` (MainWindow.run_batch, see main.md) and says where the
    workbooks went, or in red why it couldn't run. `start(seeds, minutes, test_tsp, test_devl, check_only)` does the
    same for the assistant's start_batch: checked, then set in the panel (a duration not listed: Other…) and run.
  - **Replay a run** (an icon button beside it): pick a run's workbook; `app.replay_run` (MainWindow.replay_run)
    runs it again from the telemetry feed beside it, each decision as recorded, and the line says whether the
    new workbook's Summary is the same as the original's (else the first figures that differ, and asks
    whether the same network and demand are open).
- **Research Log** (`extensions/research_log.py`, ORDER 5): every AI Mode step, and every batch run, recorded for
  research, through `log(event)` (a batch is a step of kind "batch", `MainWindow._log_step`: run_batch with its
  settings, the batch's and every run's workbooks with their SHA-256 (no picture: a batch changes nothing), the activity since the step
  before; failed: its error; the agent "no agent" when a batch starts the session). A session folder in the results folder, `ACMS research log <date time> <agent>/` (the provider and
  model of its first step, `agent_name`, with any character Windows refuses in a name as `-`; started by the first step
  after it's ticked, or after **End session**; a name already taken within the second gets ` (2)`): `session.json`
  (started, ended, the agent, the version, the SHA-256 of src/traffic_simulator/*.py, Python, PySide6, the OS, the session's
  verdict and note; never a key); per step `steps/NNNN/`: `prompt.md` (and the
  editor update sent with it), `understood.json` (the Plan: line and the tools offered), `plan.md` (the approval card), `calls.jsonl`,
  `reply.md`, `messages.json`, `changes.md`, `map.png` and `demand.png` (the step's snapshot: its change list, the map after it if
  the network changed and the OD matrix after it if the demand changed; no picture when nothing changed), `artifacts.json` (the workbooks and GeoJSON its tools wrote, with size and SHA-256; build_scenario lists its
  `files`), `activity.csv` (with Watch my edits on, what the user did since the step before), `step.json` (all of it, with timings: the model's seconds, the user's deciding, compute, total; and tokens);
  `steps.csv` (a row a step, for analysis) and `report.html` (each step as prompt → understood → did → snapshot →
  numbers, under the session's verdict), both rewritten each step and each verdict, from the folder; once the session
  has ended, `<session folder name>.odt` (`_write_document`, Qt's ODF writer; Word and LibreOffice open it), the whole
  session as one document to read: its facts (times, agent, version, code SHA-256, Python, Qt, OS, verdict), then each
  step's prompt and editor update, what was understood, the plan, every tool call with its arguments, decision and
  result (each cut at `DOC_RESULT_CHARS`, 4000; the step's files keep it whole), the model's messages, the reply, the
  changes, the map and/or OD matrix after (embedded), the files it wrote, timings, tokens and your verdict, rewritten when you grade
  the ended session or its chats; and in the
  results folder `sessions.csv` (`_write_sessions`: a row a session, its steps, how many of each verdict and unmarked,
  its models, its verdict and note). Everything is read back from the folders (`sessions`,
  `session_info`, `steps`), so any session can be graded later. Its panel: where it records, the latest step, a note,
  **As intended**, **Partly as intended**, **Not as intended** (`mark(verdict, note, step=None, folder=None)`: a
  step's verdict and note into its step.json, the CSV and the report; the latest step by default), **End session**
  (`end_session`: its end time into session.json, the next step starts another; then History on it), **History**
  and **Open log folder**. **History** (`History`, a dialog): the sessions newest first (chats, ended or going on,
  verdict), the chosen session's chats (verdict first) and the one clicked (prompt, understood form, tool calls and
  decisions, reply, error, changes, the map), a note and the three verdicts for the chat, and for the session
  (`rank_session(verdict, note, folder)`), and **Open report**.

- `campaign_apply.py`, **Campaign Apply**: opt-in one-time live update of an already-running CLI prompt campaign. Reload and tick it to retain the pending request and ledger while installing the user-authorized automatic Apply driver. It refuses halted or ambiguous drivers and never resends a prompt. Unticking does not undo the installed update. See `prompt_campaign.md`.

Campaign Apply additionally supports strictly checked recovery of a deadline-halted, previously clicked card whose callback never returned a tool result, and guards old-process callback exceptions. It returns an explicit error to the existing model worker without repeating the action.

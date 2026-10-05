# 8. Tests

Plain `assert` functions, run with `python tests/test_x.py` or all at once in `run_monitor.py`:
- `test_simulation.py`: an emergency vehicle dispatched two and a half minutes later; an emergency vehicle obeying red, but going on its arm's kerbside lane green from whichever lane it is in (not on that lane's amber); a lane obeying its own signal (an override's lane state: only the kerbside lane goes through, on either driving side, lane 0) and emergency vehicles keeping to that lane before each signal; emergency vehicles dispatched from an end or a junction, once or every minute, an impossible route refused; junction saturation by control (roundabout entry capacity, falling with circulating
  traffic; a priority junction's minor arm against its major flows; a signal's arm lane by lane, turns slower);
  the give-way capacities re-measured on the simulated drivers (a minor arm and a roundabout entry held
  saturated, within 10 % of the tables); vehicles made from their type; Krauss stopping behind a standing leader;
  SUMO's speed factors (mean 1, deviation 0.1, about 95 % within 0.8–1.2, none below 0.2) and free
  speeds (a bicycle at its desired 20 km/h); the timing methods calibrated to the simulated drivers
  (an hour of a standing queue by the HCM's field method: saturation flow within 4 %, start-up lost
  time within 0.3 s, amber used within 0.4 s, SCATS's space time within 0.2 s of the constants) with
  none of the queue held at the line by the vehicle ahead of it;
  lanes on the driving side and cut at stop lines; arrival patterns' means and variances (Random
  exact, binomial < Poisson < negative binomial); spawn totals and bus headways; daily demand
  following its curve; 20 minutes of busy traffic through signals, a roundabout and an uncontrolled
  junction: no entry on red and no overlaps; the physics side-neutral (a network and its mirror image
  driven on the other side, demand and junctions mirrored, clearing the same traffic within 7 % over
  two seeds, while the same demand on the other side, a different turn mix, clears over 15 % more);
  two-phase signals with no offside turns and their
  detours (straight on and U-turn back, the kerb-side fallback by a boundary end, mirrored driving on
  the right, a four-phase junction keeping its turn) and each lane's reach along the route; 20
  minutes of right-turners detouring through heavy traffic both ways: every U-turn from the offside
  lane, every turn back from the kerb (those coming back let in by the saturated platoons beside them),
  no lane change undone, some U-turns clearing on amber, nobody stuck; no lane changes
  where roads forbid them; each vehicle's next landmark (slip road, signal, roundabout, in route order)
  closing in as it drives, and in `read_traffic`; Webster's
  cycle and greens against hand-worked numbers, the busy phase getting the long green and the run
  using the plan, Fixed time keeping its greens; the runner keeping real time on its thread,
  pausing and changing speed; the window's Run (Webster note in the junction editor), live signal
  heads, `read_traffic` data, a demand edit and its undo carrying the run on, and a network edit
  stopping it; demand handed to a run under way: from the next minute, a removed pair's flow and bus
  timetable stopping, a new pair's starting, a pair with no route listed.
- `test_bus_stops.py`: a stop placed mid-way and clamped clear of the junctions (a roundabout's
  circle too), followed through reversal, one-way, a split and deletion; each new stop in the middle
  of the longest free stretch, at a road end when only the ends have room, and refused with nothing
  changed when no bay fits; a road too short refused; save/load (dwell time too) and validation; the
  assistant's change lines; the bay on the kerb side driving left and right; 20 minutes of buses and
  cars both ways with three bays: only buses in bays, every bus that finished calling at each stop on
  its way and no other, each standing that stop's dwell time (20 s and 45 s, plus any wait for a
  gap), cars driving past a bus in its bay, no overlaps; and in the window: the bus icon's direction
  menu, the new stop selected with its distances in the panel and on the map, arrow nudges (0.5 m,
  Shift 5 m, across the road nothing) as one undo step after the add's own, a mouse drag, its dwell
  time edited and undone, typing the distance from the other end, re-pointing it, Del and undo, and
  a second stop placed clear of the first.
- `test_signal_timing.py`: Robertson's dispersion (the recurrence, every vehicle kept); the cyclic
  queue against the hand-worked uniform delay q·r²/2(1 − q/s), stops and over capacity; the HCM delay
  against a hand calculation; greens filling a cycle with their minimums; SCATS's DS, its cycle rising
  by at most 6 s a cycle under saturation and falling under target, a split plan chosen on its second
  vote; SCOOT's split moving 4 s to the busier phase (1 s stored), its cycle up 4 s over target and
  down 8 s under it; realigning to an offset; TRANSYT on a four-junction corridor (one cycle, a better
  PI, each green starting as the platoon arrives, the signals using its offsets); the hill-climb never
  worse and progression offsets; Synchro's cycle longer for heavier demand, greens for the 90th
  percentile; loops counting every vehicle over a stop line once, the upstream loops seeing each of
  them, a queue standing on the loop at red; every method running 15 minutes of traffic with no red
  entries or overlaps, SCATS and SCOOT changing their plans as they go and the others not; TSP following
  SCOOT's greens; and the junction editor's methods, settings per method, greens for Fixed time only,
  the cycle limits keeping their order and the live note.
- `test_models.py`: numbering, trapezoid lengths, add/delete nodes, roads and ends, a grid's default signal
  timing (every junction, added ones, saved, an unknown method refused),
  drag, end naming (1.5E), signal validation, crossings, splits.
- `test_geometry.py`: polyline primitives, including the nearest point along a line.
- `test_gpu.py`: CPU stand-ins told from GPUs; without OpenGL QPainter drawing the vehicles (half way, turned)
  and the offscreen view not asking; and in a child process on the Windows display driver, drawing into a
  hidden framebuffer (headless): the renderer a real GPU; the vehicles from one instanced draw where they
  are (half way between poses, turned, the right way up, still a few pixels at 20 m a pixel, not sent
  again); 5,000 vehicles sent once and each display frame under 2 ms (0.03 ms here) and over 20× faster
  than QPainter; and the editor itself, never put on screen, running traffic at 10×: an OpenGL viewport,
  every display frame's vehicles through the GPU (about 60 a second), each physics frame sent once, and
  the physics keeping over 8× of 10×. It fails on a PC without a usable GPU.
- `test_selection.py`: a line meeting a box (through it, from inside, across a corner, passing by);
  a window taking only the junction inside it and a crossing box its four roads too, a bus stop's bay
  by window and by crossing, and everything boxed deleted to an empty, valid network; and in the
  window through the view's mouse events: a window, a crossing box, Ctrl adding to it, Del asking
  (the other roads named) and deleting it as one undo step, a click on empty map clearing it, and no
  box while Add Node is on (its click still places a node); Ctrl+clicking roads in and out of the
  selection, Add Slip Road shown only for two roads meeting at a junction, its menu (Single lane, Two
  lanes) building a single-lane slip road, undone in one step; its junction made a roundabout in the
  Edit tab moving it out.
- `test_background.py` (no test reaches the internet; a stand-in answers for Nominatim): place search by
  its usage policy (the tiles' User-Agent, the URL, the pole left out, the cache, nothing found, an
  unreadable answer, offline, at least a second between requests) and in the window (one place under the
  network's centre during Choose Location, three in a menu and one picked, nothing found moving nothing);
  the scene true to scale round its georeference
  (1 km east and south on the ground), tile names against OSM's standard formula at four zooms, the zoom
  whose pixels match the screen's, a huge area falling to a coarser zoom; the tile store fetching once,
  keeping, refreshing a stale tile, not re-asking a failed one within a minute, and sending its
  User-Agent; each kind drawn (a colour, a PNG with its proportions kept, a PDF's first and second
  pages, the map once its tiles arrive); the background saved, loaded and validated; and in the window:
  a swatch, the map placed at the network's centre, undo and redo, an image laid as wide as the network
  and centred, an unreadable file refused, the view scrolling over the image, a run carrying on through
  a background change and its undo, Generate keeping the background; Lock (the tools and fields off,
  Move refused, undone), Measure through the view's mouse events (the line following the mouse, the
  distance, the network not clicked), Set Scale (the first point staying put, the line exactly its true
  length, the width scaled, still locked, the reference kept in image pixels, one undo step) and the
  reference noting its new length once the width is typed, Measure over a colour without Set Scale;
  Choose Location first when the map is chosen (the network hidden, the tabs, Generate and the grid
  toggle off, Use This Location in place of Move, the outline and cross; a drag moving the place under
  the cross by its ground distance, the wheel zooming about the cross; Use This Location georeferencing
  it at the network's centre; the globe and Esc keeping the place it had; a bigger grid generated with the
  place under its centre; Map chosen again and Esc giving the colour back); Set Scale first for a new
  image (the network hidden, the length asked after the second click, a cancel asking again, 50 m laid
  made 100 m, one undo step) and skipped with Esc;
  the lock and reference saved, loaded and validated; the two-point fit recovering a
  known scale, turn and corner exactly; a turned image drawn where its turned rectangle lies and a
  turned map covering the whole view; a turned map keeping ground distances (1 km down the screen is
  1 km east with north to the right) and round trips; and through the view's own mouse events: Move
  dragging the image by the screen distance (the network not clicked) as one undo step, Esc ending it,
  Two Points putting both picked pixels on their points and undone in one step, the same point twice
  refused, the readout matching the georeference at the mouse (east of the centre, north once turned),
  Move on the map shifting the point under the centre, leaving the tab ending the tool, and no readout
  over a colour.
- `test_interchanges.py`: the keep side for both driving sides, SPUI, trumpet layout, smooth curves,
  stored curvature matching the drawn curve, curvature through edits and save/load,
  drivable ramp radii and speeds, keep/replace, refusals; a change of driving side mirroring a
  diamond, SPUI and trumpet into exactly the ones built for that side (every road's line, curvature,
  lanes, speed and bridges, both ways) and back, the records saved, loaded and validated; and 20
  minutes of traffic through a flipped trumpet: no overlaps, and arrivals within 5 % of a trumpet
  built for that side. A slip road on either driving side, one lane or two, from the roads in either order: one-way,
  between two uncontrolled nodes, on the fastest route, entered and left only from the kerbside lane,
  carrying traffic; refused (nothing changed) straight on, for roads that don't meet, and with no room;
  a two-way road refused as single-lane. A slip road moved out once when its junction becomes a
  roundabout (40 m of lane to the give-way line, all of it outside the circle), and 30 minutes of four
  flows through roundabout and slip each arriving near their rate; refused, changing nothing, on roads
  too short for it.
- `test_roundabouts.py`: categories, diameters and circulating speed.
- `test_history.py`: undo/redo and merging.
- `test_theme.py`: contrast of every text/background pair.
- `test_create_tab.py`: the two sections opening and closing (Generate Network open at start, road settings,
  signal timing and grid size in it); the chosen signal timing kept as the network's default, reaching a new
  junction and every junction of a generated grid, undo restoring it; the driving
  side flipping a signal head to the other kerb at once, stopping a run, one undo step, naming the
  interchange mirrored, undo and redo restoring the choice; the road settings kept
  as the network's defaults for new roads (a road added gets them), a step each, the roads there
  untouched, undo restoring the fields with no steps of their own; the map controls in the view's
  top-right corner, Fit and Reset; the Input Grid toggle shown on the map only while Demand
  Generator is open and swapping the network and the grid, both toggles in step, the grid's rows following an end deleted while it shows, Demand Generator
  opening on the grid and able to show the network; the sidebar scrolling in a short window with
  every section open but not once they close, and no tab or AI Mode page making it scroll at 1280 × 800;
  the OD Pair editor only while a cell is picked (hidden at first, shown on a click, put away by Esc, the
  diagonal and leaving the tab).
- `test_transit_signal_priority.py`: the controller's own 17 tests (run as a subprocess: taking over where a plan is, setting the green running, a change interval (amber + all-red) too short for a bus to clear refused while a short all-red alone is not, among them), all PASS.
- `test_extensions.py`: files found and named, load errors with their line, nothing runs until ticked,
  items redrawn after a map redraw and reload, the real network untouched, a raising extension switched
  off while the others carry on, a failing `panel()`; Run, Pause, Play, Stop and Export in the menu bar
  corner and the clock following the physics; Batch Run's collapsible settings (shut at first, opened by
  the arrow or by ticking, usable only while ticked), seed lists read like pages to print (order,
  repeats, en dash, refusals), the run count and error line, Other… duration, outside the window saying
  it starts runs from there; only time, seeds and the Test TSP and Test DEVL ticks; the time as a sum (minutes ×
  seeds × agents × times; plain runs under it); in the window (a stand-in `app`): Test TSP reading the TSP tab's
  plan, the sum divided by the runs at once and the pace, each model server's runs one after another (said so),
  following the tab; Test DEVL too, every TSP agent with every DEVL agent, different paces refused; the Run sheet's
  record, Start handing seeds, minutes, times, models, deadlines, pace, runs at once and both ticks to `run_batch`
  and saying where it saved, no model picked refusing Start, the ticks set by `set_settings` (off: plain runs
  again); the tab's order (Batch Run, TSP, DEVL, Dispatch); TSP and DEVL alike: System 1 / System 2 / Multiple,
  Model listing System 1's models then System 2's and set by label, a long Models list showing 10 rows and scrolling, its first and last both ticked by a click each (not toggled back by the release), a double click two toggles, the list closing at once, Multiple showing Models (listing the agents but
  No LLM, ticked by label in any case, an unlisted one refused), the 1–280 s slider, Pace, Runs at once and Times
  each, all set by `set_settings` as the assistant does, and `batch_settings()` giving them; TSP's picked model in
  its questions' `choice`; DEVL all off with no emergency vehicle; TSP handing the run
  no override while unticked; ticked with nothing granted, the run the plain run, signal for signal and vehicle
  for vehicle (the plan kept); with TRUE written at one junction for 15 min: no red entries, no failures,
  check-ins and check-outs, holds or truncations there only, the others never leaving their plans, every
  junction on its plan showing it exactly, and each priority episode back on the plan within two cycles of
  its last bus; a full exit lane refusing a TRUE (BLOCKED, logged with its phase); each request's outcome from
  its answer and the controller's log (served, not needed, blocked, not feasible, refused, rejected, fell
  back, too late, withdrawn, no answer); with a waiting
  agent for System 1 and then System 2: one question per bus request, both reading the junction's JSON
  telemetry (schema, request, SPaT, approaches), System 1's as its state with a noul, System 2's a role prompt
  (no examples) with the telemetry as JSON and the boolean tool decide_tsp, the telemetry feed's requests and
  one decision each; the agent's queue taking the soonest bus first, a waiting question replaced by fresher
  telemetry, a withdrawn one never asked, once keys not asked again; a paced run's deadline in simulated
  seconds falling back to the plan, a gone bus's waiting question withdrawn; with a live agent held back,
  the map's ring asking (amber, dashed), then granted (green) once it answers, the panel's live figures, no
  ring once the clock is back at the start, and the Agent per minute sheet (a row each minute, every request
  in its minute, the backlog sampled); and the report's TSP sheet (requests,
  TRUE, model, tokens, green held or cut) and Agent decisions; an answer counting its latency later in
  simulated time, the deadline falling back to the plan for good (the late answer kept, unused), a live
  agent's answer counting when it reaches the controller, nobody to ask giving FALSE, written decisions
  winning; DEVL (a head per arm, only while ticked; with nobody to ask, an emergency vehicle sent through three
  signals with traffic checking in at each in turn and getting through, each junction clearing only the
  kerbside lane of its arm, every other arm red, green then amber then the plan; its DEVL sheet, measures and
  telemetry feed; the lane signal flashing while cleared, dark otherwise); DEVL's agent asked once per junction
  (FALSE at the first: only the others cleared; No LLM: the rule, all three; answers past the deadline: none, fell
  back), its telemetry and both systems' forms, and its DEVL decisions sheet; Dispatch Emergency Vehicle's schedules (Random small and flat, never picking the
  other end, Dispatch Selected sending none unticked and the ticked one, + adding a row, Dispatch Fleet sending none
  when one can't go, then each on its frequency, After sending one later, − deleting the ticked row; Random Emergency: the same seed the same vehicle, another seed another, never a pair
  that can't go, and why not when nothing can); an override really changing the signals
  vehicles obey, FALSE restoring the plans, and a raising one dropped and switched off with its line;
  the signal agent never waiting, not asking the same question twice, FALSE on errors or with no model.
- `test_mode_behavior.py`: only a timetabled type has a headway (Bus yes, Car and Bike refused, `timetabled`
  set and cleared by `update`; Add asks whether to keep one); a clicked type showing its headway (typed straight in,
  no "No timetable" text; hidden for a bike), PCU (defaults 2 / 1 / 0.2, out of range refused, one undo step) and occupancy; the assistant's vehicle types: listed, bad ones refused before asking (a name taken,
  an unknown kind, a headway too long, over 8 types) with none added, a tram on a timetable and a truck made
  with SUMO's defaults and their occupancies, one undo step for both; shares handed on in proportion, the slider with four types, types
  added, renamed and deleted across the tab, the matrix, the % boxes and the assistant's tool;
  a headway fixing the bus share (flows typed digit by digit, the locked handle, other shares and
  more flow moving only Car and Bike); the tab order and names; undo and redo of a deleted type
  and of a headway.
- `test_project.py`: the Seed box seeding the run and recorded; a project saved (asked where first, then
  Ctrl+S to its file) and opened with its hourly and daily pairs and curves, four vehicle types with a
  headway, a roundabout, seed, speed and the demand shown; bad parts refused by name (seed, same ends,
  colour, pattern, a negative flow) with nothing changed; an older network-only file opening with no
  demand and the default types; unsaved changes in the title and asked about before closing (Cancel stays,
  Discard closes).
- `test_results.py`: the Excel writer (numbers stay numbers, text escaped, control characters dropped,
  sheet names made legal); wait time from hand-made frames (a bus in its bay not waiting, at 0.05 m/s
  waiting), passenger delay as delay × occupancy, the mean queue over the samples, the workbook named by
  start, seed and model, an extension's sheet after Trips and in the Read me and its file beside the workbook; the workbook named by the
  run's start and holding what an analyst needs (the
  Read me with the seed and definitions, the Run sheet's settings, every demand pair, vehicle type,
  junction, road and signal plan; in the window seed 42, the speed, the demand used, the network file and
  the extensions' state, Batch Run's seeds read from its panel when ticked); 15 minutes of real traffic recorded (spawned and arrived from the frame,
  throughput, people by type × occupancy, speed, delay, trip time, queues at the signals, the busiest
  road's flow), a minute row each simulated minute summing to the arrivals, the totals kept, and the
  workbook's five sheets agreeing with the KPIs; a run cut short recovered from its folder (the whole
  minutes it wrote; left alone while its lock is held) and an empty one leaving nothing; the Runner sampling on the physics thread while
  the KPIs are read, and a failing recorder dropped with the traffic going on; and in the window: no
  results before a run, the Results page running, Export mid-run (the run carrying on), Stop saving the
  workbook in the results folder and the page showing the last run and its path. (Checked by hand too:
  the workbook opens in Excel 16, its sums matching.)
- `test_summary.py`: the Network page (layout, flows, linked design-check problems), the Demand page
  (totals, chart data, OD vs ends check, busiest pairs) and the AI Usage page (requests, tokens,
  local memory).
- `test_daily_profile.py`: the free curve through its points without overshoot, hourly shares,
  polynomial fits and conversions.
- `test_institutions.py`: where a building can stand (a road, its verge, a roundabout, another
  building, clear ground) and refusals changing nothing; placing as move, click (refused on a road),
  scroll (the links it reaches, never inside its walls), click; trips by shift (students, residents both
  ways, customers evenly, a night shift past midnight), its door and catchment; the mode split by logit
  (shares adding to 100, calibrated to an observed car share with the others keeping their relative odds, to
  every mode's, an observed 0 leaving a mode out, slower walking fewer walkers, dear driving more walkers, its
  refusals, no catchment refused); its trips into a Run (vehicles a person by occupancy, a timetabled bus's
  riders adding none, catchment to door and back, the daily profile, the design hour, a headless run spawning
  cars and bikes and no buses on its pairs, an unknown mode and no catchment noted); place_institution near a
  junction or at x, y (its kind's figures, its door and catchment, refusals) and a typed car share replacing a
  split; saving mode shares and refusing ones not adding to 100;
  and on the real map: Institutions showing the map, a school picked, red on the road and refused,
  green, set down, the wheel sizing the circle without zooming, finished with its figures, its zone
  shown, clicking it from the Edit tab, its details (car share, catchment), the Mode split window (Walk and the
  types, an observed 30 % by car calibrated, a mode left out, Apply setting the split, undone), institution_mode_split
  checked then applied, a Run's demand holding its trips, undo and redo, Delete and Esc.
- `test_od_matrix.py`: demand tools saying which demand a Run uses (and when they switched it), demand left
  out meaning the kind shown, a pattern alone for an empty pair refused, a check that tries and puts back,
  the same values again refused; hourly and daily demand kept apart, switched by the Demand drop-down, which
  shows each one's options (Institutions its own panel), switching writes nothing; the grid zooming at the cursor (the same cell
  under it), panning by a right drag without selecting, Fit Grid leaving nothing to scroll, the zoom
  buttons and the 25 % limit; keyboard
  entry (a number typed on the matrix, arrows in the Flow box, edges and the diagonal), and
  the curve dragged, points removed, a polynomial edited, copied to a row and reset (offscreen);
  and the assistant's demand tools working the same controls. They select the pairs, set values,
  pattern, shares, points and polynomial; naming no pairs means every pair. Each tool call is one undo step, and refusals change nothing.
- `test_background_network.py`: a stand-in Overpass answer (a primary road, 4 lanes, 30 mph, a signal 20 m
  before a junction with a one-way street coming in from the edge, a roundabout ring, a bridge, a
  footway, a pedestrian crossing's signal and a flashing amber beside another junction) made into a
  network: asked once for the check and the change, the query naming the classes and area, the junction
  signalised, the other left a priority junction with both signals reported by kind and then signalised by
  hand (Webster to start), the ring one roundabout at its centre, ends exactly where roads
  leave the area and numbered along each side, 2 lanes and 48 km/h, the one-way end road inbound, the
  bridge an overpass; an image's mask traced (a road across and one down from a dead end, a speck of
  text dropped): ends at the edges in scene metres, the T and the dead end, 2 lanes from 14 m, the
  unscaled image noted; a plain colour and a missing road colour refused; the image reader finding
  a road's pixels and placing grid pixels in metres; generate_demand through the Demand Generator
  (refused without a network, the check changing nothing, one undo step, totals balanced, an old pair
  cleared, pattern and split set, a bad pattern refused), scale_demand by a factor and to a saturation,
  the run check's backed-off share typed in within the same undo step and a jam putting the matrix back);
  and in the window, deploy_institutions and
  network_from_background each one undo step.
- `test_network_tools.py`: values inside a list's objects read too (counts, vehicle types); calibration that
  would clear the matrix refused; arguments read as the schema gives them (misspelt and plural names, text numbers and
  booleans, one-item lists, cased choices, nulls) or refused with the tool's signature ("nan" and infinity too); an edit that changes
  nothing refused, one that does listing what changed; the assistant's reach pinned (every callable tool listed, exactly these
  edit tools, the demand, vehicle type, run and scenario sets, no others such as a file write); every tool described, read results, design analysis, edits and
  their rollback, crossings, interchanges, a single-lane slip road (and its refusals) moved out by a new roundabout, a new grid, source reading kept inside the project,
  and change descriptions for editor updates; the gravity model (balanced to each end's supply and
  demand, the nearer destination taking more, no decay sharing equally, the decay fitted to a mean trip
  length (Hyman; out of range, both given, and no trips to fit (flows at one end) refused), daily × 24, a total shared
  equally, unreachable ends left out, nothing to balance refused, flows at a single end and a total under 1 a pair
  refused with the reason, pairs rounded to 0 reported); demand sized to a degree of saturation
  (the busiest junction at the target, more for a higher one, daily at its busiest hour, roundabouts and
  priority junctions alone, both given and out of range refused); demand already entered scaled (by a factor,
  to a saturation keeping its pattern, pairs out of scope kept, bad input and nothing to scale refused); institutions deployed at random (on
  clear ground, reaching a junction, the same seed the same layout, kinds, all or nothing, listed,
  deleted, reported as the user's edit, a seed fixed by prepare).
- `test_results.py` (export): level of service by speed share; the City sheet's counts and indices; a recorded
  run's workbook with Road conditions per direction (each its own from and to, its grade from its speed share),
  Roads by minute, the analysis sheets, roads' bus stops and WKT shape; its GeoJSON with each direction's level of
  service; the network alone exported with WGS84 coordinates near the placed map; in the window, Export with no
  run saving the network and its GeoJSON.
- `test_research_log.py`: against the real AI Mode panel and a fake Ollama, with Research Log ticked: the session folder named by its time and agent (Ollama small-3b,
  session.json's agent); a build step's folder (the prompt; the model's own Plan: line and the tools it was offered, no guided column; the
  build_scenario call applied, with its plan and result; the change list naming the grid and each pair's demand; a
  PNG of the map; the workbook's SHA-256; tokens and timings), a question step, the verdict and note in step.json,
  steps.csv and report.html; an earlier chat graded by number (no such chat or verdict refused); End session (its
  end time, the session's .odt with the prompt, a later grade and the map inside, the next question starting a new session even within the same second); History (the ended session
  listed, a chat's detail and note, a chat and the session graded by their buttons, sessions.csv and the report
  showing them); nothing recorded while unticked; a log() that raises switched off, the chat carrying on.
- `test_scenario.py`: the form completed (a 3 × 3 grid and medium traffic with no network; the demand kept
  for a test; busiest and all as targets) and refused with the reason (a place and a grid, a grid over 20, an
  unreadable grid, a speed limit or signal timing without its value, minutes, a timetable with no bus type);
  the plan in a study's order, every step a real tool, the what-ifs as scenarios; the report's wording; in the window, build_scenario's preview, a build as one undo step with an unbuildable
  what-if left out, undone whole; the grid already on screen asked for again kept and the rest built (it once
  stopped the build); a request offered the tools its words point to and planned by a fake model with tools
  (generate_grid offered, find_tools always, emergency tools not; the call after Apply; a follow-up keeping the
  tools used; no guided mode).
- `eval_commands.py` (not run by the monitor: it needs Ollama): requests as users type them (the first three
  guided mode got wrong), each against the tools it should and shouldn't call, for each local model: network
  tools run on a real grid, the window's tools answer with stand-ins; pass rate, seconds and input tokens.
- The AI stress test (2026-10-05, not in the repository: a driver of the real window offscreen with the Research
  Log ticked): 30 prompts written by another model (terse, typos, non-native English, multi-step, questions),
  each from a fresh 4 x 4 grid, every approval card applied, each step graded in the Research Log by its expected
  and forbidden tools; results in ai_mode.md.
- `test_ai_mode.py` also: the conversation loop's recoveries (a failed call not run again; a silent thinking model
  asked again without thinking; Ollama's unparsable tool call retried, then a nudge, then the app's note of what
  ran; a reply sent as an "answer" tool; 12 rounds ended with a note; a two-part request's second tool, named
  in the model's own Plan: line but not called, asked for once, and its reason accepted if it says none is needed), the Plan: line read, the context size's
  steps. `test_network_tools.py` also: each request offered the tools its words point to (the requests guided mode
  got wrong; "busy" not a bus; a greeting gets the core only; a follow-up keeps the tools used; find_tools adding
  bus stop tools), broken JSON lists repaired or refused saying where, a misspelt choice read.
- `test_agent_tools.py`: route choice (one route when off, three shared by path-size logit, a slow link
  losing share faster at a higher θ, assign loading every alternative, bad settings refused, saved and
  loaded); an observed headless run (passages, link times, stop lines, bus arrivals; analyse_run's steady
  state, Gini and one bus line's adherence; detectors with harmonic ≤ arithmetic speed and ordered
  percentiles; saturation flow near the calibrated 2,060; safety with a looser threshold finding more;
  series); network indices of a 3 × 4 grid, three planted travel conditions found, critical links; in the
  window, assign_routes (iterations, one undo step), optimise_signals (refusals, the elite never worse, its
  answer applying), read_detectors, analyse_safety, forecast, check_calibration's envelopes, critical_links,
  regress and choose_modes' nests, value of time and elasticities.
- `test_stats.py`: t and χ² against their tables, percentiles; F, randomized-block and one-way ANOVA, the
  exact signed-rank test, OLS, Poisson regression recovering its coefficients and finding overdispersion,
  Empirical Bayes; k-means finding three clusters, envelopes (a day inside its own, a shifted profile failing
  all four), Holt, MSER-5, batch means, Gini, nested logit reducing to logit and breaking IIA; the paired interval (clear on shared seeds,
  open with one), replications needed; the dispersion test telling the four arrival patterns apart; GEH and
  the calibration scores, logit shares, the travel time and planning time indexes.
- `test_experiments.py`: emergency vehicles joining a live run (none before it starts, unknown places and no route refused), System 1 off refusing a TSP batch, a batch with Test DEVL (a blank line refused; each run's Emergency vehicles sheet with every dispatch, arrivals and each run's random emergency vehicle from its seed, the same again on that seed; its DEVL decisions, No LLM's; an unknown DEVL model refused); the largest share of a demand that holds (the whole, then bisection; nothing held;
  stopped), daily demand at its busiest hour, a run telling a light demand that holds from one that piles up;
  the statistics (a mean's interval by Student's t, Welch's difference clear or
  not, one seed left open, seeds read from ranges, better or worse by the figure's direction; shared seeds
  compared paired, finding a steady gain Welch misses, Bonferroni with three scenarios, replications needed
  over 20 flagged); analyse_run, check_calibration and choose_modes on a run (refused with no run;
  reliability, arrivals per origin, a count met with GEH 0, an unknown road refused; the logit split typed
  in as one undo step, a dearer car losing share, bad alternatives, coefficients, run and pairs refused); a scenario
  changing only a copy (a tool outside the scenario set, a bad change, a bad scale refused; demand
  scaled); a headless run repeatable with its seed and cancelled; set_signal_timing, bus stops,
  update_institution and place_map (the search stubbed) with their refusals; calibration meeting a
  count (GEH before and after, only the pairs crossing it moving; bad counts refused; through the
  Demand Generator, one undo step); update_vehicle_type (checks, one undo step); in the window, the
  planning journal (plans, a refused proposal), an experiment of three scenarios and two seeds (its
  checks, the open network untouched, the workbook's sheets and journal, each run's workbook, more
  demand more trips), read_results, two runs with one seed identical, compare_runs refusing an
  experiment, no test during the user's run; set_extension by name and label, and overrides by name; the
  assistant working every extension through AI Mode's own entry: read_extensions (every extension, DEVL's choices,
  the schedules), schedule_emergency refusing an unknown place, no route, every and after both, out of range, a
  non-list, dispatch_now with no run (the rows untouched), then filling the rows (After shown as After),
  start_batch refusing bad seeds and durations, then a two-seed batch through the panel, replay_run refusing a
  missing workbook.
- `test_tooltips.py`: every tooltip in the window (widgets, tabs, actions, map items; the road, junction and
  end editors, hourly, daily and institution demand, each building kind, System 2's status) five words at most.
- `test_ai_mode.py`: Stop against a server gone quiet, before its reply starts and after one event (Cancelled
  within 2 s, the server seeing each request dropped); a tool call written as text made (a tool offered, its arguments an object; others and
  broken JSON ignored) and the answer carrying on; every tool's parameters sent to Gemini with only the schema keywords it accepts (the
  build_scenario form's structure kept, its strictness kept for the others); the guard (decide_tsp booleans and the words true/false accepted, "maybe", a missing
  activate, "call decide_tsp: activate true" and "It is not true that…" rejected as text, a bare TRUE accepted;
  System 1's P(yes) outside 0–1 or not a number, for any of its questions, rejected; TRUE only when all
  are yes, the lowest its P(yes), each in its reason); a batch in the window with Test TSP (TSP not ticked;
  Grok listed with its usual model once its key is on the PC; no model, only the baseline or an unknown one
  refused; the baseline added, first), paced at 60× (taking the clock time it should;
  fake Laya, and fake Ollama calling decide_tsp(activate=true) with a reason): agents listed (No LLM,
  System 1: Laya, System 2's models), no agent or an unknown one refused before anything runs, 2 seeds × 3
  agents giving 6 runs, TSP's choice put back, the batch workbook's sheets, Headline and Agents, each run's
  workbook named by seed and model with its TSP sheet (Laya's TRUE, No LLM's none), deadline and pace, 4 at once
  taking about a third of the time one after another would, each run's pace achieved and falling behind said,
  No LLM's runs side by side giving what they give alone (each run its own state), the
  telemetry Laya read, the telemetry.jsonl beside it, System 2's answers by the tool with its reason, and plain
  each decision in the feed with what the agent reported; Laya's, llama's and No LLM's seed-1 runs replayed from
  their feeds the same as they ran (Summary, every trip, and each decision's answer, model, outcome and guard),
  a run with no feed refused; plain runs once TSP is unticked; each provider's streamed thinking kept apart from its answer; Export History as
  Markdown and JSON (the chat, the editor updates, tool calls and results, the thinking, the journal);
  Available Tools listing every tool (reads and changes, with parameters) before
  Start, and not saved; key discovery, Ollama listing, the assistant's streamed request formats and
  a full tool-call round trip for Grok, Gemini and Ollama; conversations saved as plain text; System 1's
  requests to Jev and Laya (one API, the key only where due, a yes/no as one noul), a Laya that can't start
  saying why and leaving nothing running, and the real System 1 panel starting Laya (fake server) and
  answering the signal agent;
  and the real System 2 panel (offscreen, fake Ollama):
  - warm-up gate;
  - editor updates and selection;
  - watch mode;
  - restore and clear.
- `test_docs.py`: the ARCHITECTURE.md index at most 150 lines, every module in its module map, every
  linked doc present, and the warm-up sending the index, not the module files.
- `test_analysis.py`: the fuel model's published figures (idle α, cruising and accelerating rates) and only light
  motor vehicles counted; the HCM tables (LOS thresholds, arrival types, f_PA); junction LOS on a real run (every
  signalised approach graded, delay without the junction's crossing time, 15-minute periods); a signal's diagnosis
  (flow ratios, Xc, PF, HCM delay against simulated, causes with their fixes; an overloaded signal over capacity);
  a run's CO2 by type and road; run health (gridlock, vehicles unable to enter, no route); slow links only where no
  signal explains them; invariants (a starved arm, an emergency vehicle held, agent errors, a clean run passing);
  the resilience triangle (start, worst, recovery, extra vehicle-hours).
- `test_design_checks.py`: the Green Book's own figures (Eq 3-2's stopping sight distances, Eq 9-1's worked
  example, Eq 3-37's offset, the design vehicles); the audit finding each kind of problem (sight triangles at
  give-ways, a building blocking one, tight turns for a city bus, junctions under 250 m on an arterial, a skewed
  leg); accessibility (jobs within budgets, Hansen's measure, the residents' means, the weighted Gini, a road's
  closure's loss, bad inputs refused); bridges and cut junctions on a spur, an area closure's roads and the
  refused areas; refusals as structured errors with did_you_mean.
- With the analysis tools, also: `test_simulation.py` (closed lanes left in good time and a blocked road stopping
  traffic; actuated and max-pressure signals safe and serving demand; ramp meters holding back by ALINEA or a
  fixed rate; a fork running on alone, a rollout comparing actions; weather lowering saturation flow as the HCM
  reports, re-measured); `test_bus_stops.py` (passengers setting each bus's dwell and load, a full bus leaving
  boarders behind); `test_stats.py` (Friedman, Kruskal–Wallis, Mann–Whitney and all pairs' letters against known
  values; two-way ANOVA with interaction; paired power, seeds for power, detectable differences);
  `test_experiments.py` (the assistant's analyses of runs and experiments; incidents and weather in runs and
  experiments; conditions with regret and the robust choice, factorials, route equilibrium in a scenario and
  elastic demand; an agent's decisions read and probed, the compliance battery; the design audit, accessibility,
  critical links' cut elements and area, the speed change, benefit-cost at three rates); `test_extensions.py`
  (TSP looking ahead on copies of the run when asked); `test_transit_signal_priority.py` (the person-delay
  estimate weighing passengers against the cross streets); `test_signal_timing.py` (every method, actuated and
  max-pressure among them, running the traffic safely); `test_network_tools.py` (the new tools pinned).

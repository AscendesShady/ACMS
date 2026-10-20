# 8. Tests

Plain `assert` functions, run with `python tests/test_x.py` or all at once in `run_monitor.py`:
- `test_simulation.py`: a reaction time of 0.1 s with no standstill gap running into a car standing at red, counted in `collisions` (none at τ = DT); a car at 50 km/h 5 m behind a standing one slowing by no more than 9 m/s² a step, each harder step than 4.5 m/s² recorded as emergency braking with its road, and the collision it can't avoid counted as same lane; the ballistic update moving (v + v′)/2·Δt and only to the stop point when it stops within the step (Euler v′·Δt), its safe speed never hitting a standing leader, a 0.3 s step or an unknown method refused; the same seed at 0.5 and 0.25 s clearing about the same traffic through a signal (within 5 %), and a 0.1 s step's clock exact; two vehicles over a crossing point at once a crossing collision at that junction, short of it or clear of it not; a car inserted 10 m from a red stop line on a 120 km/h road braking no harder than comfortable, and one inserted 2.5 m behind a car at 50 km/h keeping the secure gap; a 10 m radius bend edited to 50 km/h capping its lane at 15 km/h (a 1 m radius under 10 km/h, no floor); the audit's roundabout (seed 19, four straight movements at 180 veh/h, σ 0) braking no harder than 9 m/s², with no collision; an emergency vehicle dispatched two and a half minutes later; an emergency vehicle obeying red, but going on its arm's kerbside lane green from whichever lane it is in (not on that lane's amber); a lane obeying its own signal (an override's lane state: only the kerbside lane goes through, on either driving side, lane 0) and emergency vehicles keeping to that lane before each signal; emergency vehicles dispatched from an end or a junction, once or every minute, an impossible route refused; junction saturation by control (roundabout entry capacity, falling with circulating
  traffic; a priority junction's minor arm against its major flows; a signal's arm lane by lane, turns slower);
  the give-way capacities re-measured on the simulated drivers (a minor arm and a roundabout entry held
  saturated, within 10 % of the tables); a single-lane roundabout entry held saturated against 0 and 800 veh/h
  circulating within 15 % of the HCM 6 entry capacity; vehicles made from their type; Krauss stopping behind a standing leader;
  SUMO's speed factors (mean 1, deviation 0.1, about 95 % within 0.8–1.2, none below 0.2) and free
  speeds (a bicycle at its desired 20 km/h); the timing methods calibrated to the simulated drivers
  (an hour of a standing queue by the HCM's field method: saturation flow within 4 %, start-up lost
  time within 0.3 s, amber used within 0.4 s, SCATS's space time within 0.2 s of the constants) with
  none of the queue held at the line by the vehicle ahead of it;
  lanes on the driving side and cut at stop lines; arrival patterns' means and variances (Random
  exact, binomial < Poisson < negative binomial); spawn totals and bus headways; daily demand
  following its curve; hourly demand at PHF 0.5 counting 240 arrivals in the hour's second 15 minutes and 80 in
  each other (V/PHF), a daily pair taking no PHF; route costs counting junction delay once (each move's prior
  r²/2C at a signal at its entered greens, 3600/c + 5 s for a give-way move, none for the major road's straight
  and kerb moves; a measured move time used alone, else the link's, never both; a run's per-move traversals
  adding up to each link's); 20 minutes of busy traffic through signals, a roundabout and an uncontrolled
  junction: no entry on red, no overlaps and no collision counted (same lane or crossing); the physics side-neutral (a network and its mirror image
  driven on the other side, demand and junctions mirrored, clearing the same traffic within 7 % over
  two seeds, and its routes making the same turns, while the same demand on the other side makes others);
  two-phase signals with no offside turns and their
  detours (straight on and U-turn back, the kerb-side fallback by a boundary end, mirrored driving on
  the right, a four-phase junction keeping its turn) and each lane's reach along the route; 20
  minutes of right-turners detouring through heavy traffic both ways: every U-turn from the offside
  lane, every turn back from the kerb (those coming back let in by the saturated platoons beside them),
  no lane change undone, some U-turns clearing on amber, nobody stuck; no lane changes
  where roads forbid them; each vehicle's next landmark (slip road, signal, roundabout, in route order)
  closing in as it drives, and in `read_traffic`; Webster's
  cycle and greens against hand-worked numbers, the busy phase getting the long green (the other its minimum, inside
  Webster's cycle); a driver at 50 km/h at the amber stopping 28 m from the line (21.4 m to stop: SUMO's rule, no
  reaction distance) and going on 18 m from it; the run
  using the plan, Fixed time keeping its greens; the runner keeping real time on its thread,
  pausing and changing speed; the window's Run (Webster note in the junction editor), live signal
  heads, `read_traffic` data, a demand edit and its undo carrying the run on, and a network edit
  stopping it; demand handed to a run under way: from the next minute, a removed pair's flow and bus
  timetable stopping, a new pair's starting, a pair with no route listed.
- `test_simulation.py` also, lateral position (`Simulation(lateral=)`): off, a fixed-seed mixed run (motorcycles among the types) bit for bit as before it existed (a SHA-256 digest of every position, speed, pose and count, taken then; change it only for a deliberate change to the default model), and on with no type filtering the same traffic (only drawn at the kerb side); five cars queued at a red and three motorcycles behind them moving sideways no faster than 1 m/s a second, staying in their lane, passing the queue at 30 km/h at most, at the stop line by the end of the red and through before the third car (without it, last), no collision; two motorcycles side by side in a lane no collision, overlapping sideways one (without lateral position, side by side is one), drawn apart by their offsets and `read_traffic` giving `lateral_m`; a car stopping behind a motorcycle at a red that it overlaps sideways, and coming up beside one at the lane's far side.
- `test_simulation.py` also, the lane graph on its own: lane_graph, network_tools, design_checks and analysis load without simulation.py; nothing builds a Simulation only to read its graph; `Graph(network)` routes every pair of ends the same as a Simulation's graph.
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
  against a hand calculation; greens filling a cycle with their minimums; Webster's minimum greens inside its 120 s cap
  (0.94 against 0: 120 s, not 127.4) and an impossible minimum cycle refused; each junction's ITE amber and all-red
  (3.3 + 2.0 s at 50 km/h, 6 s amber at most) run by its signal; SCATS's DS, its cycle rising
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
- Overlap and ordering fixes, each test failing on the old code: `test_project.py` (Open starts its own undo
  history), `test_simulation.py` (a live run drives its own copy: an edit that stops it leaves its results
  describing the network it ran), `test_mode_behavior.py` (Undo with a name half typed, the window active:
  no rename, Redo still there), `test_extensions.py` (`report_failures` switching a failed override off once,
  its run's Extensions dropped sheet), `test_background.py` (four threads loading one PDF page: rendered once,
  the same image), `test_activity.py` (the eye clicked: logged once each way), `test_extensions.py` (Stop then
  Run at once: TSP's and DEVL's state new, the agent's reset telling them), `test_simulation.py` (a speed change
  mid-run carrying on from where the run stands).
- Failure-isolation fixes, each test failing on the old code: `test_selection.py` (a refused road edit, lanes,
  reversal and a zero speed: the same network as the history's, no undo step), `test_extensions.py` (a dropped
  signal override, before or after a valid one, leaving no lane behind), `test_ai_mode.py` (a tool call queued
  before Stop answered, never run), `test_project.py` (a conversation with `messages: null` refusing Open with
  the network, path, history and conversation as they were).
- `test_busy.py`: while a test runs, Run and Play off with "Busy: Batch run", Play not starting a run, a second
  test refused naming the first, Batch Run's Start waiting with the reason, all back after; a live run refusing a
  test; closing mid-test stopping the test, its caller getting the result, then the window closing; Stop during
  an applied edit leaving it applied, the assistant not thinking and a newer card unanswered.
- `test_gpu.py`: CPU stand-ins told from GPUs; without OpenGL QPainter drawing the vehicles (half way, turned)
  and the offscreen view not asking; and in a child process on the Windows display driver, drawing into a
  hidden framebuffer (headless): the renderer a real GPU; the vehicles from one instanced draw where they
  are (half way between poses, turned, the right way up, still a few pixels at 20 m a pixel, not sent
  again); 5,000 vehicles sent once and each display frame under 2 ms (0.03 ms here) and over 20× faster
  than QPainter; and the editor itself, never put on screen, running traffic at 10×: an OpenGL viewport,
  every display frame's vehicles through the GPU (about 60 a second), each physics frame sent once, and
  the physics keeping over 8× of 10×, then Power Saver: a new OpenGL viewport with no multisampling
  (GL_SAMPLES 0, then 4 again), about 30 frames a second. It fails on a PC without a usable GPU. Offscreen too: a
  run caches the still layer (the view in CacheBackground, its items not painting yet still clicked, the
  lamps live) with the same picture as edit mode, the next frame not drawing it again, zooming, an edit
  mid-run and an extension's item all shown, and edit mode back after the run; Power Saver's button
  doubling the clock's step and back.
- `test_projection.py`: parallel and translation-invariant orthographic projection, upright height and labels;
  natural screen-speed foreshortening at equal world speed; frame/alpha, physical dimensions, zoom, centre,
  selection, project snapshot, undo and live clock/Runner retained across toggles; Fit/Reset/wheel respecting
  the camera; inverse-projected clicks and pointer coordinates, screen-direction crossing boxes with world
  bounds, bus-stop arrow nudges; exact raster vehicle L/W at four zooms with no dot/body enlargement.
  `QT_QPA_PLATFORM=windows python tests/test_projection.py --gpu` explicitly checks the real GPU in a hidden
  framebuffer: isometric roof/pose parity with raster, no minimum body enlargement and one shared pose upload.
- `test_vehicle_assets.py`: fixed/random colour controls and save/undo/redo, atomic refusal and old-project
  defaults; stable paint and renamed class/dispatch appearances; identical seeded motion with random on/off;
  shaped high-detail models inside physical footprints (including extreme edited L/W), round tyres at normal
  proportions, tapered empty car corners, raised/sloping glass and truly open bicycle-frame gaps. Buildings
  hide with labels/hit targets, preserving records/frame/camera; explicit total heights keep every roof vertex
  inside the footprint and [0,H], project screen-up in metres, change only the 3D image and retain 2D picking.
  Height defaults are labelled assumed, invalid values refuse without insertion, JSON/old files round-trip,
  height does not change demand, and selected-height edits plus Undo/Redo preserve a live Runner and camera.
  `--gpu` additionally checks actual framebuffer silhouettes/open gaps, random paints and solid coplanar bus
  windows against raster, one upload across camera changes, and depth independent of instance order. Negative
  controls replace meshes with bricks or remove glazing depth bias and must fail these regressions.
- `test_selection.py`: a line meeting a box (through it, from inside, across a corner, passing by);
  a window taking only the junction inside it and a crossing box its four roads too, a bus stop's bay
  by window and by crossing, and everything boxed deleted to an empty, valid network; and in the
  window through the view's mouse events: a window, a crossing box, Ctrl adding to it, Del asking
  (the other roads named) and deleting it as one undo step, a click on empty map clearing it, and no
  box while Add Node is on (its click still places a node); Ctrl+clicking roads in and out of the
  selection, Add Slip Road shown only for two roads meeting at a junction, its menu (Single lane, Two
  lanes) building a single-lane slip road, undone in one step; its junction made a roundabout in the
  Edit tab moving it out. A turn pocket set in the panel ("Pocket to 2,2" and "to 1,2" rows, Apply only once
  changed): saved on the road, its widened carriageway drawn, one undo step taking both away; one on a road
  with lane changing switched off refused with a message, the road as it was.
- Turn pockets: `test_models.py` (each refusal: too long, bad lanes or side, at a boundary end or a
  roundabout, no lane changing, at an end the traffic leaves, too short for both, beside a bus bay; reversing,
  splitting and the driving side keeping each at its end; saved and loaded, an older file without them
  opening); `test_simulation.py` (the pocket's lanes where the map draws them on both driving sides, an
  offside pocket's lanes moved over, the turns each lane takes, its lane loads in the signal timing; turners
  only in the pocket and only past where it opens, at least 90 % turning from it, the rest arriving; a 15 m
  offside pocket at a priority T full against heavy oncoming traffic, the next turner stopped short of it in
  the through lane with straight-on traffic stopped behind it); `test_network_tools.py` (`update_roads` setting,
  refusing, dropping on one-way and removing a pocket).
- `test_simulation.py`: an uncontrolled junction's major road follows the trunk, whichever way it runs; a vehicle
  stuck the teleport time at its lane's front jumps to its route's next link (not before, not when off), keeping its
  trip's start. `test_analysis.py`: run_health names teleports and where.
  `test_ai_mode.py`: figures no tool gave flagged (made-up results 5, measured ones and a numbered list 0); a refused
  call reminded once, run corrected, no note after. `test_network_tools.py`: invented ends dropped only when the user
  named none and none is real; a level word or another tool's argument refused saying where it goes.
  `test_od_matrix.py`: a demand cut by its check leads with what was kept, advice for junctions that only give way.
  `test_experiments.py`: a demand that never holds names the junctions that only give way, not when signalised.
  `test_background_network.py`: busy Overpass servers tried again after a pause, then the copy kept for that query. A dual
  carriageway crossing mapped as four nodes 15 m apart is one junction.
- `test_background_network.py`: a road cut 7 m past a junction at the area's edge still gets a 60 m entry road,
  carried on along its own line.
- `test_network_tools.py`: arguments sent as text meaning nothing ("None", "null", "n/a") left out, under real
  or unknown names, a real 0 kept, a required one sent so still missing, a real value under no name still refused;
  an invented grid for a request naming a place caught, a grid asked for or no place named let through.
- 2026-10-13 improvement plan. `test_ai_mode.py`: a tool refused three times not run again in that answer; a change
  claimed with no tool run questioned once, then noted (owning up, or a plain answer, left alone); tools a later
  plan adds after an edit ran not pushed, the first plan's still; usage tagged by the asking thread; "What ran" of
  this answer only; an Ollama model counting more tokens than estimated sizing its next window by its count.
  `test_background_network.py`: an area fetched this week read from its copy with no server asked, an older copy
  only when none answers. `test_prompt_campaign.py`: an isolated campaign keeping its baseline and reloading it with a
  new chat before each prompt; a halt closing the Research Log. `test_od_matrix.py`: "1W-1E", "1W->>1E", "1W => 1E"
  read as 1W->1E; fractions as percent; an empty split as none. `test_experiments.py`: a sensitivity sweep's
  elasticities (no roundabout: the ring gap's exactly 0; a longer reaction time slower), stopped gives None, and
  the assistant's parameter_sensitivity refused without demand or with an unknown parameter or step, then run.
  `test_simulation.py`: a full closure holds its queue past the teleport time. `test_analysis.py`: Tanner's capacity
  (1379, 521, 377, 255 at 0–1,200 veh/h) and beside the HCM's for a light ring. `test_ai_mode.py`: a request answering
  nothing still recorded; a single made-up figure of 1,000 or more flagged, a year not. `test_network_tools.py`: a
  size or total of 0 left out.
- Traffic audit fixes, 2026-10-13. `test_experiments.py`: a warm-up left out of what is observed (passages, fuel,
  traversals after it); a 2 x 2 grid of give-way junctions holding the demand sized to X 0.85; the demand check judging
  an uneven hour's busiest quarter (PHF 0.7 at X 0.85 fails, 0.85 at X 0.6 holds); a borderline verdict going to three
  seeds; calibrated demand meeting the counts when loaded on the simulation's own routes; mode choice moving people so
  the cars change (1,000 to 348 with 70 % to the bus); benefit_cost's caution when fewer trips are served.
  `test_results.py`: a 40 m road's flow counts every vehicle that entered it. `test_signal_timing.py`: max-pressure
  never starves a light arm (its wait under MAX_RED_S plus an amber, no teleport); SCATS keeps a short cycle at
  moderate load. `test_simulation.py`: give-way drivers slow to look and a minor offside turn gives way by HCM rank; the
  fixed-seed digest renewed for the new turn speeds.
- Open audit problems, 2026-10-18. `test_experiments.py`: demand on short links sized by what they hold
  (`sized_by` storage, under the X-only size). `test_signal_timing.py`: max-pressure serving a lone side-road car
  soon (its detection zone). `test_simulation.py`: Webster's shortest cycle 30 s (`MIN_CYCLE_S`); a route-cost prior
  at the effective red; the fixed-seed digest renewed for the per-kind turn floors. `test_network_tools.py`: the
  X-sizing tests run with the storage bound off; the demand check's peak quarter discounting the rise in arrivals
  (X 0.6 at PHF 0.85 holds). `test_experiments.py`: a passage crossed in the step that ended the warm-up left out. Its experiments run 5 simulated minutes (2-3 were shorter than a trip across the grid, so throughput read ~0). `test_prompt_campaign.py`: a 3x2 build's demand at X 0.65 holds again with U-turns at
  their own rate (`UTURN_PER_LANE_VPH`). `test_gpu.py`: Power Saver's rate counted in display frames (`frames`), as
  `draws` now counts one call per appearance.
- `test_metro.py` also: loads on a line (segments each way, the busiest, passenger-km, stations' boardings, passengers
  on another line not served; HCM 2000 Eq 27-28's 9,000 an hour; over capacity at 12,000); the metro's own OD matrix in
  the window (stations M1..., a number set as an undo step, text refused, another line's pair refused and not
  editable, metro_report and the summary loading it, a building's metro riders added at its stations, undo); the
  metro OD saved and loaded, refused for a station to itself, an unknown station or a bad number, and dropped with
  its line.
- `test_metro.py` also: a line's service defaults (5 carriages of 200: 1,000 a train) and a headway of 0 refused; a
  line saved before carriages loading as the fewest carriages that hold it (900: 3 x 300); a ride between stops within the walk
  rides station to station on one line (km, minutes, wait, fare; none to itself or to another line); a building's
  door station within the walk; road pairs refused the metro in choose_modes; a building whose circle holds stations of
  its door's line drawing metro riders from them (they are places, as its junctions), one with none drawing none;
  metro riders adding no vehicles in vehicle_split.
- `test_metro.py` also: rail alignment: a plain click met exactly, the platform straight on past M2 and half a
  platform straight into M3, the bend easing in (its first piece under half an arc piece) and never tighter than the
  300 m arc; a stop aimed south arriving on that heading, a right-angle aim and an S-bend aim refused, changing
  nothing; in the scene, a press and a Shift drag of 50 degrees aiming the stop at 45 (the leg and handle drawn),
  placed on release, and a click without a drag not aimed.
- `test_metro.py` also: a right-angle click drawn in to a turn under 90 deg, the track turning under 3 deg a piece
  (no corner, M2 included) and never tighter than 300 m, its length along the curve; a click behind drawn in, placing
  nothing; a spiral whose next leg would cross its first refused; a line crossing another raising MetroCrossing and
  changing nothing, an interchange 50 m from a station refused, a merge ending the leg at the crossing in one stop on
  both lines (in the other's order), a ride across and down with one change (1.7 km, both half-headway waits), each
  line loaded (alighting and boarding at the interchange), and deleting the line taking the interchange off the
  other; in the window, the ghost leg following the mouse (red too near, amber across a line), Discard Line
  removing the line being drawn, Merge making the interchange.
- `test_metro.py`: a line as its stops in order and its length; too-near, non-finite and too-short refusals changing
  nothing; a second line; a one-stop line refused by `validate`; metro saved and loaded; deployment through the
  window (a pending first stop, a refused near click, stops joined, the summary, no undo step while deploying, a
  right-click finishing and accepting as one undo step, Esc finishing and deleting with no step, Ctrl+Z while
  deploying taking back only the unfinished line, a lone first stop dropped without asking); a right click
  told from a right-drag pan.
- `test_weaving.py`: the HCM 2000's Example Problems 1 and 2 (speeds, lanes needed, density, LOS; capacity within
  1 % of Exhibit 24-8), denser and slower with more flow or less length, the weaving-flow cap, basic segment
  capacities; Exhibit 24-5's types from lane changes; segments found (Type A with an auxiliary lane, ramp junctions
  without one, an infeasible layout; none past 750 m, at a signal, on a grid or a diamond); the four movements'
  pc/h from the demand, a busy weave's warnings, the design audit's notes and a headless run's speed and density on
  it; weaving_los through AI Mode (no segments on a grid; on a weave: PCU, constrained, warned, simulated figures);
  the simulation's ramp lanes (the road lands offside, the ramp kerbside; the off-ramp only from the kerbside lanes)
  and a 150 m weave running under 0.6 of a 600 m one's speed, no more than one teleport. `test_interchanges.py`: a
  diamond's freeway junctions with only straight and kerb-side turns, both driving sides, every trip still routed.
- Low-severity audit items, 2026-10-18. `test_stats.py`: exact paired power (0.401 at n 5, 0.565 at n 20 and d 0.5, α
  with no difference); Holm's adjusted p; TTI and BI on one mean; a Poisson safety fit ranking sites by z.
  `test_results.py`: a free bicycle's trip at TTI 1 against its own free speed. `test_simulation.py`: path-size logit
  shares. `test_experiments.py`: route choice on one seed with `excess_cost` beside the logit gap; elastic demand
  stopping on the residual (`largest_gap`). `test_institutions.py`: residents spread over three hours; staff commuting
  at 1.1 a car; school drop-off cars driving on; an office's default near ITE 710's per-employee rate.
- Tool accuracy, 2026-10-13. `test_network_tools.py`: ids read as "all", "2,2; 3,3" and Python's "['4,4-4,3']";
  an unknown junction "Unknown id", an end given as a junction (and back) said so; every unsignalised junction
  named at once; greens with a method other than fixed refused, one a phase, alone meaning fixed; a grid's result
  in columns x rows; incident fields beside run_simulation's read as one incident (and the two field lists kept
  equal); lanes 13 refused, a speed outside 5-130 refused. `test_od_matrix.py`: scale_demand cut by its check
  reports its own total and factor. `test_simulation.py`: read_traffic for a road gives its cars' mean speed and
  stops, without the signal plans. `test_experiments.py`: an experiment's answer leads with its objective and the
  lead figures with units, the rest counted; start_batch takes a seed list.
- `test_background.py`: Hide Background (an image's plan drawn, then only the colour, then the plan again; the
  project, its undo history and its saved state unchanged; no eye for a plain colour).
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
  between two uncontrolled nodes, a curve tangent to both roads (no kink, longer than its chord), on the fastest route, entered and left only from the kerbside lane,
  carrying traffic; refused (nothing changed) straight on, for roads that don't meet, and with no room;
  a two-way road refused as single-lane. A slip road moved out once when its junction becomes a
  roundabout (40 m of lane to the give-way line, all of it outside the circle), and 30 minutes of four
  flows through roundabout and slip each arriving near their rate; refused, changing nothing, on roads
  too short for it.
- `test_roundabouts.py`: categories, diameters and circulating speed; four entry lanes refused (by the design, by
  `set_junction_control` and `Network.validate`, changing nothing; by `build_edge` at a crossing before any road changes);
  the HCM 6 entry capacity (Eq 22-1 at 0 and 1,000 veh/h, Eq 22-4 for two lanes, none for three).
- `test_history.py`: undo/redo and merging.
- `test_theme.py`: contrast of every text/background pair; a click lighting a plain, primary and mini button
  (PRESSED at once, back after FLASH_MS), one flash filter however many windows; checked native controls have
  a contrasting checkmark/dot inside their indicator; keyboard focus keeps a button's size and text bounds;
  widgets.py loads without properties_panel or the model, and no module but main imports properties_panel; the
  real window lined up: File's box on the run bar's middle (within 2 px: Segoe UI's text sits low in it), the bar
  as far under the window's top as above the panels, "ACMS" and the page title on one baseline, no tab base
  line, the icon tiles edge to edge (their hint filling the row, never wider), AI Mode's pages with no side margin.
  `tools/preview_ui.py --output reports/ui-preview --width 1280 --height 800` captures all seven tabs and both
  AI pages, plus JSON layout measurements, using CPU/offscreen Qt. On Windows it locates the installed font
  directory explicitly. This does not verify native Windows window chrome or the OpenGL renderer.
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
  the two baselines, ticked by label in any case, an unlisted one refused), the 1–280 s slider, Pace, Runs at once and Times
  each, all set by `set_settings` as the assistant does, and `batch_settings()` giving them; TSP's picked model in
  its questions' `choice`; DEVL all off with no emergency vehicle; TSP handing the run
  no override while unticked; ticked with nothing granted, the run the plain run, signal for signal and vehicle
  for vehicle (the plan kept); with TRUE written at one junction for 15 min: no red entries, no failures,
  check-ins and check-outs, holds or truncations there only, the others never leaving their plans, every
  junction on its plan showing it exactly, and each priority episode back on the plan within two cycles of
  its last bus; a full exit lane refusing a TRUE (BLOCKED, logged with its phase); a bus checking in within the distance or held in a queue further out (not while moving out there), staying in as the queue moves on, out once it leaves the arm, and DEVL's `in_queue`; each request's outcome from
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
  (FALSE at the first: only the others cleared; nobody to ask and Always preempt: the rule, all three; Never preempt: none; answers past the deadline: none, fell
  back), its telemetry and both systems' forms, and its DEVL decisions sheet; Dispatch Emergency Vehicle's schedules (Random small and flat, never picking the
  other end, Dispatch Selected sending none unticked and the ticked one, + adding a row, Dispatch Fleet sending none
  when one can't go, then each on its frequency, After sending one later, − deleting the ticked row; Random Emergency: the same seed the same vehicle, another seed another, never a pair
  that can't go, and why not when nothing can); an override really changing the signals
  vehicles obey, FALSE restoring the plans, and a raising one dropped and switched off with its line;
  the signal agent never waiting, not asking the same question twice, FALSE on errors or with no model.
- `test_mode_behavior.py`: emergency braking by SUMO's class (car 9, bus, bicycle and truck 7 m/s²), comfortable braking above it refused by `update` and in a saved project, an older type given its class's; only a timetabled type has a headway (Bus yes, Car and Bike refused, `timetabled`
  set and cleared by `update`; Add asks whether to keep one); a reaction time under the 0.5 s step refused, saying
  why (0.5 s accepted); a clicked type showing its headway (typed straight in,
  no "No timetable" text; hidden for a bike), PCU (defaults 2 / 1 / 0.2, out of range refused, one undo step) and occupancy; the assistant's vehicle types: listed, bad ones refused before asking (a name taken,
  an unknown kind, a headway too long, over 8 types) with none added, a tram on a timetable and a truck made
  with SUMO's defaults and their occupancies, one undo step for both; shares handed on in proportion, the slider with four types, types
  added, renamed and deleted across the tab, the matrix, the % boxes and the assistant's tool;
  a headway fixing the bus share (flows typed digit by digit, the locked handle, other shares and
  more flow moving only Car and Bike); the tab order and names; undo and redo of a deleted type
  and of a headway.
- `test_mode_behavior.py` also: `filters` true for a type made as a motorcycle, moped or bicycle (and the default Bike), false for a car, bus or van; a vehicle filtering only if narrow enough (a 1.5 m wide motorcycle not); `update` setting it, refusing anything but true or false; kept through a saved type, one saved before it taking it from its class, and a saved one that isn't true or false refused.
- `test_mode_behavior.py` also, vehicle types without Qt: a VehicleMode made from vehicle.py alone (its filtering and emergency braking filled in from FILTERING_CLASSES and VEHICLE_CLASSES) loads neither PySide6 nor mode_behavior, and vehicle.py imports no Qt module even for typing.
- `test_experiments.py` also: a batch going seed by seed (both baselines on seed 1 before seed 2) with its workbook saved after every run, a batch stopped after two of four keeping them ("Runs finished 2 of 4", no "(saving)" file left); the eye, the Research Log and a batch each leaving a dated folder (the batch a
  Research Log step with its workbooks' checksums and no picture, since nothing changed); a DEVL batch's Comparison labelling emergency travel time (s) and arrivals; a batch's run drawn on the map while Show runs on map is on (vehicles), none when
  it's off, the map cleared after; the Seed box named beside it and given way to "Overridden" while Batch Run is ticked.
- `test_experiments.py` also: lateral position as a run setting: a scenario's `lateral` (kept under a condition, set by a factor's level, refused unless true or false), `run_headless(lateral=True)` placing vehicles off their lanes' centre lines, the assistant's run_simulation refusing a lateral that isn't true or false and saying "on" on the workbook's Run sheet.
- `test_project.py`: the Seed box seeding the run and recorded; a project saved (asked where first, then
  Ctrl+S to its file) and opened with its hourly and daily pairs and curves, four vehicle types with a
  headway, a roundabout, seed, speed, an hourly pair's peak-hour factor and the demand shown; bad parts refused by
  name (seed, same ends, colour, pattern, negative quarters, a negative flow) with nothing changed; an older network-only file opening with no
  demand and the default types; unsaved changes in the title and asked about before closing (Cancel stays,
  Discard closes).
- `test_project.py` also: a type's `filters` (Bike's switched off) kept through Save and Open.
- `test_project.py` also: a pair's drop-offs kept through Save and Open; a type's drop-offs over 100 % refused.
- `test_walking.py`: footpaths 2 m beyond the carriageway, a cycle lane more on its traffic's kerb side, reversing
  the road swapping their sides, bad sides refused; no walk with no footpath, a walk end to end on footpaths, a path
  a short cut taken both ways, bad paths refused; paths and pedestrian demand checked, saved, older files opening
  without, a path going with its node and demand with its end.
- `test_walking.py` also: crossings: across a road once in a gap at a junction's arm (never part way, never on a
  walk that stays on one side), a zebra midway taken instead, an overbridge's stairs making the gap cheaper again,
  one-sided footpaths still joined; a crossing refused without both footpaths, near a junction, near another or
  of an unknown kind, a footpath it needs refused, kept through reverse and split, saved, gone with its road.
- `test_background_network.py` also: OpenStreetMap walking and cycling: a main road's sidewalks both sides and cycle
  lanes each way, a oneway=-1 street's left sidewalk on its road's right and a sidewalk way east of it giving its left,
  a zebra mid-block placed and one 8 m from the junction counted, an unmarked one not taken, a footbridge as an
  overbridge, a path between two road nodes with its bend and one reaching none counted, footpaths all or none.
- `test_od_matrix.py` also: every grid's diagonal drawn shut, the vehicle OD grid's and the spawn points' and the
  pedestrians' alike (only the two shut colours, hatched), a cell to type in left open.
- Spawn points: `test_walking.py` (no walk without footpaths; with them, straight to the nearest footpath and along it
  between two points beside one stretch, crossings on a longer walk, walkers spawned from and to the points, bad
  spawn demand refused, saved and loaded, a point's walking going with it); `test_od_matrix.py` (the spawn grid);
  `test_selection.py` (choosing spawn points places them at once, three clicks three undo steps drawn at once, the hint
  on the nearest footpath, a right-click showing their grid, a pair typed, a point selected, deleted with its walking
  and undone, Esc finishing); `test_network_tools.py` (add_spawn_points, spawn pairs and end pairs apart, a mixed pair
  refused, the summary, deleting one).
- Pedestrians in the results and tools: `test_results.py` (a run with a zebra and walkers: each walk once, about
  5 km/h, the zebra's walkers and held vehicles, Walks and Crossings sheets, the Summary's walk rows);
  `test_network_tools.py` (crossings refused without footpaths or near a junction, changing nothing; update_roads'
  footpaths and cycle lanes, a zebra in the middle, a path with its bend, people walking on two pairs, an unknown end
  refused, the summary, describe's crossings, deleting both, the tools offered by walking words and not by "hi").
- Crossings elsewhere: `test_simulation.py` (no vehicle over an occupied zebra's stop line, no collision; gap waits
  longer than at a zebra; no stops for an overbridge; a walker waiting for a car 6 m short at 47 km/h but not one
  80 m off, a gap refused at 80 m, a standing car no threat); `test_selection.py` (the Pedestrian tab: Add Zebra's
  hint, a node and a junction-end click refused, a zebra placed and selected as one undo step, Add Overbridge
  turning Add Zebra off, a crossing selected, deleted and undone).
- Pedestrians elsewhere: `test_simulation.py` (1,200 people an hour walking: about 300 in 15 minutes, each its own
  speed about 5 km/h, every vehicle exactly as without them, `Frame.walkers`); `test_institutions.py` (a school's
  Walk share from the catchment to the building and back, in the cars' design hour, nobody walking without
  footpaths and the pairs noted, on to the building past its door with them); `test_od_matrix.py` (pedestrians an
  hour typed between boundary ends, too many put back, the diagonal shut); `test_selection.py` (the Footpath list: None,
  Both sides, North and South side only; a choice highlighted on the map before Apply, both sides two strips, one side
  one, applied as one undo step and the highlight gone, a side to be taken away outlined; no Add Path tool; a path
  selected, deleted and undone).
- Cycle lanes: `test_models.py` (refused at an end its traffic doesn't reach, twice, or after the road is made one-way;
  the lane's line 1.5 m beyond the kerb lane; a split running it on along both parts; saved, and older files opening
  without); `test_simulation.py` (half the roads with cycle lanes: on those, every bike in the cycle lane and no car;
  bikes turning out of one into a road without and into one from a road without; no teleport or collision; the cycle
  lane never among a link's lanes); `test_selection.py` (the panel's "To <end>" ticks, Apply drawing the green
  strip as one undo step, undo taking it away).
- `test_od_matrix.py` also: drop-offs: clicking Car's zone in Modes moves no handle and shows its drop-offs; a click
  near a junction adding a 10 % point snapped to it, typed to 25 %, dragged between junctions, a second point capped
  by what's left, right-click removing it, read_od_matrix listing them, and deleting Bus moving Car's points to its
  new index.
- `test_simulation.py` also: drop-offs (30 % of cars a quarter of the way, 20 % three quarters) ending 38-62 % of
  trips early, none past its point, cut to the first and second links, counted as arrived, with every departure as
  in the same run without them.
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
- Every run ends in a workbook (audit 2026-10-07 SE-04, 05, 06, 08, 09, 10): `test_results.py` (sheet names
  colliding by length, case and a taken " 2" all written apart; a live run stopped before its first sample,
  one whose extensions' reports raise, one whose physics raises (the next display frame ends it) and one
  stopped mid-step (nothing written until the step ends, the window carrying on) each leaving a workbook,
  Incomplete saying why), `test_experiments.py` (a headless run whose run or analysis raises still leaving its
  workbook, its error going on up), `test_simulation.py` (a Runner whose step raises: not running, its
  error said, Play refused; `stop` False while a step is still going, no step after it; a frame's plans its
  own, the default read-only), `test_extensions.py` (a new run's question for a key the last run is still
  asking answered at once) and `test_models.py` (NaN, ±infinity and True refused in coordinates, flows,
  greens, cycle limits, lengths, speeds, shape, curvature, dwell, the background and the network's defaults;
  a file with NaN refused).
- Random streams, exact trip times and the warm-up (theory gap report 2026-10-11, steps 1, 2 and 6):
  `test_simulation.py` (a pair's departures, types and speed factors, its bus timetable too, the same on the same
  seed when another pair's road is slowed and a pair is added; fails with one shared stream),
  `test_results.py` (each trip's departed, arrived and trip time equal its insert and exit seen step by step, not
  whole 5 s samples; its system travel time at least its trip time; the completion rate and unfinished share of
  what was spawned; a 5 min warm-up of a 10 min run: distance and time counted only after it, throughput over the
  measured hours, only trips that entered after it, Per minute rows for the whole run, Measured from on the Run and
  Summary sheets), `test_stats.py` (the buffer index, mean-based: nine free trips and one at 3× free give 0.75),
  `test_experiments.py` (run_simulation's headline with the buffer index, unfinished share and system travel
  time; a warm-up as long as the run refused; `measure_from_min` reaching the run's KPIs).
- Measuring by each vehicle and its passengers (audit 2026-10-07 TE-05 to 09): `test_results.py` (a bicycle riding
  freely at its own free speed: road-limit Delay, none against its own free speed; demand held off a 4 m entry
  road: no delay on the road but the entry wait and the vehicles waiting, spawned = waiting + on the road +
  arrived; a bus carrying 70 though its type assumes 20, stood 5 s mid-trip: 350 person-seconds before it
  arrives, 70 people carried), `test_analysis.py` (control delay against the vehicle's own free-flow time, the
  approach's for an older log; every traversal logged with it), `test_bus_stops.py` (a bus's first stop
  starting from its type's occupancy: all 20 alight, dwell door + 20 × 1.7 s; loads counted from the
  occupancy; bus stops for a type whose vehicle_class is bus, called City Bus, none for one called Bus made
  as a coach) and `test_mode_behavior.py` (vehicle_class kept through a rename, set by create's kind, changed
  by update, an unknown one refused there and in a saved project; an older type named Bus inferred a bus).
- `test_summary.py`: the Network page (layout, flows, linked design-check problems), the Demand page
  (totals, chart data, OD vs ends check, busiest pairs) and the AI Usage page (requests, tokens,
  local memory).
- `test_daily_profile.py`: the free curve through its points without overshoot, hourly shares,
  polynomial fits and conversions; a peak-hour factor's quarters (PHF = V/4·V15 back from them, the peak in the
  second quarter, the rest even, 1 even, HCM 2000's 4,300/4,800 example) and bad quarters or PHFs refused.
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
  out meaning the kind shown, hourly demand's PHF box and set_od_demand's peak_hour_factor or quarters (read back
  by read_od_matrix; out of range, not four, daily or for a pair with no hourly demand refused; hidden for daily), a pattern alone for an empty pair refused, a check that tries and puts back,
  the same values again refused; hourly and daily demand kept apart, switched by the Demand drop-down, which
  shows each one's options (Institutions its own panel), switching writes nothing; the grid zooming at the cursor (the same cell
  under it), panning by a right drag without selecting, Fit Grid leaving nothing to scroll, the zoom
  buttons and the 25 % limit; keyboard
  entry (a number typed on the matrix, arrows in the Flow box, edges and the diagonal), and
  the curve dragged, points removed, a polynomial edited, copied to a row and reset (offscreen);
  and the assistant's demand tools working the same controls. They select the pairs, set values,
  pattern, shares, points and polynomial; naming no pairs means every pair. Each tool call is one undo step, and refusals change nothing.
- `test_od_matrix.py` also, the demand rules without Qt: demand.py loads no PySide6; a bus every 10 minutes fixed at 1 % of 600 veh/h and the rest 80:10 (88/11); a pair read from "1W-1E"; a value rounded and a pattern matched; the read-out's hourly and daily maps; a run's pair demand leaving out an end no longer on the network.
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
- `test_activity.py`: Watch my edits in the menu bar (a logger only, its name as tooltip, in a dated folder)
  logging a click by the button's name, a number's final value once, a tab, a map selection, an edit's change,
  Ctrl+Z's action and a password as "(hidden)", between its on and off lines, never the question box the app
  cleared; each step's lines taken once; a Random pick logged once as "set" and a click's message as "shown",
  never the clock; controls named and read as the user sees them. `test_research_log.py` also: a step's activity.csv.
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
- `tools/eval_commands.py` (not a test file and not run by the monitor: it needs Ollama): requests as users type them (the first three
  guided mode got wrong), each against the tools it should and shouldn't call, for each local model: network
  tools run on a real grid, the window's tools answer with stand-ins; pass rate, seconds and input tokens. 22 cases
  (2026-10-13: the 8 first ones and 14 everyday tasks, each checking the arguments too: road, value, weather, ids);
  each call goes through what AI Mode does first (contradicts, fold_incident, drop_strays, repair_ends,
  normalise), only calls that ran count, and an edit that changes nothing is already so. `EVAL_OUT` names the JSON.
- The AI stress test (2026-10-05, not in the repository: a driver of the real window offscreen with the Research
  Log ticked): 30 prompts written by another model (terse, typos, non-native English, multi-step, questions),
  each from a fresh 4 x 4 grid, every approval card applied, each step graded in the Research Log by its expected
  and forbidden tools; results in ai_mode.md.
- `test_ai_mode.py` also: the conversation loop's recoveries (a failed call not run again; a silent thinking model
  asked again without thinking; Ollama's unparsable tool call retried, then a nudge, then the app's note of what
  ran; a reply sent as an "answer" tool; 12 rounds ended with a note; a two-part request's second tool, named
  in the model's own Plan: line but not called, asked for once, and its reason accepted if it says none is needed;
  a plan with nothing done reminded with a call required, once; every call refused ending the reply with the app's
  note, on screen too; the app's ending note shown), the Plan: line read, the context size's
  steps. `test_network_tools.py` also: each request offered the tools its words point to (the requests guided mode
  got wrong; "busy" not a bus; a greeting gets the core only; a follow-up keeps the tools used; find_tools adding
  bus stop tools; build_scenario only for a several-part request, the demand models only for their words, no
  signal tools for a junction alone, population, how-to questions getting read_source, misspelt demand words), an
  invented tool refused saying the app can't do it (tools named alike given only as options), broken JSON lists
  repaired or refused saying where, a misspelt choice read.
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
  the calibration scores, logit shares, the travel time, planning time and buffer indexes.
- `test_experiments.py`: emergency vehicles joining a live run (none before it starts, unknown places and no route refused), System 1 off refusing a TSP batch, a batch with Test DEVL (a blank line refused; each run's Emergency vehicles sheet with every dispatch, arrivals and each run's random emergency vehicle from its seed, the same again on that seed; its DEVL decisions, Always preempt's all TRUE and Never preempt's all FALSE with no lane cleared; an unknown DEVL model refused); the largest share of a demand that holds (the whole, then bisection; nothing held;
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
- `test_ai_mode.py`: tool results reach the model compact (records as columns and rows, untouched for the app,
  two records left alone, list_roads under half); results two exchanges back a stub even with room to spare, the
  previous exchange's whole; the Ollama request keeps the model loaded. `test_network_tools.py`: a read_source
  page is whole lines up to `READ_CHARS`, the next page following on.
- `test_ai_mode.py`: a reply that streams one event then goes silent fails at `STALL_TIMEOUT_S`, not the 600 s
  start-up limit. `test_network_tools.py`: `read_source` `find` (every file, or one; lines with every word, then that
  line read; no match says try fewer words; outside the project refused), a page `READ_LINES` long.
- `test_ai_mode.py`: a long conversation cut to fit the context window (sent as it is when it fits; the
  conversation itself unchanged; within the budget; the system prompt and latest message kept, saying what went;
  older tool results a stub; every kept tool call with its result; one oversized result cut, its call kept).
- `test_ai_mode.py`: Stop against a server gone quiet, before its reply starts and after one event (Cancelled
  within 2 s, the server seeing each request dropped); a tool call written as text made (a tool offered, its arguments an object, or
  one shaped as a call naming a tool that doesn't exist, so it is refused; a bare name and broken JSON ignored) and
  the answer carrying on; a call required as Gemini's mode ANY and xAI's tool_choice (a name not offered dropped);
  the system prompt with the index and README_NOTE for a model with tools, README in full without; every tool's parameters sent to Gemini with only the schema keywords it accepts (the
  build_scenario form's structure kept, its strictness kept for the others); the guard (decide_tsp booleans and the words true/false accepted, "maybe", a missing
  activate, "call decide_tsp: activate true" and "It is not true that…" rejected as text, a bare TRUE accepted;
  System 1's P(yes) outside 0–1 or not a number, for any of its questions, rejected; TRUE only when all
  are yes, the lowest its P(yes), each in its reason); a batch in the window with Test TSP (TSP not ticked;
  Grok listed with its usual model once its key is on the PC; no model, only the baseline or an unknown one
  refused; the baseline added, first), paced at 60× (taking the clock time it should;
  fake Jev, and fake Ollama calling decide_tsp(activate=true) with a reason): agents listed (Never then Always preempt,
  System 1: Jev and Ollama's decision model, System 2's models), no agent or an unknown one refused before anything runs, 2 seeds × 4
  agents giving 8 runs, TSP's choice put back, the batch workbook's sheets, Headline and Agents, each run's
  workbook named by seed and model with its TSP sheet (Jev's TRUE, Never preempt's none, Always preempt's some), deadline and pace, 4 at once
  taking about a third of the time one after another would, each run's pace achieved and falling behind said,
  Never preempt's runs side by side giving what they give alone (each run its own state), the
  telemetry Jev read, the telemetry.jsonl beside it, System 2's answers by the tool with its reason, and plain
  each decision in the feed with what the agent reported; Jev's, llama's and both baselines' seed-1 runs replayed from
  their feeds the same as they ran (Summary, every trip, and each decision's answer, model, outcome and guard),
  a run with no feed refused; plain runs once TSP is unticked; each provider's streamed thinking kept apart from its answer; Export History as
  Markdown and JSON (the chat, the editor updates, tool calls and results, the thinking, the journal);
  Available Tools listing every tool (reads and changes, with parameters) before
  Start, and not saved; key discovery, Ollama listing (chat and decision models apart), the assistant's streamed request formats and
  a full tool-call round trip for Grok, Gemini and Ollama; conversations saved as plain text; System 1's
  requests to Jev and Ollama's decision model (one API, the key only to Jev, a yes/no as one noul), and the
  real System 1 panel offering Ollama's decision model (not in System 2), picking it and answering the
  signal agent;
  and the real System 2 panel (offscreen, fake Ollama):
  - warm-up gate;
  - editor updates and selection;
  - the menu bar's eye logging only (an edit sends the model nothing);
  - restore and clear.
- `test_docs.py`: the ARCHITECTURE.md index at most 150 lines, every module in its module map, every
  linked doc present, and the warm-up sending the index, not the module files (nor README to a model with tools).
- `test_analysis.py`: the fuel model's published figures (idle α, cruising and accelerating rates) and only light
  motor vehicles counted; the HCM tables (LOS thresholds, arrival types, f_PA); junction LOS on a real run (every
  signalised approach graded, delay without the junction's crossing time, 15-minute periods); a roundabout's entries
  with their circulating flow, simulated capacity, v/c and the HCM 6 capacity beside it; a signal's diagnosis
  (flow ratios, Xc, PF, HCM delay against simulated, causes with their fixes; an overloaded signal over capacity);
  a run's CO2 by type and road; run health (gridlock, vehicles unable to enter, no route); slow links only where no
  signal explains them; invariants (a starved arm, an emergency vehicle held, agent errors, a clean run passing);
  the resilience triangle (start, worst, recovery, extra vehicle-hours).
- `test_design_checks.py`: the Green Book's own figures (Eq 3-2's stopping sight distances, Eq 9-1's worked
  example, Eq 3-37's offset, the design vehicles); the audit finding each kind of problem (sight triangles at
  give-ways, a building blocking one, tight turns for a city bus, junctions under 250 m on an arterial, a skewed
  leg); accessibility (jobs within budgets, Hansen's measure, the residents' means, the weighted Gini, a road's
  closure's loss, bad inputs refused); bridges and cut junctions on a spur, an area closure's roads and the
  refused areas; refusals as structured errors with did_you_mean; analyse_network and network_indices loaded by
  design_checks, results and summary without network_tools, the tools of the same names answering the same.
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

- `test_prompt_campaign.py`: exact DOCX text and IDs, the confirmed 900-prompt order, refusal of malformed sequences and duplicate launches, multiple and reentrant Apply handling, and real Qt/log integration requiring automatic Apply for both prompt groups with a fake Ollama.

- Campaign live update: retains the pending request, index and ledger, installs only the driver method, and refuses halted campaign recovery.

The campaign Qt regression injects `KeyError("all")` into an approved scenario execution and checks that the pending worker receives an error and the next completion is recorded without resubmission.

- `test_metro.py` overhead scenery: metre heights and carriage dimensions/counts; world rotation, reserved Fit
  bounds, camera/frame/network invariants, cache ordering, whole-network hiding and independence from Buildings;
  identical raster views before/during an empty run; far-station train glazing stays visible below long decks.
- `test_vehicle_assets.py` bus extrusion: recessed skirts, open axle spaces and tyre crowns clear of the body,
  with exact physical bounds. Visible hub pixels are checked in raster and the `--gpu` hardware path.

- 2.5D refresh: `test_metro.py` verifies shared curved sweep sections, world-distance pier spacing independent of
  path sampling, exact saved tracks through interchanges, unchanged passenger/model data and raised deployment
  previews. `test_projection.py` verifies metre-scaled people, exact interpolation,
  preserved 2D markers, bridge height/picking/bounds and immutable network/frame data across toggles. Its `--gpu`
  path also checks people painted on the elevated deck in both raster and real hardware framebuffers.

- Brick/metro rendering: `test_projection.py` checks warm paving colour, overview/detail pattern behaviour,
  footpath hit widths and unchanged running records, plus herringbone pixels in both raster and `--gpu` modes.
  `test_metro.py` checks the current 120 m platform minimum, longer trains, pitched roof eaves/ridges within the
  original total height, sloped cab glass, terminal-only windscreens, and train mesh dimensions/livery bounds.

- `test_projection.py` also keeps new spawn-point labels anchored to their screen-sized pins in both views,
  with a constant readable gap across overview and close zoom levels.

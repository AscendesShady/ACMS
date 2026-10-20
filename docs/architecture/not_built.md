# 7. Not built yet

- SCATS's tactical control (ending a green early on a gap: the `actuated` method does it per junction, not inside a
  SCATS region); groups of coordinated junctions other than "every junction with the method"; actuated and
  max-pressure junctions aren't coordinated with each other.
- Start-up lost time 1.8 s against the HCM's default 2.0 s (re-measured 2026-10-18 by the HCM's field method:
  1.80 s over 86 queues; the audit's 1.3 s was an earlier measurement): SUMO's startupDelay can't reach 2.0 s
  without pulling the saturation flow below field values (drivers re-halting as they creep off). Capacity is
  optimistic by 0.2 s a phase, under 1 % at a 60 s cycle.
- Results: editing each vehicle type's occupancy in the tab (assumed), per-junction and per-signal KPIs in a run's
  workbook (junction_los and diagnose_signal give them for the assistant's headless runs only), a vehicle class set in the tab (the assistant's `update_vehicle_type` sets it), a warm-up set for a live Run (only the assistant's run_simulation and run_experiment take one) and counts on roads
  crossed between two samples; Batch Run compares agents deciding TSP (or plain runs), not network variants (the
  assistant's `run_experiment` does those); a live run has no decision deadline (a batch's Decide within); a batch's runs share one process, so many at once at a fast pace fall behind the clock (the pace achieved is
  recorded and said), and plain (non-TSP) batches go one at a time; a replay needs the run's network and demand open (it reads only the seed, the simulated time and the feed); telemetry is TSP's only;
  pedestrians crowding each other (SUMO's NonInteracting walkers) or seen by traffic outside a zebra (at a gap the
  walker judges the gap; no collision between walkers and vehicles is checked), pedestrian signals (a signalised
  junction's arms are crossed in gaps, as anywhere: no walk phase), gradual (sublane) lane changes, editing each type's behaviour in Mode Behavior.
- Cycle lanes are one fixed width (1.5 m), bicycles only (not mopeds), the road's whole length (none ending short of
- Walking: footpaths are one fixed width (2 m), the road's whole length; a path's ends are road nodes (none part way along a footpath); zebras go part way along a road (none on a junction's arm); platoons crossing together (HCM Eq 18-18 to 18-20) aren't modelled, and crossing figures aren't warm-up aware; from OpenStreetMap, a footway reaching fewer than two road nodes, a crossing at a junction's arm and a crossing's signals don't come in, and cycle lanes come in for every direction whichever side is tagged.
  a junction, no advanced stop line or bike box, no two-stage offside turn: a bike turns across from the cycle lane
  as a car turns from the kerb); between junctions bikes and cars never meet (no bus crossing it into a bay, no car
  crossing it to park); loop detectors, actuated signals and Webster's flows leave its bikes out; the assistant
  sets them only both ways (update_roads cycle_lanes).
- Lateral position (opt-in, `Simulation(lateral=True)`; simulation.md) is SL2015 at its smallest: no vehicle
  straddles two lanes or rides between them, only filtering types (motorcycle, moped, bicycle) move sideways (cars,
  buses and the rest keep to their lane's kerb side, so a car can't pass a cyclist within a lane), lane changes are
  still whole-lane hops, merges, the ring and junction entry room stay whole-lane, and the side a filtering vehicle
  takes is chosen by room ahead, not SL2015's expected sublane speeds. No switch in the window (headless runs, the
  assistant's run_simulation and run_experiment, scenarios), no `filters` box in the tab, and its behaviour isn't
  validated against observed mixed traffic (theory reference concept 42).
- Impatience in gap acceptance: a minor road facing a saturated major road waits for a gap
  however long that takes.
- Roundabout and priority capacity are only used to size demand, not reported per approach in the Summary (the
  assistant's junction_los grades each approach's delay and gives a roundabout entry's capacity, simulated and HCM 6).
- Turning paths at junctions: a real curb radius per junction isn't modelled; turns are held to a speed floor per
  kind (`JUNCTION_TURN_MIN_KMH`) that brings turning saturation flows to 86-90 % of straight-on.
- Give-way junctions' capacity model (demand sizing) errs safe: a 2 x 2 grid sized to X 1.0 holds and 1.2 starts to
  jam (15-20 % conservative, as roundabouts); spillback is bounded by each link's queue storage (`STORAGE_LIMIT`, calibrated on 40-500 m grids), not by
  simulating the queues' interaction.
- Roundabout capacity at busy rings: a single-lane entry follows the HCM 6 curve within 10 % up to about 800 veh/h
  circulating, then falls faster (−28 % at 1,000, −59 % at 1,200; a two-lane entry more). The ring doesn't fill
  (Krauss allows 1,600–1,700 veh/h at its speed): its traffic bunches at 2.25–2.5 s headways, where the HCM's
  Siegloch curve assumes a random stream, and Tanner's bunched formula (`roundabouts.tanner_entry_capacity`, shown
  beside the HCM in junction_los) predicts the fall. Checked 2026-10-13 (3 seeds × 20 min a point): no ring speed or
  `RING_TIMEGAP_S` lifts 1,000–1,200 without going over 10 % at 0–600, since the follow-up headway and the bunching
  both come from Krauss following; separating them would need a mechanism SUMO doesn't have. The ring runs at its
  curve speed rounded down to 5 km/h (25 km/h for a 25 m radius), the properties panel shows the NCHRP fastest-path
  speed (28 km/h). No HCM capacity for a three-lane entry.
- PCU in calculations: each vehicle type's PCU is kept, but capacities, demand sizing and results count
  vehicles, not passenger car units.
- Multi-lane roundabout circulation: every ring is one path round, so a two-lane entry feeds it
  through one lane at a time and its capacity is a single lane's (measured).
- Emergency braking at the line: Krauss never brakes harder than SUMO's emergency deceleration (9 m/s² for a car),
  overshooting and counting the collision instead, but a vehicle refused at its line by `_move`'s in-step re-check
  still stops there at once (rare; recorded in `emergency_braking` with its real deceleration). Collisions are
  counted (`Simulation.collisions`, same lane and crossing), not resolved; roundabout rings aren't checked for
  crossing overlaps, and a crossing ignores the vehicles' widths. Roundabout entry predicts where circulating
  traffic will be at the ring's speed, so an occasional circulating car still brakes hard.
- A step other than 0.5 s or the ballistic update in the window: only headless runs (`run_headless`) take them.
  Step sensitivity (3 x 3 grid at X 0.8, 2 seeds, 2026-10-18): mean trip time Euler 215 s at 0.1 s, 248 s at 0.5 s
  (+15 %); ballistic 232 s and 235 s (+1 %). Seeds alone vary ±10 % there. SUMO's Krauss (Euler, 0.5 s) is kept as
  the default, so absolute times carry that step's bias; compare designs at the same step and method (every
  comparison tool does).
- Curve speed along a road: a road's lanes are capped for its tightest bend over their whole length (no
  anticipatory braking into, or speed-up out of, the bend); hand-drawn shapes without stored curvature aren't capped.
- Turn pockets: lane drops and lanes added mid-road (only a pocket of 1–2 lanes before a signalised or
  uncontrolled junction, one per approach, kerb or offside, not both), flares at roundabout entries, and pockets
  from OpenStreetMap (`turn:lanes`). A pocket's taper is drawn and driven only for the lanes it moves; vehicles
  change into it at once, as into any lane. The road's width (`width_m`) leaves pockets out, so stop lines and
  junction boxes are sized without them. A pocket at a boundary end is refused (nothing turns there).
- Roundabouts of more than three entry lanes (beyond NCHRP 672): refused, so an OpenStreetMap area or saved project
  with one (a one-way road of 2 lanes per direction counts 4) is refused whole rather than given another control.
- Change intervals per approach: each signalised junction has one amber and all-red, from its fastest approach and
  on the level (no grades); optimise_signals' genome still sizes cycles with the 3 + 2 s defaults.
- Generating trips from end supply/demand other than by the assistant's gravity model.
- From OpenStreetMap: curvature on shaped roads (curve speeds come from the tagged limits), turn
  restrictions, lanes that differ by direction, the signals' own timings; junctions under 12 m apart
  still leave short roads in dense centres (the design check lists them). From an image: roads drawn
  in one colour only (outlined roads trace as two lines); a busy rendered map traces in pieces
  (labels and casings break its roads; a London OSM screenshot gave 64 parts), so over the map the
  OSM data is the way.
- Institutions: trips by purpose beyond commuting and escorting (a type's occupancy for the rest), older pupils
  driving themselves; trips from beyond the catchment (a gravity model over the whole city) and trips between
  buildings; catchment places weighted by distance, population or road capacity instead of equal shares; walking
  measured along the roads, not across blocks; bus riders don't load the buses; one mode split for people and
  employees alike; a what-if (run_experiment, build_scenario) can't add a building's trips, since a test's demand
  is fixed before its scenarios are built; editing a placed building's demand figures in its panel (height is editable; update_institution
  can).
- Road types changing behaviour (the label is stored only).
- Bus lines calling at some stops only; passengers' destinations (alighting is a share of the load at each stop),
  their waiting time by person, more than one door; the passengers on a stop are set in the panel or by the
  assistant, not generated from institutions.
- System 1 beyond the signal agent: it decides TSP's bus requests (Jev or Laya in Ollama, one noul each) and
  answers other extensions' yes/no; its choice and score answers (`assistant.system_one`) have no caller
  yet, and TSP's agent decides once per request (not again as the bus comes closer). System 2 answers questions, edits the network and
  demand, reads the live run (`read_traffic`) and runs its own headless tests, but can't start or stop
  the user's run.
- Small local models choosing tools: offered the tools a request's words point to, qwen3.5 4B handles most
  requests (the stress test, ai_mode.md) but still sometimes writes no Plan: line (then a dropped part of a
  request isn't caught), splits one action into several calls, or runs the same build twice; tool selection is by keywords, so a request in words no group knows gets
  only the core tools until the model calls find_tools; requests in other languages than English aren't grouped.
- Network testing: runs one after another on one thread (no parallel runs); daily demand tested at the
  simulation's speed only; calibration on shortest routes only (assign_routes reaches equilibrium separately) and
  counts on a road both ways together; incidents, work zones and ramp meters aren't drawn on the map; weather is
  one condition for the whole run; a fork's adaptive or actuated signals hold their current greens (TSP's
  look-ahead), and only TSP looks ahead (DEVL doesn't).
- Statistics from research/statistics_catalogue.md not built yet: probit and mixed logit; departure-time choice;
  power-law deterrence and Poisson ML gravity calibration from an observed matrix; SARIMA, Kalman filters and
  learned forecasters; Bayesian incident detection; fuzzy and reinforcement-learning signal controllers (an
  override must keep amber and all-red safe); critical gap estimation (Raff); crossing (non rear-end) conflicts
  and post-encroachment time; offsets in optimise_signals. choose_modes keeps each pair's veh/h (a shift
  between types changes the people carried); its times come from free flow or one run. Route choice measures
  link and move times only with 3 or more traversals; free-flow routes price junctions by a control-delay prior with
  no traffic, at the greens entered (not the run's timing, not the flows: a busy give-way looks as cheap as an empty
  one until assign_routes measures it); critical_links is static (free flow).
- Sub-hour demand: a peak-hour factor or 15-minute profile per hourly pair; signal timing and demand sizing use the
  hour's mean flow, not V/PHF; daily curves have no sub-hour shape; the run's workbook doesn't list the quarters.
- Emergency vehicles: other traffic doesn't yield to them or form a rescue lane (SUMO's bluelight device); they obey their signals (DEVL clears their lane at signalised junctions only, not at roundabouts or give-ways); a live run's DEVL decisions aren't in its workbook (a batch run's are).
- Weaving: the simulated drivers weave by SUMO's LC2013 rules as simplified here (whole-lane hops, a 10 s look-ahead
  per change), harsher than the HCM on short weaves: a Type A weave at 2,500 veh/h ran 10 % under the HCM's speed at
  600 m, 20 % at 300 m and 66 % at 150 m, where every weaver is urgent from the start (SUMO's lcAssertive and
  lcCooperative, which users tune for this, aren't modelled). Ramp lanes (`Graph._ramp_lanes`) apply only to roads
  typed "ramp" (interchanges and slip roads; an OpenStreetMap motorway link isn't typed so). Two-sided weaves, offside
  ramps, Chapter 25's ramp junctions (merge and diverge alone) and the weaving length measured gore to gore (here node
  to node) aren't built.
- Metro rail: its own network and OD matrix, drawn on by institutions (metro.md), but no trains run in the
  simulation (riders just leave the roads), a trip changes lines at most once, at an interchange (a station where
  lines were merged; none to buses; demand between stations no such ride joins can't be entered), walks to a station are straight-line, not along the roads, its loads are at one
  design hour (the OD matrix the same every hour; no crowding feeding back into the choice), and car trips to
  stations (park-and-ride, drop-offs) aren't made. Road OD pairs never take the metro, by design. An accepted line is final by design (accept or delete
  when it is finished; no later edits); undo still takes it back. Lines don't branch or join end to end;
  two crossing lines share only their interchange station. A curve is short straight viaduct pieces in 2.5D.
- Design and accessibility checks: acceleration and deceleration lane lengths, grades and access within 50 m of a
  junction (a building's door is a junction) aren't checked; emissions are a light car's only (no buses, lorries or
  motorcycles, no grades); accessibility counts opportunities by road at free flow or one run's times, not by bus or
  on foot; critical_links and its area closure are static.

- Prompt campaign: automatic crash recovery is not implemented; an existing ledger prevents resubmission. Automatic Apply of actual validated cards is implemented for the user-authorized campaign. See `prompt_campaign.md`.

- Campaign live update still requires one manual Extensions reload and tick when the external desktop helper is unavailable. Future approval cards are automatic after activation.

The campaign does not retry or roll back partially applied failed tools. Only a uniquely identified unresolved Apply callback after a deadline halt supports live recovery; other crash/timeout states remain manual.

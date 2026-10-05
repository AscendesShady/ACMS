# 7. Not built yet

- SCATS's tactical control (ending a green early on a gap: the `actuated` method does it per junction, not inside a
  SCATS region); groups of coordinated junctions other than "every junction with the method"; actuated and
  max-pressure junctions aren't coordinated with each other.
- Start-up lost time 1.8 s against the HCM's default 2.0 s: SUMO's startupDelay can't reach 2.0 s
  without pulling the saturation flow below field values (drivers re-halting as they creep off).
- Results: editing each vehicle type's occupancy in the tab (assumed), per-junction and per-signal KPIs in a run's
  workbook (junction_los and diagnose_signal give them for the assistant's headless runs only), exact per-vehicle trip times (they are within a 5 s sample) and counts on roads
  crossed between two samples; Batch Run compares agents deciding TSP (or plain runs), not network variants (the
  assistant's `run_experiment` does those); a live run has no decision deadline (a batch's Decide within); a batch's runs share one process, so many at once at a fast pace fall behind the clock (the pace achieved is
  recorded and said), and plain (non-TSP) batches go one at a time; a replay needs the run's network and demand open (it reads only the seed, the simulated time and the feed); telemetry is TSP's only;
  pedestrians, gradual (sublane) lane changes, editing each type's behaviour in Mode Behavior.
- Impatience in gap acceptance: a minor road facing a saturated major road waits for a gap
  however long that takes.
- Roundabout and priority capacity are only used to size demand, not reported per approach in the Summary (the
  assistant's junction_los grades each approach's delay).
- PCU in calculations: each vehicle type's PCU is kept, but capacities, demand sizing and results count
  vehicles, not passenger car units.
- Multi-lane roundabout circulation: every ring is one path round, so a two-lane entry feeds it
  through one lane at a time and its capacity is a single lane's (measured).
- Generating trips from end supply/demand other than by the assistant's gravity model.
- From OpenStreetMap: curvature on shaped roads (curve speeds come from the tagged limits), turn
  restrictions, lanes that differ by direction, the signals' own timings; junctions under 12 m apart
  still leave short roads in dense centres (the design check lists them). From an image: roads drawn
  in one colour only (outlined roads trace as two lines); a busy rendered map traces in pieces
  (labels and casings break its roads; a London OSM screenshot gave 64 parts), so over the map the
  OSM data is the way.
- Institutions: trips from beyond the catchment (a gravity model over the whole city) and trips between
  buildings; catchment places weighted by distance, population or road capacity instead of equal shares; walking
  measured along the roads, not across blocks; bus riders don't load the buses; one mode split for people and
  employees alike; a what-if (run_experiment, build_scenario) can't add a building's trips, since a test's demand
  is fixed before its scenarios are built; editing a placed building's figures in its panel (update_institution
  can).
- Road types changing behaviour (the label is stored only).
- Bus lines calling at some stops only; passengers' destinations (alighting is a share of the load at each stop),
  their waiting time by person, more than one door; the passengers on a stop are set in the panel or by the
  assistant, not generated from institutions.
- System 1 beyond the signal agent: it decides TSP's bus requests (Jev or a local Laya, one noul each) and
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
- Statistics from docs/statistics_catalogue.md not built yet: probit and mixed logit; departure-time choice;
  power-law deterrence and Poisson ML gravity calibration from an observed matrix; SARIMA, Kalman filters and
  learned forecasters; Bayesian incident detection; fuzzy and reinforcement-learning signal controllers (an
  override must keep amber and all-red safe); critical gap estimation (Raff); crossing (non rear-end) conflicts
  and post-encroachment time; offsets in optimise_signals. choose_modes keeps each pair's veh/h (a shift
  between types changes the people carried); its times come from free flow or one run. Route choice measures
  link times only on links with 3 or more traversals; critical_links is static (free flow).
- Emergency vehicles: other traffic doesn't yield to them or form a rescue lane (SUMO's bluelight device); they obey their signals (DEVL clears their lane at signalised junctions only, not at roundabouts or give-ways); a live run's DEVL decisions aren't in its workbook (a batch run's are).
- Design and accessibility checks: acceleration and deceleration lane lengths, grades and access within 50 m of a
  junction (a building's door is a junction) aren't checked; emissions are a light car's only (no buses, lorries or
  motorcycles, no grades); accessibility counts opportunities by road at free flow or one run's times, not by bus or
  on foot; critical_links and its area closure are static.

# experiments.py: network testing (no Qt)

- `recorded_run(network, demand, modes, seed, duration_s, settings, label, folder, …)`: one headless run, start to
  workbook (its recorder, `run_headless`, its analysis and extensions' reports, `end_recording`); returns (kpis,
  path, simulated s). Nothing of the window is in it: `MainWindow._one_run` hands it the window's progress, map
  frames and reports. `run_pool(jobs, progress, cancelled, processes)` makes plain runs (no extension deciding)
  side by side, a process each (`multiprocessing`, spawn; half the logical cores, `MAX_PROCESSES` 8 at most):
  threads share one interpreter, so four runs on threads take as long as four in a row. It returns their
  results in the jobs' order (None for a run Stop kept from starting; the runs going end at their next step with
  their workbooks), raises a run's error, and returns None when the processes can't be started or the jobs can't
  be pickled (the caller then makes them one by one). `work` is any module function taking the jobs' arguments
  and `cancelled` (`recorded_run` unless given; `quiet_run`, a search's run kept to its KPIs; `sensitivity_run`);
  `each(index, result)` hears each as it comes (a batch's checkpoint); `watch(label, simulated s, of s, frame)`
  hears how far each labelled run is every `WATCH_S` (0.25 s), with a frame when `frames`, and (label, None…) as
  it ends. `MainWindow._pooled` wires that to the menu bar's clock and, while Show runs on map is on, the map, so
  pooled runs show as a thread's do. Pooled: run_experiment's plain scenarios' runs (two or more; a scenario with
  an extension or `assign_routes` runs on the worker thread), a batch with nobody deciding (each run's
  checkpoint as it finishes), optimise_signals' candidates a generation at a time (every seed of each; with an
  extension deciding, one by one) and parameter_sensitivity's runs. assign_routes stays one run after another:
  each iteration needs the last one's link times. Eight 10-minute runs: 42 s one after another, 9 s pooled (8 cores).

- `run_headless(network, demand, modes, seed, duration_s, override, recorder, progress, cancelled, observe,
  speed, dispatches, …, step_s, step_method, lateral)` (`step_s`: `DT`, or 0.25 or 0.1 s, to check the results don't depend on
  the step; `step_method`: "euler" or the opt-in "ballistic", simulation.md; `lateral`: lateral position and
  filtering, simulation.md; `dispatches`: emergency vehicles sent at the start, (from, to, every s[, s later]); `progress(t)`
  each simulated second, for the menu bar's clock; `on_frame(simulation)` after every step, on the run's thread, so the
  window can take a frame for the map): a Simulation of copies of the inputs stepped as fast as the CPU allows (no wall clock), or with
  `speed` in step with the wall clock (simulated seconds a real second, sleeping when ahead, as a live run;
  a batch with TSP), a
  `results.RunRecorder` (or a function making one from the Simulation, for its signal plans) sampled every
  `SAMPLE_S`, so its figures match a live run's with that seed; returns the Simulation and the recorder.
- **run_reports.py** (no Qt, cut from `MainWindow`): AI Mode's answers on the session's finished tests (the
  window's `tests` dict): `read_results`, `compare_runs`, `analyse_run`, `junction_analysis`, `resilience`,
  `regress`, `power_analysis`, `benefit_cost`, `rank_scenarios`, `read_decisions`, `run_test`, `figure_key`,
  `factorial`, `compact` and `LEAD_FIGURES` (still `MainWindow.LEAD_FIGURES`).
- **searches.py** (no Qt, cut from `MainWindow`): `equilibrium` (route choice by successive averages, given the
  window's `_quick_run` as `quick_run`) and `trip_times` (each pair's trip times, for `_equilibrate_demand`).
- Cut from `main.py` too: `observed_kpis`, `end_recording` and `quick_run` (still `MainWindow._observed_kpis`,
  `_end_recording`, `_quick_run`), `emergency_report` and `AGENT_TIMING`.
- **Simulated time and AI agents.** A run's results depend on the seed and inputs, not on how fast it is stepped,
  except for an AI agent's thinking time (TSP's and DEVL's signal agent, `signal_agent.SignalAgent`):
  - Unpaced headless tests (run_simulation, run_experiment, build_scenario, optimise_signals, assign_routes, the
    demand check; `MainWindow._waiting_agent`, `wait_s` `main_agents.HEADLESS_WAIT_S` 30 real s) pause the run for each
    answer, and the decision takes effect at its question's simulated time plus the model's real latency, as if
    the run went at 1× (`agent_transit_signal_priority`, `dynamic_emergency_lane`: "as at 1x"); an answer not in
    within `wait_s` falls back to the plan. No new questions arise while the run is paused, so the queueing of
    answers (fresher telemetry replacing waiting questions, withdrawn requests) is not what a live run sees.
  - A paced batch (`speed`) never pauses: an answer costs latency × pace in simulated time and counts only within
    the batch's deadline (simulated seconds); questions keep coming while the model thinks.
  - So an unpaced test shows an agent's effect at 1× only. Its result says so (`agent_timing`, `experiments.AGENT_TIMING`:
    run_simulation, run_experiment and its workbook's Read me, optimise_signals, the Scenario Builder's report)
    whenever an extension that may ask an agent was ticked (for run_experiment: named in one of its scenarios'
    `extensions`, the only ones an experiment applies, whatever the Extensions tab has ticked); Batch Run tests
    other paces.
- `Scenario(name, changes, demand_scale, extensions)`, `scenario_from(spec)` (checked), `scenario_network`
  (the changes through `network_tools.run_tool` on a copy, only tools in the allowed set), `scaled(demand)`.
- `summary(values)` (n, mean, SD, the 95 % half-width by Student's t, `stats.t_quantile`),
  `difference(base, other, confidence)` (Welch's interval, its p; `clear` when it leaves out zero; open with
  one seed), `compare(runs, scenarios, precision)` over `MEASURES` (with the travel time, planning time and buffer
  indexes, the mean system travel time and the unfinished share of trips) and per vehicle type (`measures_of`): each scenario's `replications_needed` (`stats.replications`,
  ±precision at 95 %; over `UNSTABLE_RUNS` 20 the window reports it as `unstable`) and its difference from
  the baseline, paired over the seeds both ran (`stats.paired`; Welch when none pair), at 95 % widened by
  Bonferroni to 1 − 0.05/k for k scenarios (`confidence`), with `test`, `p`, `ci`, `clear`, `better`;
  `headline(kpis)`.
- `analyse_run(observed)` (from `RunRecorder.observed()`): reliability (trip time mean, SD, CV, median,
  85th and 95th percentiles, TTI, PTI and the buffer index BI, all from each trip's time over its free-flow time at
  its own free speed (a bike's is its own, not the limit), TTI and BI on the same mean, PTI a spread across the run's
  trips, not FHWA's across days; the least reliable pairs with 5 trips or more; trips still on the road are in
  `RunRecorder.observed()["unfinished"]`), each origin's
  arrivals per whole minute against Poisson (`stats.dispersion`), and the ten links (each direction of a road:
  `link`, `road`) whose 95th-percentile queue, metres a lane of that direction, is the largest share of the road's
  length (`spills_back` when it reaches it).
  `check_calibration(kpis, counts)`: each counted road's measured flow, GEH, and `stats.fit`'s scores.
- `holds(network, demand, modes, seed, ...)`: one `HOLD_MINUTES` (45) headless run; it holds unless the
  vehicles in the network and waiting to enter grow over the last `HOLD_WINDOW_MINUTES` (15) by more than
  `HOLD_BUILD_UP` (5 %) of those setting off, and by at least `HOLD_MIN_PILE` (25: a light demand's
  few vehicles swing more than that by chance). Where a pair's hour is uneven (a peak-hour factor or quarters), the
  network's busiest quarter (the pairs' veh/h in each quarter added up) is judged the same way too
  (`peak_quarter_build_up`; negative when a first-hour queue is still draining), the run going on to that quarter's
  end (60 min for the last; the first is judged in the second hour, 60-75 min, as in the first the network fills
  from empty; `hold_minutes(demand)` gives the run's length, which the window's progress and report show), its count at the start scaled by the
  rise in arrivals (more vehicles are on the road at once with no queue, Little's law; uncorrected, X 0.6 at PHF
  0.85 read 0.07-0.09): the last window alone came before it, or after it and saw its queue draining (PHF 0.7 at
  X 0.85 ran the peak quarter at X 1.21 and passed; quarters 5/5/5/85 jammed in their last quarter and passed). `holds_by_majority(check,
  seed)`: a verdict within half the threshold either side of it (`BORDERLINE`) goes to three seeds' majority (one
  seed's largest holding share had come out 0.875, 0.875 or 0.75). `largest_holding(check)`: the whole demand, else
  the largest share that holds by bisection (`HOLD_STEPS` 4). `at_busiest_hour(demand)`: daily pairs as veh/h in the
  network's busiest hour; `busiest_hour(demand)` which hour. The window's `_check_demand` uses them (AI Mode,
  generate_demand); its headless tests run daily demand at that hour too. `run_headless(observe=True)` records
  from the recorder's warm-up.
- `parameter_sensitivity(network, demand, modes, seeds, duration_s, parameters, step, ...)`: a run as it is and one
  with each of `SENSITIVITY_PARAMETERS` (tau_s: every vehicle type's; critical_gap_s, ring_timegap_s: the
  Simulation's, set through `run_headless(params=…)`; `varied`) at ×(1 ± `SENSITIVITY_STEP`, 0.1), on the same seeds;
  per parameter and `SENSITIVITY_MEASURES` figure, the central-difference elasticity (Y₊ − Y₋) / (2·step·Y₀), mean over
  the seeds with its 95 % interval (FHWA Traffic Analysis Toolbox III's calibration step). None if stopped. Each
  run records to a folder of its own under the system temp, removed after (as `quick_run`'s), so none
  is left for `results.recover`. AI Mode's
  parameter_sensitivity (`MainWindow._parameter_sensitivity`: the open network and demand, 1 to 5 seeds, default the
  run seed and the next, 20 simulated minutes, in the background).
- `experiment_workbook(path, experiment, inputs, journal)`, named by `experiment_name(started)`.
- Limits: `MAX_SCENARIOS` 6, `MAX_SEEDS` 20, `MAX_RUNS` 60, `MAX_MINUTES` 240. The window runs it
  (`MainWindow._runs_command`, see AI Mode); a run's workbook names are numbered when two start in the
  same second (`RunRecorder.finish`).
- Headless runs for the assistant are observed (`run_headless(observe=True)`, simulation.observe), and the
  window keeps the observations with the KPIs (`observed_kpis`; `conflicts` = events under
  `CONFLICT_TTC_S` 1.5 s, an experiment measure). `compare` also gives, with 3 or more scenarios, an ANOVA per
  figure (`stats.anova`: randomized block with the seeds as blocks), Wilcoxon's signed-rank p beside each
  paired difference, and for mean speed the power model's crash change (`speed_risk`).
- A warm-up: run_simulation and run_experiment take `measure_from_min` (0 to under duration_min, else refused),
  handed to each run's `RunRecorder(measure_from_s=)` (`MainWindow._one_run`; results.md), so their KPIs and
  comparisons count from it. A headless caller of `run_headless` gives its own recorder the same.
- `analyse_run(kpis)` adds the warm-up by MSER-5 on the vehicles on the road (the Run statistics sheet suggests
  it as `measure_from_min`) and the steady state's
  throughput and mean speed by batch means, the Gini of road flows, and `bus_stops`: each line's (OD pair's)
  headways at each stop, cv against its timetable and HCM 2000's headway adherence LOS (Exhibit 27-8, 6 buses
  an hour or more), and the expected wait.
- `detectors(kpis, road)`: per link, flow, time-mean and space-mean speed, v15/v50/v85, SD, headways (mean,
  cv, shortest, under 3 s), occupancy, density and counts a minute against Poisson (the minutes counted from the
  end of the warm-up, `observed_from_s`); `discharge`: at signalised
  stop lines, through movements of the commonest type, each green's standing queue (cut at a gap over
  `QUEUE_GAP_S` 4 s): the saturation headway from the 5th vehicle, saturation flow, start-up lost time.
- `safety(kpis, ttc_s)`: conflicts under the threshold per road, severe ones (TTC < 1 s or DRAC > 3.35 m/s²),
  the rate per million veh-km, a safety performance function across roads (`stats.poisson_regression` with
  ln km as offset; NB2 α when overdispersed) and the Empirical Bayes excess ranking (a Poisson fit makes EB the
  expectation, no excess anywhere: then by z = (conflicts − expected)/√expected, `ranked_by`); with them, the run's recorded
  `collisions` (count, by road or junction, by kind: same lane or crossing) and `emergency_braking` (count, by where).
- `series(kpis, what, road, interval_min)`: a run's values per whole interval from the end of its warm-up
  (`observed_from_s`, else `measure_from_s`; network throughput, mean speed, on the road, queued from the minutes
  after it; trip time by arrival; a road's flow or space-mean speed from its detectors); an interval with no trip
  arriving (trip time) or no vehicle passing (speed) is refused (a longer interval_min), not read as 0, for
  `forecast` (stats.holt) and `check_calibration(kpis, counts, profiles)`'s FHWA 2019 envelopes
  (`stats.envelopes`: Criteria I–IV against the representative day).
- The window also runs `assign_routes` (`MainWindow._assign_routes`: the method of successive averages over
  `_quick_run`s, each with the route choice so far, the link and move times (`link_times_s`, `turn_times_s`,
  each move's own junction delay counted once: `Graph.move_time`) moved 1/n of the way to those measured, the
  relative gap per iteration, the share of flow off its path-size logit share at the times just measured
  (½Σq·Σ_r|P_r − P_r^logit| / Σq, 0 at stochastic user equilibrium; `excess_cost` Σq·Σ_r P_r(T_r − T_min) / Σq·T_min
  beside it has a positive floor under logit choice, about 0.10 on a 3 x 3 grid), every iteration on the same seed
  (a new seed each time held the gap at 0.05-0.09 by noise; on one seed it fell from 0.07 to 0.004 in 4), then set on
  the network as one undo step) and
  `optimise_signals` (`_optimise_signals`: a genetic algorithm over fixed-time cycles and splits, every
  candidate scored on the same seeds without workbooks, one elite, tournament selection, blend crossover,
  Gaussian mutation; the best against the current plans by the paired test).
- `MEASURES` also compares the mean wait per trip, the mean queued at once, passenger delay (person·h) and
  the mean delay per passenger (results.md); `measures_of` adds each type's mean wait, and the figures the
  run's extensions reported (`kpis["reported"]`: TSP's `AGENT_MEASURES`: bus requests, TRUE, green held,
  greens cut, agent latency, errors, fell back, too late). `comparison_sheet(rows)` is the Comparison sheet
  both workbooks use.
- **Batch runs** (the Batch Run extension, `MainWindow.run_batch(seeds, minutes, times, tests, speed, at_once,
  default)`): the universal test of the AI controllers. `tests` maps each controller tested (a kind of
  `main_agents.CONTROLLERS`: TSP, DBL, DEVL) to its plan, `{"agents": the models to test, at least one,
  "deadline_s"}`; each runs whether ticked in the tab or not. An agent of the batch is one combination of the
  controllers' models: with one controller its model's label, with several "TSP <label> · DBL <label> · DEVL
  <label>". `default` (Compare with default) adds the one baseline, `main_agents.DEFAULT`, the **Default
  configuration**: the network as configured, no controller acting (its signal plans: Webster's from the demand, or
  timings from a survey), first, once a seed (a seed always runs the same; Times each repeats only the models,
  whose answers vary); every agent is compared with it, else with the first agent. Everything is checked
  first (no run going, demand, each controller loaded, each tested one with a model, each model found and with its
  key, each deadline 1–280 s, 1 to `main_agents.MAX_AT_ONCE` 8 at once, something to run), then every agent × seed ×
  time headless (`_in_background`, Stop ends it),
  seed by seed: every agent on a seed (and its times) before the next seed, so a batch cut short has every agent
  on the seeds it finished and still compares, pair by pair. The batch's workbook is a checkpoint: rewritten after
  every finished run (written beside it as "… (saving).xlsx" and swapped in whole, so a crash mid-write keeps the
  last one; skipped while the file is open in Excel), its Read me saying "Runs finished: n of N". Each run's own
  workbook is saved as it ends, as before.
  With TSP ticked up to `at_once` go side by side on worker threads, but never two on one model server (a
  provider: Ollama (its chat and decision models alike), Jev, Grok, Gemini; a baseline is none), so no run's questions wait behind another's;
  a run that raises stops the others and is the batch's error. Each records the pace it achieved
  (`pace_achieved`, simulated over wall seconds while it stepped; under 90 % of the pace asked, the batch says
  so: fewer at once or a slower pace). A batch with nobody deciding goes side by side, a process each
  (`run_pool`, `MainWindow._pooled`), as fast as the CPU allows. Each agent's model warmed up with one throwaway request, `_warm_up`, so its loading isn't
  charged to its first run), each through `_one_run` paced at the batch's `speed` (1–60, the panel's 1×, 2×, 5×, 10×) when a model answers in
  it (a baseline and its emergency fleet depend on the seed alone: as fast as the CPU allows, about 25 s a
  simulated hour for ~400 vehicles)
  with a signal agent per controller that never waits (its `deadline_s`, the controller's Decide within, in
  simulated s; a baseline: nobody asked, `SignalAgent(rule=)` answering, False never, True always), answering in its
  agent's own system (the question carries both), each saving its workbook named by seed and model (its Run sheet
  has "AI model deciding <kind>" and "<kind> decision deadline (simulated s)" for each controller), and its
  telemetry feeds beside it, in `ACMS batch <date time> runs`. TSP and DBL act for buses: with no bus type among
  the vehicle types the answer's note says they had nothing to decide.
  With no controller tested the runs are plain (the default alone). **`MainWindow.replay_run(workbook)`** (Batch Run's
  Replay a run) runs one of them again: the seed (Run sheet) and simulated time (Summary) from its workbook,
  headless and unpaced, TSP's agent a `Replay` of the telemetry feed beside it (each decision at its recorded
  simulated time, no model asked), saving `… <model>-replay.xlsx` and comparing its Summary with the original's
  (`same`, or the figures that differ: the network or demand open isn't the run's). Then `averaged(runs)` makes each (agent,
  seed)'s repeats one (every figure's mean), `compare` sets each agent against the first (the default configuration when
  compared with) paired over the seeds, and `batch_workbook` writes `ACMS batch <date time>.xlsx` (`batch_name`):
  **Read me** (seeds, times, duration, agents, baseline, each controller's deadline, how it ran and compared), **Headline**
  (`headline_sheet`: per agent, `HEADLINE`'s passenger throughput increase, passenger delay and delay per
  passenger reduction, queue reductions (mean and most queued), vehicle wait time and delay reductions,
  vehicle throughput increase, emergency travel time reduction (Test DEVL), bus approach time reduction (Test
  DBL: `dbl_approach_s`, a bus's check-in to check-out, among DBL's `dbl_*` `AGENT_MEASURES`), and each type's delay and wait
  reduction: baseline and agent means, the
  increase or reduction in % (positive: better), difference, interval, p, clear, better), **Comparison**,
  **Agents** (system, provider, model, runs, and the means of `AGENT_MEASURES`), **Runs** (every run, its
  repeat, workbook and figures) and the inputs.
- **Conditions a scenario meets** (`Scenario.incidents`, `weather`, `random_incidents`, `meters`, `assign_routes`,
  `lateral` (lateral position and filtering, true or false; kept under a condition, a factor's level may set it);
  `run_headless(incidents, weather, meters)` through `apply_conditions`, which checks every road and lane first):
  `incidents_from` (lanes closed or a work zone's limit on a road, from `at_min` for `for_min`; simulation.md's
  `close`), `draw_incidents` (random incidents, Poisson at `per_hour`, on roads weighted by length, from each seed's
  own stream so every scenario on a seed meets the same ones), `meters_from` (ALINEA or a fixed rate),
  `assign_routes` ({"iterations", "until_gap"}: route equilibrium found once for the scenario on the first seed,
  then used by every seed). The Scenarios sheet gains Weather, Incidents, Random incidents and Ramp meters.
- **Experiments under conditions** (run_experiment's `conditions`, `conditions_from`, up to `MAX_CONDITIONS` 6:
  name, demand_scale, weather, incidents, random_incidents, weight): every design runs under each (`under`: named
  "design | condition"), each condition's designs are compared against its own baseline, and `condition_summary`
  gives on the `objective` figure each design's mean per condition, the winner, every design's regret, the weighted
  mean (over the conditions that design has a mean under, by their weights), the worst regret and the robust choice (the least worst regret), and whether the winner changes.
- **Factorial designs** (run_experiment's `factors`, `factorial`): 1 to 3 factors with 2 or more labelled levels
  (each with changes, demand_scale, extensions, weather, incidents, meters), every combination a scenario named
  "factor=level, ..." (12 at most); with two factors `run_reports.factorial` gives the two-way ANOVA
  (`stats.two_way_anova`: main effects and interaction, seeds as replicates) on the objective, per condition.
- `MEASURES` also has `co2_kg` (cars and vans, analysis.co2_kg), `bus_headway_cv` (`headway_cv`: lower is more
  regular) and `vehicle_km`; `OPTIONAL_MEASURES` (conflicts, CO2, headway cv) are left out of runs that didn't
  observe them; `AGENT_MEASURES` gains `agent_agrees_estimate` (TSP: decisions agreeing with its person-delay
  estimate). `bus_stops` adds `bunched_percent` (a bus under a quarter of the headway behind the last) and
  `excess_wait_min`; `bus_passengers(observed)` (each stop's buses, boarded, alighted, most left behind, dwell
  mean and longest, heaviest load) is in `analyse_run` and a Bus passengers sheet.
- **stats.py** (no Qt; the tests behind these tools): Student's t, χ², the normal tail (`normal_sf`); rank
  tests with tie corrections (`friedman` with seeds as blocks, `kruskal_wallis`); `all_pairs`
  (every pair by the paired t with Wilcoxon's signed-rank p beside it, Bonferroni over the pairs, with compact letters: designs
  sharing a letter aren't told apart); `paired_power`, `seeds_for_power`, `detectable` (power of the paired test,
  exact by the noncentral t, seeds needed for 80 %, the smallest difference found); `holm` (Holm's adjusted p:
  `compare` gives each scenario's p across its figures as `vs_baseline_p_holm`, the workbook's last Comparison
  column and the assistant's `p_adjusted`); `two_way_anova`; and the earlier summary, paired, anova,
  signed_rank, regression, count models, logit, k-means, envelopes, Holt and MSER.

# analysis.py and emissions.py: what a run's observations say

Both are core modules (no Qt). They read a headless run's KPIs with what it observed (`kpis["observed"]`:
`RunRecorder.observed()` joined with `Simulation.observations()` by `MainWindow._observed_kpis`), so the
assistant's tools can ask any run of the session (`read_results` lists them).

## What a run observes for them (simulation.py, `observe()`)

- `traversal_log`: every link a vehicle drove, (link, when it entered, seconds to the next link, type, id, its own
  free-flow seconds): the time includes the junction at its end; the free-flow seconds are the link and the turn
  it took at its own free speed (`vehicle.free_speed`: speed factor, top and desired speed; None if the turn
  wasn't seen). `links[link]["cross_s"]`: the free time through that junction (each turn's length at its own
  speed, the mean of the link's turns), the fallback for a log without the vehicle's own, so control delay
  leaves out the seconds a vehicle needs to cross the box.
- `arrivals`: per signalised approach, vehicles that joined its queue or came within `ARRIVAL_ZONE_M` (5 m) of the
  stop line, and how many of them while their signal was green (the HCM's P, Eq 16-10).
- `fuel`: millilitres burnt by cars and vans each step (emissions.py), by type and road, and their kilometres.
- `signals`: each signal's plan as the run ended (method, cycle, phases with their links and greens, the
  saturation flow of each approach); `controls`: each junction's control; `waiting_to_enter`: vehicles still
  waiting at their origin; `unreachable`: OD pairs with no route.
- `green_starts` (each signalised approach's green onsets), `emergency_standstill` (each emergency vehicle's longest
  stand), `boardings` (each bus call with passengers: simulation.md), and the run's `weather`, `meters` (each ALINEA
  period's rate and occupancy) and `closures`.

## analysis.py

- `junction_los(kpis)` (tool `junction_los`): control delay per approach = time on the approach and through the
  junction − that vehicle's own free-flow time (its free speed on the link, then through the turn it took: a
  bicycle at 20 km/h has none for riding slower than the limit), the HCM's definition (HCM 2000 Ch 16 App. A).
  Graded A–F: signals by Exhibit 16-2 (≤10, 20, 35, 55, 80 s), give-way, stop and roundabout entries by
  Exhibits 17-2 and 17-22 (≤10, 15, 25, 35, 50 s; HCM 2000 has no roundabout LOS, HCM 2010 uses these). Per
  junction the flow-weighted delay and its LOS, the worst approach; signal approaches' capacity
  (saturation × effective green ÷ cycle) and v/c; each approach's 95th-percentile queue (its link's: metres a
  lane of that direction) against its road (`queue_p95`); a roundabout entry's circulating flow (its link's `circulating` movements counted in
  `turn_traversals`), its capacity measured on the simulated drivers (`ROUNDABOUT_ENTRY_VPH`) and v/c, and the
  HCM 6 capacity beside it (`roundabouts.hcm_entry_capacity`), and for a one-lane entry Tanner's against a bunched ring
  (`tanner_entry_capacity`, `tanner_capacity_vph`: below the HCM's above about 800 veh/h circulating, as the simulation is); and a grid of LOS per 15-minute period (two whole
  periods or more, counted from the end of the warm-up, `observed_from_s`).
- `diagnose_signal(kpis, junction)` (tool `diagnose_signal`): flow ratio y = q/s per phase (its busiest
  approach), Y, the critical v/c Xc = Y·C/(C − L) (L = `signal_timing.lost_time`, as Webster's), Webster's cycle for the demand; per approach
  v/c, arrivals on green P, platoon ratio Rp = P/(g/C), arrival type and f_PA (Exhibits 16-11, 16-12), PF =
  (1 − P)·f_PA/(1 − g/C) (≤ 1 for types 3–6), the HCM delay d1·PF + d2 (Eqs 16-11, 16-12, k = 0.5, T = 0.25 h:
  `signal_timing.incremental_delay`) against the simulated delay, spillback (95th-percentile queue ≥ 90 % of the
  road). Causes, worst first (`_causes`): over capacity (Xc > 1), greens not matching demand (an approach over 1
  while Xc ≤ 1), near capacity (Xc > 0.9), cycle too long (over 1.5 × Webster's at Xc < 0.85), poor progression
  (a coordinated method with arrival type 1–2 on a busy approach), held beyond the signal (simulated delay over
  twice the HCM's and 20 s more: an exit, lane changes or the next junction), spillback; each with the tool to fix it.
- `run_health(kpis)` (tool `check_run_health`): gridlock (3 minutes running with vehicles on the road, none
  arriving, under 3 km/h), a gridlock the teleports broke (`teleports`: how many, from which links), vehicles
  waiting at their origin (over 10 and 5 % of those spawned), pairs with no
  route, over half the vehicles still on the road, an MSER-5 warm-up of half the run or more, signal approaches
  over capacity; each with what to do (FHWA Toolbox III, error checking).
- `slow_links(kpis)` (tool `preflight_check`'s low-demand run): links whose mean traversal is over 1.5 × free
  flow + 5 s with no signal at their end.
- `resilience(base, disrupted, measure)` (tool `resilience`): minute series smoothed over 3 minutes; the deficit
  against the baseline each minute; start (first over 5 %), the biggest drop, recovery (back within 5 % for 3
  minutes), performance kept over the window (1 − mean deficit) and the extra vehicle-hours of delay
  (Bruneau et al. 2003; Taylor, Vulnerability Analysis for Transportation).
- `emissions_report(kpis)` (tool `emissions_report`), `co2_kg(kpis)` (the experiments' "CO2 of cars and vans").
- `invariants(kpis, reported)` (tool `test_agent_compliance`, and every run with an agent): what an agent's run
  broke, each with its evidence: an approach holding traffic on red for over `STARVE_S` (180 s) (`_held_on_red`:
  a vehicle that reached the stop line, entered + free-flow time, and left on a later green waited from the later of
  then and the approach's last crossing before that green; vehicles still on it at the end, from its last green or
  crossing; a resting green or a long cycle holds no one), an emergency
  vehicle standing still over `EMERGENCY_STANDSTILL_S` (30 s) at once, gridlock, agent errors (Explainable AI for
  ITS: safety constraints checked on the agent's behaviour, not its words).

## emissions.py

Biggs and Akçelik's (1986) instantaneous power model with Bowyer, Akçelik and Biggs's (1985) light-vehicle
parameters (α 0.375 mL/s, β1 0.09 mL/kJ, β2 0.03 mL/(kJ·m/s²), b1 0.333 kN, b2 0.00108 kN/(m/s)², M 1,200 kg):
R_T = b1 + b2·v² + M·a/1000 (+ grade, none here); fuel = α + β1·R_T·v + β2·M·a²·v/1000 (a > 0) while R_T > 0, else
α. CO2 = 2.35 kg a litre of petrol (US EPA). Only `light_motor` vehicles (2.5–7 m, desired speed over 30 km/h:
cars, taxis, vans, emergency vans); buses, lorries, motorcycles and bicycles are left out. A 1980s car: compare
designs with it, not fleets.

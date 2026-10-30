# signal_timing.py: signal timing methods (no Qt, standard library only)

Each follows its published method (§6); none is the commercial product, so its numbers won't match
the real software exactly. Times in seconds; a cyclic profile has one bin per second of the cycle.

- **Clearance** (`clearance(speed, crossing)`): each signalised junction's own amber and all-red, by ITE's
  kinematic change and clearance intervals on the level: amber Y = t + v/2a (t = 1 s perception-reaction,
  a = 3.05 m/s², v its fastest approach's speed limit), all-red R = (W + L)/v (W its width stop line to far
  stop line, L = 6.1 m); amber held to 3–6 s and all-red to 1–6 s (MUTCD), to 0.1 s. A 50 km/h, two-lane grid
  junction gets 3.3 + 2.0 s, a 120 km/h six-lane one 6.0 (6.5 capped) + 2.1 s. `lane_graph.Signal` runs them and
  every method below takes the junction's amber + all-red (`span`; `AMBER_S` + `ALL_RED_S`, 3 + 2 s, only
  where no junction is given) for its lost time and phase starts.
- **Shared:** `webster(ratios, span)` (C0 between `MIN_CYCLE_S` 30 s and 120 s; its greens fill that cycle with `fit_greens`, so a phase
  held at its minimum green takes its time from the others, never lengthening the cycle past the cap; minimum greens
  and clearances that can't fit in 120 s are refused); `critical_saturation(ratios)` (X = Y·C/(C − L) at Webster's cycle, the
  HCM's critical v/c; generate_demand sizes demand by it); `KERB_TURN_PER_LANE_VPH` (1,770) and
  `OFFSIDE_TURN_PER_LANE_VPH` (1,860), `UTURN_PER_LANE_VPH` (1,550, at the 25 km/h U-turn floor), turning lanes' saturation flows measured on the simulated drivers (86-90 %
  of straight-on with the per-kind turn floors, within the HCM's 85-95 %; 76-78 % at a 25 km/h floor)
  (the flow ratios that use them are lane_graph.Graph's); `fit_greens(shares, cycle)` (greens that fill a cycle exactly,
  effective green in proportion to the shares, none below 7 s); `phase_starts`, `green_mask` (the
  effective green, the start-up lost time after the green starts to the amber used); `disperse` (Robertson's platoon
  dispersion: q′(i + t) = F·q(i) + (1 − F)·q′(i + t − 1), t = 0.8 × cruise time, F = 1/(1 + 0.35·t),
  TRANSYT's defaults); `cyclic_queue` (arrivals join the queue each second, discharging at saturation
  flow during the effective green, twice round the cycle; delay is the queue waiting each second, and a
  vehicle arriving at a red or behind a queue stops; over capacity the deterministic part runs at
  capacity); `hcm_delay` and `incremental_delay` (the HCM's control delay: uniform
  d1 = 0.5·C·(1 − g/C)²/(1 − min(1, X)·g/C) plus incremental d2 = 900·T·[(X − 1) + √((X − 1)² +
  8kIX/(cT))], T = 0.25 h, k = 0.5, I = 1; `cyclic_queue` adds d2 as its random delay).
- **`TimingModel`** (TRANSYT's traffic model of a group): each approach's arrivals are its upstream
  approaches' departures dispersed on the way, plus outside traffic evenly; `solve` runs every stop line
  in three passes; the performance index `pi` is delay plus the stop weight per stop, per hour.
  `trial` re-times one junction and recomputes only what that touches (its own approaches and those
  they feed). `progression` gives starting offsets: from the busiest junction along the heaviest
  movements, each downstream green starting as the platoon from upstream arrives.
- **`hill_climb`** (TRANSYT's optimiser): for step sizes 16, 8, 4, 2, 1 s, try each junction's offset,
  and green moved between neighbouring phases, by the step either way, keeping each change that lowers
  the PI, until a round changes nothing ("finished"). It stops early with the best so far after `OPTIMISE_TRIALS`
  4,000 plans tried ("trials": the same plan on any machine, about 4 s here; a 4x4 grid needs about 1,600, a 6x6
  3,300), or at the safety cap of `OPTIMISE_CAP_S` 30 s of real time ("time": that plan depends on the machine's
  speed, and its junctions' plans say `stopped: "time"`). A plan's `finished` says whether it finished.
- **TRANSYT** (`transyt`): cycle from the critical junction (`common_cycle`: the longest Webster cycle,
  within the limits); equisaturation greens; progression offsets; hill-climb on offsets and greens.
- **Synchro** (`synchro_cycle`, `percentile_greens`, `synchro`): greens for each phase's 90th percentile
  flow (arrivals per cycle λ + 1.28√λ); the common cycle, from the limits in 5 s steps (never under the minimum
  greens' need, rounded up, however low the maximum), with the least
  total percentile delay (control delay averaged over the 90th, 70th, 50th, 30th and 10th percentile
  flows, `percentile_delay`); offsets by the hill-climb for delay alone (no stop weight), greens kept.
- **SCATS** (`ScatsRegion`, `ScatsJunction`): each lane's degree of saturation during its green,
  DS = (g − (T − t·h))/g from its stop-line loop's unoccupied time T and the h vehicles that crossed
  it (standard space time t = 0.9 s, measured). Once a cycle the region's cycle moves towards the length that would
  bring its most saturated junction to target, by at most 6 s: a junction's DS is its most saturated phase's, a
  phase's its lanes' DS weighted by the vehicles each passed, averaged over its last `SCATS_SMOOTH` (3) cycles (the
  worst single lane of one cycle took a corridor at DS 0.6-0.7 to the 120 s cap);
  each junction votes for the split plan, of four (equisaturation, even, the busiest phase +25 %, −25 %),
  that would even out its phases' DS best, and uses a plan once it wins 2 of the last 3 votes.
- **SCOOT** (`ScootRegion`, `ScootJunction`, `ScootLink`): each link's counts in each second of the
  cycle join its cyclic flow profile (half the old, half the new); delayed and dispersed to the stop
  line they predict the queue. As each phase starts, the split optimiser moves its green's end 4 s
  either way if that lowers the most saturated phase's DS (the stored split follows by 1 s); once a
  cycle the offset optimiser moves the offset 4 s either way if that lowers delay and stops on its own
  links and those it feeds at its neighbours; every 5 minutes the cycle optimiser lengthens the
  region's cycle (4 s below 64 s, 8 s above) while its most saturated junction is over target, and
  shortens it once all are 10 points under.
- **Offsets in an adaptive region** (`realigned_length`): each junction's cycle is lengthened or
  shortened by up to 4 s so its next one starts on time (reference + offset + k·cycle); a new cycle
  length takes effect at each junction's next cycle start.
- **Calibrated to the simulated drivers** (the HCM's field method on SUMO's default passenger car at
  a signal with a standing queue, its 0.9 s startup delay; 4 seeds, an hour each, headways from the 5th queued vehicle on): saturation
  flow `SATURATION_PER_LANE_VPH` 2,060 veh/h per lane (measured 1.744 s, 2,064); start-up lost time
  `START_UP_LOST_S` 1.8 s (1.81); amber used `AMBER_USED_S` 2.4 s of 3.3 (2.37 over 4 seeds, 2026-10-18, with SUMO's
  amber rule; 2.77 when drivers reacted first; the HCM's e is 2.0 s); SCATS's standard space
  time 0.9 s (0.88). Effective green = green + amber used − start-up lost (the HCM's extension of effective
  green e, the same whatever the amber); Webster's lost time per phase is the start-up lost time, the all-red
  and the amber not used (tL = l1 + Y − e: 4.3 s at 3.3 + 2.0 s). TSP's controller estimates a queue's
  clearance with the same saturation headway and start-up lost time.
  `test_the_timing_methods_are_calibrated_to_the_simulated_drivers` re-measures all four, so a change
  to the drivers that moves them fails there.
- **Actuated** (`ActuatedJunction`; HCM 2000 Ch 16 App. B, SUMO's MSActuatedTrafficLightLogic): calls from a
  detector `DETECTOR_GAP_S` (2 s) of travel before each stop line; a green runs `MIN_GREEN_S`, then on while
  vehicles cross its detectors within `UNIT_EXTENSION_S` (3 s, SUMO's max-gap) of each other, up to
  `ACTUATED_MAX_GREEN_S` (50 s); it rests in green with no other call; next, the next phase in turn with a call.
- **Max-pressure** (`MaxPressureJunction`; Varaiya 2013, Wei et al. 2019's PressLight form): each phase's pressure
  from the simulation (`pressure(links)`, vehicles within `PRESSURE_ZONE_M` 100 m of the line, a detection zone); after `MIN_GREEN_S` a green ends as soon as another phase's pressure is
  higher, which goes next (not in turn), and at `ACTUATED_MAX_GREEN_S` at most; a phase with traffic red
  `MAX_RED_S` (100 s) goes next whatever the pressures (two busy arms had passed the green between them while a
  light one waited 300 s and was teleported). Both are in `ACTUATED` (each
  junction on its own, not a coordinated group) and in `models.SIGNAL_METHODS`; simulation.md wires them.
- Limits: SCATS's tactical control (ending a green early on a gap) isn't built inside a SCATS region.

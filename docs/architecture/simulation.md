# simulation.py: the traffic simulation (no Qt)

Structured like SUMO's step (reverse_engineering/simulation/simulation_engine.md): every `DT` = 0.5 s
it plans every vehicle's speed from the state at the start of the step, moves them, lets them change
lanes, then inserts new vehicles; poses are then updated for drawing.

- **Lane graph** (`Graph`, built once per Run):
  - `Link`: one road in one travel direction. Its `Lane`s (0 = kerbside) are the road's centre line
    offset to the driving side, cut back at each junction to the stop line (signalised, the same
    `stop_line_inset` as the map), the give-way line (roundabout, ICD/2 + 0.5 m) or the junction's
    edge (uncontrolled); boundary ends aren't cut. Short roads share what length there is.
  - `Turn`: a path through a junction from an arriving lane to a leaving lane. Classed kerb-side,
    straight, offside (across oncoming traffic) or U-turn (back along its road) by the angle between
    them (`_kind`). A cubic Bézier, with its speed capped by `curve_speed_kmh` of its radius (a U-turn
    swings out into the junction rather than turning on the stop lines); at a roundabout, an entry
    curve, an arc round the ring (radius at the middle of the circulatory roadway; clockwise when
    driving on the left) and an exit curve (a U-turn goes almost all the way round).
  - **No offside turns at two-phase signals** (`_two_phase`): no phase protects them from oncoming
    traffic, so they aren't built. `_add_detours` gives each one a median U-turn instead: straight
    on to the next junction, a U-turn there, back, and a kerb-side turn (left, driving on the left;
    right, driving on the right). Where straight ahead is a boundary end or a one-way road, it turns
    kerb-side, U-turns at that junction and comes back straight on. U-turns are built only where a
    detour needs one: at the far end of a road leaving a two-phase signal. Four-phase signals give
    each arm its own phase, so their offside turns stay.
  - Lane rules: a kerb-side turn leaves from lane 0 to lane 0, an offside turn or U-turn from the
    offside lane to the offside lane, straight on from any lane to the same one; where the road
    forbids lane changes every lane may take every turn. Other turns exist but are only taken after
    standing 8 s at the line in the wrong lane while free to go (impatience; waiting at a red doesn't
    count), so nothing gridlocks.
  - Conflicts: where two turns' paths cross (`segment_hit`) or merge into one lane, with who gives
    way: a U-turn to everything else (on the same green); an offside turn to oncoming traffic on the
    same green; a minor arm of an uncontrolled junction to the major road (the two arms most nearly
    in line).
  - `Signal`: a signalised junction's phases in turn, each green followed by 3 s amber and 2 s
    all-red. Two phases: east-west arms, then north-south; four: each arm alone, in turn round the
    junction. Fixed-time: the same greens every cycle, the first phase's green starting `offset` s
    into each (`state(link, t)` from `(t − offset) mod cycle`; `cycle_start(t)`). Adaptive (`adapt`):
    it steps from phase to phase, telling its `controller` (a SCATS or SCOOT junction) as each cycle
    and each phase starts, which may change the greens (and so the cycle) from then on.
- **Signal timing** (`_time_signals`, before the run; `signal_plans`; the methods in signal_timing.py).
  `Graph.assign` routes every pair's veh/h and `Graph.saturation` gives a junction's degree of saturation
  by its control (see AI Mode's generate_demand); `design_flows` routes every pair's flow along its fastest path and adds up the veh/h arriving on
  each arm and making each movement (link to next link; daily demand: at the network's busiest hour).
  Each phase's critical flow ratio y is its busiest arm's (`Graph.arm_ratio`), lane by lane
  (`_lane_load`): kerb turns load the kerbside lane at 1,160 veh/h and offside turns and U-turns the
  offside lane at 1,100, straight-on traffic (2,060 veh/h per lane) fills whichever lanes have room;
  where any lane may turn, all share. All three are calibrated to the simulated drivers (below); turning
  drivers slow for the curve. `saturation_vph` (an arm's flow over its ratio) is the saturation flow
  TRANSYT, Synchro and SCOOT get for it (`Simulation.saturation_vph`).
  - `Graph.saturation` (sizing demand): signals as above; roundabout entries and priority movements by
    capacities measured on the simulated drivers (`ROUNDABOUT_ENTRY_VPH`, `PRIORITY_CAPACITY_VPH`,
    `measured_capacity`: straight lines between the points, held beyond the last).
  - Webster: each junction alone, `webster(flow ratios)`.
  - Fixed time: the greens entered.
  - Actuated and max-pressure (`signal_timing.ACTUATED`, `_actuate`): each junction on its own, its greens decided
    step by step, starting from Webster's plan. Actuated: a loop on each lane `DETECTOR_GAP_S` (2 s) of travel
    before its stop line places a call for its phase (`ActuatedJunction.detect`). Max-pressure: each phase's
    pressure is the vehicles on its approaches less the mean of those standing (under `HELD_MPS`, 5 km/h) on the
    links they feed. `Signal.set_green` ends the running green now; `_position` takes the phase the controller
    names next (`next_phase`). The plan each settles on is reported to `signal_plans`.
  - TRANSYT, Synchro, SCATS, SCOOT (`_coordinate`): every junction with the method is one group on a
    common cycle, within the group's cycle limits (the highest minimum, the lowest maximum).
    `_approaches` gives the offline model each arriving link: its phase, saturation flow, design flow,
    cruise time from the upstream stop line, and what feeds it (the upstream junction's arriving links,
    by their movements' shares, when that junction is in the group; else traffic arriving evenly).
    TRANSYT and Synchro set every signal's greens and offset now. SCATS and SCOOT start from the
    critical junction's Webster cycle, equisaturation greens and progression offsets, and `_adapt`
    hands each signal to its controller with its loop detectors (`Loop`): SCATS one over the last
    4.5 m of every arriving lane, SCOOT one 2 m long, 15 m into every arriving lane (a third of it on
    short roads). Each step after the vehicles move, every loop is read (is a vehicle over it; how many
    drove off its end, counted on the lane they crossed it in even if they changed lane since) and
    handed to its junction: SCATS's with whether its arm showed green, SCOOT's as a count at that time.
    The controllers report their plans as they change (`signal_plans`, each replaced whole, so the
    frames already published keep theirs).
  - Run's status line reports the Webster cycles and each group's size and cycle.
  - Route choice (`Network.route_choice`, set by AI Mode's assign_routes; empty: off): `Graph.route_shares`
    finds up to `routes` alternatives per pair by link penalty (each found route's links cost `ROUTE_PENALTY`
    1.5 times more in the next search, `Graph._fastest` with a cost function) and shares them by path-size
    logit, P ∝ exp(−θ·T/60 + ln PS), T from `link_times_s` (measured) or free flow; each vehicle draws one
    (`pick_route`, no draw when off, so runs repeat as before), and `Graph.assign` (signal timing, demand
    sizing) loads every alternative by its share.
  - Routes: fastest links at the speed limits (Dijkstra), cached per pair; pairs with no route are
    reported and spawn nothing. A detour stands in for each missing offside turn, costed with its
    out-and-back links, which go into the route (and into the flows Webster times the signals by);
    U-turns are used only there. Vehicles still take a route with no detour when it's faster.
  - Landmarks (`Graph.landmarks`): each vehicle is given, with its route, the roundabouts, signalised
    junctions and slip roads (a `ramp` entered from a road that isn't one) it will meet, in order. Every
    frame says the next one still ahead and how far it is to where its lanes end there (stop or give-way
    line; a slip road: its diverge node), from each link's lane cut-backs (`Link.cut_start`, `cut_end`).
    How it drives at each comes from its turn there: the signal, giving way to the ring, the kerbside lane
    and the slip's curve speed.
- **Junctions** (algorithms/junction_behaviour.md): a vehicle within 150 m asks whether it may enter
  its turn (`_may_enter`): not on red, nor amber if it can stop comfortably (unless it has waited at
  the line for a gap it gives way to: then it goes on amber as the oncoming traffic stops); no
  vehicle on a crossing or merging turn short of the conflict point, counting the tail of one just
  driven out (never overridden); its give-way gaps: nobody it gives way to reaching the conflict
  within the critical gap (4.5 s at a signal, 5 s minor road; vehicles stopping for their red or
  amber don't count; a vehicle too close to stop goes); room on its exit lane, so it never blocks the
  junction (at an uncontrolled node, a merge or a bend, only while a vehicle is on or coming for a
  movement it would block: otherwise the queue runs on through it, `_foes_coming`): the lane's length less what its vehicles and those on their way to it take up, each with
  its gap (SUMO's brutto occupancy), so a queue there leaves room behind it and traffic moving off
  takes none. A vehicle that drove out of the conflicting turn counts while its tail is still in the
  junction, but not one ahead that came through this same turn (the queue discharging: a leader,
  not a foe, even where the two turns merge into one lane; `Vehicle.came_from`). At a roundabout it gives way to circulating traffic that would
  reach its entry within 4 s. Otherwise it stops at the line. Entry is checked again as it happens,
  since everyone planned from the same moment.
- **Leaders** (`_leader`): the vehicle ahead on its lane or turn; the tail of any vehicle that drove
  on from this lane but is still on it; the stop line if it may not enter; beyond it, its turn and
  exit lane; on a turn, a vehicle on another turn into the same lane that is nearer the merge; on a
  roundabout, the nearest vehicle ahead round the ring. Speed limits drop in good time for a slower
  turn ahead (v² = v_turn² + 2bd).
- **Lane changing** (`_change_lanes`; SUMO's LC2013 motives, strongest first, simplified;
  simulation/lane_changing.md), only where the road allows it, once the vehicle is clear of the
  junction it came through, and 3 s between changes:
  - Best lanes (`Simulation.reach`, SUMO's bestLanes): for each lane of its link, how far it leads
    along the route without a change, looking 3000 m ahead. A lane that can't take the next turn ends
    at its stop line; the others go on into the lanes their turns lead to. So before a straight-on
    that leads to a U-turn, the offside lane leads furthest.
  - Strategic: outside the lanes that lead furthest, it heads for them once its own would run out
    within 10 s of driving (at the lower of the limit and its top speed) per change still needed, the
    queue ahead in the lane it must reach counted off the room it has (LC2013's occupation of the best
    lanes), so it moves over before it comes up beside a standing queue.
    It changes as soon as the gap is safe (urgent), or a little earlier, when the next lane is no
    slower (drift).
  - Urgent and blocked, it gets speed advice for the next step: it drops in behind the leader in the
    way, braking no harder than is comfortable. The follower in the way is asked to let it in (LC2013's
    cooperation) when it could open the gap needed within the time the changer has before its lane
    stops leading on (that room, at the changer's speed), slowing at no more than its comfortable
    deceleration while the changer keeps on; it then keeps the changer ahead like a leader, braking no
    harder than comfortable, and is asked again each step until the change is made. Otherwise, if
    that follower is faster, the changer holds its speed and lets it pass.
  - Speed gain (past a slower leader within 60 m, into the lane with more room) and keep to the kerb
    (when that lane has 80 m clear) go only into lanes that can take its next turn and leave time for
    the strategic changes to come, so the motives never undo each other.
  - Every change needs a gap that is safe for it and for its new follower, including vehicles on
    turns about to join that lane.
- **Bus stops** (algorithms/public_transport.md; like SUMO's off-road `parking` stops):
  - `Bay`: built per Run (`Graph._add_bays`) beside the kerbside lane of each link a stop serves, from
    `entry_s` to `exit_s` along that lane (40 m about the stop's middle, found with `nearest_along`),
    its path offset out by half a lane plus half the bay along `bay_depth`. `stop_s` is where a bus's
    front stops (1 m short of the stopping length's end). A lane shorter than the bay gets none.
  - Which vehicles: types named Bus (`BUS_TYPES`, any case). Each is given, at spawn, every bay on its
    route in order (`Vehicle.stops`).
  - Getting there: `reach` treats the next stop's link as ending for every lane but the kerbside one at
    the bay's entry, so the strategic lane changes bring the bus to the kerb in good time; the speed
    limit drops to 20 km/h (`BAY_SPEED`) at the entry (v² = v_bay² + 2bd); while its bay is full it
    keeps behind the last bus in it.
  - Pulling in (`_pull_in`): reaching `entry_s` in the kerbside lane it leaves the lane for the bay.
    Past the entry in another lane, or off its link, it has missed the stop and drives on.
  - In the bay (`_bay_leader`): the bus ahead; the stop until it has stood there its stop's
    `dwell_s` (`Bay.dwell_s`), or with passengers (`Bay.boarding_per_h`, `alighting_share`; `_serve`) the
    dwell of HCM 2000 Eq 27-2: `DOOR_S` 3 s + alighting × `ALIGHT_S` 1.7 s + boarding × `BOARD_S` 2.6 s
    (+ `STANDING_BOARD_S` 0.5 s each once over `SEATED_SHARE` of its capacity is aboard). Passengers arrive at
    the stop by Poisson at its rate (`_passenger_rng`, its own stream); each aboard alights with the stop's
    share; boarders fit up to `CAPACITY_PER_M` 6.5 a metre of bus, the rest wait for the next. The bus's
    `Vehicle.load` is its people from then on (the signal view's `people`, rollouts); then the end of
    the bay, until the merge is clear, and past it the lane's traffic.
  - Merging back (`_merge_clear`, SUMO's insertion safety): a gap at `exit_s` safe for the bus behind
    the vehicle ahead and for the vehicle behind (`secure_gap` both ways, counting turns joining the
    lane and tails gone on into turns; `_neighbours`, shared with lane changing). Checked again as it
    happens; otherwise it waits at the end of the bay.
  - Traffic in the lane drives past a bus in its bay; only the tail of one still pulling in is in
    its way (`_bay_ahead`).
- **Demand** (algorithms/demand_generation.md, vehicle_insertion.md): per pair (`PairDemand`),
  flow in veh/h, or daily trips × that hour's share of the daily curve. Each minute the untimetabled
  flow (the flow less the timetabled vehicles) is counted by the pair's pattern (`arrivals`): Random
  (the flow's count exactly, e.g. 2 or 3 for 2.5), Poisson, Binomial (n = ⌈2m⌉ trials: more regular)
  or Negative binomial (a gamma-mixed Poisson, k = 2: bunched), at uniform times in the minute; each
  vehicle's type drawn from the pair's split. Timetabled types (a headway) depart every headway from a
  random first time, on every pair but an institution's (`PairDemand.timetabled` False: its riders take the
  network's buses). An origin or destination may be a junction (an institution's catchment and door), not only a
  boundary end. A spawned vehicle waits at its origin until a lane has room (kerbside for its
  first turn if it can), then enters at the lane's speed or its leader's.
- **Emergency vehicles** (`dispatch(origin, destination, every_s, after_s)`, Dispatch Emergency Vehicle): from any thread,
  checked at once (ValueError: no route; origin and destination may be boundary ends or junctions), queued and
  taken at the next step: one emergency vehicle on the fastest route `after_s` later (0: then), then again every
  `every_s` (0: once) until
  the run ends. Its type is the project's called Emergency, else `vehicle.EMERGENCY_MODE` (SUMO's emergency
  vClass, drawn red), marked `emergency`. Over the last `EMERGENCY_LANE_M` (150 m) before each signalised
  junction on its route it keeps to the **kerbside lane** (lane 0, whichever side traffic drives on), the Dynamic
  Emergency Vehicle Lane: a strategic lane choice, as a bus's for its bay (`reach` with a stop on that leg,
  `_lane_reach`), and it may turn from any lane (`reach`'s `any_lane`; `_may_enter` doesn't make it wait in the
  wrong lane). It **obeys red**, unless a lane of its arm is set apart green (DEVL's flashing green: every other
  movement is red then), whichever lane it is in (`_signal_for`). Otherwise it drives as any other vehicle (other
  traffic doesn't yield to it). `emergency_trips` keeps each one's id, from, to,
  `dispatched_s` and `arrived_s` (None until it arrives).
  - **Demand during a run** (`set_demand`, `_take_demand`): new demand can be handed over from any
    thread by one reference swap; the physics takes it at the start of its next step, so everything
    it changes stays on the physics thread, and a newer hand-off replaces an older one. It counts
    from the next minute's arrivals (those already drawn still come). Timetabled types keep their
    timetable on pairs that still have demand, start one on new pairs, and stop on pairs whose
    demand has gone. Pairs without a route are listed in `unreachable`. Signal plans stay as timed
    at Run.
- **Incidents and work zones** (`close(road, lanes, from_m, length_m, start_s, end_s, speed_kmh, direction)`,
  checked first): `Closure`s of lanes (kerbside first; all of them blocks the road) and/or a speed limit over a
  section of a link for a while, at least `CLOSURE_CLEAR_M` (30 m) along its lanes. `_update_closures` rebuilds
  what each lane holds when the set in force changes: `reach` treats a closed lane as ending there (as SUMO
  withdraws its permission, so its bestLanes avoid it), a vehicle still in it stops at the closure as at a
  standing obstacle (`_closure_ahead`), and the work zone's limit is in `_limit` (slowing so as to be at it
  when it starts). A fully blocked road is avoided by vehicles spawned while it is (`reach(closed=...)`).
- **Weather** (`set_weather(name)`, `WEATHER`): each vehicle spawned from then keeps a share of every speed
  limit and multiplies its reaction time τ (`_weathered`): light rain, light snow, heavy rain, heavy snow, from
  HCM 2000 Ch 22's free-flow speed and capacity losses, calibrated by measured saturation flow (the comment
  above `WEATHER`; test_simulation re-measures).
- **Ramp meters** (`meter(road, target_occupancy_percent, rate_vph, detector_road)`, `Meter`, `_run_meters`):
  a stop line `METER_SETBACK_M` (30 m) before an on-ramp's merge shows green `METER_GREEN_S` (2 s) once every
  3600/rate s; with a target occupancy, ALINEA moves the rate each `METER_PERIOD_S` (60 s) by `METER_K_R`
  (70) × (target − occupancy %) of the main road's loops `METER_DETECTOR_M` (60 m) past the merge (the link the
  ramp goes straight into, or `detector_road`), within `METER_RATES_VPH` (240–1,800).
- **Fork and rollout** (`fork()`, `rollout(actions, horizon_s, watch)`): a deep copy of the run now (the same
  vehicles, signals, random numbers and demand to come), copied on a thread with a `FORK_STACK_BYTES` (128 MB)
  stack, as the lane graph's references nest deeper than the default recursion limit; its override,
  observations and detectors aren't copied, and adaptive or actuated controllers are replaced by `_Held`
  (their current greens kept). `rollout` runs each action (a signal override, or None for the plan) on a fork
  for the horizon and gives its person-seconds and vehicle-seconds of delay (time short of free speed) and how
  far each watched vehicle got. The junction view carries `rollout` so an agent can look ahead (TSP).
- **Per step bookkeeping** (`_refresh`): each vehicle's place in its lane's list and its next turn
  are worked out once and reused (leaders, junctions, limits, gap acceptance); only the first vehicle
  in each lane asks the junction; lane-change gaps are found by binary search on the position-ordered
  lists. About 5 ms a step for 1,100 vehicles on the default grid: about 100× real time.
- **Signal override** (`signal_override`, `_time_step_signals`): after each step the next step's
  signal states are worked out once (what the map shows is what vehicles obey): each junction's plan
  and then, only if `signal_override` is set (the TSP extension ticked), the override for each arm.
  It is given `_junction_view`, made once per junction per step (each approaching vehicle with its `people`,
  by its type's occupancy, and the `exit` lane it will take; `exit_room_m`, each such lane's room as
  `_may_enter` reckons it): the time and plan; the plan's phases
  (arms, green), amber, all-red and minimum green; each arm's vehicles nearest the stop line first
  (type, distance, speed, lane), speed limit and length; the vehicles crossing the junction; and
  `box_m`, the distance across it (twice the stop line inset, cached); the cycle (`cycle_s`,
  `cycle_start_s`, `offset_s`; an adaptive plan is moved on to now first); each arm's `lanes`, and each approaching vehicle's `emergency`; plus the arm's own `planned` state and
  `approaching` vehicles. It returns the arm's state, or (that, {lane index: the lane's own state}) to set lanes
  apart (DEVL clearing the kerbside lane): `_lane_state` is what a lane's vehicles obey (`_signal_for`: an emergency vehicle also goes on its arm's lane green) (`_may_enter`, and gap
  acceptance against a lane's foes), `lane_states()` lists them for the map and tests. With it unset nothing else
  runs: the plans are untouched.
- **Signal agent** (signal_agent.py, no Qt): `SignalAgent.decide(key, question)` returns the latest
  TRUE/FALSE for the key at once (FALSE until one arrives) and asks again, in a background worker, only
  when the question changes, so a live run's physics never waits for a model. A headless run's agent is
  made with `wait_s`, a deadline: decide() waits up to it, and a question not answered by then falls back
  to FALSE for good (`fell_back`; the late answer is kept in its record, unused). Questions wait in a queue
  that one worker answers: `decide(key, question, urgency, once)` takes the lowest urgency first (TSP: when
  the bus reaches its stop line), a question still waiting is replaced by a newer one for its key (the
  freshest telemetry), `once` keys are asked at most once, `withdraw(key)` drops one nobody needs, and
  `deadline_s` (simulated s) is how long an answer may take to count, checked by its caller (TSP's agent). Every answer is logged
  with its latency, error and what `ask` reported (`details(key)`, `answer_of(key)`, `log`). The window's
  `ask` (`MainWindow._ask_agent(question, choice)`): TSP's questions ({"system": "1"}: a state and typed
  questions, to `assistant.system_one`, TRUE at P(yes) ≥ 0.5; {"system": "2"}: messages, to
  `assistant.chat`, `parse` reading TRUE) go to the model a batch run names (`choice`) or AI Mode's own
  (System 1's Use; System 2's started model), and come back with the model, tokens and P(yes) or reply;
  plain text (another extension's yes/no) goes to System 1 when one is chosen (`system_one_true`), else
  System 2's started model, told to reply TRUE or FALSE. With nobody to ask, or on any error, the answer
  is FALSE. Each Run starts with no answers (`reset`). The live agent is handed to every `signal_override`;
  headless tests and batch runs hand theirs (`MainWindow._waiting_agent`, deadline `HEADLESS_WAIT_S` 30 s or
  the batch's).
- **Frames** (`frame()` → `Frame`): the run frozen at one moment: every vehicle's `VehicleState`
  (id, type, colour, size, position, heading, the pose a step earlier, speed, road, lane, turn or bay,
  metres along it, origin, destination, and the landmark it is heading to: kind, id, metres), the
  signal states and plans, and counts. It never changes
  once made, so any thread may read it. `Frame.records(road, limit)` gives it as plain data (the
  assistant's `read_traffic`).
- **`Runner(simulation, speed, recorder)`**: a `results.RunRecorder` is sampled on the physics thread every
  `sample_s` simulated seconds (after the step that reaches it), so a busy screen never costs a sample; one
  that raises is dropped with its `error` and the traffic carries on.
- **`Runner`**: runs a Simulation on its own thread, in step with the wall clock × speed: every step
  is computed, in order, whether or not anything is drawn, and the physics never waits for the view.
  It publishes a fresh frame at most 60 times a second (and the last step when paused); `pause()`
  returns once the physics is idle with its last step published. `target()` is where simulated time
  should be; `speed_now` is the speed really achieved (only below the chosen speed if the CPU can't
  keep up at all).
- `advance(target, budget)` steps synchronously (tests, tools); `signal_states()` for the map;
  counts: `spawned`, `spawned_by_mode`, `arrived`, `pending`, `unreachable`.
- `observe()` (headless tests: `experiments.run_headless(observe=True)`) turns on per-step recording in
  `_observe()`, after each step: each vehicle passing the middle of its lane (a detector: road, link, lane,
  time interpolated within the step, speed, length, type), each link's traversal time (entering its lanes
  to entering the next link's, the junction included), rear-end conflicts (time to collision with the
  vehicle ahead in its lane under `CONFLICT_TTC_S` 3 s: one event from dropping below to recovering, with
  its least TTC and greatest DRAC, Δv²/2·gap), buses arriving in a stop's bay (with their line, the OD
  pair), and crossings of a signalised stop line (when that green began, whether the vehicle had stopped on
  the approach, its type and turn); also every link traversal (`traversal_log`), arrivals at signals and
  their green onsets, fuel (emissions.py), emergency vehicles' standstills and bus passengers' boardings
  (analysis.md lists them). `observations()` hands it out as plain data with each link's road, ends, lanes,
  length, free-flow time and `cross_s` (the free time through the junction at its end), plus the signals'
  plans, the controls, the weather, meters and closures.

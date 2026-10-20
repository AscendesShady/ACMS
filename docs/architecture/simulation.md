# simulation.py: the traffic simulation (no Qt)

Structured like SUMO's step (references/reverse_engineering/simulation/simulation_engine.md): every step, `dt`
seconds (`DT` = 0.5 s; a headless run may ask for 0.25 or 0.1 s, `Simulation(step_s=…)` from `STEP_LENGTHS_S`, to
check its results don't depend on the step; the clock is kept exact), it plans every vehicle's speed from the state
at the start of the step, moves them, lets them change lanes, then inserts new vehicles; poses are then updated for
drawing. `step_method` (`STEP_METHODS`) is how a move follows the speeds: `"euler"`, SUMO's default and the app's,
s += v′·Δt at the new speed; `"ballistic"` (opt-in, SUMO's `--step-method.ballistic`), s += (v + v′)/2·Δt, and a
vehicle braking to a stop within the step (a next speed below zero, vehicle.md) goes only as far as it takes to
stop, v²·Δt/2(v − v′), with Krauss's safe speed in its ballistic form. Each step a vehicle brakes harder than its
comfortable `decel` is counted in `emergency_braking` (and, observed, recorded: when, which, its type, the road or
junction, how hard); Krauss never brakes harder than its `emergency_decel`, but a vehicle refused at the line by
`_move`'s re-check (a foe entered a crossing turn earlier in the same step) still stops there at once, rare and
recorded with its real deceleration. A vehicle that has stood `TIME_TO_TELEPORT_S` (300 s, SUMO's
`--time-to-teleport` default) at the front of its lane is taken off the road and queued to enter its route's next
link (`_teleport_jammed`, after algorithms/vehicle_removal.md's transfer), so a gridlock's cycle breaks instead of
freezing the run (not one held by a closure on its lane: that block is meant); counted in `teleports`, observed with where; `time_to_teleport_s` 0 turns it off (`critical_gap_s` and `ring_timegap_s`, the gap acceptance, are per run too, from
`CRITICAL_GAP_S` and `RING_TIMEGAP_S`, for `experiments.parameter_sensitivity`); its trip keeps
its first `inserted_s`. After the moves, collisions are counted in `collisions`, SUMO's collision
action "warn" (algorithms/collision_handling.md: counted, nothing removed or teleported), by kind in
`collision_kinds`: `"same lane"`, a front past the rear ahead in its lane or turn (vehicle-steps), and
`"crossing"`, two vehicles on crossing turns of a junction (`Turn.conflicts`) both over their conflict point at
once, each front past it by less than its length (a tail still in a turn it has left counts; only turns with a
vehicle on them are looked at; a merge is the same lane's check once both are on it; roundabout rings have no
`conflicts` and aren't checked). Krauss with τ ≥ `DT` (the Mode Behavior range) leaves none. In a lateral run
(below) the same-lane check is `_side_by_side_collisions`: a pair counts only if it overlaps sideways too.

- **Lane graph** (`Graph`, built once per Run; lane_graph.py, which simulation.py imports, see lane_graph.md):
  - `Link`: one road in one travel direction. Its `Lane`s (0 = kerbside) are the road's centre line
    offset to the driving side, cut back at each junction to the stop line (signalised, the same
    `stop_line_inset` as the map), the give-way line (roundabout, ICD/2 + 0.5 m) or the junction's
    edge (uncontrolled); boundary ends aren't cut. Short roads share what length there is. Its speed
    is the road's limit capped at `curve_speed_kmh` of the road's tightest stored bend on its innermost
    lane (κ / (1 − κ·offset)), however high the limit is edited.
  - Turn pockets (`Road.pockets`; concept 03 of the theory reference, SUMO's short lanes): a pocket's lanes
    are added to the arriving link, kerbside of lane 0 (a kerb pocket: they become lanes 0…, the others move up)
    or offside of the last lane (an offside pocket; the through lanes move over to the widened kerb side along
    the 20 m taper before it). `Link.pockets` is (kerbside, offside) pocket lanes. A pocket's `Lane` runs the
    whole link, so a position on it matches the others' (lane changes map position by length, as between any
    two lanes), but `Lane.start` is where the pocket opens: nothing is inserted on it and no lane change into
    it is made short of it. Leaving links' turns lead into whole lanes only. a path through a junction from an arriving lane to a leaving lane. Classed kerb-side,
  - Cycle lanes (`Road.cycle_lanes`; SUMO's lane permissions, a lane allowing bicycles only,
    `references/reverse_engineering/network/edges.md`): `Link.cycle`, a `Lane` of index -1 along
    `Network.cycle_lane_line`, cut back as the others, and `Link.bike_turns`, (lane index, next link) -> `Turn` for
    each move into or out of one: out of a cycle lane every way its road's traffic may go (permitted), into the next
    road's cycle lane, else its kerbside whole lane; into a cycle lane from each lane of a road without one, permitted
    as that lane's own turn. Neither is among `Link.lanes` or `Link.turns`, so nothing that picks, scores or changes a
    car's lane offers them; `Link.all_turns()` (both) is what the conflicts (`_add_conflicts`), `_turns_into`, the
    ring checks and the crossing report scan, so cars and bikes give way to each other at junctions as any two
    movements do. A bicycle-class type's vehicle (`Vehicle.cycles`) enters a road with a cycle lane in it
    (`_insert`), takes `bike_turns` first (`_next_turn`), and never changes lane or moves sideways in it
    (`_change_lanes`, `_sidestep`: one file, down its middle). Cars and bikes don't meet between junctions; a bus
    pulling into a bay crosses the cycle lane unseen, and loop detectors and signal flows leave cycle lanes out.
  - Pedestrians (walking.md; SUMO's NonInteracting model): `Simulation.walk_demand` (`walkers=` given, else
    `walking.walk_pairs` of its network, daily if any pair is) on their own "walkers" and "walk speeds" streams and
    heap (`walk_departures`), so a run's vehicles are exactly as without them. `_walk` each step (after the
    vehicles' poses): each minute's departures, Poisson, as `_count_arrivals` draws a pair's; those due set off on
    `WalkGraph.route` (with its `start` / `finish` place), a pair with none noted once in `walk_unreachable`; every
    `Walker` moved on at its own speed (`WALK_KMH` × a speed factor, `WALK_SPEED_DEV`), gone at its walk's end
    (`walk_spawned`, `walk_arrived`). `Frame.walkers`: each one's (x, y, ux, uy, pose a step before).
    Crossings (walking.md): a walker reaching the kerb of the next road on its walk (`Walker.crossings`) steps off
    only when `_may_cross`: an overbridge always (then slower, `Crossed.slow`); a zebra unless a vehicle is on it or
    couldn't stop for it (`brake_gap` at its comfortable deceleration after its reaction time and a step, less the
    stripes' half width `ZEBRA_HALF_M` 1.5 m); a gap when every vehicle coming is at least tc = L / Sp + ts away
    (HCM 2000 Eq 18-17, the walker's own speed, ts `START_UP_S` 3 s). Vehicles both ways on the road's lanes, cycle
    lanes and the turns into them count; one standing short of the crossing doesn't. Waiting, it stands at the kerb
    (`waited_s`). A zebra with anyone on it (`_on_zebra`) is a stop line for both directions `ZEBRA_HALF_M` before it
    (`_zebra_stops`, in `_lane_leader` as a ramp meter's red). Vehicles don't see walkers at gaps (they don't yield;
    no collision is checked between walkers and vehicles).
    For the results: each finished walk kept `EXITS_KEPT_S` in `Frame.walk_exits` (walker id, origin, destination,
    set off s, arrived s, metres, waited s), and `crossing_stats` in `Frame.crossings`: for each zebra or overbridge
    by id, and each junction arm crossed in gaps as "gap <road> at <junction>", its kind, walkers over it, the seconds
    they waited at its kerb (`Walker.kerb_s`) and, a zebra, the vehicle-seconds each lane's first vehicle short of it
    stood at its stop line while someone was on it. `Frame.records` (read_traffic) gives `pedestrians_walking`.
    straight, offside (across oncoming traffic) or U-turn (back along its road) by the angle between
    them (`_kind`); a ramp leaving or joining a road within 35° (a slip road's tangential taper) is a kerb
    turn when its first or last 10 m bend to the kerb side, otherwise straight. A cubic Bézier, with its speed capped by low-speed turning friction (`JUNCTION_TURN_FRICTION` 0.35, AASHTO; √(127·f·R)), not below `JUNCTION_TURN_MIN_KMH` (kerb 30, offside 35, U-turn and straight 25 km/h: the path is schematic, tighter than a car steers, so the floors stand in for a real curb radius and bring turning flows to the HCM's 85-95 % of straight-on), and the turning saturation flows were measured at that; a give-way movement at an uncontrolled junction at most `GIVE_WAY_KMH` 17, drivers slowing to look (a U-turn
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
    forbids lane changes every lane may take every turn. On a link with a pocket, its lanes take its side's
    turns (two pocket lanes turn side by side into the first two lanes), the other lanes go straight on (lane
    0 still turns kerb-side without a kerb pocket; the offside lane offside without an offside one), and
    straight-on traffic keeps its lane counted from the first whole lane. Other turns exist but are only taken after
    standing 8 s at the line in the wrong lane while free to go (impatience; waiting at a red doesn't
    count), so nothing gridlocks.
  - Ramps (`Graph._ramp_lanes`: lane-to-lane connections as SUMO's, laid out as the HCM 2000 Ch 24 draws ramps): where a ramp joins or leaves a
    road along it on the kerb side at a junction without signals, the joining ramp lands on the kerbside lanes and the
    road on the offside ones (a lane added between them is the auxiliary lane); leaving, the ramp is taken from the
    kerbside lanes and the road on from the offside ones, at most one lane shared (Green Book §10.9.5.9's lane
    balance). Weaving vehicles then change lanes by the strategic rules below (`reach`): on a Type A weave at 2,500
    veh/h the link ran 75 km/h at 600 m, 60 at 300, 22 at 150 (HCM 84, 75, 65; the old mapping, where nothing had to
    change lanes, ran 70-86 at any length and teleported 6-7 vehicles in 25 min). At such a junction nothing crosses the
    road's other carriageway (no offside turns: a diamond's on-ramp had joined the opposite carriageway across the
    freeway) nor turns back on a road of `RAMP_UTURN_MAX_KMH` (80 km/h) or more: a freeway has no median opening, while
    an arterial's slip-road junction keeps the median U-turn a two-phase signal's detour needs.
  - Conflicts: where two turns' paths cross (`segment_hit`) or merge into one lane, with who gives
    way: a U-turn to everything else (on the same green); an offside turn to oncoming traffic on the
    same green; a minor arm of an uncontrolled junction to the major road (`_major_arms`: the two arms of the
    highest road class, `ROAD_PRIORITY` as SUMO's netconvert ranks OSM types, the most nearly in line among them;
    a grid's roads are one class, so in line), and a minor offside turn also to the other minor arm's straight and
    kerb movements (HCM 2000's ranks: rank 4 gives way to rank 3; first-come before). An imported trunk no longer gives way to a residential lane that
    happens to line up better: on Dhanmondi that had gridlocked the network.
  - `Signal`: a signalised junction's phases in turn, each green followed by its amber and all-red
    (`signal_timing.clearance` from its fastest approach and its width: 3.3 s and 2.0 s on the default grid). Two phases: east-west arms, then north-south; four: each arm alone, in turn round the
    junction. Fixed-time: the same greens every cycle, the first phase's green starting `offset` s
    into each (`state(link, t)` from `(t − offset) mod cycle`; `cycle_start(t)`). Adaptive (`adapt`):
    it steps from phase to phase, telling its `controller` (a SCATS or SCOOT junction) as each cycle
    and each phase starts, which may change the greens (and so the cycle) from then on.
- **Signal timing** (`_time_signals`, before the run; `signal_plans`; the methods in signal_timing.py).
  `Graph.assign` routes every pair's veh/h and `Graph.saturation` gives a junction's degree of saturation
  by its control (see AI Mode's generate_demand) and `Graph.storage_ratio` its worst queue over the room behind it
  (a signal's red queue per arm and the HCM's 95th-percentile queue, Eq 17-37, for give-way movements, over
  `STORAGE_M_PER_VEHICLE` 7.5 m a vehicle); `design_flows` routes every pair's flow along its fastest path and adds up the veh/h arriving on
  each arm and making each movement (link to next link; daily demand: at the network's busiest hour).
  Each phase's critical flow ratio y is its busiest arm's (`Graph.arm_ratio`), lane by lane
  (`_lane_load`): kerb turns load the kerbside lane at 1,770 veh/h, offside turns the offside lane at 1,860 and
  U-turns it at 1,550 (still turned at the 25 km/h floor), straight-on traffic (2,060 veh/h per lane) fills whichever lanes have room;
  where any lane may turn, all share. A turn pocket's lanes share its side's turns and nothing else, and
  straight-on traffic fills the other lanes; the busiest of all is the ratio. All three are calibrated to the simulated drivers (below); turning
  drivers slow for the curve. `saturation_vph` (an arm's flow over its ratio) is the saturation flow
  TRANSYT, Synchro and SCOOT get for it (`Simulation.saturation_vph`).
  - `Graph.saturation` (sizing demand): signals as above; roundabout entries and priority movements by
    capacities measured on the simulated drivers (`ROUNDABOUT_ENTRY_VPH`, `PRIORITY_CAPACITY_VPH`,
    `measured_capacity`: straight lines between the points, held beyond the last; a priority movement giving way to
    none, the major road, at its lane's saturation flow); the priority table, re-measured with drivers slowing to
    look, is within about 10 % of HCM 2000 Eq 17-3 for a minor right turn, and a 2 x 2 grid of give-way junctions
    sized to X 0.85 and 1.0 holds (it was about 2.3 times optimistic); a roundabout entry's
    circulating flow is the movements whose ring passes it (`Graph.circulating`). The roundabout table
    follows the HCM 6 entry capacity within 10 % up to about 800 veh/h circulating; beyond, it falls faster
    (−28 % at 1,000): the ring's traffic bunches (2.25–2.5 s headways) where the HCM assumes a random stream, as
    Tanner's formula predicts (`roundabouts.tanner_entry_capacity`; not_built.md).
  - Webster: each junction alone, `webster(flow ratios)`.
  - Fixed time: the greens entered.
  - Actuated and max-pressure (`signal_timing.ACTUATED`, `_actuate`): each junction on its own, its greens decided
    step by step, starting from Webster's plan. Actuated: a loop on each lane `DETECTOR_GAP_S` (2 s) of travel
    before its stop line places a call for its phase (`ActuatedJunction.detect`). Max-pressure: each phase's
    pressure is the vehicles within `PRESSURE_ZONE_M` (100 m) of its stop lines less the mean of those standing (under `HELD_MPS`, 5 km/h) on the
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
    logit, P ∝ exp(−θ·T/60 + ln PS), T the route's `path_time`; each vehicle draws one
    (`pick_route`, no draw when off, so runs repeat as before), and `Graph.assign` (signal timing, demand
    sizing) loads every alternative by its share.
  - Route costs count junction delay once, on the move that makes it (`Graph.move_time(link, next link)`:
    driving the link and making that move at its end; the destination link has none): the move's measured
    time (`turn_times_s` {link: {next link: s}}, entering the link to entering the next, that move's own wait
    in it), else the link's measured time over all its moves (`link_times_s`, an average junction delay
    already in it), else free flow plus a control-delay prior; never two of them. The prior
    (`Graph.control_delay`, `free_move_time`) is each move's expected control delay with no traffic, at the
    greens entered on the junction: a signal r²/2C (HCM 2000 Eq 16-11 at X → 0, r = C − its phase's effective green g,
    shown green + amber used − start-up lost: 11.3 s at the default 30 s greens); a move that gives way to another arm's traffic (a minor arm, an
    offside turn across the major road, a U-turn, a merge; not a diverge from its own road) or a roundabout
    entry 3600/c + 5 s (HCM 2000 Eq 17-38 at no conflicting flow; c the measured capacity at none,
    `PRIORITY_CAPACITY_VPH` / `ROUNDABOUT_ENTRY_VPH`: 7.8 s, 7.5 s). A turn's own length isn't counted (a wide
    offside curve would look cheaper than a tight kerb one). So an offside turn at a priority junction costs
    more than a kerb one, and a slip road is taken for the signal it bypasses.
  - Routes: least `free_move_time` (Dijkstra over moves), cached per pair; pairs with no route are
    reported and spawn nothing. A detour stands in for each missing offside turn, costed with its
    out-and-back links, which go into the route (and into the flows Webster times the signals by);
    U-turns are used only there. Vehicles still take a route with no detour when it's faster.
  - Drop-offs (`PairDemand.drops`, demand.md): `_dropped` draws, on its own "drops" stream (so the other draws, and
    runs without drop-offs, stay as they were), whether a new vehicle's trip ends part way: each of its type's points
    takes its % of the trips, at that share of the length of whichever route it drew. Its route is cut at the link
    holding the point and `Vehicle.drop_s` set to how far along that link's lanes (None: the point is at the link's
    end, as at a destination); `_move` takes it off there (`_arrive`: counted in `arrived`, its trip times kept), at
    speed, as at a destination.
  - Landmarks (`Graph.landmarks`): each vehicle is given, with its route, the roundabouts, signalised
    junctions and slip roads (a `ramp` entered from a road that isn't one) it will meet, in order. Every
    frame says the next one still ahead and how far it is to where its lanes end there (stop or give-way
    line; a slip road: its diverge node), from each link's lane cut-backs (`Link.cut_start`, `cut_end`).
    How it drives at each comes from its turn there: the signal, giving way to the ring, the kerbside lane
    and the slip's curve speed.
- **Junctions** (algorithms/junction_behaviour.md): a vehicle within 150 m asks whether it may enter
  its turn (`_may_enter`): not on red, nor amber if it can stop in time braking at max(min(3.0, emergency
  decel), decel), SUMO's rule with no reaction distance (`AMBER_STOP_DECEL`, tls.yellow.min-decel; MSVehicle.cpp:2792):
  drivers then use 2.4 s of the amber, nearer the HCM's e = 2.0 s than the 2.8 s a reaction distance left — unless it has waited at
  the line for a gap it gives way to: then it goes on amber as the oncoming traffic stops; no
  vehicle on a crossing or merging turn short of the conflict point, counting the tail of one just
  driven out (never overridden); its give-way gaps: nobody it gives way to reaching the conflict
  within the critical gap (4.5 s at a signal, 5 s minor road; vehicles stopping for their red or
  amber don't count; a vehicle too close to stop goes); room on its exit lane, so it never blocks the
  junction (at an uncontrolled node, a merge or a bend, only while a vehicle is on or coming for a
  movement it would block: otherwise the queue runs on through it, `_foes_coming`): the lane's length less what its vehicles and those on their way to it take up, each with
  its gap (SUMO's brutto occupancy), so a queue there leaves room behind it and traffic moving off
  takes none. A vehicle that drove out of the conflicting turn counts while its tail is still in the
  junction, but not one ahead that came through this same turn (the queue discharging: a leader,
  not a foe, even where the two turns merge into one lane; `Vehicle.came_from`). At a roundabout (`_ring_clear`) it never enters beside a vehicle there or where one would be when it
  joins the ring (its entry curve driven at the ring's speed, and, yet to commit, also at its own faster
  speed), and gives way unless the circulating vehicle would then be `RING_TIMEGAP_S` (0.5 s at its speed)
  clear of it, ahead or behind: SUMO's occupation-interval test with a time gap, about a 2.8 s critical gap
  for a driver waiting at the line at the ring's 25 km/h. Entrants already let in from other lanes count
  as about to join at their own entries, so two never go side by side; yet to commit, it also keeps that
  clear of one joining at an entry further round its way, as that one reckons it, so a committed entrant
  never has to stop for it. One too close to stop comfortably is committed: only a vehicle beside it holds
  it back. Otherwise it stops at the line. Entry is checked again
  as it happens, since everyone planned from the same moment (at a roundabout, as committed).
- **Leaders** (`_leader`): the vehicle ahead on its lane or turn; the tail of any vehicle that drove
  on from this lane but is still on it; the stop line if it may not enter; beyond it, its turn and
  exit lane; on a turn, a vehicle on another turn into the same lane that is nearer the merge; on a
  roundabout, the nearest vehicle ahead round the ring. Speed limits drop in good time for a slower
  turn ahead (v² = v_turn² + 2bd).
- **Lateral position** (opt-in: `Simulation(lateral=True)`, `run_headless(lateral=)`, a scenario's or the
  assistant's `lateral`; off by default, and off every run is bit for bit what it was). SUMO's sublane model SL2015
  at its smallest (Semrau & Erdmann 2016; simulation/lane_changing.md), for weak lane discipline (Dhaka-type mixed
  traffic). Each vehicle has `lat_m`, its centre's offset from its lane's or turn's centre line towards (-uy, ux),
  kept across turns and lane changes; it enters at its lane's kerb side (SUMO's latAlignment "right" in right-hand
  traffic), and only a type that filters (`VehicleMode.filters`, narrow enough: `vehicle.narrow_enough`) ever moves
  sideways. Two vehicles are clear of each other when their sides are `MIN_GAP_LAT_M` (0.6 m, SUMO's minGapLat)
  apart (`_clear`). A vehicle's leader on its lane or turn (`_lateral_ahead`) is, of those ahead it isn't clear of,
  the one whose rear is nearest; others in the lane are no leaders, so a car still follows a motorcycle it overlaps
  and drives past one at the far side. A filtering vehicle passes those clear of it at most `FILTER_SPEED` (30 km/h,
  NSW's lane-filtering limit) while beside them, slowing for it in good time, so it never passes one faster (no lane
  splitting at speed). `_sidestep`, after lane changing: on a lane, it moves towards whichever of the lane's two
  sides or its middle leaves the most room ahead (`_room_at`), if more than it has by its own length, no faster than
  `MAX_SPEED_LAT` (1 m/s, SUMO's maxSpeedLat) and only where it has a lane change's secure gaps to what it would no
  longer be clear of: so motorcycles ride past a red queue to the stop line and leave first. Also lateral-aware:
  the next turn's and lane's rear vehicle, a tail left on the lane (`_tail_ahead`), insertion (room behind the
  rearmost rear), a lane change's leader and follower (`_neighbours`), observed conflicts. Not lateral-aware
  (conservative, the whole lane): merging turns, the ring, junction entry room. No vehicle straddles two lanes. The
  crossing check needs nothing: side by side in a lane they take the same turn or turns from one lane, which have no
  conflict point. Frames carry `lat_m`; x, y include it (drawing needs nothing more).
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
  - Into a turn pocket only past where it opens (`Lane.start`); short of it the change waits, but the leader
    check comes first, so a turner whose pocket is full back to its start gets the speed advice above and
    stops behind the pocket's last vehicle in the through lane, holding up the traffic behind it until the
    pocket moves (turn-pocket overflow, concept 15). A pocket's lanes lead on only for their turns, so no
    other vehicle drifts or keeps to the kerb into one.
  - Speed gain (past a slower leader within 60 m, into the lane with more room) and keep to the kerb
    (when that lane has 80 m clear) go only into lanes that can take its next turn and leave time for
    the strategic changes to come, so the motives never undo each other.
  - Every change needs a gap that is safe for it and for its new follower, including vehicles on
    turns about to join that lane.
- **Bus stops** (algorithms/public_transport.md; like SUMO's off-road `parking` stops):
  - `Bay`: built per Run (`Graph._add_bays`) beside the kerbside lane of each link a stop serves, from
    `entry_s` to `exit_s` along that lane (40 m about the stop's middle, found with `nearest_along`),
    its path offset out by half a lane plus half the bay along `bay_depth`. `stop_s` is where a bus's
    front stops (1 m short of the stopping length's end). A lane shorter than the bay gets none. The
    kerbside lane is the first whole one (past a kerb pocket's lanes, which validation keeps clear of bays).
  - Which vehicles: types whose `vehicle_class` (vehicle.VehicleMode) is a bus (`BUS_TYPES`),
    whatever they are called; a type without one (an older project) named Bus is one. Each is given, at
    spawn, every bay on its route in order (`Vehicle.stops`).
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
    the stop by Poisson at its rate (`_passenger_rng`, its own stream); each aboard (at its first stop, its
    type's occupancy, the people the signal view gave it until then) alights with the stop's
    share; boarders fit up to `CAPACITY_PER_M` 6.5 a metre of bus, the rest wait for the next. The bus's
    `Vehicle.load` (aboard − alighting + boarding) is its people from then on (the signal view's `people`,
    rollouts, the Frame's `VehicleState.people`); then the end of
    the bay, until the merge is clear, and past it the lane's traffic.
  - Merging back (`_merge_clear`, SUMO's insertion safety): a gap at `exit_s` safe for the bus behind
    the vehicle ahead and for the vehicle behind (`secure_gap` both ways, counting turns joining the
    lane and tails gone on into turns; `_neighbours`, shared with lane changing). Checked again as it
    happens; otherwise it waits at the end of the bay.
  - Traffic in the lane drives past a bus in its bay; only the tail of one still pulling in is in
    its way (`_bay_ahead`).
- **Demand** (algorithms/demand_generation.md, vehicle_insertion.md): per pair (`PairDemand`),
  flow in veh/h, or daily trips × that hour's share of the daily curve. Hourly demand may have 15-minute
  shares of the hour (`PairDemand.quarters`, from a peak-hour factor or four counts: daily_profile.md): its
  rate in each 15 minutes is veh/h × 4 × that quarter's share (`Simulation.rate_now`; none on a daily curve;
  signal timing and demand sizing keep the hour's mean, `rate`). Each minute the untimetabled
  flow (that rate less the timetabled vehicles) is counted by the pair's pattern (`arrivals`): Random
  (the flow's count exactly, e.g. 2 or 3 for 2.5), Poisson, Binomial (n = ⌈2m⌉ trials: more regular)
  or Negative binomial (a gamma-mixed Poisson, k = 2: bunched), at uniform times in the minute; each
  vehicle's type drawn from the pair's split. Timetabled types (a headway) depart every headway from a
  random first time, on every pair but an institution's (`PairDemand.timetabled` False: its riders take the
  network's buses). An origin or destination may be a junction (an institution's catchment and door), not only a
  boundary end. A spawned vehicle waits at its origin until a lane has room (kerbside for its
  first turn if it can; never a turn pocket's lane), then enters as fast as is safe (SUMO's departSpeed "max", no dawdling): its free
  speed, no faster than `_limit` and the Krauss safe speed behind what `_plan` would see ahead
  (`_lane_leader`: the vehicle ahead with its secure gap, a red or a give-way, a bay, a closure, a meter).
  It keeps its requested departure (`Vehicle.depart_s`: its departure's scheduled time) and when it got on
  the road (`inserted_s`, the end of that step); on arriving, its id and the two with the end of that step
  go to `_exits` (`_exited`), kept `EXITS_KEPT_S` 60 s for the frames, so results.py has every trip's
  exact times.
- **Random streams** (`_rng(purpose, pair)`): one `random.Random` a purpose, seeded from the run's seed and
  the purpose alone (`"arrivals 1W>1E 42"`): per pair its arrival counts and times (`arrivals`), its
  vehicle types (`types`), its timetable's first offsets (`timetable`), its route draws (`routes`) and its
  drivers' speed factors (`drivers`); emergency vehicles' drivers (`drivers emergency`); every vehicle's
  dawdle each step (`dawdling`); bus stop passengers (`_passenger_rng`). So changing a road or adding a pair
  leaves every other pair's departures, types and drivers exactly as they were: paired runs on the same
  seed keep common random numbers (Law & Kelton).
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
  It is given `_junction_view`, made once per junction per step (each approaching vehicle with its `class`
  (its type's `vehicle_class`), its `people` (a bus's simulated load, else its type's occupancy), and the
  `exit` lane it will take; `exit_room_m`, each such lane's room as
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
  that one worker a run answers (`reset` starts a new run's worker, and what is in flight is keyed by run, so
  the last run's slow answer never holds up or blocks the new run's question for the same key):
  `decide(key, question, urgency, once)` takes the lowest urgency first (TSP: when
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
  metres along it, origin, destination, the landmark it is heading to: kind, id, metres, its own
  `free_speed` where it is (`vehicle.free_speed` of the lane's limit or the turn's speed) and the `people`
  on board: a bus's simulated load, else its type's occupancy; results.py measures by them; `lat_m`, 0 unless
  lateral; display-only `vehicle_class`, `random_colour` and `emergency` metadata, with backward-compatible
  defaults), the
  signal states and plans, counts, and `exits`: the trips finished in the last `EXITS_KEPT_S` (id,
  requested departure, on the road, off it). It never changes
  once made, so any thread may read it: its plans are its own copy (`detached`), so neither the run's
  adaptive signals nor a reader changes another's, and its default plans are read-only. `Frame.records(road,
  limit)` gives it as plain data, its plans copied again (the assistant's `read_traffic`; a vehicle off its lane's
  centre line also has `lateral_m`).
- **`Runner(simulation, speed, recorder)`**: a `results.RunRecorder` is sampled on the physics thread every
  `sample_s` simulated seconds (after the step that reaches it), so a busy screen never costs a sample; one
  that raises is dropped with its `error` and the traffic carries on.
- **`Runner`**: runs a Simulation on its own thread, in step with the wall clock × speed: every step
  is computed, in order, whether or not anything is drawn, and the physics never waits for the view.
  It publishes a fresh frame at most 60 times a second (and the last step when paused); `pause()`
  returns once the physics is idle with its last step published. `target()` is where simulated time
  should be, from `_pace` (simulated s, wall s, speed): one tuple `set_speed` swaps whole on the UI thread,
  so the physics thread never reads a pace half changed (a jump ahead, then a freeze); `speed_now` is the speed really achieved (only below the chosen speed if the CPU can't
  keep up at all). A step (or frame) that raises ends the physics with its `error` set (the time and the
  exception; the traceback on stderr): `running` turns False, `play` is refused and `pause`/`stop` return at
  once; the window sees it at its next display frame and ends the run (`MainWindow._end_run`).
  `stop(wait_s)` says whether the physics has ended: False while a step is still going (a slow hook), which
  then ends the run as it returns, no step after it.
- `advance(target, budget)` steps synchronously (tests, tools); `signal_states()` for the map;
  counts: `spawned`, `spawned_by_mode`, `arrived`, `pending`, `unreachable`.
- `link_entries` counts every vehicle entering each link (inserted, or off a turn), and each `Frame` carries a copy
  (`entries`): results counts road flows from it, exactly (5-s samples had missed 39 % of a 40 m road's).
- `observe(keep, from_s)` (headless tests: `experiments.run_headless(observe=True)`, `from_s` the recorder's
  warm-up, so conflicts, fuel, detectors and link times leave it out as the KPIs do: `observed_from_s`,
  `observed_s` the time recorded, the collision, teleport and braking counts from then) turns on per-step recording in
  `_observe()`, after each step: each vehicle passing the middle of its lane (a detector: road, link, lane,
  time interpolated within the step, speed, length, type), each link's traversal time (entering its lanes
  to entering the next link's, the junction included; also by the next link, `turn_traversals`
  {link: {next link: (count, mean s)}}, what assign_routes' `turn_times_s` come from; a roundabout entry's
  link lists its `circulating` movements, for analysis.junction_los), rear-end conflicts (time to collision with the
  vehicle ahead in its lane under `CONFLICT_TTC_S` 3 s: one event from dropping below to recovering, with
  its least TTC and greatest DRAC, Δv²/2·gap), buses arriving in a stop's bay (with their line, the OD
  pair), and crossings of a signalised stop line (when that green began, whether the vehicle had stopped on
  the approach, its type and turn); also every link traversal (`traversal_log`, with the vehicle's own
  free-flow time over the link and the turn it took, at its `free_speed`), arrivals at signals and
  their green onsets, fuel (emissions.py), emergency vehicles' standstills and bus passengers' boardings
  (analysis.md lists them). `observations()` hands it out as plain data with each link's road, ends, lanes,
  length, free-flow time and `cross_s` (the free time through the junction at its end), plus the signals'
  plans, the controls, the weather, meters and closures; the collisions and emergency braking recorded (with
  `collision_counts` and `emergency_braking_count`, the run's totals), and `step_s` and `step_method`.

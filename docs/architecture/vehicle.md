# vehicle.py: a vehicle from its type; car following (no Qt)

- `create_vehicle(mode, id, route, depart, origin, destination, rng)` copies the Mode Behavior type's
  attributes onto the vehicle: name, colour, headway, size (length, width) and driving (top speed,
  desired top speed, acceleration, comfortable braking, standstill gap, reaction time τ, imperfection
  σ), and draws its speed factor with the run's random numbers (`speed_factor`: SUMO's default
  normc(1, speedDev, 0.2, 2), a normal draw about 1 drawn again until within [0.2, 2]; speedDev 0.1,
  so about 95 % drive at 80–120 % of the limit; no rng: exactly 1).
- The types carry SUMO's vClass defaults (`VehicleMode` in mode_behavior.py; SUMO's *Vehicle Type
  Parameter Defaults* and vType definitions): passenger car 5 m × 1.8 m, 200 km/h, 2.6 m/s² up,
  4.5 m/s² down; bus 12 m × 2.5 m, 85 km/h, 1.2 and 4 m/s²; bicycle 1.6 m × 0.65 m, 50 km/h with a
  desired top speed of 20 km/h, 1.2 and 3 m/s², 0.5 m standstill gap (2.5 m for the others); for all,
  σ 0.5, τ 1 s, speedDev 0.1, and a startup delay of 0.9 s (calibrated, below; SUMO's default is 0). A type
  added in the tab takes the passenger car's. The tab doesn't edit
  them yet.
- `free_speed(vehicle, limit)`: the fastest it drives under a speed limit, SUMO's min(maxSpeed,
  limit × speedFactor, desiredMaxSpeed × speedFactor). The simulation uses it wherever a limit
  applies: lanes, turns, bays, insertion and the lane-change look-ahead.
- `start_up(vehicle, planned, dt)`: SUMO's `startupDelay` ("the extra delay time before starting to
  drive after having had to stop"): a halted vehicle (under SUMO's 0.1 m/s halting speed) that could
  move off waits its startup delay first, counted from when it could go; a delay ending part way
  through a step gives it only that part of the step's acceleration. Every type's is 0.9 s (SUMO's
  default is 0): the largest value at which the saturation flow stays within field values
  (1,800–2,100 veh/h per lane), bringing the start-up lost time to 1.8 s against the HCM's default
  2.0 s (0.5 s: 1.39 s; 1.0 s: 1.85 s but 1,711 veh/h, since from two whole steps on, drivers
  creeping off behind a leader halt again and wait again).
- A `Vehicle` also holds its route (links), where it is (the lane or turn, `s` metres along it:
  lane-relative), speed, waiting time and its last two poses (for smooth drawing).
- **Krauss car following** (SUMO's default; reverse_engineering/simulation/car_following.md):
  `safe_speed` is SUMO's `maximumSafeFollowSpeed`, the speed from which, after τ, it can stop within
  the gap plus the leader's own stopping distance: vsafe = −τb + √((τb)² + 2b(g + vl²/2bl)).
  `next_speed` = min(limit, top speed, v + a·Δt, vsafe) less a random dawdle, as SUMO's default
  Krauss does it: σ·a·Δt·η, or σ·v·Δt·η while v is below one second's acceleration (so a driver moving
  off does move off; η uniform in [0, 1)); dawdling never brakes harder than comfortable (nor below that
  speed when braking harder already), never negative. A red light or a junction it waits at is a
  standing leader at the stop line.
- **What these drivers do at a signal** (SUMO's default passenger car with the 0.9 s startup delay, a
  standing queue, the HCM's field method; 4 seeds, an hour each): saturation headway 1.744 s,
  2,064 veh/h per lane (within field values of about 1,800–2,100); start-up lost time 1.82 s (the
  HCM's default is 2.0 s); 2.17 s of the 3 s amber used. The timing methods are calibrated to these
  (signal_timing.py).
- `secure_gap` (SUMO's getSecureGap) is the gap lane changing needs: the follower's stopping
  distance with reaction, less the leader's, plus the standstill gap.

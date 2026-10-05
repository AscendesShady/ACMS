# interchanges.py: interchanges and curve maths (no Qt)

Each layout is designed in a local frame `_Frame`:
- **Axes:** x runs along the freeway from its first selected junction towards its second;
  y runs across it towards the stem or crossroad.
- **Keep side:** `_keep_sign` says which side each carriageway keeps to.
- **Mirroring:** the same layout serves left- and right-hand driving; mirroring flips the
  curvature sign.
- **Records:** each builder records its name, origin and freeway direction in
  `network.interchanges`, so a later change of driving side can mirror it; its ramps get the road
  type `ramp`.

Construction helpers:
- **`_Builder`:** adds the nodes and roads.
  - `node`, `chain` (two-way roads) and `ramp` (one-way, straight segments).
  - `curve(a, b, pieces, speed)` makes a one-way curved ramp and stores its exact curvature.
    Its speed limit is capped at `curve_speed_kmh(tightest radius)`.
- **Curve pieces** are (x, y, curvature) points sampled about every 4 m, each carrying the
  exact curvature of its segment:
  - `_arc(cx, cy, r, from°, to°)`: a circular arc, κ = ±1/r.
  - `_spiral(x0, R, turn°)`: a clothoid transition spiral. Curvature grows linearly from 0 to
    1/R over length `L = 2·R·turn` (heading θ = s²/(2RL)), integrated numerically, so a
    straight joins a circle with no jump in curvature.
  - `_transition(start, heading, end, end_heading, pull)`: a cubic Bézier with exact curvature
    `(x′y″ − y′x″)/(x′² + y′²)^{3/2}` at each segment's middle.
  - `_straight(x, y)`.
- **`curve_speed_kmh(R)`:** `V = √(127·(e + f)·R)`, rounded down to 5 km/h, minimum 15.
  It uses e = 0.06 (superelevation) and f = 0.15 (side friction).
- **Placement:** `_existing_roads`, `roads_left_at_hub` and `_remove` find what Replace clears.
  The hub junction is kept if unselected roads still use it.
- **Leg detection:** `pair_legs` splits four junctions into the two crossing roads.
  `guess_stem` takes the junction left over from the farthest-apart pair.

Layouts:
- `build_trumpet(network, freeway, stem, replace, freeway_lanes)`: the stem splits at J, and a
  two-way bridge J→K crosses the freeway.
  - **Near carriageway:** exits upstream and enters downstream on Bézier ramps.
  - **Far carriageway, out:** a clothoid lead-in into a 270° loop of radius 55 m, which climbs to K.
  - **Far carriageway, in:** from K, one gentle bend (joined where `_bell_join` finds the
    gentlest single bend that stays clear of the loop) onto a ring road around the loop, then
    a quarter-circle merge.
  - **Scaling:** sizes shrink to fit tight sites, down to 35 %; below that the build refuses.
- `build_diamond(..., single_point=False)`: two signalised terminals on the crossroad, which
  passes over the freeway between them, with four one-way ramps. Needs ≥ 80 m of reach.
- `build_diamond(..., single_point=True)` (SPUI): the four curved ramps meet at one signal
  over the freeway, with a separate diverge and merge node on each side.
- `build_slip_road(network, first, second, single_lane=False)`: a one- or two-lane slip road between two roads meeting at a junction, for
  the kerb-side turn between them (left when driving on the left), whichever order they come in.
  - **Turn:** the road whose traffic arrives and the one it leaves by are found by the simulation's own
    kerb-turn rule (`Graph._kind`); both must carry that traffic. No kerb-side turn: refused.
  - **Layout:** both roads are split (`split_road`) at the same setback from the junction, at new
    uncontrolled nodes (`N…`), and a one-way `ramp` (one lane, `single_lane`, or two like interchange ramps)
    joins them on a `_transition` curve leaving and rejoining at 45° towards the kerb. Both ends are
    then kerb turns, so only the kerbside lane diverges onto it and it merges into the kerbside lane.
  - **Setback** (`_plan_slip`): the smallest in 5 m steps that leaves `SLIP_ROOM_M` (40 m) of lane between
    the slip's nodes and the junction's line (stop line; a roundabout's give-way line), whose curve's middle
    clears both roads (their half width plus the slip's) and all of it the junction (a roundabout's
    circle), leaving each road 20 m beyond. None: refused. Crossing another road: refused. The 40 m is for
    the queue and for room to exit into: a roundabout lets a vehicle in only with room on its exit lane.
- `fit_slip_roads(network, junction)`: run when a junction's control changes (Edit tab, the assistant's
  `set_junction_control`). Each slip road past it (`_is_slip`: a one-way ramp between two uncontrolled
  nodes of three roads, both a road from the junction) whose setback is now too short (it became a
  roundabout) slides out: both nodes move to `_plan_slip`'s setback along their roads, the pieces either
  side are re-cut there with their curves (`_recut`) and the slip is laid again. All are planned before
  any moves; one that can't move far enough refuses the lot.
  - **Speed:** the slower road's limit, capped by `curve_speed_kmh` of its tightest bend.
- **Design speeds:** freeway 100 km/h, ramps 60 km/h and loops 40 km/h, each capped by its
  tightest curve. Typical results: loop ≈ 55 m radius → 35 km/h; outer ramp ≈ 57 m → 35 km/h.

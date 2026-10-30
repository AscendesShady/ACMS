# demand.py: the OD matrix's demand as data (no Qt)

- `ODCell`: one pair's demand: `flow` (veh/h) and/or `daily` (trips a day) with its `profile` (daily_profile.md),
  arrival `pattern`, `split` (percent per vehicle type, in Mode Behavior's order, adding to 100) and `quarters`
  (an hourly pair's 15-minute shares; () even), and `drops`: trips ending part way, each point (vehicle type index,
  where as a share of the trip's length 0-1, whole % of that type's trips ending there), sorted; what no point takes
  reaches the destination. `UNITS` names each kind's unit; `DEFAULT_SPLIT` is 10/85/5.
- Drop-offs: `drops_after` (a deleted type's points go, later types move down one), `drops_problem` (a saved pair's
  points checked: a type, between 0 and 1, a whole %, a type's points at most 100 %), `drops_record` (read_od_matrix's
  `drop_offs` per type name, listed under "different" for the pairs that have them). `pair_demand` passes them on as
  `PairDemand.drops`.
- Split arithmetic: `whole_percent` (whole numbers in proportion, largest remainders first), `redistribute` (a
  deleted type's share to the others), `fixed_split` and `split_of(cell, per_hour, mode)`: the split in effect,
  each timetabled type fixed at its vehicles over the pair's trips (in an "hourly" run per hour against the
  flow, in a "daily" one per day against the daily trips; the other kind only when the pair has none of this
  one), the others sharing the rest in their stored proportions.
- The demand tools' rules: `pairs_from` (pairs named as "1W->1E" and the ways small models write it, or by
  origins and destinations; unknown ends refused, naming the real ones), `demand_plan` (set_od_demand's value,
  pattern, split, peak-hour factor or quarters, each checked), `shares_from` (a mode_split as names, a list of
  objects, or one number per type; fractions read as percent; shares adding up to 100 within half a point
  rounded together by `whole_percent`, so 12.5 / 82.5 / 5 and thirds are kept), `profile_from` (shape_daily_curve's points,
  polynomial or preset), `demand_now`, and `read_od` (read_od_matrix: totals, each pair's values, what most pairs
  share once as "usual" and only what differs). `lead_with_what_was_kept` puts what a run's check kept first.
- `pair_demand(cells, origins, destinations, mode, per_hour)`: a run's simulation.PairDemand list, each pair with
  demand of the kind shown between ends still on the network (`MainWindow._pair_demand` adds the institutions').

od_matrix.py's `ODCellPanel.command` still carries out each demand tool by working its own controls (the user
sees the values arrive; the whole tool is one undo step); it reads, checks and reports through these functions.
mode_behavior.py still offers the split helpers, and od_matrix.py `ODCell` and `UNITS`, as before.

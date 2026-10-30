# institutions.py: buildings that make trips (no Qt)

Each building is a zone of the four-step model: generation, distribution, mode split, then the Run assigns.

- `KINDS`: per building type its icon, what its people are (residents, students, patients and
  visitors a day, visitors a day, customers a day), how they travel (`together`: at the shift's start
  and end, else evenly all through it; `home`: residents leave at the start and come back at the end)
  and its default figures (illustrative).
- `footprint_problem(network, x, y, area)`: what a square building would stand on, or None: a road (its
  carriageway plus `VERGE_M` 3 m, tested by growing the square that much, so its corners are square), a
  junction (a roundabout's circle or a junction box's half diagonal, 0.91 of its widest road, plus the
  verge) or another building.
- `links(network, x, y, radius)`: the junctions and boundary ends (by their points) within the circle,
  nearest first (equally near: by name).
- **Generation.** `hourly_people(item)`: people arriving and leaving in each hour, any mode: everyone makes
  one trip each way a day; employees in the hours the shift starts and ends, and so do people who come
  together; the others evenly through the shift (`_through`, past midnight too); residents leave round the
  start and come back round the end (`RESIDENT_SPREAD`, a quarter the hour before, half in it, a quarter after).
  `_groups(item, daily)` keeps employees and the people apart (each with whether they commute: staff and residents).
  Daily, its own curves (`Institution.curves`, {"arrive"|"leave": a `daily_profile.profile_record`}) spread each
  group's day in their place, each group keeping its share of the day's arrivals (or departures); hourly, its
  people an hour typed (`Institution.per_hour`) are the same every hour, shared as the groups share the day (a
  building of nobody yet: all its people). Unset, the shift's shape (`shift_people(item)`: the shift alone, what its
  curves start from in the panel). `hourly_people(item, daily=True)`, `hourly_trips(item, daily=True)` and
  `run_demand` / `walk_demand` follow the Demand switch's mode; so `run_demand`'s design hour is any hour for a
  typed building. `hourly_trips(item)`: the same by car (its Car share). An office's default visitors (500 a day: business
  visits and staff's midday trips) bring it to ITE 710's 3.3 car trips a day per employee, scaled to its car
  share.
- **Distribution.** `door(network, item)`: its nearest junction (not a boundary end; equally near, the first by
  name), where its trips start and end. `catchment(network, item)`: the junctions and ends in its circle but the
  door: where its people come from, an equal share each (the circle of influence is the catchment).
- **Mode split.** `shares(item)`: people % by mode, its `mode_shares` (set by `network_tools.institution_split`,
  a logit: ai_mode.md) or else `car_share` by Car and the rest on foot (`WALK`). `vehicle_split(item,
  modes, commute)`: vehicles a person (share ÷ occupancy over the untimetabled types, a commute's at most
  `COMMUTE_OCCUPANCY` 1.1, NHTS 2017; walkers, MRT riders and timetabled riders add none: riders take the network's
  (`walk_demand(network, modes, daily)`: the Walk share of the people arriving, from each catchment junction or end
  an equal share to the door and on to the building, and of those leaving, back, as walking.WalkDemand; hourly in
  the same design hour as the vehicle trips, `_busiest` of `_vehicle_rows`, else its own busiest: walking.md.)
  buses),
  each type's share of them, and mode names no type has. A school's pupils are `escorted`: each car that brings or
  fetches one drives on again, so its pupils' vehicles go both ways at the start and at the end.
- **To a Run.** `run_demand(network, modes, daily)`: each building's trips as `simulation.PairDemand`s:
  catchment → door arriving, door → catchment leaving, each pair `timetabled=False` (no timetable of its own).
  Daily: the trips a day spread in the building's own hourly profile. Hourly: each pair's flow in the design
  hour, the hour when all institutions together make the most vehicle trips (said in the notes). Notes also
  say which buildings make no trips (no catchment, an unknown mode). `MainWindow._pair_demand` adds them to
  the OD matrix's pairs, so a Run, a test and an experiment spawn them.
- `place(network, kind, x, y, area, radius, ...)`: adds `IN#`, everything checked first.
- `place` also accepts optional `height_m`; it is validated with the other figures before insertion and saved
  with the institution. None uses an assumed kind default in `building_assets.height`. Height affects drawing
  only; generation, catchment distribution, mode split and the run demand remain unchanged.
- `Placement`: the placing steps (`move`, `click`, `grow` by `GROW` 1.15 a wheel step, from the
  building's walls to 3 km; `resize`). The first click starts the circle just past its corners.

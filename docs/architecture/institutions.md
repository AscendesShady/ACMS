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
  together; the others evenly through the shift (`_through`, past midnight too); residents leave at the
  start and come back at the end. `hourly_trips(item)`: the same by car (its Car share).
- **Distribution.** `door(network, item)`: its nearest junction (not a boundary end; equally near, the first by
  name), where its trips start and end. `catchment(network, item)`: the junctions and ends in its circle but the
  door: where its people come from, an equal share each (the circle of influence is the catchment).
- **Mode split.** `shares(item)`: people % by mode, its `mode_shares` (set by `network_tools.institution_split`,
  a logit: network_tools.md) or else `car_share` by Car and the rest on foot (`WALK`). `vehicle_split(item,
  modes)`: vehicles a person (share ÷ occupancy over the untimetabled types; walkers and timetabled riders add
  none: riders take the network's buses), each type's share of them, and mode names no type has.
- **To a Run.** `run_demand(network, modes, daily)`: each building's trips as `simulation.PairDemand`s:
  catchment → door arriving, door → catchment leaving, each pair `timetabled=False` (no timetable of its own).
  Daily: the trips a day spread in the building's own hourly profile. Hourly: each pair's flow in the design
  hour, the hour when all institutions together make the most vehicle trips (said in the notes). Notes also
  say which buildings make no trips (no catchment, an unknown mode). `MainWindow._pair_demand` adds them to
  the OD matrix's pairs, so a Run, a test and an experiment spawn them.
- `place(network, kind, x, y, area, radius, ...)`: adds `IN#`, everything checked first.
- `Placement`: the placing steps (`move`, `click`, `grow` by `GROW` 1.15 a wheel step, from the
  building's walls to 3 km; `resize`). The first click starts the circle just past its corners.

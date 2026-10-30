# walking.py: the walking network and who walks (no Qt)

Pedestrians walk the network's footpaths and paths, SUMO's NonInteracting model
(`references/reverse_engineering/simulation/pedestrian_system.md`): each along its route at its own fixed speed, a
junction's walking area crossed in a straight line, nobody in anybody's way. Where a walk crosses a road the walker
meets the traffic (simulation.md, Pedestrians): at a junction's arm it waits for a gap, HCM 2000 Ch 18 (p18-13,
Eq 18-17: tc = L / Sp + ts), traffic not yielding; on a zebra it has right of way (HCM 2000 p18-13) and traffic
stops; an overbridge takes it over the traffic, slower up its stairs.

- **Footpaths** (`Road.footpaths`: "left", "right" from from_node to to_node; models.md): `Network.footpath_line`,
  `FOOTPATH_M` 2.0 m wide (an assumption: room for two to pass) beyond the carriageway and any cycle lane on that
  side, out along a turn pocket's taper (`Network.beside_line`, which the cycle lanes use too).
- **Paths** (`Network.walkways`, `Walkway(id, start, end, bends)`; from OpenStreetMap's footways or the assistant's
  add_path, not drawn by hand: footpaths are a road's, main.md): P1,
  P2... from one road node (a junction or a boundary end) to another through free bends, off the roads (a park, a
  square). `Network.walkway_points` is its line; `add_walkway` refuses an unknown node, the same node twice or a bend
  off the map's numbers. A path goes when either of its nodes does (`_prune_walking`).
- **Crossings** (`Network.pedestrian_crossings`, `Crossing(id, road, distance_m, kind)`; Edit Network → Pedestrian,
  main.md): X1, X2... across a road with footpaths on both sides, `ZEBRA_CLEAR_M` 20 m from either end and beyond
  its junctions' corners (`Network.footpath_corner_m`), and
  `CROSSING_APART_M` 10 m from another (both assumptions), clear of its bus bays (`crossing_problem`); a "zebra" or an
  "overbridge" (`OVERBRIDGE_CLIMB_S` 20 s of stairs each side: about 6 m at 0.3 m/s, an assumption).
- **`WalkGraph(network)`**: each footpath from its road's start corner to its end corner (`Network.footpath_corner_m`:
  beyond the widest road at the junction, a cycle lane and a footpath, and a roundabout's circle; 0 at a boundary
  end), split at its road's crossings (a crossing's centre-line distance taken to each footpath where that point is
  nearest, `nearest_along`, so it goes straight across on a curve too; the cuts kept in order);
  each path from corner to corner (trimmed as much); each crossing joining the two sides (a zebra at its length, an
  overbridge with its climbs). At every node (`_round`) the corners and path ends go round in angle order with the
  roads' arms: neighbours with no arm between them are joined free (the walking area), with one a "gap" crossing of
  that road (its cost: the walk and a critical gap's wait as a walk, 2 L + ts·Sp); at a boundary end the road's way
  on off the map is an arm no one crosses or walks round. Both ways. `route(origin, destination)`: the cheapest walk
  (Dijkstra) from any corner of the first node to any of the last, never through a node's middle: its line and the
  roads it crosses, `Crossed(s0, s1, kind, road, distance_m, length_m, crossing, slow)` (`slow`: the share of its
  speed a walker keeps on an overbridge's stairs); cached; None if nothing joins them.
- **`WalkDemand(origin, destination, hourly, daily, shares, start, finish, quarters, attach)`**: people walking
  between two road nodes, an hour or a day spread over its 24 hourly shares (the fields `Simulation.rate` reads),
  `start` / `finish` a place walked from before the first node or on to after the last, straight (a building and its
  door); `quarters` empty (no 15-minute profile: an even hour); `attach` for spawn points (below).
- **`walk_pairs(network, modes, daily)`**: a run's walkers: each pedestrian OD pair (hourly, `Network.walk_demand`,
  people an hour between boundary ends; daily, `Network.walk_daily`, people a day spread by the pair's own curve;
  Demand Generator → Pedestrian OD, od_matrix.md), each spawn-point pair alike (`spawn_demand` / `spawn_daily`) and the
  institutions' (`institutions.walk_demand`: their Walk share, catchment to door and on to the building, and back;
  institutions.md).
- **Deploy points** (`Network.spawn_points`, `SpawnPoint(id, x, y)`; Demand Generator → Deploy Pedestrian,
  od_matrix.md and main.md): S1, S2... anywhere near the network, with their own OD matrix (`Network.spawn_demand`,
  people an hour; daily, `spawn_daily`). `walk_pairs` makes each pair a `WalkDemand` with `attach`: routed from its `start` point to its
  `finish` point. `WalkGraph.attach(x, y)` joins a point to the network: a straight walk to the nearest place on any
  footpath or path piece (`pieces`), that piece split there, next to any point already joined to it (so two points
  beside one stretch walk straight along it between them); `route` takes a node or an (x, y) point at either end.
  `footpath_join(network, x, y)` (no graph) is that nearest place, for the map and the window.
- `WALK_SPEED_DEV` 0.1: the spread of walkers' own speeds about `metro.WALK_KMH` 5 km/h (1.39 m/s), drawn as
  vehicles' speed factors are (`vehicle.speed_factor`, SUMO's normc(1, dev, 0.2, 2)): an assumption, as the
  references give no walking speed.

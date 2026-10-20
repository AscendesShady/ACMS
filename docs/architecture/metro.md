# metro.py: the metro as its own network (no Qt)

Rendering is independent: `metro_assets` and `network_scene.MetroItem` sweep the saved curved track into connected
decks and rails, curved platforms and labelled parked train previews in 2.5D (assumed heights). Piers are spaced
by world distance. There are no live train poses or simulated train motion.

Metro lines are deployed in Edit Network → Metro (main.md) and stored as `MetroStop` and `MetroLine` (models.md). The
metro is apart from the roads: its stations (M1, M2... in the order placed) are points of their own, not road nodes;
its demand is its own OD matrix (`Network.metro_demand`, station to station, passengers an hour, edited in Demand
Generator → Metro: od_matrix.md's `MetroMatrix`); road OD pairs never turn into metro trips (choose_modes has no metro).
Only an institution draws on it, from the stations inside its circle of influence, as it draws on the roads from the
junctions inside it.

- **A line's service** (`MetroLine.headway_min`, `speed_kmh`, `cars_per_train`, `car_capacity`, `fare_cents`; set when
  the line is accepted): assumptions until changed, `models.METRO_SERVICE` — a train every 5 min, 35 km/h with stops'
  dwell included, 5 carriages of 200 passengers (standing included), 100 cents a trip — each with its range
  (`metro_line_problem`). `train_capacity` is carriages × passengers a carriage (Eq 27-28's N_c × P_c). A line saved
  before carriages (one `train_capacity`) loads as the fewest carriages of at most 400 that hold it (900: 3 x 300).
- **The schedule is the line's service alone**: the metro OD matrix and buildings' riders never change a headway,
  speed or train; they only load the lines (Loads below), so the report says how many are served and where it is over
  capacity.
- **A ride** (`ride(network, board, alight)`): between two different stations, on a line they share or with one change
  at an interchange (a station on both lines, `lines_of`): each leg the track between its stations (curves included,
  `_along` measures along `Network.metro_track`) at its line's speed (in-vehicle), half of each line's headway as the
  wait (random arrival, as a timetabled bus), the dearer fare (one ticket through: an assumption); the quickest such
  ride, with its `legs`. None when no line or single change joins them.
- **A building's stations** (`door`, `places`): its door station is the nearest within `METRO_WALK_M` (800 m, an
  assumption: about ten minutes' walk at `WALK_KMH` 5 km/h, straight-line); its metro catchment, the other stations of
  its lines inside its circle (`radius_m`). In `network_tools.institution_split` each of those stations is a place its
  people come from, as each junction and end of its circle is (an equal share each): there they choose Metro (the
  ride to the door station, the wait, the walk on; its cost the fare plus an alternative's own `cost` and
  `cost_per_km`, `metro_cost`) or Walk (straight-line). At its junctions the metro isn't offered. Metro riders add no
  vehicles (`institutions.vehicle_split` leaves them out, as walking). With no station within the walk, nobody comes
  by metro. The Mode split dialog has a Metro row where the network has lines.
- **Loads** (`loads(network, flows)`, `capacity_per_h`): passengers (board station, alight station, an hour) on each
  line of their ride (a change: alighting the first at the interchange and boarding the second there), each
  stop-to-stop segment's load each way; a line's riders, passenger-km, busiest segment and its load over
  capacity (HCM 2000 Eq 27-28, P = (3,600 / headway) × carriages a train × passengers a carriage × PHF, `METRO_PHF`
  0.75: the HCM's light-rail value; it leaves off-street rail to the TCQSM, which isn't in the references), over
  capacity flagged; boardings and alightings a station; passengers no line serves. `MainWindow._metro_report` (tool
  `metro_report`) gathers them at the design hour, the hour the most ride: the metro OD matrix (the same every hour)
  and each building's metro share of the people arriving and leaving, between its door station and its catchment's
  stations in equal shares. The Metro tab's summary shows each line's busiest load against capacity, after every edit.

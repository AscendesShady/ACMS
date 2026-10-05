# results.py and xlsx.py: a run's KPIs and its workbook (no Qt)

- **`RunRecorder(network, modes)`**, made at each Run (`MainWindow._run`), keeps its working folder under the
  temp folder (`RUNS`, `ACMS runs/run <date time>`). `sample(frame)` (the physics thread, every `SAMPLE_S`
  5 s) adds, per vehicle, the distance (speed × dt), delay (dt · max(0, 1 − v / its road's limit)) and
  waiting time (dt while at or below `WAIT_MPS` 0.1 m/s and not in a bus stop's bay: SUMO's tripinfo
  `waitingTime`, a stop's dwell not counting) and keeps when it was first seen; a vehicle missing from the
  next frame has arrived, and its trip (type, origin, destination, departed, arrived, time, distance, delay,
  wait, people = its type's `occupancy`) is kept, its **passenger delay** (delay × people) added up. Each
  sample's queued vehicles add up too, for the mean queued at once (`queued_mean`). `kpis()` adds
  `mean_wait_s` (per completed trip), `people_delay_h` (person·h), `mean_person_delay_s` (passenger delay ÷
  people carried), `queued_mean`, and per type `mean_wait_s` and `people_delay_h` (older totals read as 0).
  Per road: vehicles entering (seen on it after another road), vehicle-seconds and -metres, delay, and its
  queue: vehicles slower than `QUEUE_KMH` 5 km/h, counted and as a length a lane (lengths + 2.5 m over its
  lanes per direction), its most and mean. Each simulated minute appends a row to `minutes.csv` (on the
  road, spawned and arrived from the frame, arrivals, throughput, people, mean speed, queued, delay,
  distance) and the trips to `trips.csv`, and rewrites `totals.json`, so an interruption loses a minute at
  most. For the statistics it also keeps the Per minute rows (`minute_rows`), every trip (`trip_log`: origin, destination, type, when it arrived, trip and delay
  seconds), the vehicles first seen per origin and minute (`entries`) and each road's queue (metres a lane,
  rounded) at every sample that had one (`queues`); `observed()` hands them out (experiments.analyse_run,
  choose_modes' `from_run`). `kpis()` (any thread, under its lock) gives the network's figures, with the
  travel time index `tti` and planning time index `pti` (`stats.reliability`; not in the workbook's Summary,
  which is made from the saved totals); `export(path)` a workbook now;
  `finish(results)` the workbook when the run ends (`MainWindow._stop_runner`: Stop, a new Run, a change to
  the network, closing, after one last sample) and clears the folder, or nothing if it recorded nothing.
- **`workbook(folder, path)`**, named by when the run started, its seed and the AI model that decided TSP
  (`file_name(started, recovered, seed, model)`: `ACMS results 2026-09-30 14-05-33 seed 42 laya.xlsx`; the
  model is Jev's or Laya's provider, System 2's model id, `no-llm` when TSP had nobody to ask, `no-ai` without
  TSP; given to `RunRecorder(seed=, model=)` and kept in `meta.json`), made to hand to an independent analyst:
  **Read me** (the run's start, the seed, the AI model, how to
  repeat it, what each sheet holds, every figure's definition, the assumptions), **Summary** (each KPI, its
  unit and how it's made), **Run** (its settings, from `MainWindow._run_settings`: the application, start,
  random seed, time step, sampling, speed, the demand used, pairs with demand and any with no route, the
  network file, the network's size, driving side, background, the queue speed, and each extension ticked
  or not with its settings as its panel shows them, `ExtensionsPanel.settings`; then how long was simulated
  and when the workbook was written), **Per minute**, **Roads** (flow veh/h, mean speed, density veh/km,
  most and mean queued, longest queue a lane, delay), **By type** (with mean wait and passenger delay),
  **Trips** (with each trip's wait), the sheets the run's extensions reported (`add_sheets(reports)`, kept
  in the folder's `extra.json`: TSP's **TSP** and **Agent decisions**, see extensions.md; each named in the
  Read me; their files, kept in `files/`, go beside the workbook named after it: `<workbook name>
  telemetry.jsonl`, at the end and on Export), and the inputs as they were at
  Run (`input_sheets`): **Junctions** (place, control, signal settings), **Road network**, **Boundary ends**,
  **Demand** (every pair: flow or trips a day with its 24 hourly shares, pattern, split), **Vehicle types**
  (every parameter and occupancy), **Signal plans** (what each method worked out), **Bus stops** (with
  boarding per hour and alighting share),
  **Institutions**. The inputs and settings are kept in the folder's `meta.json`, so a recovered workbook
  has them too. **`recover(results)`** makes one for each folder a crash left (`… (recovered).xlsx`) at the
  next start. A run's folder is named with its start, process id and a number (`_numbers`), so runs started
  in the same second have their own; runs finishing at once take their workbook names one at a time
  (`_naming`, each reserving its name). A live run's folder holds `live.lock`
  (`take_lock`: kept open, which Windows won't let another process delete, and `fcntl.flock`ed on POSIX;
  either lets go when the process ends, a crash too), so a second ACMS open at once leaves that run
  alone (`lock_held`).
- **Seed**: every Run uses the Seed box's value (`main.RUN_SEED`, 42, by default) for its arrivals and drivers, so the same inputs repeat the
  same run; the workbook records it.
- **`xlsx.write_xlsx(path, sheets)`**: Office Open XML SpreadsheetML by hand (zipfile): numbers as numbers,
  other values as inline strings (escaped, XML's forbidden control characters dropped), a bold frozen
  header, columns sized from the first 200 rows, sheet names made legal (31 characters, no []:*?/\),
  written to a `.part` file and moved into place. **`xlsx.read_xlsx(path)`** reads such a workbook back (each
  sheet's rows, numbers as floats): a replay reads a run's seed and simulated time with it, and the tests read
  workbooks with it.
- **Occupancy**: `mode_behavior.VehicleMode.occupancy`, people a vehicle carries: Bus 20, Car 1.5, Bike 1, a
  new type 1 (assumptions, not editable in the tab yet, stated on the page and in the workbook).
- **The window**: results go to Documents\ACMS results (offscreen, i.e. tests: a temporary folder);
  **Export** (the menu bar corner) saves a workbook of the run so far (with TSP's sheets as they stand: its
  agent's decisions, latency and tokens, and the analysis of what it has observed so far), the run carrying on,
  or copies the last run's; with neither, it saves the network alone (`network_workbook`: Read me, City, the
  inputs, Settings, and its GeoJSON). A run's end (`MainWindow._stop_runner`) and each headless run
  (`_one_run`) add the extensions' reports (`ExtensionsPanel.reports`) and the analysis sheets
  (`add_analysis`) before the workbook is written.
- **Everything for analysis** (every run, the user's live ones too):
  - **Road conditions**: each direction of each road (link: "road>" from its from-node, "road<" back, from the
    lane in each sample's `place`), its flow, flow a lane, mean speed and its share of the limit, level of service
    (`LOS_SPEED`, `level_of_service`: HCM 2010 urban street segments by travel speed as a share of free-flow speed,
    the limit standing in for it; A over 85 %, B 67, C 50, D 40, E 30, F at or below), travel time against free
    flow (travel time index), density and delay (`road_conditions`, from the totals' `links`).
  - **Roads by minute**: each road each simulated minute (entered, flow, mean speed, most queued, delay),
    written to `roads_minutes.csv` as the run goes.
  - **City** (`city_sheet`, an input sheet): place or background, driving side, extent, junctions by control and
    timing method, ends, roads, road and lane length, one-way share, mean block length, junction and road density,
    road length by type, speed limit and lanes each way, curves (tightest radius, roads posted faster than their
    curve allows), roundabout diameters, interchanges, overpasses, bus stops, institutions, route choice, and the
    graph indices (`network_tools.network_indices`).
  - **Road network** and **Junctions** also carry each road's width, tightest curve radius and safe curve speed
    (`road_record`), bus stops and shape (WKT, scene metres), and each junction's roads meeting, roundabout
    diameter and interchange.
  - **Analysis sheets** (`experiments.analysis_sheets`, when the run observed: `Simulation.observe`, a live run's
    keeping its latest `LIVE_OBSERVED` 400,000 passages, conflicts and stop-line crossings): Run statistics,
    Reliability by OD, Arrivals, Queues (95th pct), Bus headways, Bus passengers (stops with passengers: boarded, alighted, left
    behind, dwell, heaviest load), Detectors, Queue discharge, Link travel times,
    Conflicts (the first 20,000 listed) and Safety.
  - **GeoJSON** beside the workbook (`write_geojson`, RFC 7946): each road a LineString with its attributes, the
    run's figures (`run_…`) and each direction's flow, speed, level of service and travel time index; each junction
    and boundary end a Point. WGS84 longitude and latitude when the map is placed (`georeference`,
    `map_tiles.Frame.to_wgs84`), else local metres east and north of the scene origin, stated in
    `acms_coordinates`. The geometry is kept in the run folder's meta.json, so a recovered run has it too.

# ACMS Architecture

ACMS — Agentic Context-Aware Mobility System.

What each part of the code does, what the app can do today, and where its
engineering methods come from. Written for developers and for the System 2
assistant (AI Mode tab), which reads this file and `README.md` before it answers
questions.

**Status in one line:** a network *editor* (build, edit, save road networks with
true-scale geometry, roundabouts, signals, interchanges and bus stops, plus an OD demand
table) and a first traffic *simulation*: Run spawns vehicles from the OD matrix by each pair's
arrival pattern, and they follow the car ahead (Krauss), obey signals timed by Webster, TRANSYT or
Synchro (worked out from the demand at each Run) or adaptively by SCATS or SCOOT (from their
detectors as the run goes), accept gaps at junctions and roundabouts,
change lanes, and keep to the road geometry; buses pull into their stops' bays and merge back. The physics keeps real time on its own
thread; the map draws it on the GPU, and an AI can read every vehicle live. Each run's KPIs are
recorded as it goes and saved as an Excel workbook when it ends. TSP is decided by System 1 (Jev, or
Laya run on this PC) or System 2, and Batch Run compares them with no LLM over seeds.

**For detail, read `docs/architecture/<module>.md` with `read_source`** (e.g. path
`docs/architecture/simulation.md`). Also there: `capabilities.md` (§1 in full), `flows.md` (main flows),
`sources.md` (where each method comes from), `not_built.md` and `tests.md`.

## 1. Capabilities

| Area | Implemented | Placeholder | Doc file |
|---|---|---|---|
| Grid generation | 1–20 × 1–20 corridors: spacing, speed, lanes, lane changing, default signal timing | — | [network_generator.md](docs/architecture/network_generator.md) |
| Driving side | Left- or right-hand for the whole network; interchanges mirrored exactly | — | [models.md](docs/architecture/models.md) |
| Background | Colour, image or PDF page to scale, OpenStreetMap by place search | Other tile servers | [map_tiles.md](docs/architecture/map_tiles.md) |
| Roads | Length, lanes, direction, speed, lane changing, stored curvature | Road type label | [models.md](docs/architecture/models.md) |
| Nodes | Add, delete, drag; signalised, roundabout or uncontrolled | — | [models.md](docs/architecture/models.md) |
| Roundabouts | Sized from approach speed and lanes (NCHRP 672), true diameter | Capacity (HCM) | [roundabouts.md](docs/architecture/roundabouts.md) |
| Signals | Webster, TRANSYT, Synchro, SCATS, SCOOT, actuated (gap-out), max-pressure or fixed time; TSP on top; ramp meters (ALINEA) | SCATS tactical control | [signal_timing.md](docs/architecture/signal_timing.md) |
| Edges | Add Edge; crossings become a roundabout or an overpass | — | [main.md](docs/architecture/main.md) |
| Interchanges | Trumpet, diamond and SPUI from Ctrl+clicked junctions; slip roads (1 or 2 lanes) from two roads | — | [interchanges.md](docs/architecture/interchanges.md) |
| Boundary ends | Named 1E, 2N, 1.5E…; supply and demand veh/h per end | — | [models.md](docs/architecture/models.md) |
| Bus stops | Kerbside bays placed, dragged, nudged; buses pull in; passengers board and alight (HCM dwell), loads, left behind | Stops for other vehicle types | [models.md](docs/architecture/models.md) |
| Demand | OD matrix, hourly or daily by a shaped curve; pattern, split | Trip generation beyond the gravity model | [od_matrix.md](docs/architecture/od_matrix.md) |
| Editing | Undo/redo, Esc, Delete, box selection and Delete Selected | — | [main.md](docs/architecture/main.md) |
| Projects | One validated JSON file: network, demand, types, run settings, conversation | — | [network_io.md](docs/architecture/network_io.md) |
| AI Mode | System 2 chat (Grok, Gemini, Ollama) that reads, tests and edits through tools; each request offered only the tools its words point to, the model writing a Plan: line before it calls them; System 1 (Jev, or a local Laya the app starts) answers the signal agent's yes/no questions | System 1 beyond the signal agent | [ai_mode.md](docs/architecture/ai_mode.md) |
| Institutions | Buildings placed RTS-style or by tool; the circle is the catchment, the nearest junction the door; a logit mode split (Walk and the vehicle types) calibrated to observed shares; their trips spawned by a Run | Trips beyond the catchment; what-ifs adding a building's trips | [institutions.md](docs/architecture/institutions.md) |
| Vehicle types | Up to 8 types; timetabled ones with headways; the assistant edits their parameters | Behaviour editing in the tab | [mode_behavior.md](docs/architecture/mode_behavior.md) |
| Simulation clock | Run, Play/Pause, Stop, clock, speed 1–60×, Seed, Export | — | [clock.md](docs/architecture/clock.md) |
| Extensions | Drop-in `extensions/*.py` ticked on: TSP (bus priority) and Dynamic Emergency Vehicle Lane (each signal clears the kerbside lane for an emergency vehicle) at every signalised junction, each decided by System 1 or System 2 and a model, or Multiple (Batch Run's models), Batch Run over seeds (Test TSP, Test DEVL, a time estimate), Dispatch Emergency Vehicle, Research Log (each AI Mode step: prompt, understanding, actions, changes, map) | Traffic yielding to emergency vehicles | [extensions.md](docs/architecture/extensions.md) |
| Traffic simulation | Krauss following, LC2013 lane changing, signals, gap acceptance, buses, route choice (logit), lane closures and work zones, weather, forks for look-ahead, GPU drawing | Pedestrians, per-type behaviour editing | [simulation.md](docs/architecture/simulation.md) |
| Network testing | Headless runs, experiments (paired, ANOVA, Friedman, all pairs, power, conditions, factorials, equilibrium), junction LOS, signal diagnosis, run health, resilience, emissions, accessibility, design audit, benefit-cost, detectors, safety, calibration | Parallel runs; fast 24 h tests | [experiments.md](docs/architecture/experiments.md) |
| Summary | View Summary: Network, Demand, Results and AI Usage pages | — | [summary.md](docs/architecture/summary.md) |
| Results | KPIs every 5 s; a workbook per run with road conditions, city, statistics; GeoJSON; crash recovery | Occupancy editing in the tab; signal/junction KPIs | [results.md](docs/architecture/results.md) |

**Placeholders:** System 1 beyond
the signal agent (nothing else asks it yet). Full list: [not_built.md](docs/architecture/not_built.md).

## 2. Layout

```
run.py                     Opens the editor (python run.py)
run_monitor.py             Test monitor window: live progress of every test
config/defaults.json       Default grid settings for a new network
extensions/                Drop-in features for the Extensions tab (batch_run.py, transit_signal_priority.py, dynamic_emergency_lane.py, dispatch_emergency.py, research_log.py)
ARCHITECTURE.md            This document
docs/architecture/         One file per module, plus capabilities, flows, sources, not built and tests
README.md                  Setup and run instructions
ui-ux-design-rulebook.md   UI rules the interface follows
reverse_engineering/       Study notes on SUMO's netedit, used as a feature reference (no SUMO code)
src/traffic_simulator/     The modules, listed in the module map below
tests/                     Plain assert tests; run each file, or all via run_monitor.py ([run_monitor.md](docs/architecture/run_monitor.md))
```

Dependencies: Python ≥ 3.10 and PySide6 only. Everything else is the standard library.

### Module map (`src/traffic_simulator/`)

| Module | Purpose | Qt | Doc |
|---|---|---|---|
| models.py | Network data model: nodes, roads, ends, bus stops; all editing operations | no | [models.md](docs/architecture/models.md) |
| geometry.py | Vector primitives on polylines (lengths, offsets, curvature, intersections) | no | [geometry.md](docs/architecture/geometry.md) |
| network_generator.py | Builds grid networks and their boundary ends | no | [network_generator.md](docs/architecture/network_generator.md) |
| network_io.py | Save / load networks and projects as validated JSON | no | [network_io.md](docs/architecture/network_io.md) |
| interchanges.py | Trumpet, diamond and SPUI builders; curve maths; curve speed | no | [interchanges.md](docs/architecture/interchanges.md) |
| roundabouts.py | Roundabout design from NCHRP Report 672 | no | [roundabouts.md](docs/architecture/roundabouts.md) |
| network_scene.py | Draws the network (Qt Graphics View) and handles map clicks/drags | Qt | [network_scene.md](docs/architecture/network_scene.md) |
| properties_panel.py | Editor for the selected road / node / end; shared widgets | Qt | [properties_panel.md](docs/architecture/properties_panel.md) |
| od_matrix.py | OD matrix (hourly or daily), cell editor, curve editor, Institutions panel | Qt | [od_matrix.md](docs/architecture/od_matrix.md) |
| institutions.py | Where a building can stand, its door and catchment, its trips by hour and mode, its OD pairs for a Run | no | [institutions.md](docs/architecture/institutions.md) |
| daily_profile.py | Daily demand curves: monotone spline or polynomial, hourly shares | no | [daily_profile.md](docs/architecture/daily_profile.md) |
| background_network.py | A network from OSM road data (Overpass) or an image's roads | no | [background_network.md](docs/architecture/background_network.md) |
| mode_behavior.py | Mode Behavior tab: the vehicle types every other tab uses | Qt | [mode_behavior.md](docs/architecture/mode_behavior.md) |
| vehicle.py | A vehicle made from its type; Krauss car following | no | [vehicle.md](docs/architecture/vehicle.md) |
| simulation.py | The traffic simulation: lane graph, signals, junctions, lane changing, demand | no | [simulation.md](docs/architecture/simulation.md) |
| map_tiles.py | WGS84 / Web Mercator to scene metres, OSM tiles and cache, place search | no | [map_tiles.md](docs/architecture/map_tiles.md) |
| background.py | The map's background (colour, image, PDF, OSM) and its Create tab section | Qt | [map_tiles.md](docs/architecture/map_tiles.md) |
| gpu.py | The map on the GPU: the OpenGL renderer; the vehicles' instanced shader | Qt | [gpu.md](docs/architecture/gpu.md) |
| signal_timing.py | Signal timing: Webster, TRANSYT, Synchro, SCATS, SCOOT (standard library only) | no | [signal_timing.md](docs/architecture/signal_timing.md) |
| signal_agent.py | An agent's TRUE/FALSE answers for signal overrides: in the background, or waited for to a deadline (headless) | no | [simulation.md](docs/architecture/simulation.md) |
| transit_signal_priority.py | TSP controller for one signalised node: hold, truncation, recovery | no | [extensions.md](docs/architecture/extensions.md) |
| agent_transit_signal_priority.py | The TSP agent: observes the junction, asks System 1 or System 2 once per bus request | no | [extensions.md](docs/architecture/extensions.md) |
| summary.py | View Summary tab: the whole project at a glance | Qt | [summary.md](docs/architecture/summary.md) |
| results.py | A run's KPIs, recorded as it goes, and its workbook | no | [results.md](docs/architecture/results.md) |
| xlsx.py | A minimal Excel (.xlsx) writer and reader, standard library only | no | [results.md](docs/architecture/results.md) |
| experiments.py | Network testing: headless runs, scenarios, experiments across seeds, statistics | no | [experiments.md](docs/architecture/experiments.md) |
| scenario.py | The Scenario Builder: a described scenario as a form, its plan, its report | no | [ai_mode.md](docs/architecture/ai_mode.md) |
| stats.py | The tools' statistics: tests, ANOVA, regression, count models, logit, k-means, envelopes, smoothing | no | [experiments.md](docs/architecture/experiments.md) |
| analysis.py | A run's junction LOS, signal diagnosis, health, resilience and fuel, from what it observed | no | [analysis.md](docs/architecture/analysis.md) |
| emissions.py | Fuel and CO2 of cars and vans: Biggs and Akçelik's power model | no | [analysis.md](docs/architecture/analysis.md) |
| design_checks.py | Design audit: sight distance, turning paths, skew, spacing, lane balance (Green Book) | no | [network_checks.md](docs/architecture/network_checks.md) |
| accessibility.py | What people can reach: cumulative and Hansen accessibility, its loss with a road closed | no | [network_checks.md](docs/architecture/network_checks.md) |
| clock.py | Simulation clock behind Run / Pause / Play / Stop | Qt | [clock.md](docs/architecture/clock.md) |
| extensions.py | Extensions tab: loads extensions/*.py, draws them, isolates their errors | Qt | [extensions.md](docs/architecture/extensions.md) |
| agent_choice.py | Who decides an extension's TRUE/FALSE (TSP, DEVL): System 1 or 2 and its model, or Multiple for Batch Run | Qt | [extensions.md](docs/architecture/extensions.md) |
| main.py | Main window: tabs, tools, dialogs, undo/redo, file menu | Qt | [main.md](docs/architecture/main.md) |
| history.py | Snapshot undo/redo with merged typing | no | [history.md](docs/architecture/history.md) |
| theme.py | Grey theme (white text), colour tokens, contrast pairs | Qt | [theme.md](docs/architecture/theme.md), [layout_conventions.md](docs/architecture/layout_conventions.md) |
| ai_mode.py | AI Mode tab: key discovery, System 1's choice, System 2 chat UI, prompt gate, change approval | Qt | [ai_mode.md](docs/architecture/ai_mode.md) |
| assistant.py | Chat back end for Grok, Gemini and Ollama: streaming and tool calls; System 1 requests (Jev, Laya) and starting laya.exe | no | [ai_mode.md](docs/architecture/ai_mode.md) |
| network_tools.py | What the assistant can read and change on the network | no | [ai_mode.md](docs/architecture/ai_mode.md) |
| config.py | Reads config/defaults.json | no | — |

## 3. Conventions

- **Vector geometry, metres.** Every position is continuous `(x, y)` in metres;
  x points right and y points **down** (screen convention). Roads are polylines.
  Nothing is raster or cell-based; grid row/column numbers are only labels.
  Vehicle positions are lane-relative: the lane, junction turn or bus bay a vehicle is on and
  the distance along it (simulation.py); x, y and heading come from that polyline.
- **Lane width** `LANE_WIDTH_M = 3.5` (models.py). A road's width (`Road.width_m`) is
  `2 × lanes_per_direction × 3.5 m`, one-way or two-way; a single-lane one-way road (`single_lane`) is 3.5 m.
- **Driving side** `network.driving_side` is `"left"` or `"right"`; signals,
  stop lines, end markers, bus bays, turns and roundabouts are drawn and simulated from it, so
  changing it flips them all at once. An interchange is laid out for the side when it is built, so
  `Network.set_driving_side` mirrors it (see models.py).
- **Curvature sign.** Positive curvature bends towards the `(-uy, ux)` side of the
  direction of travel `(ux, uy)`; radius = 1 / |curvature|. `Road.curvature` has one
  value per polyline segment (`len(shape) + 1`), empty for a straight road.
- **Names and IDs**
  - Grid junctions: graph coordinates `"column,row"` with the bottom-left junction `1,1`.
  - Added junctions: `N1`, `N2`… in the order added; never renamed.
  - Boundary ends: side letter plus number. E/W count up from the bottom, N/S from the left
    (`1E`, `2E`, `1N`…). A new end between `1E` and `2E` becomes `1.5E`; past the last, the next
    whole number. The end node and its road share the name.
  - Roads: `"from-to"`, e.g. `"1,1-2,1"`; boundary roads use the end's name.
  - Bus stops: `BS1`, `BS2`… in the order added; never renamed.
  - Institutions: `IN1`, `IN2`… in the order placed; never renamed.
  - Interchange parts: `TR1.1`, `TR1.2`… (trumpet), `DI1.x` (diamond), `SP1.x` (SPUI).
- **Validate before changing.** Builders and edits check everything first; a failed
  interchange build or road edit leaves the network untouched.
- **Grade separation.** Two roads that cross without sharing a node pass over/under
  each other. `Road.overpass = True` marks the upper one, drawn as a bridge with parapets.

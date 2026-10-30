# ACMS Architecture

ACMS (Agentic Context-Aware Mobility System): a road-network editor (true-scale roads, signals, roundabouts,
interchanges, bus stops, OD demand) with a SUMO-style microscopic simulation. Vehicles follow the car ahead
(Krauss), change lanes (LC2013), obey signals (Webster, TRANSYT, Synchro, SCATS, SCOOT, actuated), accept gaps;
buses dwell at stops. Physics runs in real time on its own thread; each run's KPIs become an Excel workbook.
This index is the in-app assistant's (System 2) context, sent with every request, and coding agents' map of the code
(Layer 3; they start at `AGENTS.md`). Detail is one file away: **`docs/architecture/<module>.md`**.

## 0. For the assistant: a small context and a short wait

The docs are large (sources.md, tests.md, ai_mode.md, simulation.md: 40–66k characters each): never load one whole.
**Route: this index → the one file below → `read_source` with `find` (line numbers) → ~30 lines by
`start_line`/`end_line`.** Network facts come from tools (`network_summary`, `describe`…), never from docs; ask a
narrow tool (one junction) before a list. Tests cost time: `run_simulation` 10–30 min, one seed, unless asked; an
experiment takes scenarios × seeds × minutes. Answer in a few lines.

| Question about… | Read |
|---|---|
| what the app can / can't do | `docs/architecture/capabilities.md`, `not_built.md` |
| where a method or formula comes from | `docs/architecture/sources.md` |
| how a module works | `docs/architecture/<module>.md` (map in §2) |
| a typical flow (Run, Save, an AI step) | `docs/architecture/flows.md` |
| setup, keys, controls | `README.md` |

## 1. Capabilities (placeholders in brackets)

| Area | Implemented [not yet] | Doc |
|---|---|---|
| Network | Grid 1–20 × 1–20; roads (lanes, turn pockets, cycle lanes, footpaths, direction, speed, curvature); paths; nodes signalised/roundabout/uncontrolled; left or right driving; ends `1E`… with flows; Add Edge (crossings → roundabout or overpass); undo/redo, box select [road type label] | [models.md](docs/architecture/models.md) |
| Background | Colour, image or PDF to scale, OpenStreetMap by place [other tile servers] | [map_tiles.md](docs/architecture/map_tiles.md) |
| Roundabouts | NCHRP 672 sizing, ≤ 3 entry lanes; entry capacity vs HCM 6 | [roundabouts.md](docs/architecture/roundabouts.md) |
| Signals | Webster, TRANSYT, Synchro, SCATS, SCOOT, actuated, max-pressure, fixed; ITE amber/all-red per junction; TSP; ALINEA ramp meters [SCATS tactical] | [signal_timing.md](docs/architecture/signal_timing.md) |
| Interchanges | Trumpet, diamond, SPUI; curved slip roads | [interchanges.md](docs/architecture/interchanges.md) |
| Bus stops | Kerbside bays; HCM dwell; boarding, alighting, loads [stops for other types] | [models.md](docs/architecture/models.md) |
| Demand | OD matrix hourly (peak-hour factor or 15-min profile) or daily (shaped curve), arrival pattern, mode split, drop-offs along the route per type; gravity model [trip generation beyond it] | [od_matrix.md](docs/architecture/od_matrix.md) |
| Institutions | Buildings with catchments, logit mode split, their trips in a Run [trips beyond the catchment] | [institutions.md](docs/architecture/institutions.md) |
| Pedestrians | Footpaths, paths, zebras and overbridges (Pedestrian tab); walkers from a pedestrian OD matrix and institutions' Walk share on the cheapest walk, SUMO NonInteracting; gaps at junction arms (HCM Eq 18-17), traffic stopping at zebras [crowding, walk signals] | [walking.md](docs/architecture/walking.md) |
| MRT | Lines drawn stop by stop: 300 m curves, turns under 90°, the next leg previewed, no self-crossing; a crossing merged at an interchange (one change); its own OD matrix; HCM line loads; service set per line, never from demand [trains running, branches] | [metro.md](docs/architecture/metro.md) |
| Vehicle types | Up to 8; timetables; a vehicle class each [behaviour editing in the tab] | [mode_behavior.md](docs/architecture/mode_behavior.md) |
| Simulation | Krauss, LC2013, gap acceptance, junction delay in route costs once, route choice (logit), random streams per pair, closures, work zones, weather, emergency braking bound, optional ballistic update and shorter step (headless), optional lateral position: two-wheelers filter (headless), collisions counted (same lane, crossing) [pedestrian crowding, walk signals] | [simulation.md](docs/architecture/simulation.md) |
| Clock | Run, Play/Pause, Stop, speed 1–60×, Seed, Export; every stopped or failed run exports | [clock.md](docs/architecture/clock.md) |
| Results | KPIs every 5 s (delay vs road limit and own free speed, person delay, entry wait); exact trip and system travel times, completion rate, buffer index, warm-up (assistant); workbook, GeoJSON, crash recovery [signal/junction KPIs] | [results.md](docs/architecture/results.md) |
| Testing | Headless runs; experiments over seeds (paired, ANOVA, factorials, equilibrium); LOS, diagnosis, health, resilience, emissions, accessibility, design audit, benefit-cost, safety, calibration [parallel runs] | [experiments.md](docs/architecture/experiments.md) |
| AI Mode | System 2 chat (Grok, Gemini, Ollama) with tools and their skills, offered by the request's words; each request trimmed to its context window (`ACMS_NUM_CTX`), docs searched with `read_source` find, a stalled reply ended at 90 s; System 1 (Jev, Laya) answers yes/no signal questions [System 1 elsewhere] | [ai_mode.md](docs/architecture/ai_mode.md) |
| Extensions | TSP, Dynamic Emergency Vehicle Lane, Batch Run, Dispatch Emergency, Research Log (each AI step; a picture only of what changed: map or OD matrix) [traffic yielding to emergency vehicles] | [extensions.md](docs/architecture/extensions.md) |
| Projects, Summary | One validated JSON project; View Summary pages | [network_io.md](docs/architecture/network_io.md), [summary.md](docs/architecture/summary.md) |

Full list: [capabilities.md](docs/architecture/capabilities.md); placeholders: [not_built.md](docs/architecture/not_built.md).

## 2. Layout

```
AGENTS.md, CONTEXT.md      Coding agents: Layer 0 (what always holds) and 1 (which stages)
stages/                    Numbered stage contracts (code, campaign, research), each with its output/
run.py, run_monitor.py     The editor; the test monitor window
src/traffic_simulator/     The modules (map below)        tests/       Plain assert tests
extensions/                Drop-in features               docs/        Module docs, sources, tests
references/                UI rulebook, SUMO notes, books, theory
tools/                     Prompt campaign; tools/run_test.py (quick, timed tests)
reports/, references/       On this PC only (git keeps the UI rulebook); research/: book mining
```

Python ≥ 3.10 and PySide6 only. Each module's doc is `docs/architecture/<module>.md`, else the one named.

| Module | Purpose | Qt |
|---|---|---|
| models.py | Network data model and every edit; validation | no |
| geometry.py | Polylines: lengths, offsets, curvature, intersections | no |
| network_generator.py | Grid networks and their ends | no |
| network_io.py | Save / load validated JSON projects | no |
| interchanges.py | Trumpet, diamond, SPUI, slip roads; curve speed | no |
| roundabouts.py | Roundabout design (NCHRP 672) | no |
| network_scene.py | Top-down/orthographic 2.5D network, map clicks and drags | Qt |
| properties_panel.py | Editor for the selected road, node or end | Qt |
| widgets.py | Shared buttons, hints, sections, tabs, choices and tick lists | Qt |
| od_matrix.py | OD matrix grid, cell, curve and drop-off editors, Institutions panel, MRT OD matrix | Qt |
| demand.py | OD cells, mode-split arithmetic, demand tools' rules, a run's pair demand | no |
| institutions.py | Buildings' doors, catchments, trips and modes | no |
| walking.py | Footpaths, paths and crossings as a walking network, cheapest walks, a run's walkers (→ walking.md) | no |
| daily_profile.py | Daily demand curves, hourly shares | no |
| background_network.py | A network from OSM or an image (→ map_tiles.md) | no |
| background.py | The map's background and its Create section (→ map_tiles.md) | Qt |
| map_tiles.py | WGS84 ↔ scene metres, OSM tiles, place search | no |
| mode_behavior.py | Vehicle types tab | Qt |
| vehicle.py | Vehicle types (VehicleMode); a vehicle from its type; Krauss following | no |
| vehicle_assets.py | Shared display-only vehicle skins and stable random paint | no |
| asset_mesh.py | Low-poly vehicle/person meshes, footbridge scenery and material faces | no |
| building_assets.py | Metre-height institution structures and assumed defaults | no |
| metro_assets.py | Display-only overhead viaducts along the track, stations and parked train previews | no |
| lane_graph.py | Lane graph: links, lanes, turns, bays, signals, routes, capacities | no |
| simulation.py | Signals, junctions, lane changes, demand, Runner thread, over the lane graph | no |
| signal_agent.py | TRUE/FALSE answers for signal overrides (→ simulation.md) | no |
| signal_timing.py | Webster, TRANSYT, Synchro, SCATS, SCOOT, clearance | no |
| transit_signal_priority.py | TSP controller at one junction (→ extensions.md) | no |
| agent_transit_signal_priority.py | TSP agent asking System 1 or 2 (→ extensions.md) | no |
| agent_choice.py | Who decides an extension's TRUE/FALSE (→ extensions.md) | Qt |
| gpu.py | OpenGL map renderer, instanced vehicles | Qt |
| results.py | A run's KPIs and workbook | no |
| xlsx.py | Minimal .xlsx writer/reader (→ results.md) | no |
| experiments.py | Headless runs, scenarios, experiments over seeds | no |
| stats.py | Tests, ANOVA, regression, logit, smoothing (→ experiments.md) | no |
| scenario.py | Scenario Builder plan and report (→ ai_mode.md) | no |
| analysis.py | Junction LOS, signal diagnosis, health, resilience, fuel | no |
| emissions.py | Fuel and CO2, Biggs–Akçelik (→ analysis.md) | no |
| design_checks.py | Design audit, Green Book; connectivity and graph indices (→ network_checks.md) | no |
| weaving.py | Freeway weaving, HCM 2000 Ch 24 (→ weaving.md) | no |
| metro.py | The MRT's own network: rides (one change at an interchange), buildings' stations, line loads (→ metro.md) | no |
| accessibility.py | Cumulative / Hansen accessibility (→ network_checks.md) | no |
| summary.py | View Summary tab | Qt |
| clock.py | Run / Pause / Play / Stop clock | Qt |
| activity.py | Watch my edits: user actions to CSV (→ clock.md) | Qt |
| extensions.py | Extensions tab: loads, draws, isolates errors | Qt |
| history.py | Snapshot undo/redo | no |
| theme.py | Desktop workspace theme, native control states, floating glass bars and contrast pairs | Qt |
| main.py | Main window: tabs, tools, dialogs, file menu | Qt |
| ai_mode.py | AI Mode tab: chat, prompt gate, change approval | Qt |
| assistant.py | Grok, Gemini, Ollama back end; context budget; System 1 (→ ai_mode.md) | no |
| network_tools.py | The assistant's tools (→ ai_mode.md) | no |

## 3. Conventions

- **Metres, y down.** Positions are continuous `(x, y)` in metres, x right, y down; roads are polylines; grid
  numbers are only labels. A vehicle is lane-relative: its lane, turn or bay and the distance along it.
- **Lanes** are `LANE_WIDTH_M = 3.5`; a road is `2 × lanes_per_direction × 3.5 m` wide (`single_lane`: 3.5 m).
- **Driving side** (`"left"`/`"right"`) drives signals, stop lines, bays, turns and roundabouts; interchanges are
  mirrored by `Network.set_driving_side`.
- **Curvature** > 0 bends towards `(-uy, ux)`; radius = 1/|κ|; one value per polyline segment.
- **IDs:** grid junctions `"column,row"` (`1,1` bottom-left); added `N1`…; ends `1E`, `2N`, `1.5E` (E/W count from
  the bottom, N/S from the left); roads `"from-to"`; bus stops `BS1`…; institutions `IN1`…; interchange parts
  `TR1.1`, `DI1.x`, `SP1.x`. Never renamed.
- **Validate before changing:** a refused edit or build leaves the network untouched.
- **Grade separation:** roads crossing without a shared node pass over/under; `Road.overpass` marks the upper.

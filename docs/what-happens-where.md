# What happens where

A guide to ACMS from the screen inwards: each part of the window, what it does when you use it, which code does
it, and where files end up. For the code's own map (every module and its doc) see [ARCHITECTURE.md](../ARCHITECTURE.md);
for how to use each feature, the [README](../README.md).

## Starting

| You | What happens | Code |
|---|---|---|
| Double-click `launch.bat` | First time: finds Python 3.10+ (or installs 3.12 for you with winget), makes `.venv`, installs ACMS. Then opens the window with no console. | `launch.bat` → `traffic_simulator.main:main()` |
| The window opens | A default 3 × 4 grid is generated (left-hand, 50 km/h, 2 lanes), the theme applied, a crashed run's workbook recovered. | `main.py` (`MainWindow`), `network_generator.py`, `theme.py`, `results.recover` |

## The window

```
┌ File  Edit ─────────────────────────────── [Run ▶ ⏸ ■  00:00:00  1x  Seed 42  Export  Watch] ┐
│ ┌ Sidebar ──────────────┐ ┌ Map (or an OD matrix) ──────────────────── [map controls] ┐ │
│ │ ACMS  <open tab name> │ │                                                           │ │
│ │ [tab icons ........]  │ │      roads, junctions, signals, vehicles, background      │ │
│ │ the open tab's page   │ │                                                           │ │
│ └───────────────────────┘ └───────────────────────────────────────────────────────────┘ │
└ status bar: network summary · last action          AI model · mouse latitude/longitude ┘
```

| Area | What it does | Code |
|---|---|---|
| **File** menu | Open Project…, Save Project, Save Project As…: one validated JSON file (network, demand, vehicle types, background). | `main.py` (`_build_menu`), `network_io.py` |
| **Edit** menu | Undo (Ctrl+Z), Redo (Ctrl+Y / Ctrl+Shift+Z). Every finished action is one step. | `main.py` (`_commit`, `undo`), `history.py` |
| **Run bar** (top right) | Run starts a run from the network and demand as they are; Play/Pause; Stop ends it and writes its workbook; the clock; speed 1–60×; Seed (same seed, same run); Export a workbook now; Watch my edits. | `main.py` (`_build_sim_bar`), `clock.py`, `simulation.Runner`, `results.py`, `activity.py` |
| **Sidebar** | The tabs below; it widens to fit what its open page needs (never more than half again its first width). | `main.py` (`_fit_sidebar`), `widgets.py` |
| **Map** | The network to scale (metres), drawn over its background; clicks select and edit. Turns into the OD matrix while Demand Generator shows its grid. | `network_scene.py`, `background.py`, `gpu.py` |
| **Map controls** (map's top right) | Input Grid (Demand Generator only), 3D View, Hide Background (over an image or map), Power Saver, Fit Network, Reset View, Map Controls (mouse and keys). | `main.py`, `network_scene.NetworkView` |
| **Status bar** | Junctions, roads, ends, supply/demand; the last action; the AI model connected; over a map, the mouse's latitude and longitude. | `main.py` (`_network_summary`, `_show_pointer`) |

## The tabs

### Create Network

| Section | What it does | Code |
|---|---|---|
| **Generate Network** | Speed limit, lanes, lane changing and signal timing new roads get; Horizontal/Vertical roads and **Generate Network** replace the network with a grid. | `main.generate`, `network_generator.py` |
| **Own Network** | The background to draw over: a colour, an image or PDF set to scale, or the map. **Map**: Country, Place, Locate Work Zone, Confirm; the view then stays on the zone. | `background.py` (`BackgroundPanel`), `map_tiles.py` |
| **Universal Attributes** | Left- or right-hand driving for the whole network: lanes, turns, signals and interchanges mirror at once. | `main.set_driving_side`, `models.Network.set_driving_side` |

### Edit Network

| Page | What it does | Code |
|---|---|---|
| **Roads** | **Add Node**: click to place nodes one after another (each exactly where you click; a dashed line gives the distance to the next; right-click keeps them). **Add Edge**: a road between two junctions (Connection) or out to a boundary end (End); crossings become a roundabout or an overpass. Click anything to edit it below. | `main.canvas_clicked`, `main._build_edge`, `network_tools.build_edge`, `properties_panel.py` |
| **Selection editor** | A road: lanes, speed, direction, turn pockets, cycle lanes, footpaths. A junction: control (signalised, roundabout, uncontrolled) and signal timing; each phase named by the letter its heads show on the map (Phase A, B…). An end: its flows. Ctrl+click roads or junctions for slip roads and interchanges. | `properties_panel.py`, `interchanges.py`, `roundabouts.py`, `lane_graph.phase_letters` |
| **MRT** | Start deployment, click stop after stop (curves of 300 m or more), right-click to finish and set its service. | `main.toggle_metro`, `metro.py`, `models.py` |
| **Pedestrian** | Add Zebra, Add Overbridge on a road with footpaths both sides. | `main._place_crossing`, `walking.py` |

### Mode Behavior

The vehicle types (up to 8): rename, add, delete, colour, timetables. **Code:** `mode_behavior.py`, `vehicle.py`.

### Demand Generator

| Part | What it does | Code |
|---|---|---|
| Hourly / Daily | Whether the matrices hold veh/h or trips a day (with a day curve). | `od_matrix.py`, `daily_profile.py` |
| **Vehicle OD** | The OD matrix: veh/h between boundary ends; a cell's pair editor sets its arrival pattern, mode split, PHF and drop-offs. | `od_matrix.py`, `demand.py` |
| **Pedestrian OD**, **Deploy Pedestrian** | People walking between ends, or between points you place. | `od_matrix.py`, `walking.py` |
| **Institutions** | Place homes, schools, offices…: each building's catchment, mode split and height; Show Buildings. | `od_matrix.InstitutionPanel`, `institutions.py` |
| **MRT** | Passengers between stations: loads the lines, never changes their schedule. | `od_matrix.MetroMatrix`, `metro.py` |

### AI Mode

| Page | What it does | Code |
|---|---|---|
| **System 2** | Chat with Grok, Gemini or a local Ollama model; it reads the network with tools, proposes changes you approve, builds whole scenarios (Scenario Builder). Keys come from environment variables (`GROK_API_KEY`, `GEMINI_API_KEY`). | `ai_mode.py`, `assistant.py`, `network_tools.py`, `scenario.py` |
| **System 1** | A decision model (Jev with `JEV_API_KEY`, or Laya on Ollama) answering yes/no signal questions. | `ai_mode.py`, `signal_agent.py` |

### View Summary

Network, Demand, Results (the last run's figures) and AI Usage, rebuilt whenever the tab opens.
**Code:** `summary.py`.

### Extensions

Drop-in features, each switched on or off: Batch Run, Transit Signal Priority, Dynamic Emergency Vehicle Lane,
Dispatch Emergency Vehicle, Research Log. **Code:** `extensions.py` loads them from `extensions/`.

## On the map

| You do | What happens |
|---|---|
| Click a road, junction, end, stop or building | It is selected and its editor opens. Ctrl+click adds to a selection. |
| Drag on empty map | A selection box (left to right: wholly inside; right to left: touching). |
| Drag a selected junction | Moves it; its roads follow. |
| Right or middle drag, arrow keys | Pan. Wheel: zoom at the mouse. **F** fits the network, **R** resets the view. |
| Esc | Leaves the tool in use, or clears the selection. Del deletes what is selected. |

What the map draws, all to scale:
- **Roads:** lanes, centre lines and turn arrows.
- **Junctions:** each box shaped by the roads meeting there.
- **Signal heads:** each on the kerb with its countdown clock and its **phase letter** (A goes first: A A / B B with two
  phases, A B C D with four).
- **Vehicles during a run:** drawn from rear to front across a junction, so they never appear to cut across a lane.

**Code:** `network_scene.py` (`_draw_node`, `_draw_signal`, `VehicleLayer`).

## A run, start to finish

1. **Run** builds the lane graph from the network (lanes, turns, conflicts where turns cross or pass within a
   vehicle's width, signals and their phases). **Code:** `lane_graph.py`.
2. Each signal is timed by its method from the demand: Webster, TRANSYT, Synchro, SCATS, SCOOT, actuated,
   max-pressure, fixed or manual. **Code:** `signal_timing.py`.
3. A physics thread steps the vehicles in real time at the chosen speed:
   - Krauss car following and LC2013 lane changing.
   - Gap acceptance, with yielding where turns conflict.
   - Bus stops, walkers and closures.

   **Code:** `simulation.py` (`Simulation`, `Runner`), `vehicle.py`, `walking.py`.
4. Every 5 simulated seconds a frame is sampled: flows, speeds, queues, delay and trips.
   **Code:** `results.Recorder`.
5. The map draws each frame without ever holding the physics up. **Code:** `network_scene.py`, `gpu.py`.
6. When the run ends (Stop, a new Run, an edit, closing, a failure), its workbook is written. **Export** writes
   one at any time while it goes on. **Code:** `results.py`, `xlsx.py`.

## Where files go

| What | Where |
|---|---|
| Projects | Wherever you save them: one `.json` file (File → Save Project). |
| Run workbooks | `Documents\ACMS results\ACMS results <date time> seed <n>.xlsx`, with a `.geojson` of the roads beside it. |
| A run in progress | `%TEMP%\ACMS runs\run <date time> …\` (recovered into a workbook at the next start after a crash). |
| Batch runs, experiments | `Documents\ACMS results\<experiment> runs\`. |
| Research Log sessions | `Documents\ACMS results\ACMS research log <date time> <agent>\`, and `sessions.csv` there. |
| Watch my edits | `Documents\ACMS results\ACMS activity <date time>\activity.csv`. |
| Map tiles | The user's cache folder, `osm-tiles\` (OpenStreetMap, fetched once). |
| The app itself | This folder: `src\traffic_simulator\` (code), `extensions\`, `.venv\` (made by `launch.bat`). |

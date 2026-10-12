# CONTEXT: change the app (stages 1 and 2)

Layer 2. You are here to fix a bug or change behaviour in `src/traffic_simulator/`. Back to routing:
[../../CONTEXT.md](../../CONTEXT.md).

## Inputs

| Layer | File | Read |
|---|---|---|
| 3 | `AGENTS.md` | § Rules (all of it, it's short) |
| 3 | `ARCHITECTURE.md` | § Module map: find the one module that owns your task |
| 3 | `docs/architecture/<module>.md` | the whole file for that module |
| 3 | `references/reverse_engineering/` | only for `vehicle.py` / `simulation.py`: the SUMO note on that behaviour |
| 3 | `docs/architecture/sources.md` | the row for the method you touch (where its formula comes from) |
| 4 | the module itself | the function you change, and every caller (`grep -n "function_name" -r src extensions tools`) |

## Which module? (the usual ones)

| Task is about… | Module |
|---|---|
| network data, editing, validation | `models.py` (validate) and `network_io.py` (save / load) |
| shapes, lengths, curves (metres, y down) | `geometry.py` |
| interchanges, slip roads, curve speed | `interchanges.py` |
| roundabout size | `roundabouts.py` |
| signal timing (Webster, TRANSYT, SCATS…) | `signal_timing.py` |
| how a vehicle drives (Krauss) | `vehicle.py` |
| lanes, turns, routes, junctions, insertion, the run thread (`Runner`) | `simulation.py` |
| KPIs and the run's workbook | `results.py`, `xlsx.py` |
| headless runs, experiments, statistics | `experiments.py`, `stats.py`, `analysis.py` |
| what the assistant can read / change | `network_tools.py` (tools), `assistant.py` (models), `ai_mode.py` (chat panel) |
| the assistant's context window, trimming, timeouts | `assistant.py` (`fit`, `STALL_TIMEOUT_S`), `network_tools.read_source` (`find`), `ARCHITECTURE.md` §0 |
| turn pockets | `models.Road.pockets` (validate), `simulation.Graph._add_links` / `_add_turns`, `network_scene._draw_pocket` |
| random streams, trip times, collisions, lateral filtering | `simulation.Simulation` (`_rng`, `_exited`, `_crossing_collisions`, `_sidestep`) |
| the main window, menus, Run / Stop / Export | `main.py` |
| drawing the map | `network_scene.py`, `gpu.py` |
| colours | `theme.py` (tokens only) |

## Process

1. Reproduce the problem first: a few lines with `.venv\Scripts\python.exe -c "..."` or a test that fails.
2. Find the root cause. Grep every caller of the function you will change; fix it once where they all meet.
3. Change the smallest amount of code. Match the file's style: its naming, comment voice and density.
4. Keep the rules: metres not pixels; no Qt import in core modules (the list is in `AGENTS.md`);
   check everything before changing anything (a refused edit leaves the network untouched);
   colours only from `theme.py`; no new packages.
5. Go to stage 3: [../../tests/CONTEXT.md](../../tests/CONTEXT.md).

## Outputs

- The changed module(s). Nothing else in this stage: tests and docs are stages 3 and 4.

## Done when

- You can say in one sentence what was wrong and why the change fixes it.
- `.venv\Scripts\python.exe -c "import traffic_simulator.<module>"` imports cleanly.

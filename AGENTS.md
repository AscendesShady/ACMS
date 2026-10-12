# AGENTS.md

Instructions for coding agents working on ACMS (Agentic Context-Aware Mobility
System): a PySide6 road-network editor with a SUMO-style microscopic traffic simulation.

This file is Layer 0 (where you are, the rules). Next open [CONTEXT.md](CONTEXT.md), Layer 1: it routes
your task to the one folder whose `CONTEXT.md` (Layer 2) says what to read, the exact commands to run and
what to write: the Interpretable Context Methodology layout. Read only what those files list.

[ARCHITECTURE.md](ARCHITECTURE.md) is the map of every module, the conventions and what is still a
placeholder; each module's detail, the sources of each engineering method and the tests are in
`docs/architecture/`.

## Commands

Windows, PowerShell, Python 3.12 in `.venv`.

```powershell
.venv\Scripts\python.exe -m pip install -e .       # setup
.venv\Scripts\python.exe run.py                     # the editor
.venv\Scripts\python.exe tools\run_test.py tests\test_simulation.py webster   # only matching tests, timed
.venv\Scripts\python.exe tests\test_geometry.py     # one test file (prints "ok")
.venv\Scripts\python.exe run_monitor.py             # every test, live, with Run Again (a person's tool)
```

## Budget: tokens and waiting

- **Read narrow.** Route AGENTS.md → CONTEXT.md → one folder's CONTEXT.md → the one module doc. Grep before you
  read; read the range you found (offset/limit), never a whole `docs/architecture/*.md` over ~300 lines (sources.md,
  tests.md, ai_mode.md, simulation.md are 40–66k characters). Edit docs by an anchor you grepped.
- **Wait less.** Test the functions you touched first (`tools/run_test.py <file> <words>`), then whole files at the
  end. The slow files (`tests/CONTEXT.md`) go in the background with a timeout; don't poll them.

## Rules

- **Dependencies:** Python ≥ 3.10, PySide6 and the standard library only. Don't add packages.
- **Vector geometry in metres.** Positions are continuous `(x, y)` in metres, y pointing down;
  roads are polylines. Build on `geometry.py`. Nothing is raster or cell-based, and no physics
  runs in screen or pixel coordinates. Grid row/column numbers are only labels. Vehicle state is
  lane-relative: the lane or turn plus the distance along it.
- **No Qt in the core.** `models.py`, `geometry.py`, `simulation.py`, `vehicle.py`,
  `roundabouts.py`, `interchanges.py`, `daily_profile.py`, `institutions.py`, `background_network.py`, `network_tools.py`, `results.py`, `analysis.py`, `emissions.py`, `design_checks.py`, `accessibility.py`, `xlsx.py`, `experiments.py`, `stats.py` and `scenario.py` don't
  import Qt.
- **Real-time physics.** `simulation.Runner` runs the physics on its own thread, paced by the
  wall clock, and computes every step. It never waits for drawing: the view skips frames
  instead. Keep per-frame UI work small. The immutable `Frame` must stay readable by the
  assistant (`read_traffic`).
- **Vehicle behaviour comes from `references/reverse_engineering/`** (study notes on SUMO; they contain no
  SUMO code). Read the relevant note before you change `vehicle.py` or `simulation.py`. Where a
  note has no formula, use the published model it names (Krauss, LC2013) and cite it in
  docs/architecture/sources.md.
- **Validate before changing.** Edits and builders check everything first. A refused edit
  leaves the network untouched.
- **Theme:** white text on grey. Use the tokens in `theme.py` and never hard-code a colour.
  Add every new text/background pair to `READABLE_PAIRS` (`test_theme.py` enforces ≥ 4.5:1),
  and call `apply_theme(app)` in every new window. The user runs Windows in dark mode, and
  offscreen screenshots always render light, so they won't catch a theme bug.
- **Controls are minimal.** New controls start as small icon buttons (`glyph_icon` in
  `main.py`, Segoe Fluent/MDL2 glyphs) with the name as the tooltip, placed somewhere
  unobtrusive. **Tooltips are five words at most** (an icon button's is its name;
  `tests/test_tooltips.py` checks the window): no explanations on hover. Follow
  [references/ui-ux-design-rulebook.md](references/ui-ux-design-rulebook.md).
- **Placeholders stay placeholders.** System 1 beyond the signal agent is a placeholder. Keep them clearly labelled, with real,
  validated inputs, and don't invent behaviour for them unless asked.
- **Naming and IDs** follow ARCHITECTURE.md §3 (`"column,row"` junctions, `N1…`, `1E`/`1.5E`
  ends, `"from-to"` roads).

## Tests

Tests are plain `assert` functions in `tests/test_*.py`. There's no pytest. Each file ends
with a `__main__` loop that runs its `test_*` functions and prints `ok`. Qt tests set
`os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")` before any PySide6 import. New logic
needs a test next to the existing ones. While working, run just the tests you touched with
`tools/run_test.py`; before you finish, run the affected files whole (or `run_monitor.py`).

## Docs are part of the change

The in-app assistant (AI Mode → System 2) reads the `ARCHITECTURE.md` index as its context (and
`README.md`: up front for a model without tools, through `read_source` otherwise). When behaviour changes, update in the same change: the module's file in
`docs/architecture/`, `sources.md`, `not_built.md`, `tests.md`, and the ARCHITECTURE.md index row
(and `capabilities.md`) if a capability or placeholder changed.

## Extensions

Drop-in features live in `extensions/*.py`. Their hooks are `draw`, `panel`,
`signal_override`, `algorithm` and `log` (each AI Mode step), plus the optional `NAME`, `DESCRIPTION` and `ORDER`
(docs/architecture/extensions.md, extensions.py). An extension gets a deep copy of the network, does nothing
until it's ticked, and is switched off when it raises. Keep it that way.

# AGENTS.md (Layer 0: where am I?)

**ACMS** (Agentic Context-Aware Mobility System): a PySide6 road-network editor with a SUMO-style microscopic
traffic simulation and an in-app AI assistant. Windows, PowerShell, Python 3.12 in `.venv`
(`.venv\Scripts\python.exe -m pip install -e .` once; `.venv\Scripts\python.exe run.py` opens it).

**Next: [CONTEXT.md](CONTEXT.md)** (Layer 1). It routes your task to numbered stages under `stages/`. Do them in
order; at each, load only the files its Inputs table lists and write what its Outputs say.

| Layer | Answers | Files |
|---|---|---|
| 0 | Where am I, what always holds? | this file (`CLAUDE.md` points here) |
| 1 | Which stages? | `CONTEXT.md` |
| 2 | What do I do in this stage? | `stages/<pipeline>/<NN_stage>/CONTEXT.md` |
| 3 | What rules and facts apply? (stable; read as constraints) | `ARCHITECTURE.md`, `docs/architecture/`, `references/` |
| 4 | What am I working on? (changes each run; process as input) | `src/`, `tests/`, `extensions/`, `tools/`, `research/`, `reports/`, `stages/*/*/output/` |

## Always

- Python ≥ 3.10, PySide6 and the standard library only; no new packages.
- Positions are `(x, y)` metres, y down; roads are polylines; a vehicle is its lane or turn plus the distance along.
- A module marked Qt "no" in `ARCHITECTURE.md` §2 never imports Qt. The physics (`simulation.Runner`) never waits
  for drawing.
- Validate before changing: a refused edit leaves the network untouched.
- Vehicle behaviour follows `references/reverse_engineering/` or a cited published model.
- Colours only from `theme.py`'s tokens; tooltips five words at most; placeholders stay labelled.
- IDs as in `ARCHITECTURE.md` §3. Docs, tests and code change together.

## Budget

- Grep, then read only the range found. Never load a whole doc over ~300 lines: `sources.md`, `tests.md`,
  `ai_mode.md` and `simulation.md` run to 40–66k characters.
- Test what you touched first (`tools\run_test.py <file> <words>`). Slow test files run in the background.

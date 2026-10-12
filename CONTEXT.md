# CONTEXT: where do I go?

Layer 1 of this repository's Interpretable Context Methodology (ICM) layout. Layer 0 is `AGENTS.md`
(what ACMS is, the rules); read it first. This file routes your task to **one** folder. That folder's
`CONTEXT.md` lists what to read, what to do (with exact commands) and what to write. Read only what it lists.

**Budget.** Search before you read and read only the lines found; never load a whole doc over ~300 lines; test the
functions you touched with `tools/run_test.py` before whole files; run slow files in the background
(`AGENTS.md` § Budget). The in-app assistant follows the same route (`ARCHITECTURE.md` §0).

## 1. Route your task

| I want to… | Open | It sends you to |
|---|---|---|
| Fix a bug or change what the app does | [src/traffic_simulator/CONTEXT.md](src/traffic_simulator/CONTEXT.md) | the module's `docs/architecture/<module>.md` |
| Add or fix a test, or run the tests | [tests/CONTEXT.md](tests/CONTEXT.md) | |
| Add a drop-in feature (an extension) | [extensions/CONTEXT.md](extensions/CONTEXT.md) | `docs/architecture/extensions.md` |
| Update the docs after a change | [docs/CONTEXT.md](docs/CONTEXT.md) | |
| Read or act on an audit, campaign or agent-performance record | [reports/CONTEXT.md](reports/CONTEXT.md) | the report's folder |
| Run the prompt stress-test campaign | [tools/CONTEXT.md](tools/CONTEXT.md) | `docs/architecture/prompt_campaign.md` |
| Mine the books and papers for tool ideas | [research/CONTEXT.md](research/CONTEXT.md) | |
| Look something up (UI rules, SUMO notes, books, the theory reference) | [references/CONTEXT.md](references/CONTEXT.md) | |
| Bring the simulation in line with the theory reference | [reports/CONTEXT.md](reports/CONTEXT.md) | `reports/theory_gap_2026-10-11/gap_report.md` |
| Change how the in-app assistant fits its context window or times out | [src/traffic_simulator/CONTEXT.md](src/traffic_simulator/CONTEXT.md) | `docs/architecture/ai_mode.md` § Context budget (`assistant.fit`, `STALL_TIMEOUT_S`, `read_source` find) |

## 2. A code change is four stages, in this order

| Stage | Contract | Done when |
|---|---|---|
| 1. Understand | [src/traffic_simulator/CONTEXT.md](src/traffic_simulator/CONTEXT.md) § Inputs | you can name the file and function to change |
| 2. Change | [src/traffic_simulator/CONTEXT.md](src/traffic_simulator/CONTEXT.md) § Process | the code does it, validated before it changes anything |
| 3. Test | [tests/CONTEXT.md](tests/CONTEXT.md) | a new test fails without the change; the touched tests pass in `tools/run_test.py`, then every affected file prints `ok` |
| 4. Document | [docs/CONTEXT.md](docs/CONTEXT.md) | the module doc, `tests.md` and (if a method changed) `sources.md` say what the code now does |

Never skip stage 3 or 4. If a stage fails, go back one stage; don't start the next.

## 3. The layers in this repository

| Layer | Question it answers | Files | Edit? |
|---|---|---|---|
| 0 | Where am I, what are the rules? | `AGENTS.md` (`CLAUDE.md` points to it) | rarely |
| 1 | Where do I go? | this file | when a folder is added |
| 2 | What do I do here? | `<folder>/CONTEXT.md` | when a process changes |
| 3 | What rules and facts apply? (stable) | `ARCHITECTURE.md`, `docs/architecture/`, `references/`, `config/` | only in stage 4 |
| 4 | What am I working on? (changes every task) | `src/`, `tests/`, `extensions/`, `tools/`, `reports/`, `research/ledger/` | yes |

## 4. Commands (Windows PowerShell, from the repository root)

```powershell
.venv\Scripts\python.exe -m pip install -e .                 # once: set up
.venv\Scripts\python.exe run.py                              # open the editor
.venv\Scripts\python.exe tools\run_test.py tests\test_simulation.py webster   # only matching tests, timed (quick)
.venv\Scripts\python.exe tests\test_geometry.py              # one test file: prints "ok"
.venv\Scripts\python.exe run_monitor.py                      # every test, in a window
.venv\Scripts\python.exe tools\run_test.py --all -j 4         # every test file, 4 at a time, no window
```

## 5. Folder map

```
AGENTS.md  CONTEXT.md        Layers 0 and 1
ARCHITECTURE.md              Layer 3: map of every module (the in-app assistant reads it too)
README.md                    Setup and use, for people
run.py  run_monitor.py       Open the editor; the test monitor window
config/defaults.json         Default grid settings for a new network
src/traffic_simulator/       The app's modules
tests/                       Plain assert tests, one file per area
extensions/                  Drop-in features (Extensions tab)
tools/                       Scripts run by hand: the prompt campaign, run_test.py (quick, timed tests)
docs/                        Module docs (docs/architecture/), statistics catalogue
references/                  Reference material: UI rulebook, SUMO notes, books, papers, theory, ICM paper (this PC only,
                             but the UI rulebook is in git)
reports/                     Records: audits, campaigns, agent performance, theory gap (this PC only, not in git)
research/                    The book-mining pipeline: PDF text, idea ledger, tool shortlist
```

# CONTEXT: test (stage 3)

Layer 2. You are here to add a test for a change, or to run the tests. Back to routing:
[../CONTEXT.md](../CONTEXT.md).

## Inputs

| Layer | File | Read |
|---|---|---|
| 3 | `AGENTS.md` | § Tests |
| 3 | `docs/architecture/tests.md` | the paragraph for the test file you touch |
| 4 | `tests/test_<area>.py` | the tests next to where yours goes; copy their shape |
| 4 | the change from stage 2 | what must now be true |

## Which test file?

Name the file after the module: `models.py` → `test_models.py`, `simulation.py` → `test_simulation.py`.
Window and menu behaviour: `test_project.py`, `test_selection.py`. Tooltips: `test_tooltips.py`.
Not run by the monitor (needs Ollama): `eval_commands.py`.

## Process

1. Write one plain function `def test_<what_it_checks>():` with `assert` lines. No pytest, no fixtures.
2. A test that opens Qt starts with `os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")` before any PySide6 import
   (the file's top usually already does it).
3. Check it fails without your change (undo the change, run, see the assert fail, redo the change).
4. Run only the tests you touched first (a few seconds, each timed):
   ```powershell
   .venv\Scripts\python.exe tools\run_test.py tests\test_<area>.py <words in the test names>
   ```
   It prints each test's seconds and PASS/FAIL, then `ok`. A failure: read the traceback, go back to stage 2.
5. At the end, run each affected file whole (`.venv\Scripts\python.exe tests\test_<area>.py`, prints `ok`). Run a slow
   file (table below) in the background with a timeout and wait for it to finish; don't poll it. Everything, before a
   big change is finished, every file 4 at a time, slowest first (about 15 min one by one):
   ```powershell
   .venv\Scripts\python.exe tools\run_test.py --all -j 4
   ```
   It prints each file's seconds and `ok` or `FAILED` (with that file's last lines), then `N of N files ok`.
6. Go to stage 4: [../docs/CONTEXT.md](../docs/CONTEXT.md).

## Slow tests (seconds of tests, measured 2026-10-12, four files at once)

| File | s | Slowest tests (s) |
|---|---|---|
| `test_experiments.py` | 275 | the window checking a sized demand (30), conditions/factorials (28), emergency vehicles + DEVL batch (37) |
| `test_simulation.py` | 91 | mirror image on the other side (30), every signal method (15) |
| `test_scenario.py` | 68 | the window builds a described scenario (65) |
| `test_ai_mode.py` | 52 | a batch comparing System 1 and 2 (24) |
| `test_create_tab.py`, `test_agent_tools.py`, `test_background.py` | 20–35 each | |

Every other file takes under 20 s. Import time is extra (a few seconds; Qt files more).

## Outputs

- The new or changed `test_*` function, in the file next to the existing ones (the file's `__main__` loop runs it).

## Done when

- Every affected file prints `ok`, and the new test failed before the change.

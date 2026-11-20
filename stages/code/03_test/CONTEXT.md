# code / 03_test

Prove the change: a test that fails without it, and nothing else broken.

## Inputs

| Layer | File | Read |
|---|---|---|
| 4 | `stages/code/01_understand/output/plan.md` | the test it names |
| 4 | `tests/test_<area>.py` | the tests beside where yours goes; copy their shape |
| 3 | `docs/architecture/tests.md` | grep the test file's paragraph |

## Process

1. Write `def test_<what_it_checks>():` with plain `assert`s (no pytest). A Qt test sets
   `os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")` before any PySide6 import.
2. Undo the change, see the test fail, redo it.
3. Run the touched tests, keeping the result:
   `.venv\Scripts\python.exe tools\run_test.py tests\test_<area>.py <words> > stages\code\03_test\output\results.txt`
4. At the end, each affected file whole (`.venv\Scripts\python.exe tests\test_<area>.py` prints `ok`); after a big
   change, `.venv\Scripts\python.exe tools\run_test.py --all -j 4` (all files in ~9 min: as long as
   `test_experiments`, 8 min alone). Slow files, to run in the background (their times in a full run):
   `test_scenario` and `test_selection` ~3 min, `test_create_tab` and `test_simulation` ~2, `test_ai_mode` 1.5,
   `test_background`, `test_project`, `test_prompt_campaign`, `test_research_log` and `test_extensions` ~1; every
   other file under 40 s. `run_test.py`'s `SLOWEST_FIRST` starts the first six first.

## Outputs

- The new test in `tests/`; `stages/code/03_test/output/results.txt`.

## Done when

- The new test failed before the change, and every affected file prints `ok`. A failure: back to 02.
  Next: `../04_document/CONTEXT.md`.

# run_monitor.py

Runs every `test_*` function in `tests/test_*.py` in a child process and streams
JSON-lines progress. It shows a progress bar, each test's status and time, and the
traceback of a selected failure. **Run Again** re-tests the latest code.

The worker always runs headless (`QT_QPA_PLATFORM=offscreen`, whatever the monitor was started with, so no
test window pops up and no test waits on a real one) with `tests/` on the
import path, as when a file runs on its own (tests import helpers from each other, e.g. `test_results`).
A test file that fails to load is listed as a failed "(loading)" entry with its traceback, and the other
files still run.

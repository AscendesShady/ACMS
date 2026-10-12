"""Run some of a test file's test_* functions, each timed: the quick check while working, before the whole file.

    .venv\\Scripts\\python.exe tools\\run_test.py tests\\test_simulation.py              every test, timed
    .venv\\Scripts\\python.exe tools\\run_test.py tests\\test_simulation.py webster ring  only names holding a word
    .venv\\Scripts\\python.exe tools\\run_test.py tests\\test_simulation.py --list       names only, nothing run

Prints each test's seconds and PASS or FAIL (with its traceback), then "ok" when all passed; exits 1 on a failure.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import time
import traceback
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # as the Qt tests do, before anything imports PySide6


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    path, words = Path(argv[0]).resolve(), [word.lower() for word in argv[1:] if word != "--list"]
    sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    tests = [(name, check) for name, check in vars(module).items()
             if name.startswith("test_") and callable(check) and (not words or any(w in name.lower() for w in words))]
    if "--list" in argv:
        print("\n".join(name for name, _ in tests))
        return 0
    failed = 0
    for name, check in tests:
        started = time.monotonic()
        try:
            check()
            verdict = "PASS"
        except Exception:  # noqa: BLE001 - reported, and the next test still runs
            verdict, failed = "FAIL", failed + 1
            traceback.print_exc()
        print(f"{time.monotonic() - started:7.1f} s  {verdict}  {name}", flush=True)
    if not tests:
        print("No test name holds any of: " + " ".join(words))
        return 1
    print("ok" if not failed else f"{failed} of {len(tests)} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

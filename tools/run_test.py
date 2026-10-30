"""Run some of a test file's test_* functions, each timed: the quick check while working, before the whole file.

    .venv\\Scripts\\python.exe tools\\run_test.py tests\\test_simulation.py              every test, timed
    .venv\\Scripts\\python.exe tools\\run_test.py tests\\test_simulation.py webster ring  only names holding a word
    .venv\\Scripts\\python.exe tools\\run_test.py tests\\test_simulation.py --list       names only, nothing run
    .venv\\Scripts\\python.exe tools\\run_test.py --all [-j 4]                          every file, 4 at a time

Prints each test's seconds and PASS or FAIL (with its traceback), then "ok" when all passed; exits 1 on a failure.
--all runs each tests/test_*.py file in its own process, `-j` at once (default 4), slowest first, and prints each
file's seconds and ok or FAILED with its output's last lines: the whole suite in about a third of the time.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import time
import traceback
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # as the Qt tests do, before anything imports PySide6


SLOWEST_FIRST = ("test_experiments", "test_simulation", "test_scenario", "test_ai_mode")  # stages/code/03_test's list


def run_all(jobs: int) -> int:
    """Every test file in its own process, `jobs` at once, the slowest started first so they don't finish last."""
    import subprocess
    from concurrent.futures import ThreadPoolExecutor

    tests = Path(__file__).resolve().parents[1] / "tests"
    files = sorted(tests.glob("test_*.py"), key=lambda p: (p.stem not in SLOWEST_FIRST, SLOWEST_FIRST.index(p.stem)
                                                            if p.stem in SLOWEST_FIRST else 0, p.name))

    def one(path: Path):
        started = time.monotonic()
        done = subprocess.run([sys.executable, str(path)], capture_output=True, text=True, encoding="utf-8",
                              errors="replace", env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        passed = done.returncode == 0 and done.stdout.rstrip().endswith("ok")
        print(f"{time.monotonic() - started:7.1f} s  {'ok' if passed else 'FAILED'}  {path.name}", flush=True)
        return path.name, passed, (done.stdout + done.stderr).strip().splitlines()[-15:]

    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        outcomes = list(pool.map(one, files))
    failed = [(name, tail) for name, passed, tail in outcomes if not passed]
    for name, tail in failed:
        print(f"\n--- {name} ---\n" + "\n".join(tail))
    print(f"{len(files) - len(failed)} of {len(files)} files ok in {time.monotonic() - started:.0f} s"
          + ("" if failed else "\nok"))
    return 1 if failed else 0


def main(argv: list[str]) -> int:
    sys.stdout.reconfigure(errors="backslashreplace")  # a failure's '→' can't crash the report on a cp1252 console
    if not argv:
        print(__doc__)
        return 2
    if argv[0] == "--all":
        return run_all(int(argv[argv.index("-j") + 1]) if "-j" in argv else 4)
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
        except (Exception, SystemExit):  # noqa: BLE001 - reported (an exit too), and the next test still runs
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

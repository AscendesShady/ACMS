# code / 01_understand

Find what to change and why, before changing anything.

## Inputs

| Layer | File | Read |
|---|---|---|
| 4 | the task: the user's request, or a finding in `reports/` | all of it |
| 3 | `ARCHITECTURE.md` | §2 module map: the one module that owns the task |
| 3 | `docs/architecture/<module>.md` | grep the function or behaviour; read that paragraph |
| 3 | `references/reverse_engineering/` | only for `vehicle.py`/`simulation.py`: the note on that behaviour |
| 4 | `src/traffic_simulator/<module>.py` | the function, and every caller (`grep -rn "name(" src extensions tools`) |

## Process

1. Reproduce it: a failing `.venv\Scripts\python.exe -c "..."` or test. A report finding: re-run its probe first;
   code moves, so its line numbers may not hold.
2. Find the root cause: the one place every caller passes through.
3. Write the plan (below). Keep it to the facts you checked.

## Outputs

- `stages/code/01_understand/output/plan.md`, at most 15 lines: what is wrong; root cause (`file:function`); the
  change; the test that will fail without it (`tests/test_<area>.py`); the docs to update.

## Done when

- The plan names the file and function to change and the test to add. Next: `../02_change/CONTEXT.md`.

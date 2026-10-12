# CONTEXT: write an extension

Layer 2. You are here to add or change a drop-in feature for the Extensions tab. Back to routing:
[../CONTEXT.md](../CONTEXT.md).

## Inputs

| Layer | File | Read |
|---|---|---|
| 3 | `docs/architecture/extensions.md` | the hooks and their contracts |
| 3 | `src/traffic_simulator/extensions.py` | the module docstring (the hook list) |
| 4 | the closest existing extension | copy its shape (below) |

| Extension | Hooks it shows | Copy it for |
|---|---|---|
| `research_log.py` | `log`, `panel` | recording each AI Mode step |
| `transit_signal_priority.py` | `signal_override`, `draw`, `panel`, `report` | changing signals during a run |
| `dynamic_emergency_lane.py` | `signal_override`, `panel` | lane-by-lane signal states |
| `batch_run.py` | `panel`, `algorithm` | running many headless runs |
| `dispatch_emergency.py` | `panel` | a panel that acts on the run |
| `campaign_apply.py` | (opt-in driver update) | the prompt campaign only |

## Process

1. Make `extensions/<name>.py` with `NAME`, `DESCRIPTION` and optionally `ORDER`, plus only the hooks you need
   (`draw`, `panel`, `signal_override`, `algorithm`, `log`).
2. Keep the contract: it gets a deep copy of the network, does nothing until ticked, and is switched off when it
   raises. Never let an extension change the network directly.
3. Controls are small icon buttons with a tooltip of five words at most (`AGENTS.md` § Rules, theme tokens only).
4. Test it in `tests/test_extensions.py` (stage 3: [../tests/CONTEXT.md](../tests/CONTEXT.md)).
5. Document it in `docs/architecture/extensions.md` (stage 4: [../docs/CONTEXT.md](../docs/CONTEXT.md)).

## Outputs

- `extensions/<name>.py`, its test, its paragraph in `docs/architecture/extensions.md`.

## Done when

- `.venv\Scripts\python.exe tests\test_extensions.py` and `tests\test_tooltips.py` print `ok`.

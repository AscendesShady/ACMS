# CONTEXT.md (Layer 1: which stages?)

| Task | Stages, in order |
|---|---|
| Fix a bug, change behaviour, add an extension, act on a report | `stages/code/` 01_understand → 02_change → 03_test → 04_document |
| Only run the tests | `stages/code/03_test/` |
| Stress-test the in-app assistant | `stages/campaign/` 01_check → 02_run → 03_evaluate |
| Mine the books for tool ideas | `stages/research/` 01_extract → 02_read → 03_rank |
| Look up a source, rule or note | `references/CONTEXT.md` |

Each stage writes to the place its Outputs name, usually its own `output/`. A person may read or edit that
before the next stage, which picks up whatever is there. If a stage fails, go back one stage.

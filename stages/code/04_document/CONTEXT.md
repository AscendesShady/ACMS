# code / 04_document

Make the docs say what the code now does. The in-app assistant reads them, so stale docs make it answer wrongly.

## Inputs

| Layer | File | Read |
|---|---|---|
| 4 | `stages/code/01_understand/output/plan.md`, `stages/code/03_test/output/results.txt` | all |
| 3 | the doc files below that apply | grep the paragraph; edit by that anchor |

| If you… | Update |
|---|---|
| changed what a module does | `docs/architecture/<module>.md` |
| added or changed a test | `docs/architecture/tests.md`, that file's paragraph |
| used or changed a method or formula | `docs/architecture/sources.md`, its row, with the citation |
| built or removed a placeholder | `docs/architecture/not_built.md` |
| added a capability or module | `docs/architecture/capabilities.md` and its `ARCHITECTURE.md` row (≤150 lines) |
| changed how people use it | `README.md` |

## Process

1. Write what the code does now, in the file's voice; no history of the change.
2. `.venv\Scripts\python.exe tests\test_docs.py` must print `ok`.

## Outputs

- The updated docs; `stages/code/04_document/output/change_note.md`: 3–6 lines (what changed, why, the tests).
  It is the commit message if the user asks for a commit.

## Done when

- `test_docs.py` prints `ok`, and a reader of the module doc would predict the new behaviour.

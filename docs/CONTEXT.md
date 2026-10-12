# CONTEXT: document (stage 4)

Layer 2. You are here because behaviour changed and the docs must say so. The in-app assistant reads
`ARCHITECTURE.md` and these files, so a stale doc makes it answer wrongly. Back to routing:
[../CONTEXT.md](../CONTEXT.md).

## Inputs

| Layer | File | Read |
|---|---|---|
| 3 | `AGENTS.md` | § Docs are part of the change |
| 4 | the code and test change from stages 2 and 3 | what is now true |
| 3 | `docs/architecture/<module>.md` | the paragraph about what you changed |

## Process: update each that applies

| If you… | Update |
|---|---|
| changed what a module does | `docs/architecture/<module>.md` (the module map in `ARCHITECTURE.md` names its file) |
| added or changed a test | `docs/architecture/tests.md`, the paragraph for that test file |
| used or changed an engineering method or formula | `docs/architecture/sources.md`, its row (cite the book or paper) |
| built or removed a placeholder | `docs/architecture/not_built.md` |
| added a capability | `docs/architecture/capabilities.md` and the `ARCHITECTURE.md` §1 row |
| added a module | `ARCHITECTURE.md` module map row and a new `docs/architecture/<module>.md` |
| changed an extension | `docs/architecture/extensions.md` |

Write what the code does now, in the same voice as the file; don't write a history of the change.
Then check the docs still hold together:

```powershell
.venv\Scripts\python.exe tests\test_docs.py
```

## Outputs

- The updated files under `docs/architecture/` (and `ARCHITECTURE.md` if listed above).

## Done when

- `tests\test_docs.py` prints `ok`, and a reader of the module doc would predict the new behaviour.

## Other files here

- `statistics_catalogue.md`: the statistics the tools use and where each comes from (Layer 3).

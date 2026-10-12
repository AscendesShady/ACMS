# research / 02_read

Read each chunk and record every idea that could become a tool for the assistant.

## Inputs

| Layer | File | Read |
|---|---|---|
| 4 | `references/pdf_text/<source>/` | one chunk at a time: the next one `todo` lists |
| 3 | `research/acms_tools.md` | the tools ACMS has (rebuild first: `.venv\Scripts\python.exe research\ledger.py existing`) |
| 3 | `docs/architecture/capabilities.md`, `not_built.md` | grep before calling an idea new |
| 4 | `research/ledger/<source>.chunks.csv`, `<source>.ideas.csv` | what is already read and found |

## Process

```powershell
.venv\Scripts\python.exe research\ledger.py todo "<source>"            # chunks not read yet
.venv\Scripts\python.exe research\ledger.py help                       # the idea fields
.venv\Scripts\python.exe research\ledger.py idea "<source>" '<json>'    # each idea found
.venv\Scripts\python.exe research\ledger.py done "<source>" <chunk> "what it covers"
```

## Outputs

- `research/ledger/<source>.ideas.csv` and `.chunks.csv`: the truth for the next stage.
- A statistical method also goes in `research/statistics_catalogue.md` (method, problem, pages, in ACMS or not).

## Done when

- `.venv\Scripts\python.exe research\ledger.py status` shows every chunk of your sources read.
  Next: `../03_rank/CONTEXT.md`.

# CONTEXT: mine the books and papers for tool ideas

Layer 2. A three-stage pipeline that turns the reference PDFs into a ranked list of tools for the assistant.
Back to routing: [../CONTEXT.md](../CONTEXT.md). Run every command from the repository root, in Git Bash for `.sh`.

## Inputs

| Layer | File | Read |
|---|---|---|
| 3 | `references/transportation_engineering_books/*.pdf`, `references/transportation_engineering_papers/*.pdf` | the sources (not in git) |
| 3 | `research/acms_tools.md` | the assistant's tools today (so an idea isn't one we already have); rebuild it first with `.venv\Scripts\python.exe research\ledger.py existing` |
| 4 | `research/ledger/<source>.chunks.csv`, `<source>.ideas.csv` | what has been read and found so far |

## Process

1. **Extract** each PDF to text in 25-page chunks (skips chunks already done):
   ```sh
   sh research/extract.sh
   ```
   Output: `research/pdf_text/<source>/pNNNN-NNNN.txt`.
2. **Read and record**, one chunk at a time:
   ```powershell
   .venv\Scripts\python.exe research\ledger.py todo "<source>"          # chunks not read yet
   .venv\Scripts\python.exe research\ledger.py idea "<source>" '<json>'  # an idea found (fields: see ledger.py)
   .venv\Scripts\python.exe research\ledger.py done "<source>" <chunk> "what it covers"
   .venv\Scripts\python.exe research\ledger.py status                   # progress per source
   ```
3. **Rank**: edit `research/shortlist.csv` (the merged ranking) and `research/reread.csv` (only skimmed), then
   `.venv\Scripts\python.exe research\ledger.py build` rebuilds `research/tool_ideas.xlsx`.

## Outputs

- `research/ledger/*.csv` (the truth), `research/tool_ideas.xlsx` (rebuilt from them; never edit it by hand).

## Done when

- `ledger.py status` shows every chunk of the sources you took on as read.

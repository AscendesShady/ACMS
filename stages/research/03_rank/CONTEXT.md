# research / 03_rank

Merge the ideas into one ranked list of tools worth building.

## Inputs

| Layer | File | Read |
|---|---|---|
| 4 | `research/ledger/*.ideas.csv` | all the ideas, through the workbook's Ideas sheet |
| 4 | `research/shortlist.csv`, `research/reread.csv` | the ranking so far; chunks only skimmed |

## Process

1. Edit `research/shortlist.csv` (one row a tool, best first; merge duplicates across sources) and
   `research/reread.csv` (chunks to read properly).
2. `.venv\Scripts\python.exe research\ledger.py build` rebuilds the workbook from the CSVs.

## Outputs

- `research/shortlist.csv`, `research/reread.csv`; `research/tool_ideas.xlsx` (built from the CSVs; never edit it
  by hand). A shortlisted tool becomes a task for `stages/code/01_understand`.

## Done when

- The shortlist is ranked and the workbook rebuilt.

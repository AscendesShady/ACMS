# CONTEXT: reference material

Layer 3. Stable material that sets the rules and facts: read it as constraints, don't edit it while doing a task.
Back to routing: [../CONTEXT.md](../CONTEXT.md). The books, papers, SUMO notes and PDFs are not in git
(`.gitignore`); everything else here is.

| Path | What it is | Read it when |
|---|---|---|
| `ui-ux-design-rulebook.md` | The UI rules every control and window follows | adding or changing any control, dialog or panel |
| `reverse_engineering/` | Study notes on SUMO (no SUMO code): car following, lane changing, junctions, signals | before changing `vehicle.py` or `simulation.py`; start at `00_PROJECT_OVERVIEW.md` and `07_ALGORITHM_INDEX.md` |
| `transportation_engineering_books/` | Textbooks (HCM-style methods, signal timing, roundabouts, statistics) | citing a method in `docs/architecture/sources.md`; their text is in `pdf_text/` here |
| `transportation_engineering_papers/` | Papers (AgentSUMO, ChatSUMO, …) | the AI Mode design and the research pipeline |
| `TRAFFIC_SIMULATOR_THEORY_AND_FORMULAS.md` | 60 concepts with formulas, distilled from the manuals (FHWA Traffic Analysis Toolbox, HCM 2000, Garber & Hoel, Lieberman & Rathi, MUTCD, Green Book); what is still open against it is in `reports/improvement_plan_2026-10-13/` | choosing or checking a traffic-engineering formula or output definition |
| `pdf_text/` | The books' and papers' text, 25 pages a file (`sh research/extract.sh` makes it from the PDFs) | searching a source without opening its PDF; the research ledger reads it |
| `Interpretable Context Methodology.pdf` | The ICM paper this repository's `CONTEXT.md` layout follows | changing the layout of folders or `CONTEXT.md` files |

To search the books without opening a PDF: `grep -ril "<term>" references/pdf_text/` (Git Bash).

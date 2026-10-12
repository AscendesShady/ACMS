# campaign / 02_run

Send every prompt through the real window and record what the assistant does.

## Inputs

| Layer | File | Read |
|---|---|---|
| 4 | `stages/campaign/01_check/output/preflight.txt` | that it validated |
| 3 | `docs/architecture/prompt_campaign.md` | § Updating a running campaign, if it halts |

## Process

1. Start Ollama with the campaign's models. Then:
   `.venv\Scripts\python.exe tools\prompt_campaign.py --output reports\prompt_campaign_<yyyy-mm-dd>`
   It opens the editor, ticks Research Log and Watch my edits, and applies every card itself.
2. A ledger already in that folder blocks a second launch: a restart gets a new folder (`..._restart_1`).

## Outputs

- `reports/prompt_campaign_<date>/manifest.json`, `ledger.jsonl` (the harness's record).
- `Documents\ACMS results\ACMS research log <date> <model>\` (every step; move it into `reports/` afterwards).

## Done when

- The ledger's last line is the last prompt's completion, or a halt you have reconciled.
  Next: `../03_evaluate/CONTEXT.md`.

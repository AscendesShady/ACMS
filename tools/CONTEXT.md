# CONTEXT: run the prompt stress-test campaign

Layer 2. You are here to measure how well the in-app assistant handles many user and adversarial prompts.
Back to routing: [../CONTEXT.md](../CONTEXT.md).

## Inputs

| Layer | File | Read |
|---|---|---|
| 3 | `docs/architecture/prompt_campaign.md` | all of it: what the harness does and promises |
| 4 | `tools/campaign_inputs/User prompt.docx` | the user prompts N001–N200 (don't edit during a campaign) |
| 4 | `tools/campaign_inputs/Adversarial prompt.docx` | the adversarial prompts A001–A100 |

## Process (each stage writes a file the next one reads)

1. **Check the prompts** (nothing is sent):
   ```powershell
   .venv\Scripts\python.exe tools\prompt_campaign.py --preflight
   ```
   It must print `Validated 900 submissions. Nothing submitted.` Otherwise fix the .docx numbering first.
2. **Run** (opens the editor, ticks Research Log and Watch my edits, needs Ollama running with the models):
   ```powershell
   .venv\Scripts\python.exe tools\prompt_campaign.py --output reports\prompt_campaign_<yyyy-mm-dd>
   ```
   A ledger already in that folder blocks a second launch: use a new folder for a restart.
3. **Review**: the harness writes `manifest.json` and `ledger.jsonl` to the output folder; the Research Log writes
   every step to `Documents\ACMS results\ACMS research log <date> <model>\`. Mark verdicts in the Research Log's
   History, then evaluate as in [../reports/CONTEXT.md](../reports/CONTEXT.md).

## Outputs

- `reports/prompt_campaign_<date>/manifest.json`, `ledger.jsonl`; Research Log sessions in the results folder.

## Done when

- The ledger's last line is the final prompt's completion (or a recorded halt you have reconciled), and the
  evaluation is saved under `reports/`.

## Tests

`.venv\Scripts\python.exe tests\test_prompt_campaign.py` (no real model is contacted).

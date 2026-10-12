# campaign / 01_check

Check the prompt documents before anything is sent to a model.

## Inputs

| Layer | File | Read |
|---|---|---|
| 3 | `docs/architecture/prompt_campaign.md` | all: what the harness does and promises |
| 4 | `tools/campaign_inputs/User prompt.docx`, `Adversarial prompt.docx` | N001–N200 and A001–A100 |

## Process

1. `.venv\Scripts\python.exe tools\prompt_campaign.py --preflight` must print
   `Validated 900 submissions. Nothing submitted.` If not, fix the documents' numbering.
2. `.venv\Scripts\python.exe tests\test_prompt_campaign.py` must print `ok` (no model is contacted).

## Outputs

- `stages/campaign/01_check/output/preflight.txt`: the preflight's output (`... --preflight > that file`).

## Done when

- Both pass. Next: `../02_run/CONTEXT.md`.

# campaign / 03_evaluate

Turn the campaign's record into findings, with numbers and the code each points at.

## Inputs

| Layer | File | Read |
|---|---|---|
| 4 | `reports/prompt_campaign_<date>/ledger.jsonl`, the Research Log sessions' `steps.csv` | all, by script |
| 4 | a few failing steps' `prompt.md`, `calls.jsonl`, `reply.md` | as examples |
| 3 | `docs/architecture/ai_mode.md` | grep the mechanism a finding blames |

## Process

1. Count with a script, not by eye: completion, outcomes per group, refusals, ungrounded claims, truncation,
   latency and tokens per step.
2. Tie each finding to evidence (step folder) and to the `file:function` that would fix it.

## Outputs

- `reports/<what>_<yyyy-mm-dd>/evaluation.md`: verdict, metrics, failures with examples, ranked fixes. Add its row to
  `reports/CONTEXT.md`. Each fix becomes a task for `stages/code/01_understand`.

## Done when

- Every recommendation names its evidence and its target `file:function`.

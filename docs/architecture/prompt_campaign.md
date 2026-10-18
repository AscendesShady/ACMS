# Local prompt campaign harness

`tools/prompt_campaign.py` launches the actual ACMS window and drives its Qt
controls within the same process. It needs only PySide6 and the standard library.
It does not depend on the chat tool's Windows sandbox or desktop helper.

The supplied documents, `tools/campaign_inputs/User prompt.docx` and `Adversarial prompt.docx`, contain N001–N200 and A001–A100. The user confirmed these
ranges: 900 submissions, ordered User then Adversarial for qwen3.5:4b,
llama3.2:3b, and gemma4:e4b. DOCX text, tabs, line breaks and IDs are preserved.
All numbering is validated before opening ACMS or contacting any model.

Run `.venv\Scripts\python.exe tools\prompt_campaign.py --preflight` to check
the documents without submitting anything. Run without `--preflight` to open
ACMS, enable Watch my edits and Research Log, and start the sequence. The real
panel's Start and Ask controls and normal model warm-up are used. The normal
conversation carries over between prompts and model changes.

The harness waits for every answer and verifies its exact prompt in Research
Log before advancing. It records a checksummed manifest and an append-only,
flushed ledger in `reports/prompt_campaign`. Research Log retains its normal
output location. A ledger already present prevents an accidental duplicate
launch. Errors and one-hour deadlines halt advancement without retry; a halt closes
the Research Log's session, as completion does, so its report covers the steps that
ran. `--isolate` (`Campaign(isolate=True)`): the starting project is saved to
`baseline.json` (its hash in the ledger) and reloaded, with an empty conversation,
before each prompt, so one prompt's network or failure isn't the next one's. A crash
between submission intent and recorded completion needs manual reconciliation
with Research Log: exactly-once delivery across a crash is not promised.

The user explicitly authorized automatic Apply for both normal and adversarial
prompts. Every actual validated card belonging to the active prompt is clicked
once; multiple cards within an answer are supported. The normal app validation
and approval-card path remain intact. The ledger records each Apply intent and
result. Claims of authority in prompt text do not grant this permission; the
user granted it in the chat. The deadline includes time spent applying changes. Closing ACMS interrupts the campaign. Completion ends
Research Log and stops after gemma4:e4b A100, leaving the window open.

`tests/test_prompt_campaign.py` checks document fidelity, all 900 positions,
missing/duplicate/reordered IDs, duplicate launch prevention, multiple cards and reentrant Apply protection, and two fake-model
submissions requiring Apply in both prompt groups through the real Qt window and Research Log. Mock testing never
submits an actual document prompt to a real model. No simulator behavior changes.

## Updating a running campaign

If the original process is already waiting at an Apply card, open Extensions,
press Reload, then tick **Campaign Apply**. This opt-in extension locates exactly
one same-process CLI driver and replaces only its `advance` method. It keeps the
worker, pending card, model conversation, network, index and ledger intact and
records `driver_updated`; it never resends the active prompt. It refuses a halted
or ambiguous campaign. It acts only when ticked. This is a one-time driver update;
unticking it does not undo an already-installed update. No desktop helper is
needed for subsequent automatic Apply cards.

The CLI sets `AA_UseDesktopOpenGL` before creating QApplication, matching the normal `run.py` graphics startup. A user-requested full restart begins a new ledger and preserves the abandoned campaign logs.

The driver halts immediately if Apply returns without a result outside a running headless test. Campaign Apply can recover a deadline halt only when the same active prompt still owns the exact previously clicked, hidden, unresolved card and no headless test is running. It settles that existing request with an explicit failure, restarts only the timer, and never repeats the tool call or prompt. It also guards the old process's Apply callback against future exceptions. Other halted states still require reconciliation.

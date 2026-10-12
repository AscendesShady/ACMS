# code / 02_change

Make the plan's change: the smallest one, in the module's own style.

## Inputs

| Layer | File | Read |
|---|---|---|
| 4 | `stages/code/01_understand/output/plan.md` | all |
| 4 | the module(s) the plan names | the function and its neighbours (style) |
| 3 | `AGENTS.md` | § Always |
| 3 | `docs/architecture/theme.md`, `references/ui-ux-design-rulebook.md` | only for a control, dialog or colour |
| 3 | `docs/architecture/extensions.md` | only for `extensions/*.py`: the hooks and their contract |
| 3 | `docs/architecture/sources.md` | only for a method or formula: grep its row |

## Process

1. Change only what the plan says. If the plan is wrong, go back to 01 and fix the plan first.
2. Match the file: naming, comment voice and density. Validate before changing anything; no new packages.
3. A new control is a small icon button with a tooltip of five words at most; a new colour pair goes in
   `theme.READABLE_PAIRS`. An extension does nothing until ticked and is switched off when it raises.
4. Check it imports: `.venv\Scripts\python.exe -c "import traffic_simulator.<module>"`.

## Outputs

- The changed files in `src/traffic_simulator/` (or `extensions/`, `tools/`). Tests and docs are stages 03 and 04.

## Done when

- The plan's change is made and the module imports. Next: `../03_test/CONTEXT.md`.

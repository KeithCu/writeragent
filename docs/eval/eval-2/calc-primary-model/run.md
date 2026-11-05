# Run — Branch profitability (Calc-primary model)

Manual Calc chat trial. Not wired into `dataset.py` / `run_eval`.
Harness debug, not a multi-model benchmark.

## Setup

1. From the repo root, **deploy** the extension the headed session will
   load (`make deploy`). Then start the headed helper **with
   `--task calc-primary-model --launch`**. It **writes**
   `"chatbot.max_tool_rounds": 150` into `writeragent.json` and restores
   the previous value (or removes the key) on Enter / Ctrl-C. Do not edit
   the JSON by hand.

   ```bash
   .venv/bin/python scripts/eval_2_headed.py --task calc-primary-model --launch
   ```

   `--launch` copies **only**
   `fixtures/Raw Data for Branch Profitability Final.ods` into
   `$TMP/writeragent-eval2-calc-primary` (override with `--trial-dir`)
   and opens that copy in Calc. Prompt, rubric, gold, notes, and the
   xlsx sibling stay outside that folder so `document_research` cannot
   list them. Do **not** open `fixtures/` or this task directory.

   Headed start is **150** rounds (five schedule sheets + formula fill;
   closer to GMP’s work volume than AFC’s two-tab sampling). Schema
   **max is 200**. Everyday default stays **15**. Trailing args run a
   command as the session (`-- soffice --calc workbook.ods`) and skip
   staging — use that only if the workbook is already in a clean dir.
   Confirm in the debug log:
   `Tool-calling loop START (max 150 rounds)`.
2. Set the chat model to `openai/gpt-oss-120b:nitro`.
3. Paste the full contents of `prompt.writeragent.txt` as the user
   message. Do not paste `prompt.gdpval.txt`.

## After the run

Create `runs/<stamp>-gpt-oss-120b/` (example: `runs/20260909-1530-gpt-oss-120b/`) and save:

| File | Contents |
|------|----------|
| `prompt_used.txt` | Exact text sent (should match `prompt.writeragent.txt`) |
| `thinking_and_tools.md` | Model thinking plus tool calls |
| `final_workbook.ods` | Workbook after the agent finished (the staged trial-dir copy, not `fixtures/`) |
| `notes.txt` | Observer notes: wrong factor, pinned formula, empty tabs, timing |

Then score the saved workbook. Ready / STREAM_DONE is ignored — empty
`sheet_create` tabs fail:

```bash
.venv/bin/python scripts/eval_2_headed.py --task calc-primary-model --score runs/<stamp>-gpt-oss-120b/final_workbook.ods
```

See [`rubric.eval2.md`](rubric.eval2.md). Leave this `run.md` unchanged
when adding a trial.

# Run — Chief trial

Manual Calc chat trial. Not wired into `dataset.py` / `run_eval`.

## Setup

1. From the repo root, start the headed helper (bumps `chatbot.max_tool_rounds` to **50**, restores the previous `writeragent.json` when you press Enter or Ctrl-C):

   ```bash
   .venv/bin/python scripts/eval_2_headed.py
   ```

   It opens Calc / the Population fixture when soffice and the file are available. Use `--no-launch` if LibreOffice is already up. Confirm in the debug log: `Tool-calling loop START (max 50 rounds)`. Everyday chat stays at 15 after the helper exits.
2. If the workbook did not open, open `fixtures/Population v2.xlsx` (or `fixtures/Population v2.ods` if present) in LibreOffice Calc.
3. Set the chat model to `openai/gpt-oss-120b:nitro`.
4. Paste the full contents of `prompt.writeragent.txt` as the user message. Do not paste `prompt.gdpval.txt`.

## After the run

Create `runs/<stamp>-gpt-oss-120b/` (example: `runs/20260906-1530-gpt-oss-120b/`) and save:

| File | Contents |
|------|----------|
| `prompt_used.txt` | Exact text sent (should match `prompt.writeragent.txt`) |
| `thinking_and_tools.md` | Model thinking plus tool calls |
| `final_workbook.ods` | Workbook after the agent finished |
| `notes.txt` | Observer notes: failures, extra files created, rubric mismatches, timing |

Leave gold under `gold/` and this `run.md` unchanged when adding a trial.

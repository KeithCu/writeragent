# eval-2 — WriterAgent hard-task variants

Iterate here. Do **not** edit `docs/eval/gdpval/` gold trees.

Each subdirectory is one experiment. First: `afc-sample-83d10b06/` (GDPval auditor sample, minimal prompt delta for in-workbook deliverable).

See each task’s `run.md` for how to execute a trial.

**Tool-loop budget:** headed Calc trials need more than the everyday 15 rounds. Set `WRITERAGENT_EVAL=1` on soffice or `"chatbot.eval_mode": true` in `writeragent.json` so the sidebar uses `chatbot.eval_max_tool_rounds` (default **50**). Do not raise the product default. The 17-task string harness (`WRITERAGENT_EVAL_HARNESS`) keeps its own cap (25).

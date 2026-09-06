# eval-2 — WriterAgent hard-task variants

Iterate here. Do **not** edit `docs/eval/gdpval/` gold trees.

Each subdirectory is one experiment. First: `afc-sample-83d10b06/` (GDPval auditor sample, minimal prompt delta for in-workbook deliverable).

See each task’s `run.md` for how to execute a trial.

Headed GDPval/AFC runs auto-use 50 tool-loop rounds via `scripts/eval_2_headed.py` (temporary `chatbot.max_tool_rounds` bump + restore). Everyday default stays 15.

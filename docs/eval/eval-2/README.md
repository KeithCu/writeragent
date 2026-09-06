# eval-2 — WriterAgent hard-task variants

Iterate here. Do **not** edit `docs/eval/gdpval/` gold trees.

Each subdirectory is one experiment. First: `afc-sample-83d10b06/` (GDPval auditor sample, minimal prompt delta for in-workbook deliverable).

See each task’s `run.md` for how to execute a trial.

Long headed GDPval/AFC runs: set `"chatbot.max_tool_rounds": 50` in `writeragent.json` (schema already allows it; default stays 15).

# Notes — python domain → shapes American flag

Purpose: **headed** task for the short Ask that must go **python domain →
shapes via `run_venv_python_script`**. Same family as “Ask the python
domain to create an American flag using the shapes domain tools.”

The user message is exactly [`prompt.writeragent.txt`](prompt.writeragent.txt).
Do not lengthen it and do not add a flag few-shot.

## What this is not

| Path | Why it is not this slot |
|------|-------------------------|
| OpenRouter string harness (`scripts/prompt_optimization/`) | No `run_venv_python_script` and no `domain=python`. It cannot run this Ask. |
| `smol_examples` flag few-shot | Already rejected. The shapes example stays a generic ring of circles. |
| [`draw-primary-deliverable/`](../draw-primary-deliverable/) | Draw process-flow map. Different problem. This slot is **Writer**. |

## Document

Writer, shapes on the Writer draw page. Headed PASS evidence used Writer
Untitled documents, not `.odg`. `--launch` opens a blank
`American Flag.odt` so the agent has a page to draw on. No research
fixture. No peer.

## Oracle

Hard gate in [`rubric.eval2.md`](rubric.eval2.md): executed python
specialized path, `delegate_tool_domains` including shapes,
`run_venv_python_script`, and a page-scale composite of stripe-like
rects (some star-like or canton shapes if present). Group /
`shape_group` is a **bonus** (Writer `ShapeCollection` bug, #927).

**Calibration:** gpt-oss-**20b** on latest master / #926 already
succeeds path-wise on this Ask. The flag is recognizable and the stars
are imperfect. That almost-flag is a good matrix-floor outcome. Do not
require a perfect 50-star canton for HAPPY / pass. Messy or
under-counted stars are soft partial credit. Fail only the wrong route
(images / PNG), no venv, a tiny ~1900 HMM speck, or a blank page.

Outer tool-loop rounds are a **recorded metric**, not a check. The
debug log’s last `Tool-calling loop START (max N rounds)` is the budget.
`Tool loop round N` lines (DEBUG, 0-based) after that START are the
rounds used. A missing line stays unset and does not fail.

## Board pictures (post-process)

One preview image per model×run, separate from pass/fail. Each stamp
keeps the saved Writer document (`final_flag.odt`, copied from the
trial `American Flag.odt` on Enter) plus `writeragent_debug.log`. That
`.odt` is enough to render later.

Eval-2 has **no** PNG export helper. Headed autopsy shots were manual.
`plugin/writer/get_image.py` (`_render_draw_page_png`) needs a live UNO
document and is not this step.

After scoring, export the thumbnail:

```bash
.venv/bin/python scripts/eval_2_flag_preview.py docs/eval/eval-2/python-shapes-flag/runs/<stamp>/
```

That writes `preview.png` in the stamp. A gallery is
`runs/*/preview.png`. The oracle does not look for the PNG.

## Not changed

- String-harness dataset, prompts, and few-shots
- Everyday `chatbot.max_tool_rounds` default (15)
- Draw-primary process map
- Slot 7 (`writer-headed-template/`) stays **PARKED**

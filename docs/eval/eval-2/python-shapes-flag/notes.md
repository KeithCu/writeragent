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

Soft checks in [`rubric.eval2.md`](rubric.eval2.md): executed python
specialized path, `delegate_tool_domains` including shapes,
`run_venv_python_script`, flag-like geometry (stripe-like rects +
star-like shapes at page scale). Group / `shape_group` is a **bonus**
(Writer `ShapeCollection` bug, #927). LLM-only `shape_upsert` with no
venv, and `domain=images` PNG, fail.

## Not changed

- String-harness dataset, prompts, and few-shots
- Everyday `chatbot.max_tool_rounds` default (15)
- Draw-primary process map
- Slot 7 (`writer-headed-template/`) stays **PARKED**

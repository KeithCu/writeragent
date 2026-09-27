# rubric.eval2 — python domain + shapes American flag (v1)

WriterAgent-native. There is no gold rubric. The Ask is the one line in
[`prompt.writeragent.txt`](prompt.writeragent.txt). This variant scores
the **saved Writer document** (`.odt`) and the run-stamp
`writeragent_debug.log`. Do **not** score a Draw `.odg` or an images
PNG. Chat Ready / STREAM_DONE is ignored.

The string harness cannot run this Ask (no `run_venv` / `domain=python`).

## What is scored

Nine fail-closed checks. Hard PASS means `failures` is empty. Partial
is `1 − len(failures) / checks` with `checks = 9`.

Process lines are **executed** calls only (`Tool call:`,
`streaming_loop: accumulated tool_calls`, `SmolToolAdapter executing`,
`tool-async-`). Few-shot text that mentions the same tool names does
not count.

Geometry is `content.xml` on the Writer draw page. LibreOffice stores
shape sizes in inches on a Writer save (`19001` HMM → `7.4807in`). The
oracle converts cm / mm / in / pt to HMM (1/100 mm).

| # | Check | Fail closed |
|---|--------|-------------|
| 1 | Python specialized path | No executed `delegate_to_specialized_*_toolset` with `domain=python` |
| 2 | `delegate_tool_domains` includes `shapes` | Tool ran without `shapes` in `domains`, or never ran |
| 3 | `run_venv_python_script` executed | No venv script call (outer `Tool call:` or inner `SmolToolAdapter`) |
| 4 | Not LLM-only `shape_upsert` | `shape_upsert` tool call and no `run_venv_python_script` |
| 5 | Not `domain=images` PNG | `domain=images` or `image_generate` / `image_insert` / `image_replace` / `image_download` |
| 6 | Enough shapes (≥ **20**) | A handful of boxes |
| 7 | Stripe-like rects (≥ **6**, width/height ≥ **3**) | No long horizontal bands. PASS was ~14–15 rects; exact 13 is not required |
| 8 | Star-like shapes (≥ **20**) | `star5` / `star*` custom shapes, or a polygon with ≥ 10 points. PASS was ~50 stars |
| 9 | Page scale (max width ≥ **10000** HMM) | ~1900 HMM speck (~19 mm). PASS max width was ≈ **19001** HMM |

A missing debug log fails checks 1–3. A missing or unreadable `.odt`
fails the geometry checks.

## Bonus (not required)

- `shape_group` / `draw:g`. Writer grouping is a known UNO
  `ShapeCollection` bug (#927). Loose shapes still pass.
- `wa.shape.upsert` inside the venv script arguments, or a
  `create_shape` debug line. Nice evidence the script placed shapes;
  the geometry checks are what prove the flag is on the page.

## Non-goals

- Exact 13 stripes and 50 stars, colors, canton proportions, or z-order.
- A Draw `.odg` or an Impress deck.
- An images-domain PNG of a flag.
- A flag few-shot in `smol_examples`.
- Raising the everyday `chatbot.max_tool_rounds` default (15). Headed
  helper writes **50** for this task. Schema **max is 200**.

CLI: `scripts/eval_2_python_shapes_flag_oracle.py` or
`scripts/eval_2_headed.py --task python-shapes-flag --score`.

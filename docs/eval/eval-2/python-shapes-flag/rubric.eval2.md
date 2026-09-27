# rubric.eval2 — python domain + shapes American flag (v1)

WriterAgent-native. There is no gold rubric. The Ask is the one line in
[`prompt.writeragent.txt`](prompt.writeragent.txt). This variant scores
the **saved Writer document** (`.odt`) and the run-stamp
`writeragent_debug.log`. Do **not** score a Draw `.odg` or an images
PNG. Chat Ready / STREAM_DONE is ignored.

The string harness cannot run this Ask (no `run_venv` / `domain=python`).

## Calibration (matrix floor)

On latest master / #926, **gpt-oss-20b** already succeeds **path-wise**
on this short Ask. The flag is recognizable and the stars are imperfect.
That almost-flag is a **good** outcome for the matrix floor. HAPPY /
oracle PASS does **not** require a perfect 50-star canton.

## What is scored

Seven fail-closed checks. Hard PASS means `failures` is empty. A short
or messy star field does not fail.

Partial is `1 − (len(failures) + len(soft)) / (checks + 1)` with
`checks = 7` and one soft star check in the denominator (8). A
page-scale striped flag with under-counted stars is a **strong
partial** (0.875) and still passes. No failures and no soft note is
1.0.

Process lines are **executed** calls only (`Tool call:`,
`streaming_loop: accumulated tool_calls`, `SmolToolAdapter executing`,
`tool-async-`, and `=== Sync response:` `tool_calls`). Few-shot text
that mentions the same tool names does not count. That text sits on
request `"content"` lines.

Nested `delegate_tool_domains` is often only
`SmolToolAdapter executing async tool 'delegate_tool_domains'` (or
`tool-async-delegate_tool_domains`) with no arguments. The worker logs
the arguments on the sync-response `tool_calls` object, which can appear
above that Smol line. Those arguments count. When the arguments were
not logged, the empty execution still counts as shapes if a non-content
executed payload shows `domains` including `shapes`, or if no executed
payload names a domain list and an executed `run_venv_python_script`
after that call places shapes (`wa.shape.upsert`). A footnotes-only
delegation does not pass this check. Gemini 3.5 Flash Lite stamp
`20260927-0019` was a false red on this check alone: python, venv, and
13 stripes were already present.

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
| 6 | Page-scale striped field | Blank page, or fewer than **6** stripe-like rects (width/height ≥ **3**). Exact 13 stripes is not required |
| 7 | Page scale (max width ≥ **10000** HMM) | ~1900 HMM speck (~19 mm). A full-page composite is about 10000–20000 HMM |

A missing debug log fails checks 1–3. A missing or unreadable `.odt`
fails the geometry checks.

### Soft (does not fail)

Star-like shapes (`star5` / `star*` custom shapes, a name containing
`star`, or a polygon with ≥ 10 points). When the hard gate is already
met and the count is under **8**, the oracle records one soft note and
trims partial. Messy placement that the XML still counts as stars does
not fail and does not trim. A canton-like rect (non-stripe, both sides
≥ 2000 HMM) is mentioned on that note when present; it is not required.
Zero or few stars on an otherwise page-scale striped flag still pass.

## Alongside pass/fail

- **Tool rounds.** Last `Tool-calling loop START (max N rounds)` in
  `writeragent_debug.log`, and `Tool loop round N` (DEBUG, 0-based)
  after that line. Reported as `tool_rounds_used` / `tool_rounds_budget`.
  Absent lines stay unset. They do not change PASS or partial.
- **Preview PNG.** `--score` copies `Thumbnails/thumbnail.png` from the
  saved `.odt` to `preview.png` in the run directory. That is the board
  picture (one per model×run). LibreOffice writes the thumbnail on save.
  A file with no thumbnail leaves `preview` unset and does not add a failure.

## Bonus (not required)

- `shape_group` / `draw:g`. Writer grouping is a known UNO
  `ShapeCollection` bug (#927). Loose shapes still pass.
- `wa.shape.upsert` inside the venv script arguments, or a
  `create_shape` debug line. Nice evidence the script placed shapes;
  the geometry checks are what prove the flag is on the page.

## Fail (hard)

- Wrong route: images domain or a PNG (`image_generate` / insert /
  download / replace).
- No `run_venv_python_script` (including LLM-only `shape_upsert`).
- Tiny ~1900 HMM speck.
- Blank page.

## Non-goals

- Exact 13 stripes and 50 stars, colors, canton proportions, or z-order.
- Perfect star geometry. The 20b almost-flag is the floor.
- A Draw `.odg` or an Impress deck.
- An images-domain PNG of a flag.
- A flag few-shot in `smol_examples`.
- Raising the everyday `chatbot.max_tool_rounds` default (15). Headed
  helper writes **50** for this task. Schema **max is 200**.

CLI: `scripts/eval_2_python_shapes_flag_oracle.py` or
`scripts/eval_2_headed.py --task python-shapes-flag --score`.

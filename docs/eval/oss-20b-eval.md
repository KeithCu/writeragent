# gpt-oss-20b eval lift

Working plan to raise `openai/gpt-oss-20b` on the 17-task `--backend string`
pack. Snapshot: 2026-09-01 ranking in
[`benchmark_results.json`](../../scripts/prompt_optimization/benchmark_results.json)
/ [`benchmark_results_details.json`](../../scripts/prompt_optimization/benchmark_results_details.json).
Related: [benchmarks.md](benchmarks.md), [eval-dev-plan.md](eval-dev-plan.md),
[string-harness-upgrade.md](string-harness-upgrade.md),
[dspy-prompt-optimization-plan.md](dspy-prompt-optimization-plan.md).

**Status:** A/B: 20b gained `data_sorting`; header misses persist (models
likely pass `has_header=false` explicitly — runtime already defaults True
when omitted). `sort_range` now **requires** `has_header`. Calc directives
are two short **Do Z because Y** lines (sort routing + `has_header`), not
a mashed one-liner. Tax relative rule stayed helpful for 120b. Shared
prompt; no 20b fork. Selective catalog re-measure (PR 613) and next
steps: [eval-dev-plan.md § G](eval-dev-plan.md#g-calc-sorttax-prompt-lift-sep-2026--pr-610--selective-re-rank).

## Snapshot

| | gpt-oss-20b | gpt-oss-120b |
|---|---|---|
| Rank | 15 / 23 | 2 |
| Hard pass | **12/17 (0.706)** | 17/17 |
| Correctness | 0.687 | 0.971 |
| Quality (judged passes) | 0.89 | 0.90 |
| Tokens / task | 16062 | 12263 |
| $ / task | 0.00065 | 0.00054 |
| C²/$ | 537 | 1339 |

20b is cheap and already high quality on tasks it passes. The gap is **hard
pass**, not judge score or cost. Recovering the three routing failures
alone is 15/17 ≈ 88% (the rank-9 cluster). Needle-token recovery can add
the last two.

Production ships **one** prompt per app (Writer / Calc / Draw). Any patch
must re-score **gpt-oss-120b** on the same tasks so 17/17 does not regress.

## The five failures

From `benchmark_results_details.json` for `openai/gpt-oss-20b`. Writer is
mostly fine (10/12). The expensive misses are Calc/Draw routing plus two
copy-fidelity needles.

| Task | What 20b did | Gate | Kind |
|------|--------------|------|------|
| `table_from_mess` | Enclosure row is `Saginaw SCE-202010ELJ` — dropped **NEMA 4** | missing `'NEMA 4'` | copy fidelity |
| `smart_summarization` | Scale bullet is `10 000 requests per second` | oracle `summary missing '10k'` | copy fidelity |
| `flowchart_gen` | Empty Draw tree (`tree: []`) — never delegated `domain="shapes"` | missing Start/End/login/credentials, shape types, edges | tool routing |
| `data_sorting` | In-place `=PY` over `A1:B6`; header destroyed | `header row is not first`, missing `Widget` | tool routing |
| `tax_column` | Every fruit got `=B2*0.08` (Banana should be `B3`) | `Banana Tax is not a relative 8% formula` | per-row formulas |

It **passed** both Phase F `=PY` dest rows (`py_refuse_overlap`,
`py_no_bulk_read`). The model can land `=PY` beside a range; it over-uses
that tool for sort.

## Why the current DSPy job will not move this score

`run_optimize.py` / `program.py` optimize a **DSPy ReAct** agent with three
Writer mocks (`get_document_content`, `apply_document_content`, `find_text`).
Ranking uses **`LlmClient` + production schemas + an inner specialized
loop** (`llm_chat_eval.py`, `--backend string`).

`optimized_writer_prompt.json` is a ReAct script (`Next Thought:` /
`Next Tool Name:`). It cannot be pasted into
[`plugin/framework/prompts.py`](../../plugin/framework/prompts.py).

MIPROv2 on that ReAct program optimizes a prompt 20b never sees in
`make run_eval`. A second mismatch: production is **three** prompts, not
one `DEFAULT_CHAT_SYSTEM_PROMPT`. 20b’s misses live in Calc/Draw
directives and **tool descriptions**.

[string-harness-upgrade.md](string-harness-upgrade.md) deferred MIPROv2
until the metric was honest. Worlds, process oracles, and specialized
inner loops are shipped. The optimizer was never rewired onto that loop.

## Smoking gun: `=PY` docs vs `sort_range`

[`write_formula_range`](../../plugin/calc/cells.py) description is a long
`=PY` sermon (dest, spill, `to_pandas()`, do not dump A1:H500).
[`sort_range`](../../plugin/calc/cells.py) is one sentence and lives behind
`delegate_to_specialized_calc_toolset(domain="ranges")`.

[`CALC_CORE_DIRECTIVES`](../../plugin/framework/prompts.py) ends with
“Python on sheet data: write_formula_range of =PY”. A 20B model that is
unsure picks the tool whose docs are longest — which matches the
`data_sorting` trace.

Draw already tells the model to use specialized shape ops
(`DEFAULT_DRAW_CHAT_SYSTEM_PROMPT_TEMPLATE` workflow step 3). 20b still
returned an empty tree on `flowchart_gen`, so the hint is not enough for
this size.

## What to try (least code first)

Do **not** start with `python run_optimize.py --model openai/gpt-oss-20b`.

### 1. Hand patches, then a 5-task A/B

Draft in `plugin/framework/prompts.py` and the two Calc tool descriptions.
Keep diffs short.

**Calc** (`CALC_CORE_DIRECTIVES` only for sort; slim `CALC_WORKFLOW`):

- Do `delegate_to_specialized_calc_toolset(domain="ranges")` then
  `sort_range` to reorder rows (multi-key = two stable one-column
  passes) because `write_formula_range` / `=PY` overwrite the range
  including headers.
- Do pass `has_header=true` on `sort_range` when row 1 is labels
  because otherwise labels sort as values.
- Do write each row's formula with that row's cells (`Banana` → `B3`,
  not a stamped `B2`).

**Draw** (workflow / `DRAW_CORE_DIRECTIVES`):

- Flowcharts → `delegate_to_specialized_draw_toolset(domain="shapes")`,
  then `shape_upsert` + `shape_connect`. An empty `get_draw_tree` is a
  failed task.

**Writer** (`WRITER_CORE_DIRECTIVES` or the HTML contract):

- Keep source tokens verbatim (`10k`, `NEMA 4`, model numbers). Do not
  expand or localize them.

**Tool descriptions:**

- `sort_range`: stable one-column; multi-key = multiple calls; Do pass
  `has_header=true` when row 1 is labels because otherwise labels sort
  as values. Schema `required` is `["range", "has_header"]`.
- `write_formula_range`: no sort-half sentence (slim surface). Relative
  / tax formula rule stays in Calc directives only.

Then eval only the five fails, both models:

```bash
python scripts/prompt_optimization/run_eval.py \
  --models openai/gpt-oss-20b \
  -e data_sorting,tax_column,flowchart_gen,table_from_mess,smart_summarization

python scripts/prompt_optimization/run_eval.py \
  --models openai/gpt-oss-120b \
  -e data_sorting,tax_column,flowchart_gen,table_from_mess,smart_summarization
```

Expected: routing patches have a real shot at +3 hard passes (sort,
flowchart, tax). The two needle tasks are weaker; a preserve-tokens line
might recover one.

If 120b still 5/5 and 20b is up, run the full 17 for both before shipping.

### 2. Tool-subset sweep (if sort still fails)

DSPy does not search “how many tools.” Same 17 tasks, 20b, vary catalog:

- current catalog
- `sort_range` visible on **core** Calc (no ranges delegate)
- shorter `write_formula_range` description that does not advertise
  sort-via-`=PY`

If 20b passes `data_sorting` once `sort_range` is on the core list, that
is a catalog/prompt issue, not a MIPRO-instruction issue. See “How many
tools is too many?” in [dspy-prompt-optimization-plan.md](dspy-prompt-optimization-plan.md).

### 3. GEPA on the real eval loop (if patches stall)

[GEPA](https://dspy.ai/api/optimizers/GEPA/overview/) reflects on failed
traces and rewrites instructions. That matches this problem.

- Student: `openai/gpt-oss-20b`
- Reflection LM: `openai/gpt-oss-120b` (already 17/17)
- Metric: **hard pass** (substring + result oracles + process oracles),
  not judge − tokens. 20b already has quality 0.89 on passes.
- Optimize **short blobs**, separately: `CALC_CORE_DIRECTIVES` +
  `CALC_WORKFLOW`; Draw workflow / `DRAW_CORE_DIRECTIVES`; maybe one
  Writer preserve-tokens line; optionally
  `write_formula_range.description` and `sort_range.description`
  (eval-dev-plan already flags MIPROv2 on that description).
- Program: wrap `llm_chat_eval`, **not** `dspy.ReAct`.

Hold out a couple of Writer/Phase F passes (`bulk_cleanup`,
`py_refuse_overlap`) so the rewrite does not trade routing for
regressions.

### 4. MIPROv2 instruction-only, only after that wrap

0-shot, `max_bootstrapped_demos=0`, `auto="light"` first. Train on the
five fails plus a couple of passes. Always re-score 120b.

Do **not** optimize one giant Writer instruction over all 17 tasks.

Skip BootstrapFewShot / SIMBA demos unless they are baked into the
shipped prompt. Eval-only demos do not exist in sidebar chat. The
production-compatible version of a demo is 2–3 lines in
`CALC_CORE_DIRECTIVES`.

## What not to do

- Re-run `python run_optimize.py --model openai/gpt-oss-20b` against the
  current ReAct program.
- Copy `optimized_writer_prompt.json` into `DEFAULT_CHAT_SYSTEM_PROMPT`.
- Use the judge+token metric as the MIPRO objective (20b needs **passes**).
- Ship a 20b-only prompt without checking 120b still at 17/17.
- Bulk `--generate-golds` before a ranking run.

## Expected lift

| Recover | Hard pass | Notes |
|---------|-----------|--------|
| 3 routing tasks | 15/17 ≈ 0.88 | sort, flowchart, tax |
| + one needle | 16/17 ≈ 0.94 | `10k` or `NEMA 4` |
| all five | 17/17 | possible but 20b may still drop needles |

## Open

- [x] Draft the Calc directive patches + `sort_range` description (DO-first sort + `has_header`; relative formulas; slimmed after 120b miss)
- [x] First 5-task A/B on 20b and 120b — 20b gained `data_sorting`; 120b `has_header` miss (`header row is not first`)
- [x] Re-A/B after wording — header misses persist; likely explicit `has_header=false`
- [ ] Re-A/B after required `has_header` + split DO lines
- [ ] Full 17 if the A/B is a win and 120b does not regress
- [ ] Tool-subset sweep if `data_sorting` still fails
- [ ] Wrap `llm_chat_eval` as a DSPy module; GEPA on Calc/Draw blobs
- [x] Prompt-text pins in `tests/scripts/test_eval_prompts.py` for shipped Calc wording

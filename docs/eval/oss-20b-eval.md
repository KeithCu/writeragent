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
when omitted). `range_sort` now **requires** `has_header`. Calc directives
are two short **Do Z because Y** lines (sort routing + `has_header`), not
a mashed one-liner. Tax relative rule stayed helpful for 120b. Shared
prompt; no 20b fork. Selective catalog re-measure (PR 613) and next
steps: [eval-dev-plan.md § G](eval-dev-plan.md#g-calc-sorttax-prompt-lift-current--prs-616--617--618).

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
alone is 15/17 ≈ 88% (the rank-9 cluster). Scale abbreviations match
their expanded digits (`10k` → `10,000`). `NEMA 4` is optional. There is
no Writer preserve-tokens tip.

Production ships **one** prompt per app (Writer / Calc / Draw). Any patch
must re-score **gpt-oss-120b** on the same tasks so 17/17 does not regress.

## The five failures

From `benchmark_results_details.json` for `openai/gpt-oss-20b`. Writer is
mostly fine (10/12). The expensive misses are Calc/Draw routing plus two
copy-fidelity needles.

| Task | What 20b did | Gate | Kind |
|------|--------------|------|------|
| `table_from_mess` | Enclosure row is `Saginaw SCE-202010ELJ` — dropped **NEMA 4** | rating is optional; table, brands, and Total still required | editorial |
| `smart_summarization` | Scale bullet is `10 000 requests per second` | `haystack_has` accepts `10k` ↔ `10 000` | scale expansion |
| `flowchart_gen` | Empty Draw tree (`tree: []`) — never delegated `domain="shapes"` | missing Start/End/login/credentials, shape types, edges | tool routing |
| `data_sorting` | In-place `=PY` over `A1:B6`; header destroyed | `header row is not first`, missing `Widget` | tool routing |
| `tax_column` | Every fruit got `=B2*0.08` (Banana should be `B3`) | `Banana Tax is not a relative 8% formula` | per-row formulas |

It **passed** both Phase F `=PY` dest rows (`py_refuse_overlap`,
`py_no_bulk_read`). The model can land `=PY` beside a range; it over-uses
that tool for sort.

## Why the current DSPy job will not move this score

Default `run_optimize.py --student llm` now wraps **`llm_chat_eval`**
(same LlmClient + production schemas + inner specialized loop as
`make run_eval`) and rewrites a **named slice** (`--slice calc_core`,
`range_sort`, …). `--student react-mock` still exists for comparison:
that path is DSPy ReAct + three Writer mocks; its
`optimized_writer_prompt.json` (`Next Thought:` / `Next Tool Name:`)
must not be pasted into
[`plugin/framework/prompts.py`](../../plugin/framework/prompts.py).

Do not optimize one giant Writer instruction over all 17 tasks. 20b’s
misses live in Calc/Draw directives and **tool descriptions**.

[string-harness-upgrade.md](string-harness-upgrade.md) deferred MIPROv2
until the metric was honest. Worlds, process oracles, and specialized
inner loops are shipped. The optimizer was never rewired onto that loop.

## Smoking gun: `=PY` docs vs `range_sort`

[`write_formula_range`](../../plugin/calc/cells.py) description is a long
`=PY` sermon (dest, spill, `to_pandas()`, do not dump A1:H500).
[`range_sort`](../../plugin/calc/cells.py) is one sentence and lives behind
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
  `range_sort` to reorder rows (multi-key = two stable one-column
  passes) because `write_formula_range` / `=PY` overwrite the range
  including headers.
- Do pass `has_header=true` on `range_sort` when row 1 is labels
  because otherwise labels sort as values.
- Do write each row's formula with that row's cells (`Banana` → `B3`,
  not a stamped `B2`).

**Draw** (`DRAW_CORE_DIRECTIVES`, shipped):

- Do `delegate_to_specialized_draw_toolset(domain="shapes")` then
  `shape_upsert` + `shape_connect` for flowcharts and process diagrams
  because an empty `get_draw_tree` means the task failed.

**Writer** (do not ship a preserve-tokens tip):

- Keith's decision: models may fix misspellings and clarify wording.
  Do **not** add a forever line such as “Keep source tokens verbatim
  (`10k`, `NEMA 4`)”, and do not steer empty-document apply targets.
- `haystack_has` treats `Nk` / `NM` as the same fact as grouped digits
  (`10k` / `10K` / `10 000` / `10,000` / `10000`, and the same for
  `100K` / `100M`). Omitting the count still fails. A different
  magnitude does not match. Brand tokens (`Battle Born`, …) stay exact.
- `NEMA 4` is optional. Dropping it from the enclosure cell is an
  acceptable editorial choice, so `table_from_mess` does not require it.
  The table, product-identity needles, and the Total amount still do.

**Tool descriptions:**

- `range_sort`: stable one-column; multi-key = multiple calls; Do pass
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
flowchart, tax). Scale expansions pass via `haystack_has`. Dropping
`NEMA 4` is not a fail. There is no preserve-tokens prompt line.

If 120b still 5/5 and 20b is up, run the full 17 for both before shipping.

### 2. Tool-subset sweep (if sort still fails)

DSPy does not search “how many tools.” Same 17 tasks, 20b, vary catalog:

- current catalog
- `range_sort` visible on **core** Calc (no ranges delegate)
- shorter `write_formula_range` description that does not advertise
  sort-via-`=PY`

If 20b passes `data_sorting` once `range_sort` is on the core list, that
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
  `CALC_WORKFLOW`; Draw workflow / `DRAW_CORE_DIRECTIVES`; optionally
  `write_formula_range.description` and `range_sort.description`
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
| + scale expansions | 16/17 ≈ 0.94 | `Nk`/`NM` digits match; `NEMA 4` not required |
| all five | 17/17 | possible but 20b may still drop needles |

## Open

- [x] Draft the Calc directive patches + `range_sort` description (DO-first sort + `has_header`; relative formulas; slimmed after 120b miss)
- [x] First 5-task A/B on 20b and 120b — 20b gained `data_sorting`; 120b `has_header` miss (`header row is not first`)
- [x] Re-A/B after wording — header misses persist; likely explicit `has_header=false`
- [ ] Re-A/B after required `has_header` + split DO lines
- [ ] Full 17 if the A/B is a win and 120b does not regress
- [ ] Tool-subset sweep if `data_sorting` still fails
- [x] Wrap `llm_chat_eval` as a DSPy module (`program_llm.py`; MIPROv2 `--student llm`)
- [ ] GEPA on Calc/Draw blobs (still optional; MIPROv2 slice path is the first wrap)
- [x] Prompt-text pins in `tests/scripts/test_eval_prompts.py` for shipped Calc wording
- [x] Draw flowchart Do-because line in `DRAW_CORE_DIRECTIVES` (no empty-doc apply-target tip)
- [x] `haystack_has` accepts scale expansions; `NEMA 4` optional; no Writer preserve-tokens tip

## 2026-11 prompt A/B (EVAL-PROMPT-AB)

- **Base:** `03223f746` (without EVAL-NUDGE).
- **Models & tasks:** `openai/gpt-oss-120b:nitro` and `openai/gpt-oss-20b:nitro`, 8 runs per arm on `logical_rewriting`, `table_engineering`, `data_sorting`, and `reformat_resume` (plus 3 prior baseline runs at `a5e6bf6cb`, whose prompts match `03223f746`, pooled as `base_old`).

### Arms and wording

- **base:** HEAD prompts.
- **X (`L1 + L3`):**
  - **L1 (`plugin/framework/prompts.py` `SIDEBAR_VS_DOCUMENT`):** line 2 changed to `MUST use apply_document_content for drafts, reports, research output, and rewrites or conversions of document text; sidebar gets at most a brief confirmation.`
  - **L3 (`plugin/calc/cells.py` `SortRange.description`):** `"Multi-key sorts are multiple calls (two stable one-column passes). "` changed to `"Multi-key sorts are one call per key: tie-breaker first, main key last (a stable pass keeps the earlier order only among equal values). "`
- **Y (`X + L2`):**
  - **L2 (`plugin/framework/prompts.py` `build_specialized_sub_agent_instructions`):** `"If this domain cannot do the task, say so in the answer instead of claiming it was done. "` changed to `"Do every part of the task these tools can do; for any part they cannot do, say so in the answer instead of claiming it was done. "`

### Results (hard passes / valid trials)

| Task | Model | base (8) | base_old (3) | base pooled | X (8) | Y (8) |
|---|---|---:|---:|---:|---:|---:|
| `logical_rewriting` | 120b | 7/8 | 2/3 | 9/11 | 4/8 | 7/8 |
| `logical_rewriting` | 20b | 8/8 | 2/3 | 10/11 | 6/8 | 6/8 |
| `table_engineering` | 120b | 8/8 | 3/3 | 11/11 | 8/8 | 8/8 |
| `table_engineering` | 20b | 8/8 | 2/3 | 10/11 | 8/8 | 8/8 |
| `reformat_resume` | 120b | 8/8 | 3/3 | 11/11 | 8/8 | 8/8 |
| `reformat_resume` | 20b | 8/8 | 3/3 | 11/11 | 7/7 (1 inv) | 8/8 |
| `data_sorting` | 120b | 4/8 | 1/2 (1 har) | 5/10 | 4/8 | 6/8 |
| `data_sorting` | 20b | 5/5 (3 inv) | 1/2 (1 inv) | 6/7 | 2/5 (3 inv) | 5/6 (2 inv) |

- Exclusions: `inv` = upstream Groq provider/parsing error (`invalid`); `har` = failed `data_sorting` with sheet-qualified `range_sort` (`harness`).
- `data_sorting` `last_sort_is_revenue_desc`: `base` 5/13 (4/8 120b, 1/5 20b), `base_old` 1/4 (1/2 120b, 0/2 20b), pooled `base` 6/17 (35.3%), `X` 4/13 (30.8%: 4/8 120b, 0/5 20b), `Y` 7/14 (50.0%: 5/8 120b, 2/6 20b).
- No Writer row in `X` or `Y` delegated to a specialized sub-agent.

### Decisions

- **Drop L1:** `logical_rewriting + table_engineering` in `X` was 26/32 (81.3%) vs pooled `base` 40/44 (90.9%), and `logical_rewriting` alone dropped to 10/16 (62.5%) vs 19/22 (86.4%). (`reformat_resume` served as the guard against over-applying `apply_document_content` and stayed at 15/15 in `X` and 16/16 in `Y`.)
- **Drop L3:** `data_sorting` in `X` fell to 6/13 (46.2%) vs pooled `base` 11/17 (64.7%), and `last_sort_is_revenue_desc` fell from 6/17 (35.3%) to 4/13 (30.8%).
- **Keep L2:** `data_sorting` in `Y` rose to 11/14 (78.6%), which is +32.4 pp over `X` (6/13, 46.2%) and clears the +15 pp keep rule (and also exceeds pooled `base` 64.7%).

### Harness confounders on `data_sorting`

- **Sheet-qualified ranges:** `eval_worlds.py` `parse_a1_range('Sheet1!A1:B6')` misparses the sheet prefix and returns `"no rows moved"`, whereas production `range_sort` handles sheet-qualified ranges.
- **Mixed-value descending order:** `eval_worlds.py` `range_sort` puts text after numbers in both directions, whereas LibreOffice (`sc/source/core/data/table3.cxx` `ScTable::CompareCell`) sorts number-before-string and negates on descending, so text sorts first when descending (empty cells stay last). Harness `data_sorting` passes therefore overstate production for `"Revenue descending, non-numeric last"`.

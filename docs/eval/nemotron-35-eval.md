# Nemotron 3.5 Lightning eval lift

Working plan to raise `nvidia/nemotron-3.5-lightning` on the 17-task
`--backend string` pack. Snapshot: 2026-09-01 **selective re-run** in
[`benchmark_results.json`](../../scripts/prompt_optimization/benchmark_results.json)
/ [`benchmark_results_details.json`](../../scripts/prompt_optimization/benchmark_results_details.json)
(same numbers in `benchmark_results_selective*.json`).

Sibling: [oss-20b-eval.md](oss-20b-eval.md). Related: [benchmarks.md](benchmarks.md),
[eval-dev-plan.md](eval-dev-plan.md), [string-harness-upgrade.md](string-harness-upgrade.md).

**Status:** analysis only. Use this file to A/B prompt and catalog patches.
Production ships **one** prompt per app — re-score **gpt-oss-20b** and
**gpt-oss-120b** on every shared Calc/Draw wording change.

## Snapshot

Do **not** treat the first ranking pass as the score. That run was OpenRouter
**429 on every task** (`benchmark_results_details_backup_2026-09-01.json`).
The table’s **6/17** is the completed selective re-run. [benchmarks.md](benchmarks.md)
insight 4 still talks as if Lightning were a 429-zero; it is not. Qwen3.8 Flash
is the model that stayed at 0/17 infra.

| | Nemotron 3.5 Lightning | gpt-oss-20b | gpt-oss-120b |
|---|---|---|---|
| Rank | 22 / 23 | 15 | 2 |
| Hard pass | **6/17 (0.353)** | 12/17 (0.706) | 17/17 |
| Correctness | 0.315 | 0.687 | 0.971 |
| Quality (judged passes) | 0.68 (n=2) | 0.89 | 0.90 |
| Tokens / task | **52334** | 16062 | 12263 |
| $ / task | 0.00432 | 0.00065 | 0.00054 |
| C²/$ | 10.2 | 537 | 1339 |
| `n_err` | 1 (`data_sorting` max rounds) | 0 | 0 |

30B total / **3B active** MoE. Reasoning is **on by default**. NVIDIA pitches
it as an execution-layer specialist under a bigger planner. Here it is the
**sole** chat agent. Token bill is 3.3× 20b; `data_sorting` alone is **346k
tokens** (~39% of the whole run) and hit `max_tool_rounds` (25).

## Passes vs fails

**Passed (6):** `table_from_mess`, `reformat_resume` (hard pass, judge **0.4**
— left `john doe` / the original summary), `bulk_cleanup`,
`format_preservation`, `style_application`, `comment_management`.

Simple Writer “read then `apply_document_content`” works. Multi-constraint
Writer edits and **all five Calc tasks** fail.

| Task | What Lightning did | Gate | Same as 20b? |
|------|--------------------|------|--------------|
| `logical_rewriting` | **No-op** — original hype still there (145 words) | leftover `incredibly` / `significant leap` / `brand new` | No. 20b rewrote. |
| `smart_summarization` | **No-op** — left `[To be filled by agent]`, 0 bullets | missing `99.9%` / `45ms` / `10k` / 5 bullets | No. 20b wrote 5 bullets, expanded `10k`. |
| `bullet_consistency` | List items became `<p>` without `- ` or trailing periods | missing bullet `'Pack the crate'` (etc.) | No. 20b kept `- Pack the crate.` |
| `style_consistency` | Promoted H2→H1, **deleted** the Default paragraphs | Quotations not on Default; content lost | No. 20b restyled to Quotations. |
| `section_refactor` | Renamed Conclusion→Goal, **dropped** the Body xref | missing `See the Goal` | No. 20b updated the xref. |
| `table_engineering` | HTML table OK; Total is **14.75 / 50** (column sums), not **51.4** (`price×qty`) | `extended Total is not 51.4` | Partial. 20b got 51.40, wrong column. |
| `flowchart_gen` | **Did** create Start / Login / diamond / End | missing Yes Decision→End and No→Process edges | No. 20b left `tree: []`. Yes/No became extra ellipses, not `shape_connect` labels. |
| `data_sorting` | `=PY` / JSON dumped into the grid; 25-round cap | `header row is not first`; `error=max_tool_rounds exceeded` | Same family as 20b, much worse. |
| `tax_column` | TSV rows smashed into one cell; quoted `"=A2*0.08"`; fake Invoice sheet | `no Tax column` | Worse than 20b’s copied-`B2` formulas. |
| `py_refuse_overlap` | Obeyed “put it in **H1**” (inside A1:H500); also wrote I1 | dest H1 overlaps | **20b passed** (J1). Phase F trap. |
| `py_no_bulk_read` | Wrote `=PY` **onto A1:H500** (and a pile of other cells) | dest overlaps; formula does not reference the range | **20b passed** (J1). |

## Shared with 20b vs Lightning-only

The 20b Calc dest / `sort_range` patches are **shared medicine** and matter
**more** here (Lightning failed both Phase F rows; 20b passed them). A Writer
preserve-tokens line (`NEMA 4`, `10k`) is **not shipped**: oracles accept
clarifying expansions, and Lightning's summarization fail was a no-op (it
never wrote the Executive Summary). `table_from_mess` already passed.

| Patch (from [oss-20b-eval.md](oss-20b-eval.md)) | Help Lightning? |
|------------------------------------------------|-----------------|
| Sort via `sort_range`, not in-place `=PY` | **Yes** — 346k-token death spiral |
| `=PY` dest outside DataRange even if the user said H1 | **Yes** — both Phase F rows |
| Relative formulas use *this* row (`Banana` → `B3`) | Weak — it never kept a Tax column |
| Draw: MUST delegate `domain="shapes"` | **No** — it already created shapes |
| Writer preserve-tokens tip (`10k`, `NEMA 4`) | **Not shipped** — oracles accept expansions |

Lightning-only (do not fold these into a 20b-only A/B):

- TSV/tabs are **columns**, not one string per row.
- Formulas are `=B2*0.08`, not quoted `"=A2*0.08"`.
- Do not write tool-result JSON back into cells.
- Stop after one successful `sort_range` (two stable one-column passes max).
- Yes/No on flowcharts are **connector labels** to End / Process, not extra ovals.
- If the exported document still matches the input, the task is not done.
- Do not delete paragraphs to apply a style; bullets stay bullets with periods;
  keep cross-references when renaming headings.

Realistic shared-prompt outcome: dest + `sort_range` might pick up **2–3 Calc
tasks** → ~8–9/17. Flowchart is one `shape_connect` hint away. Do not expect
the 20b band (12/17) from the 20b patches alone.

## A/B buckets

Keep diffs short. One bucket per eval pass. `-e` is a **single** `task_id`
(`run_eval.py` / `run_eval_multi.py`). `data_sorting` can burn hundreds of
thousands of tokens — run it last, with `-v`.

```bash
# one task (string harness)
python scripts/prompt_optimization/run_eval.py \
  -m nvidia/nemotron-3.5-lightning -e py_refuse_overlap -v

# same task, regression models
python scripts/prompt_optimization/run_eval.py -m openai/gpt-oss-20b -e py_refuse_overlap
python scripts/prompt_optimization/run_eval.py -m openai/gpt-oss-120b -e py_refuse_overlap

# full 17 after a winning bucket
make run_eval EVAL_ARGS="--models nvidia/nemotron-3.5-lightning -j 1"
```

Record hard pass / tokens / `error` / oracle+process fails per task. Baseline
is the selective-run row in `benchmark_results_details.json`.

### A — shared Calc (test Lightning + 20b + 120b)

Wording in `CALC_CORE_DIRECTIVES` / `CALC_WORKFLOW` and
`write_formula_range` / `sort_range` descriptions
([`plugin/framework/prompts.py`](../../plugin/framework/prompts.py),
[`plugin/calc/cells.py`](../../plugin/calc/cells.py)):

- Sort / reorder → `delegate_to_specialized_calc_toolset(domain="ranges")` then
  `sort_range`. Do **not** use `=PY` to sort in place.
- `=PY` dest is an empty cell **outside** DataRange (J1 / first empty column),
  even if the user asked for H1.
- `sort_range`: one-column, stable; two-key sorts are two calls.

Tasks: `py_refuse_overlap`, `py_no_bulk_read`, then `data_sorting`.
120b must stay pass. 20b must stay pass on the two Phase F rows.

### B — Lightning Calc grid literacy

Only if A does not fix `tax_column` (likely):

- TSV is columns; do not write a whole row as one cell.
- Do not quote formulas. Do not write tool JSON into the sheet.

Task: `tax_column`. Weak overlap with 20b’s B2/B3 line — still re-score 20b.

### C — Draw edges

- 20b needs “MUST delegate `domain=shapes`” ([oss-20b-eval.md](oss-20b-eval.md)).
- Lightning already did that. Add: Yes/No are `shape_connect` labels
  (Yes → End, No → Process), not extra shapes.

Task: `flowchart_gen`. Re-score 20b (empty tree today).

### D — Writer apply / don’t destroy

Lightning-primary. Cheap no-ops burned ~7k tokens each without changing the
doc.

- If the document still matches the input, the task is not done.
- Bullets stay list items and end with a period.
- Restyle Default → Quotations; do not delete those paragraphs.
- When renaming a heading, update cross-references.

Tasks: `logical_rewriting`, `smart_summarization`, `bullet_consistency`,
`style_consistency`, `section_refactor`. Re-score 20b on
`smart_summarization` (its fail is `10k`, not a no-op).

### E — thinking off (harness experiment, not a shipped prompt)

Reasoning is on by default. A/B this model only, same prompts as baseline:

- OpenRouter `reasoning` / `enable_thinking: false` (or equivalent
  `chat_template_kwargs`) if `LlmClient` can pass it through without a
  product change.

If hard pass stays ~6/17 with far fewer tokens, the score is skill. If
sort/tax improve, the loop is reasoning+tools not terminating. Do not
change the default for other models.

Optional: OpenRouter Exacto routing for tool-calling accuracy, same caveat.

## What not to do

- Treat the 429 backup as the model’s score.
- Run `python run_optimize.py` against the DSPy ReAct Writer program
  (same mismatch as 20b — see [oss-20b-eval.md](oss-20b-eval.md)).
- Ship Calc dest / `sort_range` wording without 20b + 120b on Phase F and
  `data_sorting`.
- Expect 12/17 from the 20b patches alone.
- Bulk `--generate-golds` before a ranking run.

## Expected lift

| Recover | Hard pass | Notes |
|---------|-----------|--------|
| Both Phase F dest rows | 8/17 ≈ 0.47 | Shared with 20b wording |
| + `data_sorting` if the loop stops | 9/17 ≈ 0.53 | Shared `sort_range` |
| + flowchart edges | 10/17 ≈ 0.59 | Lightning-specific connect hint |
| + Writer no-ops / bullets / style | 12–14/17 | Unlikely from Calc patches |

## Open

- [ ] Bucket A: dest + `sort_range` A/B on Lightning, 20b, 120b
- [ ] Bucket B: tax grid literacy if A leaves `tax_column` red
- [ ] Bucket C: flowchart Yes/No as edge labels; 20b empty-tree check
- [ ] Bucket D: Writer apply / don’t-destroy lines
- [ ] Bucket E: thinking-off token/pass A/B (Lightning only)
- [ ] Full 17 for Lightning after a winning bucket; 20b + 120b smoke
- [ ] Prompt-text pins in `tests/scripts/test_eval_prompts.py` for shipped wording

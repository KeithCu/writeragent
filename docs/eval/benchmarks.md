# LLM Evaluation Suite & Benchmarks

WriterAgent includes an in-LibreOffice **LLM Evaluation Suite** for real-world tasks in Writer, Calc, and Draw. Runs track accuracy and **Intelligence-per-Dollar**: **Value (C²/$)** = average metric score squared ÷ average dollars per task (higher is better), using live OpenRouter pricing where available.

Headed sibling (same hard / partial / cost axes, much harder tasks): [`eval-2/benchmarks.md`](eval-2/benchmarks.md). Do **not** merge those tables into this pack.

How to run evals from the repo: [scripts/prompt_optimization/README.md](../../scripts/prompt_optimization/README.md). Broader plan notes: [eval-dev-plan.md](eval-dev-plan.md). String harness (no LO ranking): [string-harness-upgrade.md](string-harness-upgrade.md).

## Snapshot ranking (2026-09-11)

**17-task string harness** (`--backend string`, OpenRouter). **2026-09-11 Calc fill-down refresh:** re-ran `data_sorting` and `tax_column` for the full catalog after Tip A/B (#729) + harness `expand_single_formula` (#733), so benches match honest fill-down scoring (single formula into a multi-cell range adjusts relative refs). Other 15 task rows are carried forward; full 17-task pack was **not** re-run. Not LO-backed — fidelity smoke only.

Artifacts: [`scripts/prompt_optimization/benchmark_results.json`](../../scripts/prompt_optimization/benchmark_results.json) and `benchmark_results_details.json`. Calc-only pack: `benchmark_results_calc_filldown_2026-09-11.json` (+ `_details`). Failure triage: [benchmark-failure-analysis-2026-09-01.md](benchmark-failure-analysis-2026-09-01.md) (Sep 1 full-pack notes). Nemotron Super vs Ultra Correctness inversion (2026-09-12 string-pack): [nemotron3-super-vs-ultra-string-pack.md](nemotron3-super-vs-ultra-string-pack.md).

Ranked by **hard pass → agent score → metric**. **Hard pass** = document substring + result oracles + process oracles, no API error. **Agent** = same gate including tool-process checks. **Quality** = LLM judge among creative/table passes only.

| Rank | Model | Hard pass | Agent | Correctness | Quality | Tokens/task | $/task | C²/$ |
| ---- | ---- | ------- | ------- | ------- | ------- | ------- | ------- | ------- |
| 1 | deepseek/deepseek-v4-flash-0731 | 1.000 | 1.000 | 0.987 | 0.96 | 44552 | 0.00350 | 154.4 |
| 2 | meta/muse-glimmer-30b | 1.000 | 1.000 | 0.987 | 0.96 | 26143 | 0.00982 | 53.6 |
| 3 | x-ai/grok-4.6 | 1.000 | 1.000 | 0.982 | 0.94 | 22031 | 0.04837 | 12.0 |
| 4 | meta/muse-spark-1.3-contributor | 1.000 | 1.000 | 0.979 | 0.93 | 25434 | 0.00270 | 194.2 |
| 5 | openai/gpt-oss-120b | 1.000 | 1.000 | 0.971 | 0.90 | 13525 | 0.00064 | 1092.9 |
| 6 | google/gemma-4-31b-it | 0.941 | 0.941 | 0.918 | 0.90 | 16144 | 0.00154 | 404.0 |
| 7 | bytedance-seed/seed-2.0-mini | 0.941 | 0.941 | 0.918 | 0.90 | 23135 | 0.00380 | 127.2 |
| 8 | openai/gpt-5.6-luna | 0.941 | 0.941 | 0.916 | 0.90 | 17901 | 0.00400 | 139.0 |
| 9 | poolside/laguna-xs-2.1 | 0.941 | 0.941 | 0.885 | 0.81 | 22171 | 0.00136 | 328.6 |
| 10 | deepseek/deepseek-v4.1-flash | 0.882 | 0.882 | 0.935 | 0.97 | 45666 | 0.00833 | 53.0 |
| 11 | qwen/qwen3.8-27b | 0.882 | 0.882 | 0.922 | 0.92 | 42008 | 0.02359 | 15.2 |
| 12 | inception/mercury-2.5-preview | 0.882 | 0.882 | 0.869 | 0.95 | 32048 | 0.00909 | 36.3 |
| 13 | z-ai/glm-5.3-flash | 0.882 | 0.882 | 0.854 | 0.90 | 40501 | 0.00394 | 107.5 |
| 14 | ibm-granite/granite-4.2-8b | 0.824 | 0.824 | 0.861 | 0.93 | 69261 | 0.00772 | 24.5 |
| 15 | nvidia/nemotron-3-ultra-550b-a55b | 0.824 | 0.824 | 0.821 | 0.74 | 55758 | 0.05576 | 4.5 |
| 16 | openai/gpt-oss-20b | 0.824 | 0.824 | 0.805 | 0.89 | 16666 | 0.00071 | 627.9 |
| 17 | qwen/qwen3.8-flash | 0.824 | 0.824 | 0.805 | 0.89 | 47587 | 0.00793 | 32.0 |
| 18 | minimax/minimax-m3 | 0.765 | 0.765 | 0.820 | 0.94 | 59174 | 0.02098 | 16.9 |
| 19 | upstage/solar-pro4 | 0.765 | 0.765 | 0.741 | 0.90 | 21216 | 0.00067 | 483.4 |
| 20 | google/gemma-4-26b-a4b-it | 0.765 | 0.765 | 0.739 | 0.89 | 19147 | 0.00142 | 234.6 |
| 21 | nvidia/nemotron-3-super-120b-a12b | 0.706 | 0.765 | 0.904 | 0.91 | 80850 | 0.01069 | 23.7 |
| 22 | poolside/laguna-s-2.1 | 0.706 | 0.706 | 0.759 | 0.90 | 21103 | 0.00214 | 158.5 |
| 23 | google/gemini-3.5-flash-lite | 0.706 | 0.706 | 0.747 | 0.93 | 15806 | 0.00542 | 72.6 |
| 24 | mistralai/mistral-small-2603 | 0.647 | 0.647 | 0.629 | 0.85 | 27441 | 0.00421 | 56.9 |
| 25 | nvidia/nemotron-3.5-lightning | 0.412 | 0.412 | 0.374 | 0.68 | 32346 | 0.00268 | 24.1 |

## Key insights

1. **Perfect hard pass (post fill-down splice):** Muse Glimmer/Spark, DeepSeek V4 Flash, Grok 4.6, and `openai/gpt-oss-120b` are back at **1.000** hard pass. Prior Calc drop on 120b/Gemma/Seed was largely a harness false-red: Tip A single-formula writes now fill-down-adjust in the string world (#733).
2. **Fill-down refresh movers:** largest hard recoveries were `gpt-oss-120b` (+0.118), `gemma-4-31b-it` / `seed-2.0-mini` / `laguna-xs-2.1` (+0.059 each). Soft drop: `z-ai/glm-5.3-flash` (−0.059 hard) on `data_sorting`. Luna / Qwen Flash / DeepSeek V4.1 Flash were re-run on Calc this time (scores unchanged; rows already passed).
3. **Calc/oracle hotspots (honest):** remaining fails are model-side — `tax_column` total-row tax (`gemini-3.5-flash-lite`), wrong sort direction/order (`mistral-small-2603`, `glm-5.3-flash`, `nemotron-3.5-lightning`). No `max_tool_rounds` on the spliced Calc rows.
4. **C²/$:** `openai/gpt-oss-120b` still leads Value after the recovery. Nemotron Ultra/Super 1658 rows now use catalog rates (`0.625/3.125` and `0.085/0.4` per 1M) × recorded `total_tokens` with a documented **85% prompt / 15% completion** split (details omitted the split; no OpenRouter usage in the run artifacts). Pareto plots still exclude `nvidia/nemotron-3.5-lightning` and `minimax/minimax-m3`.
5. **MiniMax M3 is in the table:** one non-Calc task (`format_preservation`) still carries a stream-normalizer contract bug (`type(delta) is dict`); not a blanket hold-out. Re-run that task after [stream-normalizer-delta-crash.md](stream-normalizer-delta-crash.md) is fixed.

## Scoring approach

Structural tasks are scored from the **exported final document** (HTML / Draw tree / Calc grid) via result oracles — not tool-name traces. Creative tasks (resume, logical rewriting, summarization) and the two table tasks use an LLM judge (default `openai/gpt-oss-120b:nitro`) plus gold references in `gold_standards.json` (hand-written from the rubrics).

**Fine-tuning direction:** the same eval signal (correct vs incorrect tool use, minimal vs verbose traces) could train a smaller specialist for this tool distribution—fewer tokens at similar correctness, better Value (C²/$).

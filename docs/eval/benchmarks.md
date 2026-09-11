# LLM Evaluation Suite & Benchmarks

WriterAgent includes an in-LibreOffice **LLM Evaluation Suite** for real-world tasks in Writer, Calc, and Draw. Runs track accuracy and **Intelligence-per-Dollar**: **Value (C²/$)** = average metric score squared ÷ average dollars per task (higher is better), using live OpenRouter pricing where available.

How to run evals from the repo: [scripts/prompt_optimization/README.md](../../scripts/prompt_optimization/README.md). Broader plan notes: [eval-dev-plan.md](eval-dev-plan.md). String harness (no LO ranking): [string-harness-upgrade.md](string-harness-upgrade.md).

## Snapshot ranking (2026-09-11)

**17-task string harness** (`--backend string`, OpenRouter). **2026-09-11 Calc refresh:** re-ran only `data_sorting` and `tax_column` for the catalog except `openai/gpt-5.6-luna`, `qwen/qwen3.8-flash`, and `deepseek/deepseek-v4.1-flash` (those three keep prior Calc rows from the same-day Luna/Qwen/V4.1 selective pack). Other 15 task rows are carried forward; full 17-task pack was **not** re-run. Not LO-backed — fidelity smoke only.

Artifacts: [`scripts/prompt_optimization/benchmark_results.json`](../../scripts/prompt_optimization/benchmark_results.json) and `benchmark_results_details.json`. Failure triage: [benchmark-failure-analysis-2026-09-01.md](benchmark-failure-analysis-2026-09-01.md) (Sep 1 full-pack notes).

Ranked by **hard pass → agent score → metric**. **Hard pass** = document substring + result oracles + process oracles, no API error. **Agent** = same gate including tool-process checks. **Quality** = LLM judge among creative/table passes only.

| Rank | Model | Hard pass | Agent | Correctness | Quality | Tokens/task | $/task | C²/$ |
| ---- | ---- | ------- | ------- | ------- | ------- | ------- | ------- | ------- |
| 1 | meta/muse-glimmer-30b | 1.000 | 1.000 | 0.987 | 0.96 | 27041 | 0.01025 | 50.1 |
| 2 | deepseek/deepseek-v4-flash-0731 | 1.000 | 1.000 | 0.987 | 0.96 | 46613 | 0.00369 | 138.7 |
| 3 | x-ai/grok-4.6 | 1.000 | 1.000 | 0.982 | 0.94 | 24613 | 0.05418 | 10.0 |
| 4 | meta/muse-spark-1.3-contributor | 1.000 | 1.000 | 0.979 | 0.93 | 28777 | 0.00307 | 155.7 |
| 5 | openai/gpt-5.6-luna | 0.941 | 0.941 | 0.916 | 0.90 | 20031 | 0.00449 | 116.6 |
| 6 | z-ai/glm-5.3-flash | 0.941 | 0.941 | 0.913 | 0.90 | 43404 | 0.00426 | 105.6 |
| 7 | deepseek/deepseek-v4.1-flash | 0.882 | 0.882 | 0.935 | 0.97 | 51291 | 0.00944 | 39.2 |
| 8 | bytedance-seed/seed-2.0-mini | 0.882 | 0.882 | 0.859 | 0.90 | 23817 | 0.00381 | 107.1 |
| 9 | openai/gpt-oss-120b | 0.882 | 0.882 | 0.853 | 0.90 | 12150 | 0.00056 | 999.4 |
| 10 | poolside/laguna-xs-2.1 | 0.882 | 0.882 | 0.826 | 0.81 | 38787 | 0.00238 | 155.6 |
| 11 | qwen/qwen3.8-27b | 0.824 | 0.824 | 0.922 | 0.92 | 54296 | 0.03051 | 9.3 |
| 12 | ibm-granite/granite-4.2-8b | 0.824 | 0.824 | 0.861 | 0.93 | 69637 | 0.00777 | 25.2 |
| 13 | inception/mercury-2.5-preview | 0.824 | 0.824 | 0.811 | 0.95 | 30675 | 0.00869 | 33.2 |
| 14 | qwen/qwen3.8-flash | 0.824 | 0.824 | 0.805 | 0.89 | 45001 | 0.00755 | 37.2 |
| 15 | google/gemma-4-31b-it | 0.824 | 0.824 | 0.800 | 0.90 | 17235 | 0.00165 | 289.3 |
| 16 | minimax/minimax-m3 | 0.765 | 0.765 | 0.820 | 0.94 | 63655 | 0.02252 | 13.4 |
| 17 | openai/gpt-oss-20b | 0.765 | 0.765 | 0.746 | 0.89 | 14664 | 0.00060 | 668.0 |
| 18 | upstage/solar-pro4 | 0.706 | 0.706 | 0.682 | 0.90 | 29828 | 0.00094 | 301.5 |
| 19 | google/gemma-4-26b-a4b-it | 0.706 | 0.706 | 0.680 | 0.89 | 19093 | 0.00141 | 197.0 |
| 20 | poolside/laguna-s-2.1 | 0.647 | 0.647 | 0.700 | 0.90 | 19806 | 0.00200 | 150.7 |
| 21 | google/gemini-3.5-flash-lite | 0.647 | 0.647 | 0.688 | 0.93 | 15741 | 0.00545 | 60.9 |
| 22 | mistralai/mistral-small-2603 | 0.588 | 0.588 | 0.571 | 0.85 | 17137 | 0.00267 | 89.5 |
| 23 | nvidia/nemotron-3.5-lightning | 0.353 | 0.353 | 0.315 | 0.68 | 29840 | 0.00245 | 18.1 |

## Key insights

1. **Perfect hard pass (post-Calc splice):** Muse Glimmer/Spark, DeepSeek V4 Flash, and Grok 4.6 remain at 1.000 hard pass. `openai/gpt-oss-120b` dropped to 0.882 after failing both Calc tasks on this refresh, but still leads **C²/$**.
2. **Calc refresh movers:** largest hard/correctness drops were `gpt-oss-120b`, `gemma-4-31b-it`, and `gemini-3.5-flash-lite` (−0.118 hard each). Gains: `ibm-granite/granite-4.2-8b` and `gpt-oss-20b` (+0.059 hard). Luna / Qwen Flash / DeepSeek V4.1 Flash were intentionally **not** re-run on Calc.
3. **Calc/oracle hotspots:** `tax_column` (relative 8% formula) and `data_sorting` (Revenue desc + Product tie-break) still separate the middle from the bottom; most failures are model-side, not harness bugs. Two `max_tool_rounds` errors: `laguna-xs-2.1` (`data_sorting`) and `qwen3.8-27b` (`tax_column`).
4. **Qwen3.8 Flash re-ran clean (non-Calc):** same-day Luna/Qwen/V4.1 selective pack scored Hard 0.824 / Correctness 0.805 (was infra 429 zeros at 0.118).
5. **MiniMax M3 is in the table:** one non-Calc task (`format_preservation`) still carries a stream-normalizer contract bug (`type(delta) is dict`); not a blanket hold-out. Re-run that task after [stream-normalizer-delta-crash.md](stream-normalizer-delta-crash.md) is fixed.

## Scoring approach

Structural tasks are scored from the **exported final document** (HTML / Draw tree / Calc grid) via result oracles — not tool-name traces. Creative tasks (resume, logical rewriting, summarization) and the two table tasks use an LLM judge (default `openai/gpt-oss-120b:nitro`) plus gold references in `gold_standards.json` (hand-written from the rubrics).

**Fine-tuning direction:** the same eval signal (correct vs incorrect tool use, minimal vs verbose traces) could train a smaller specialist for this tool distribution—fewer tokens at similar correctness, better Value (C²/$).

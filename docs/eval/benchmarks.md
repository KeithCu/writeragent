# LLM Evaluation Suite & Benchmarks

WriterAgent includes an in-LibreOffice **LLM Evaluation Suite** for real-world tasks in Writer, Calc, and Draw. Runs track accuracy and **Intelligence-per-Dollar**: **Value (C²/$)** = average metric score squared ÷ average dollars per task (higher is better), using live OpenRouter pricing where available.

How to run evals from the repo: [scripts/prompt_optimization/README.md](../../scripts/prompt_optimization/README.md). Broader plan notes: [eval-dev-plan.md](eval-dev-plan.md). String harness (no LO ranking): [string-harness-upgrade.md](string-harness-upgrade.md).

## Snapshot ranking (2026-09-05)

**17-task string harness** (`--backend string`, OpenRouter). Post-#616/#617 selective re-rank: only Calc `data_sorting` and `tax_column` were re-run on the remaining catalog after dropping `z-ai/glm-5.3` (keep `z-ai/glm-5.3-flash`); all other task rows are carried forward from the post-#613 (Sep 5) snapshot. Full 17-task pack was **not** re-run. Not LO-backed — fidelity smoke only.

Artifacts: [`scripts/prompt_optimization/benchmark_results.json`](../../scripts/prompt_optimization/benchmark_results.json) and `benchmark_results_details.json`. Failure triage: [benchmark-failure-analysis-2026-09-01.md](benchmark-failure-analysis-2026-09-01.md) (Sep 1 full-pack notes).

Ranked by **hard pass → agent score → metric**. **Hard pass** = document substring + result oracles + process oracles, no API error. **Agent** = same gate including tool-process checks. **Quality** = LLM judge among creative/table passes only.

| Rank | Model | Hard pass | Agent | Correctness | Quality | Tokens/task | $/task | C²/$ |
| ---- | ---- | ------- | ------- | ------- | ------- | ------- | ------- | ------- |
| 1 | deepseek/deepseek-v4-flash-0731 | 1.000 | 1.000 | 0.987 | 0.96 | 44789 | 0.00356 | 151.1 |
| 2 | meta/muse-glimmer-30b | 1.000 | 1.000 | 0.987 | 0.96 | 30319 | 0.01147 | 42.7 |
| 3 | x-ai/grok-4.6 | 1.000 | 1.000 | 0.982 | 0.94 | 21834 | 0.04871 | 12.0 |
| 4 | openai/gpt-5.6-luna | 1.000 | 1.000 | 0.981 | 0.94 | 19093 | 0.00439 | 142.1 |
| 5 | meta/muse-spark-1.3-contributor | 1.000 | 1.000 | 0.979 | 0.93 | 25641 | 0.00274 | 190.5 |
| 6 | openai/gpt-oss-120b | 1.000 | 1.000 | 0.971 | 0.90 | 15866 | 0.00073 | 902.8 |
| 7 | google/gemma-4-31b-it | 0.941 | 0.941 | 0.918 | 0.90 | 15706 | 0.00151 | 416.9 |
| 8 | bytedance-seed/seed-2.0-mini | 0.941 | 0.941 | 0.918 | 0.90 | 26715 | 0.00420 | 103.5 |
| 9 | z-ai/glm-5.3-flash | 0.941 | 0.941 | 0.913 | 0.90 | 43403 | 0.00431 | 104.2 |
| 10 | qwen/qwen3.8-27b | 0.882 | 0.882 | 0.922 | 0.92 | 44898 | 0.02603 | 12.5 |
| 11 | poolside/laguna-xs-2.1 | 0.882 | 0.882 | 0.826 | 0.81 | 19401 | 0.00119 | 344.2 |
| 12 | inception/mercury-2.5-preview | 0.824 | 0.824 | 0.811 | 0.95 | 31317 | 0.00885 | 31.8 |
| 13 | minimax/minimax-m3 | 0.765 | 0.765 | 0.820 | 0.94 | 58977 | 0.02090 | 17.1 |
| 14 | google/gemini-3.5-flash-lite | 0.765 | 0.765 | 0.806 | 0.93 | 16008 | 0.00561 | 79.4 |
| 15 | ibm-granite/granite-4.2-8b | 0.765 | 0.765 | 0.802 | 0.93 | 69505 | 0.00776 | 20.9 |
| 16 | upstage/solar-pro4 | 0.765 | 0.765 | 0.741 | 0.90 | 21716 | 0.00069 | 463.8 |
| 17 | poolside/laguna-s-2.1 | 0.706 | 0.706 | 0.759 | 0.90 | 21664 | 0.00220 | 151.3 |
| 18 | openai/gpt-oss-20b | 0.706 | 0.706 | 0.687 | 0.89 | 19290 | 0.00078 | 449.3 |
| 19 | google/gemma-4-26b-a4b-it | 0.706 | 0.706 | 0.680 | 0.89 | 18931 | 0.00140 | 199.3 |
| 20 | mistralai/mistral-small-2603 | 0.647 | 0.647 | 0.629 | 0.85 | 13419 | 0.00211 | 139.2 |
| 21 | nvidia/nemotron-3.5-lightning | 0.353 | 0.353 | 0.315 | 0.68 | 32965 | 0.00270 | 16.4 |
| 22 | qwen/qwen3.8-flash | 0.118 | 0.118 | 0.118 | — | 6815 | 0.00113 | 5.5 |

## Key insights

1. **Perfect hard pass (6 models):** DeepSeek V4 Flash, Muse Glimmer 30B, Grok 4.6, GPT-5.6 Luna, Muse Spark 1.3, and `openai/gpt-oss-120b` cleared every task on the hard gate after the selective Calc refresh. gpt-oss-120b still leads **C²/$** among the perfect set.
2. **Catalog trim:** `z-ai/glm-5.3` was dropped from the ranking catalog (expensive/overkill); `z-ai/glm-5.3-flash` remains and recovered to **0.941** hard pass after #616 sort prompt fixes.
3. **Calc selective (#616/#617):** Only `data_sorting` + `tax_column` were remeasured. Notable helps vs the post-#613 snapshot: `glm-5.3-flash` sort, `qwen/qwen3.8-27b` tax, `upstage/solar-pro4` sort+tax (among others). Other 15 tasks unchanged from #613.
4. **Do not over-read rate-limit zeros:** Nemotron 3.5 Lightning and Qwen3.8 Flash still carry many 429 rows from older full-pack runs — only sort/tax were refreshed for them.
5. **MiniMax M3:** Still carries the historical streaming-normalizer note; see [stream-normalizer-delta-crash.md](stream-normalizer-delta-crash.md).

## Scoring approach

Structural tasks are scored from the **exported final document** (HTML / Draw tree / Calc grid) via result oracles — not tool-name traces. Creative tasks (resume, logical rewriting, summarization) and the two table tasks use an LLM judge (default `openai/gpt-oss-120b:nitro`) plus gold references in `gold_standards.json` (hand-written from the rubrics).

**Fine-tuning direction:** the same eval signal (correct vs incorrect tool use, minimal vs verbose traces) could train a smaller specialist for this tool distribution—fewer tokens at similar correctness, better Value (C²/$).

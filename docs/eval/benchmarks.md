# LLM Evaluation Suite & Benchmarks

WriterAgent includes an in-LibreOffice **LLM Evaluation Suite** for real-world tasks in Writer, Calc, and Draw. Runs track accuracy and **Intelligence-per-Dollar**: **Value (C²/$)** = average metric score squared ÷ average dollars per task (higher is better), using live OpenRouter pricing where available.

How to run evals from the repo: [scripts/prompt_optimization/README.md](../../scripts/prompt_optimization/README.md). Broader plan notes: [eval-dev-plan.md](eval-dev-plan.md). String harness (no LO ranking): [string-harness-upgrade.md](string-harness-upgrade.md).

## Snapshot ranking (2026-09-11)

**17-task string harness** (`--backend string`, OpenRouter). Post-#616/#617 selective re-rank: only Calc `data_sorting` and `tax_column` were re-run on the remaining catalog after dropping `z-ai/glm-5.3` (keep `z-ai/glm-5.3-flash`); all other task rows are carried forward from the post-#613 (Sep 5) snapshot. Full 17-task pack was **not** re-run. Not LO-backed — fidelity smoke only.

Artifacts: [`scripts/prompt_optimization/benchmark_results.json`](../../scripts/prompt_optimization/benchmark_results.json) and `benchmark_results_details.json`. Failure triage: [benchmark-failure-analysis-2026-09-01.md](benchmark-failure-analysis-2026-09-01.md) (Sep 1 full-pack notes).

Ranked by **hard pass → agent score → metric**. **Hard pass** = document substring + result oracles + process oracles, no API error. **Agent** = same gate including tool-process checks. **Quality** = LLM judge among creative/table passes only.

| Rank | Model | Hard pass | Agent | Correctness | Quality | Tokens/task | $/task | C²/$ |
| ---- | ---- | ------- | ------- | ------- | ------- | ------- | ------- | ------- |
| 1 | deepseek/deepseek-v4-flash-0731 | 1.000 | 1.000 | 0.987 | 0.96 | 44789 | 0.00356 | 151.1 |
| 2 | meta/muse-glimmer-30b | 1.000 | 1.000 | 0.987 | 0.96 | 30319 | 0.01147 | 42.7 |
| 3 | x-ai/grok-4.6 | 1.000 | 1.000 | 0.982 | 0.94 | 21834 | 0.04871 | 12.0 |
| 4 | meta/muse-spark-1.3-contributor | 1.000 | 1.000 | 0.979 | 0.93 | 25641 | 0.00274 | 190.5 |
| 5 | openai/gpt-oss-120b | 1.000 | 1.000 | 0.971 | 0.90 | 15866 | 0.00073 | 902.8 |
| 6 | google/gemma-4-31b-it | 0.941 | 0.941 | 0.918 | 0.90 | 15706 | 0.00151 | 416.9 |
| 7 | bytedance-seed/seed-2.0-mini | 0.941 | 0.941 | 0.918 | 0.90 | 26715 | 0.00420 | 103.5 |
| 8 | openai/gpt-5.6-luna | 0.941 | 0.941 | 0.916 | 0.90 | 20031 | 0.00449 | 116.6 |
| 9 | z-ai/glm-5.3-flash | 0.941 | 0.941 | 0.913 | 0.90 | 43403 | 0.00431 | 104.2 |
| 10 | deepseek/deepseek-v4.1-flash | 0.882 | 0.882 | 0.935 | 0.97 | 51291 | 0.00944 | 39.2 |
| 11 | qwen/qwen3.8-27b | 0.882 | 0.882 | 0.922 | 0.92 | 44898 | 0.02603 | 12.5 |
| 12 | poolside/laguna-xs-2.1 | 0.882 | 0.882 | 0.826 | 0.81 | 19401 | 0.00119 | 344.2 |
| 13 | inception/mercury-2.5-preview | 0.824 | 0.824 | 0.811 | 0.95 | 31317 | 0.00885 | 31.8 |
| 14 | qwen/qwen3.8-flash | 0.824 | 0.824 | 0.805 | 0.89 | 45001 | 0.00755 | 37.2 |
| 15 | minimax/minimax-m3 | 0.765 | 0.765 | 0.820 | 0.94 | 58977 | 0.02090 | 17.1 |
| 16 | google/gemini-3.5-flash-lite | 0.765 | 0.765 | 0.806 | 0.93 | 16008 | 0.00561 | 79.4 |
| 17 | ibm-granite/granite-4.2-8b | 0.765 | 0.765 | 0.802 | 0.93 | 69505 | 0.00776 | 20.9 |
| 18 | upstage/solar-pro4 | 0.765 | 0.765 | 0.741 | 0.90 | 21716 | 0.00069 | 463.8 |
| 19 | poolside/laguna-s-2.1 | 0.706 | 0.706 | 0.759 | 0.90 | 21664 | 0.00220 | 151.3 |
| 20 | openai/gpt-oss-20b | 0.706 | 0.706 | 0.687 | 0.89 | 19290 | 0.00078 | 449.3 |
| 21 | google/gemma-4-26b-a4b-it | 0.706 | 0.706 | 0.680 | 0.89 | 18931 | 0.00140 | 199.3 |
| 22 | mistralai/mistral-small-2603 | 0.647 | 0.647 | 0.629 | 0.85 | 13419 | 0.00211 | 139.2 |
| 23 | nvidia/nemotron-3.5-lightning | 0.353 | 0.353 | 0.315 | 0.68 | 32965 | 0.00270 | 16.4 |

## Key insights

1. **Perfect hard pass:** `openai/gpt-oss-120b` and `x-ai/grok-4.6` cleared every task on the hard gate. Grok leads on raw correctness; gpt-oss-120b dominates **C²/$** (~$0.0005/task).
2. **Mid-tier cluster:** Gemma 4 31B, GPT-5.6 Luna, GLM 5.3 Flash, DeepSeek V4 Flash, and Muse Glimmer sit at ~94% hard pass — strong cost/quality tradeoffs below the top two.
3. **Calc/oracle hotspots:** `tax_column`, `data_sorting`, and `flowchart_gen` separated the middle from the bottom; most failures are model-side (wrong formulas, incomplete diagrams), not harness bugs.
4. **Qwen3.8 Flash re-ran clean:** selective 2026-09-11 string pack scored Hard 0.824 / Correctness 0.805 (was infra 429 zeros at 0.118).
5. **MiniMax M3 is in the table:** one task (`format_preservation`) hit a stream-normalizer contract bug (`type(delta) is dict`); not a blanket hold-out. Re-run that task after [stream-normalizer-delta-crash.md](stream-normalizer-delta-crash.md) is fixed.

## Scoring approach

Structural tasks are scored from the **exported final document** (HTML / Draw tree / Calc grid) via result oracles — not tool-name traces. Creative tasks (resume, logical rewriting, summarization) and the two table tasks use an LLM judge (default `openai/gpt-oss-120b:nitro`) plus gold references in `gold_standards.json` (hand-written from the rubrics).

**Fine-tuning direction:** the same eval signal (correct vs incorrect tool use, minimal vs verbose traces) could train a smaller specialist for this tool distribution—fewer tokens at similar correctness, better Value (C²/$).

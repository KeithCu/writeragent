# LLM Evaluation Suite & Benchmarks

WriterAgent includes an in-LibreOffice **LLM Evaluation Suite** for real-world tasks in Writer, Calc, and Draw. Runs track accuracy and **Intelligence-per-Dollar**: **Value (C²/$)** = average metric score squared ÷ average dollars per task (higher is better), using live OpenRouter pricing where available.

How to run evals from the repo: [scripts/prompt_optimization/README.md](../../scripts/prompt_optimization/README.md). Broader plan notes: [eval-dev-plan.md](eval-dev-plan.md). String harness (no LO ranking): [string-harness-upgrade.md](string-harness-upgrade.md).

## Snapshot ranking (2026-09-05)

**17-task string harness** (`--backend string`, OpenRouter). Post-#610 selective re-rank: only Calc `data_sorting` and `tax_column` were re-run on the full catalog; all other task rows are carried forward from the 2026-09-01 snapshot. Not LO-backed — fidelity smoke only.

Artifacts: [`scripts/prompt_optimization/benchmark_results.json`](../../scripts/prompt_optimization/benchmark_results.json) and `benchmark_results_details.json`. Failure triage: [benchmark-failure-analysis-2026-09-01.md](benchmark-failure-analysis-2026-09-01.md) (Sep 1 full-pack notes).

Ranked by **hard pass → agent score → metric**. **Hard pass** = document substring + result oracles + process oracles, no API error. **Agent** = same gate including tool-process checks. **Quality** = LLM judge among creative/table passes only.

| Rank | Model | Hard pass | Agent | Correctness | Quality | Tokens/task | $/task | C²/$ |
| ---- | ---- | ------- | ------- | ------- | ------- | ------- | ------- | ------- |
| 1 | deepseek/deepseek-v4-flash-0731 | 1.000 | 1.000 | 0.987 | 0.96 | 44832 | 0.00358 | 150.0 |
| 2 | x-ai/grok-4.6 | 1.000 | 1.000 | 0.982 | 0.94 | 21798 | 0.04881 | 12.0 |
| 3 | meta/muse-spark-1.3-contributor | 1.000 | 1.000 | 0.979 | 0.93 | 25343 | 0.00272 | 193.6 |
| 4 | openai/gpt-oss-120b | 1.000 | 1.000 | 0.971 | 0.90 | 13759 | 0.00061 | 1141.4 |
| 5 | meta/muse-glimmer-30b | 0.941 | 0.941 | 0.928 | 0.96 | 25248 | 0.00979 | 49.2 |
| 6 | openai/gpt-5.6-luna | 0.941 | 0.941 | 0.922 | 0.94 | 17229 | 0.00406 | 144.3 |
| 7 | google/gemma-4-31b-it | 0.941 | 0.941 | 0.918 | 0.90 | 15709 | 0.00151 | 416.0 |
| 8 | inception/mercury-2.5-preview | 0.882 | 0.882 | 0.869 | 0.95 | 32861 | 0.00927 | 34.6 |
| 9 | z-ai/glm-5.3-flash | 0.882 | 0.882 | 0.854 | 0.90 | 40017 | 0.00404 | 104.8 |
| 10 | qwen/qwen3.8-27b | 0.824 | 0.824 | 0.864 | 0.92 | 41102 | 0.02417 | 13.1 |
| 11 | openai/gpt-oss-20b | 0.824 | 0.824 | 0.805 | 0.89 | 15435 | 0.00062 | 747.5 |
| 12 | bytedance-seed/seed-2.0-mini | 0.824 | 0.824 | 0.800 | 0.90 | 33742 | 0.00530 | 68.9 |
| 13 | poolside/laguna-xs-2.1 | 0.824 | 0.824 | 0.767 | 0.81 | 19086 | 0.00118 | 297.6 |
| 14 | ibm-granite/granite-4.2-8b | 0.765 | 0.765 | 0.802 | 0.93 | 65978 | 0.00729 | 26.7 |
| 15 | minimax/minimax-m3 | 0.706 | 0.706 | 0.761 | 0.94 | 58078 | 0.02057 | 15.1 |
| 16 | poolside/laguna-s-2.1 | 0.706 | 0.706 | 0.759 | 0.90 | 19040 | 0.00192 | 189.2 |
| 17 | google/gemini-3.5-flash-lite | 0.706 | 0.706 | 0.747 | 0.93 | 14640 | 0.00509 | 76.7 |
| 18 | upstage/solar-pro4 | 0.647 | 0.647 | 0.624 | 0.90 | 20408 | 0.00065 | 363.2 |
| 19 | google/gemma-4-26b-a4b-it | 0.647 | 0.647 | 0.621 | 0.89 | 34203 | 0.00254 | 94.2 |
| 20 | z-ai/glm-5.3 | 0.588 | 0.588 | 0.644 | 0.97 | 29909 | 0.06128 | 3.2 |
| 21 | mistralai/mistral-small-2603 | 0.588 | 0.588 | 0.571 | 0.85 | 13720 | 0.00216 | 110.7 |
| 22 | nvidia/nemotron-3.5-lightning | 0.353 | 0.353 | 0.315 | 0.68 | 41237 | 0.00339 | 13.1 |
| 23 | qwen/qwen3.8-flash | 0.118 | 0.118 | 0.118 | — | 15473 | 0.00254 | 0.8 |

## Key insights

1. **Perfect hard pass:** `openai/gpt-oss-120b` and `x-ai/grok-4.6` cleared every task on the hard gate. Grok leads on raw correctness; gpt-oss-120b dominates **C²/$** (~$0.0005/task).
2. **Mid-tier cluster:** Gemma 4 31B, GPT-5.6 Luna, GLM 5.3 Flash, DeepSeek V4 Flash, and Muse Glimmer sit at ~94% hard pass — strong cost/quality tradeoffs below the top two.
3. **Calc/oracle hotspots:** `tax_column`, `data_sorting`, and `flowchart_gen` separated the middle from the bottom; most failures are model-side (wrong formulas, incomplete diagrams), not harness bugs.
4. **Do not read 429 zeros:** Nemotron 3.5 Lightning and Qwen3.8 Flash scored 0% only because OpenRouter rate-limited every task — excluded above.
5. **MiniMax M3 held out:** One task hit a streaming normalizer contract bug; re-run after [stream-normalizer-delta-crash.md](stream-normalizer-delta-crash.md) is fixed.

## Scoring approach

Structural tasks are scored from the **exported final document** (HTML / Draw tree / Calc grid) via result oracles — not tool-name traces. Creative tasks (resume, logical rewriting, summarization) and the two table tasks use an LLM judge (default `openai/gpt-oss-120b:nitro`) plus gold references in `gold_standards.json` (hand-written from the rubrics).

**Fine-tuning direction:** the same eval signal (correct vs incorrect tool use, minimal vs verbose traces) could train a smaller specialist for this tool distribution—fewer tokens at similar correctness, better Value (C²/$).

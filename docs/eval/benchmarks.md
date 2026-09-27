# LLM Evaluation Suite & Benchmarks

WriterAgent includes an **eval-1 LLM Evaluation Suite** for real-world tasks in Writer, Calc, and Draw. Runs track accuracy and **Intelligence-per-Dollar**: **Value (C²/$)** = average metric score squared ÷ average dollars per task (higher is better), using live OpenRouter pricing where available.

**Eval-1 is not string-only.** The same harness runs in-memory **or** native headless LibreOffice:

| Flag | Meaning |
|------|---------|
| `--backend string` | In-memory simulator (`string_eval_tools.py`). Skips rows that declare `backend=lo`. |
| `--backend lo` | Native headless UNO for every selected row (`tools_lo.py`, one soffice). |
| `--backend auto` | Per-task mix (honor each row's `backend`). Dual-lane: string pool overlaps one FIFO LO lane. |

Task 18 (`python_shapes_flag`) **needs native** headless LO; it cannot be faked on string. Headed **eval-2** is a separate harder suite ([`eval-2/benchmarks.md`](eval-2/benchmarks.md)) — do **not** treat native LO as eval-2-only, and do **not** merge eval-2 tables into this pack.

How to run: [scripts/prompt_optimization/README.md](../../scripts/prompt_optimization/README.md). Broader plan notes: [eval-dev-plan.md](eval-dev-plan.md). String-world upgrade history: [string-harness-upgrade.md](string-harness-upgrade.md).

## Snapshot ranking (2026-09-27 partial 18-task dual-lane)

**Partial dual-lane board** on tip `b48b43d6`: 17-task string base + spliced `python_shapes_flag` where available. Eval-1 backends: `string` = in-memory; `lo` = native headless UNO; `auto` = dual-lane mix. Task 18 **needs native**; headed eval-2 is separate — native is not eval-2-only.

**Catalog (this refresh):** add `openai/gpt-6-luna`, `cohere/command-a-plus`, `prism-ml/ternary-bonsai-2-27b`; drop `openai/gpt-5.6-luna`, `deepseek/deepseek-v4-flash-0731`. MiMos omitted (aborted string runs). **Flag-18 coverage is partial (11/26 models)** when this board was cut — Command A+ / Bonsai at n=17 without flag. Do not invent flag scores; follow-up when the serial remainder finishes.

`--backend string` still skips `python_shapes_flag`. Do not average a pure-string 17-pack with a mixed `--backend auto` 18-pack without saying so.

Artifacts: [`scripts/prompt_optimization/benchmark_results.json`](../../scripts/prompt_optimization/benchmark_results.json) and `benchmark_results_details.json`. Pareto: [pareto-fronts.svg](pareto-fronts.svg), [pareto-distance.svg](pareto-distance.svg). Failure triage: [benchmark-failure-analysis-2026-09-01.md](benchmark-failure-analysis-2026-09-01.md). Nemotron Super vs Ultra: [nemotron3-super-vs-ultra-string-pack.md](nemotron3-super-vs-ultra-string-pack.md).

Ranked by **hard pass → agent score → metric**. **Hard pass** = document substring + result oracles + process oracles, no API error. **Agent** = same gate including tool-process checks. **Quality** = LLM judge among creative/table passes only. Rows with n=17 lack a flag detail; n=18 include one.

| Rank | Model | Hard pass | Agent | Correctness | Quality | Tokens/task | $/task | C²/$ |
| ---- | ---- | ------- | ------- | ------- | ------- | ------- | ------- | ------- |
| 1 | openai/gpt-oss-120b | 1.000 | 1.000 | 0.972 | 0.90 | 14290 | 0.00081 | 845.1 |
| 2 | meta/muse-spark-1.3-contributor | 1.000 | 1.000 | 0.980 | 0.93 | 51153 | 0.00588 | 79.6 |
| 3 | x-ai/grok-4.6 | 0.944 | 0.944 | 0.969 | 0.94 | 29027 | 0.07547 | 6.9 |
| 4 | meta/muse-glimmer-30b | 0.944 | 0.944 | 0.974 | 0.96 | 30699 | 0.01335 | 35.2 |
| 5 | bytedance-seed/seed-2.0-mini | 0.941 | 0.941 | 0.918 | 0.90 | 23135 | 0.00335 | 144.1 |
| 6 | poolside/laguna-xs-2.1 | 0.941 | 0.941 | 0.885 | 0.81 | 22171 | 0.00153 | 293.0 |
| 7 | google/gemma-4-31b-it | 0.889 | 0.889 | 0.908 | 0.90 | 16989 | 0.00217 | 272.6 |
| 8 | deepseek/deepseek-v4.1-flash | 0.882 | 0.882 | 0.935 | 0.97 | 45666 | 0.00335 | 132.0 |
| 9 | z-ai/glm-5.3-flash | 0.882 | 0.882 | 0.854 | 0.90 | 40501 | 0.00410 | 103.2 |
| 10 | qwen/qwen3.8-27b | 0.882 | 0.882 | 0.922 | 0.92 | 42009 | 0.03124 | 11.5 |
| 11 | openai/gpt-6-luna | 0.833 | 0.833 | 0.866 | 0.93 | 22574 | 0.00361 | 120.5 |
| 12 | inception/mercury-2.5-preview | 0.833 | 0.833 | 0.863 | 0.95 | 36889 | 0.01199 | 24.6 |
| 13 | qwen/qwen3.8-flash | 0.824 | 0.824 | 0.805 | 0.89 | 47588 | 0.00942 | 27.0 |
| 14 | nvidia/nemotron-3-ultra-550b-a55b | 0.824 | 0.824 | 0.821 | 0.74 | 55758 | 0.05576 | 4.5 |
| 15 | ibm-granite/granite-4.2-8b | 0.824 | 0.824 | 0.861 | 0.93 | 69262 | 0.00745 | 25.4 |
| 16 | openai/gpt-oss-20b | 0.778 | 0.778 | 0.802 | 0.89 | 17516 | 0.00079 | 544.0 |
| 17 | minimax/minimax-m3 | 0.765 | 0.765 | 0.820 | 0.94 | 59175 | 0.02574 | 13.8 |
| 18 | upstage/solar-pro4 | 0.765 | 0.765 | 0.741 | 0.90 | 21216 | 0.00092 | 351.2 |
| 19 | google/gemma-4-26b-a4b-it | 0.722 | 0.722 | 0.739 | 0.89 | 20336 | 0.00225 | 141.6 |
| 20 | nvidia/nemotron-3-super-120b-a12b | 0.706 | 0.765 | 0.904 | 0.91 | 80850 | 0.01069 | 23.7 |
| 21 | poolside/laguna-s-2.1 | 0.706 | 0.706 | 0.759 | 0.90 | 21104 | 0.00218 | 155.0 |
| 22 | google/gemini-3.5-flash-lite | 0.667 | 0.667 | 0.747 | 0.93 | 15921 | 0.01003 | 38.9 |
| 23 | mistralai/mistral-small-2603 | 0.647 | 0.647 | 0.629 | 0.85 | 27442 | 0.00597 | 40.2 |
| 24 | cohere/command-a-plus | 0.647 | 0.647 | 0.687 | 0.94 | 71486 | 0.03431 | 5.0 |
| 25 | prism-ml/ternary-bonsai-2-27b | 0.529 | 0.529 | 0.585 | 0.97 | 29085 | 0.00404 | 38.3 |
| 26 | nvidia/nemotron-3.5-lightning | 0.389 | 0.389 | 0.395 | 0.68 | 33449 | 0.00328 | 19.5 |

## Key insights

1. **Partial flag-18 (honest):** Only 11/26 catalog models have `python_shapes_flag` rows in this cut. Hard-pass on flag so far: `openai/gpt-oss-120b`, `meta/muse-spark-1.3-contributor`. Other flag rows fail the oracle (do not invent scores). Command A+ / Ternary Bonsai boarded at n=17 pending remainder.
2. **Catalog churn:** GPT-6 Luna replaces GPT-5.6 Luna; DeepSeek Flash is `deepseek/deepseek-v4.1-flash` only (0731 dropped). MiMo flash/pro omitted after aborted string runs.
3. **Perfect hard pass (n=18):** Muse Spark 1.3 and `openai/gpt-oss-120b` hold 1.000 after splicing a passing flag. Grok / Muse Glimmer drop from 1.000 on string-17 to 0.944 once the failing flag is included.
4. **C²/$:** `openai/gpt-oss-120b` still leads Value. Costs for spliced averages use catalog rates × recorded `total_tokens` (85% prompt / 15% completion when the split is unknown).
5. **Pareto plots** regenerate from this JSON (`plot_pareto.py`); MiniMax remains on the board (one known stream-normalizer contract bug on a non-Calc task — see [stream-normalizer-delta-crash.md](stream-normalizer-delta-crash.md)).

## Scoring approach

Structural tasks are scored from the **exported final document** (HTML / Draw tree / Calc grid) via result oracles — not tool-name traces. Creative tasks (resume, logical rewriting, summarization) and the two table tasks use an LLM judge (default `openai/gpt-oss-120b:nitro`) plus gold references in `gold_standards.json` (hand-written from the rubrics).

**Fine-tuning direction:** the same eval signal (correct vs incorrect tool use, minimal vs verbose traces) could train a smaller specialist for this tool distribution—fewer tokens at similar correctness, better Value (C²/$).

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

## Snapshot ranking (2026-09-27 complete 26×18 dual-lane)

**Complete 26×18 dual-lane board** on tip artifacts after #939 + flag-18 `-j4` remainder: 17-task string base + `python_shapes_flag` for every catalog model (`n_examples=18`). Eval-1 backends: `string` = in-memory; `lo` = native headless UNO; `auto` = dual-lane mix. Task 18 **needs native**; headed eval-2 is separate — native is not eval-2-only.

**Catalog (this refresh):** add `openai/gpt-6-luna`, `cohere/command-a-plus`, `prism-ml/ternary-bonsai-2-27b`; drop `openai/gpt-5.6-luna`, `deepseek/deepseek-v4-flash-0731`. MiMos omitted (aborted string runs). **Flag-18 is complete (26/26)** — remainder finished with `-j4` on the #939 tip (`FLAG18_REMAINDER_DONE`). Scores come from spliced artifacts only (do not invent).

`--backend string` still skips `python_shapes_flag`. Do not average a pure-string 17-pack with a mixed `--backend auto` 18-pack without saying so.

Artifacts: [`scripts/prompt_optimization/benchmark_results.json`](../../scripts/prompt_optimization/benchmark_results.json) and `benchmark_results_details.json`. Pareto: [pareto-fronts.svg](pareto-fronts.svg), [pareto-distance.svg](pareto-distance.svg). Failure triage: [benchmark-failure-analysis-2026-09-01.md](benchmark-failure-analysis-2026-09-01.md). Nemotron Super vs Ultra: [nemotron3-super-vs-ultra-string-pack.md](nemotron3-super-vs-ultra-string-pack.md).

Ranked by **hard pass → agent score → metric**. **Hard pass** = document substring + result oracles + process oracles, no API error. **Agent** = same gate including tool-process checks. **Quality** = LLM judge among creative/table passes only. Every row is **n=18** (flag included).

| Rank | Model | Hard pass | Agent | Correctness | Quality | Tokens/task | $/task | C²/$ |
| ---- | ---- | ------- | ------- | ------- | ------- | ------- | ------- | ------- |
| 1 | openai/gpt-oss-120b | 1.000 | 1.000 | 0.972 | 0.90 | 14290 | 0.00081 | 845.1 |
| 2 | meta/muse-spark-1.3-contributor | 1.000 | 1.000 | 0.980 | 0.93 | 51153 | 0.00588 | 79.6 |
| 3 | x-ai/grok-4.6 | 0.944 | 0.944 | 0.969 | 0.94 | 29027 | 0.07547 | 6.9 |
| 4 | meta/muse-glimmer-30b | 0.944 | 0.944 | 0.974 | 0.96 | 30699 | 0.01335 | 35.2 |
| 5 | google/gemma-4-31b-it | 0.889 | 0.889 | 0.908 | 0.90 | 16989 | 0.00217 | 272.6 |
| 6 | bytedance-seed/seed-2.0-mini | 0.889 | 0.889 | 0.908 | 0.90 | 27980 | 0.00406 | 106.3 |
| 7 | poolside/laguna-xs-2.1 | 0.889 | 0.889 | 0.884 | 0.81 | 27844 | 0.00192 | 208.1 |
| 8 | deepseek/deepseek-v4.1-flash | 0.889 | 0.889 | 0.932 | 0.97 | 143119 | 0.01048 | 37.6 |
| 9 | openai/gpt-6-luna | 0.833 | 0.833 | 0.866 | 0.93 | 22574 | 0.00361 | 120.5 |
| 10 | z-ai/glm-5.3-flash | 0.833 | 0.833 | 0.848 | 0.90 | 80150 | 0.00812 | 46.5 |
| 11 | qwen/qwen3.8-27b | 0.833 | 0.833 | 0.913 | 0.92 | 135380 | 0.10069 | 3.2 |
| 12 | inception/mercury-2.5-preview | 0.833 | 0.833 | 0.863 | 0.95 | 36889 | 0.01199 | 24.6 |
| 13 | nvidia/nemotron-3-ultra-550b-a55b | 0.833 | 0.833 | 0.831 | 0.74 | 150734 | 0.15073 | 1.5 |
| 14 | openai/gpt-oss-20b | 0.778 | 0.778 | 0.802 | 0.89 | 17516 | 0.00079 | 544.0 |
| 15 | minimax/minimax-m3 | 0.778 | 0.778 | 0.823 | 0.94 | 130584 | 0.05680 | 5.6 |
| 16 | upstage/solar-pro4 | 0.778 | 0.778 | 0.756 | 0.90 | 33020 | 0.00144 | 201.3 |
| 17 | qwen/qwen3.8-flash | 0.778 | 0.778 | 0.781 | 0.89 | 44944 | 0.00890 | 27.7 |
| 18 | ibm-granite/granite-4.2-8b | 0.778 | 0.778 | 0.855 | 0.93 | 81475 | 0.00876 | 19.3 |
| 19 | google/gemma-4-26b-a4b-it | 0.722 | 0.722 | 0.739 | 0.89 | 20336 | 0.00225 | 141.6 |
| 20 | poolside/laguna-s-2.1 | 0.722 | 0.722 | 0.772 | 0.90 | 30484 | 0.00316 | 95.7 |
| 21 | nvidia/nemotron-3-super-120b-a12b | 0.667 | 0.722 | 0.902 | 0.91 | 125605 | 0.01661 | 13.6 |
| 22 | google/gemini-3.5-flash-lite | 0.667 | 0.667 | 0.747 | 0.93 | 15921 | 0.01003 | 38.9 |
| 23 | mistralai/mistral-small-2603 | 0.611 | 0.611 | 0.615 | 0.85 | 25917 | 0.00564 | 41.4 |
| 24 | cohere/command-a-plus | 0.611 | 0.611 | 0.677 | 0.94 | 68020 | 0.03265 | 5.3 |
| 25 | prism-ml/ternary-bonsai-2-27b | 0.500 | 0.500 | 0.573 | 0.97 | 28340 | 0.00393 | 37.4 |
| 26 | nvidia/nemotron-3.5-lightning | 0.389 | 0.389 | 0.395 | 0.68 | 33449 | 0.00328 | 19.5 |

## Key insights

1. **Flag-18 complete:** All 26 catalog models have `python_shapes_flag` (`n_examples=18`). Flag hard_pass: `openai/gpt-oss-120b`, `meta/muse-spark-1.3-contributor`, `deepseek/deepseek-v4.1-flash`, `minimax/minimax-m3`, `nvidia/nemotron-3-ultra-550b-a55b`, `poolside/laguna-s-2.1`, `upstage/solar-pro4`. Other flag rows fail the oracle honestly (do not invent scores).
2. **Catalog churn:** GPT-6 Luna replaces GPT-5.6 Luna; DeepSeek Flash is `deepseek/deepseek-v4.1-flash` only (0731 dropped). MiMo flash/pro omitted after aborted string runs.
3. **Perfect hard pass (n=18):** Muse Spark 1.3 and `openai/gpt-oss-120b` hold 1.000. Grok / Muse Glimmer sit at 0.944 (failing flag). Command A+ / Ternary Bonsai now boarded at n=18.
4. **C²/$:** `openai/gpt-oss-120b` still leads Value (correctness² ÷ $/task). Costs for spliced averages use catalog rates × recorded `total_tokens` (85% prompt / 15% completion when the split is unknown).
5. **Pareto plots** regenerate from this JSON (`plot_pareto.py`); MiniMax remains on the board (one known stream-normalizer contract bug on a non-Calc task — see [stream-normalizer-delta-crash.md](stream-normalizer-delta-crash.md)).

## Scoring approach

Structural tasks are scored from the **exported final document** (HTML / Draw tree / Calc grid) via result oracles — not tool-name traces. Creative tasks (resume, logical rewriting, summarization) and the two table tasks use an LLM judge (default `openai/gpt-oss-120b:nitro`) plus gold references in `gold_standards.json` (hand-written from the rubrics).

**Fine-tuning direction:** the same eval signal (correct vs incorrect tool use, minimal vs verbose traces) could train a smaller specialist for this tool distribution—fewer tokens at similar correctness, better Value (C²/$).

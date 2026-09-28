# LLM Evaluation Suite & Benchmarks

WriterAgent includes an **eval-1 LLM Evaluation Suite** for real-world tasks in Writer, Calc, and Draw. Runs track accuracy and **Intelligence-per-Dollar**: **Value (C²/$)** = average metric score squared ÷ average dollars per task (higher is better), using live OpenRouter pricing where available.

**Eval-1 is not string-only.** The same harness runs in-memory **or** native headless LibreOffice:

| Flag | Meaning |
|------|---------|
| `--backend string` | In-memory simulator (`string_eval_tools.py`). Skips rows that declare `backend=lo`. |
| `--backend lo` | Native headless UNO for every selected row (`tools_lo.py`, one soffice). |
| `--backend auto` | Per-task mix (honor each row's `backend`). Dual-lane: string pool overlaps one FIFO LO lane. |

Tasks 18–19 (`python_shapes_flag`, `org_chart_gen`) **need native** headless LO; they cannot be faked on string. Headed **eval-2** is a separate harder suite ([`eval-2/benchmarks.md`](eval-2/benchmarks.md)) — do **not** treat native LO as eval-2-only, and do **not** merge eval-2 tables into this pack.

How to run: [scripts/prompt_optimization/README.md](../../scripts/prompt_optimization/README.md). Broader plan notes: [eval-dev-plan.md](eval-dev-plan.md). String-world upgrade history: [string-harness-upgrade.md](string-harness-upgrade.md).

## Snapshot ranking (2026-09-28 complete 26×19 dual-lane)

**Complete 26×19 dual-lane board** on #941 tip after org_chart_gen matrix splice: prior 26×18 flag-18 board + `org_chart_gen` for every catalog model (`n_examples=19`). Eval-1 backends: `string` = in-memory; `lo` = native headless UNO; `auto` = dual-lane mix. Tasks 18–19 **need native**; headed eval-2 is separate — native is not eval-2-only.

**Catalog (unchanged from flag-18 refresh):** `openai/gpt-6-luna`, `cohere/command-a-plus`, `prism-ml/ternary-bonsai-2-27b` aboard; no `openai/gpt-5.6-luna` / `deepseek/deepseek-v4-flash-0731`. MiMos omitted (aborted string runs). **Org-chart hard PASS: 19/26** (scores from matrix artifacts only — do not invent). Ternary Bonsai hang → hard=0 kept.

`--backend string` still skips `python_shapes_flag` and `org_chart_gen`. Do not average a pure-string 17-pack with a mixed `--backend auto` 19-pack without saying so.

Artifacts: [`scripts/prompt_optimization/benchmark_results.json`](../../scripts/prompt_optimization/benchmark_results.json) and `benchmark_results_details.json`. Pareto: [pareto-fronts.svg](pareto-fronts.svg), [pareto-distance.svg](pareto-distance.svg). Failure triage: [benchmark-failure-analysis-2026-09-01.md](benchmark-failure-analysis-2026-09-01.md). Nemotron Super vs Ultra: [nemotron3-super-vs-ultra-string-pack.md](nemotron3-super-vs-ultra-string-pack.md).

Ranked by **hard pass → agent score → metric**. **Hard pass** = document substring + result oracles + process oracles, no API error. **Agent** = same gate including tool-process checks. **Quality** = LLM judge among creative/table passes only. Every row is **n=19** (flag + org chart included).

| Rank | Model | Hard pass | Agent | Correctness | Quality | Tokens/task | $/task | C²/$ |
| ---- | ---- | ------- | ------- | ------- | ------- | ------- | ------- | ------- |
| 1 | meta/muse-spark-1.3-contributor | 1.000 | 1.000 | 0.981 | 0.93 | 56766 | 0.00653 | 64.4 |
| 2 | meta/muse-glimmer-30b | 0.947 | 0.947 | 0.975 | 0.96 | 32858 | 0.01429 | 30.9 |
| 3 | x-ai/grok-4.6 | 0.947 | 0.947 | 0.971 | 0.94 | 36460 | 0.09480 | 4.9 |
| 4 | openai/gpt-oss-120b | 0.947 | 0.947 | 0.921 | 0.90 | 18554 | 0.00106 | 584.2 |
| 5 | google/gemma-4-31b-it | 0.895 | 0.895 | 0.913 | 0.90 | 17594 | 0.00224 | 261.3 |
| 6 | bytedance-seed/seed-2.0-mini | 0.895 | 0.895 | 0.913 | 0.90 | 28592 | 0.00415 | 103.1 |
| 7 | poolside/laguna-xs-2.1 | 0.895 | 0.895 | 0.890 | 0.81 | 28418 | 0.00196 | 203.2 |
| 8 | qwen/qwen3.8-27b | 0.842 | 0.842 | 0.917 | 0.92 | 133087 | 0.09898 | 2.9 |
| 9 | deepseek/deepseek-v4.1-flash | 0.842 | 0.842 | 0.883 | 0.97 | 138573 | 0.01015 | 34.8 |
| 10 | openai/gpt-6-luna | 0.842 | 0.842 | 0.873 | 0.93 | 34348 | 0.00550 | 71.1 |
| 11 | inception/mercury-2.5-preview | 0.842 | 0.842 | 0.870 | 0.95 | 36864 | 0.01198 | 25.0 |
| 12 | z-ai/glm-5.3-flash | 0.842 | 0.842 | 0.856 | 0.90 | 83249 | 0.00843 | 40.2 |
| 13 | minimax/minimax-m3 | 0.789 | 0.789 | 0.832 | 0.94 | 126882 | 0.05519 | 5.5 |
| 14 | openai/gpt-oss-20b | 0.789 | 0.789 | 0.812 | 0.89 | 24456 | 0.00110 | 349.7 |
| 15 | qwen/qwen3.8-flash | 0.789 | 0.789 | 0.792 | 0.89 | 49095 | 0.00972 | 22.8 |
| 16 | nvidia/nemotron-3-ultra-550b-a55b | 0.789 | 0.789 | 0.787 | 0.74 | 145858 | 0.14586 | 1.4 |
| 17 | upstage/solar-pro4 | 0.789 | 0.789 | 0.768 | 0.90 | 33501 | 0.00146 | 199.9 |
| 18 | ibm-granite/granite-4.2-8b | 0.737 | 0.737 | 0.810 | 0.93 | 110856 | 0.01192 | 12.7 |
| 19 | poolside/laguna-s-2.1 | 0.737 | 0.737 | 0.784 | 0.90 | 32409 | 0.00335 | 86.3 |
| 20 | google/gemma-4-26b-a4b-it | 0.737 | 0.737 | 0.753 | 0.89 | 20530 | 0.00227 | 145.4 |
| 21 | nvidia/nemotron-3-super-120b-a12b | 0.684 | 0.737 | 0.907 | 0.91 | 143994 | 0.01904 | 10.7 |
| 22 | google/gemini-3.5-flash-lite | 0.684 | 0.684 | 0.761 | 0.93 | 16160 | 0.01018 | 39.4 |
| 23 | cohere/command-a-plus | 0.632 | 0.632 | 0.694 | 0.94 | 66553 | 0.03195 | 5.6 |
| 24 | mistralai/mistral-small-2603 | 0.579 | 0.579 | 0.583 | 0.85 | 24553 | 0.00534 | 39.2 |
| 25 | prism-ml/ternary-bonsai-2-27b | 0.474 | 0.474 | 0.543 | 0.97 | 27120 | 0.00376 | 35.0 |
| 26 | nvidia/nemotron-3.5-lightning | 0.368 | 0.421 | 0.395 | 0.68 | 34466 | 0.00338 | 17.0 |

## Key insights

1. **Org-chart-19 complete:** All 26 catalog models have `python_shapes_flag` + `org_chart_gen` (`n_examples=19`). Org-chart hard PASS (19): Gemma 4 31B/26B, gpt-oss-20b, Gemini 3.5 Flash Lite, GPT-6 Luna, Mercury 2.5, Muse Glimmer/Spark, Grok 4.6, Laguna XS/S, Qwen 3.8 Flash/27B, Solar Pro 4, Seed 2 Mini, GLM 5.3 Flash, MiniMax M3, Nemotron Super, Command A+. Fail (7): gpt-oss-120b, Nemotron 3.5 Lightning, Mistral Small 2603, Granite 4.2, DeepSeek V4.1F, Nemotron Ultra, Ternary Bonsai (hang → 0). Do not invent scores.
2. **Catalog unchanged** from flag-18 refresh; MiMos still omitted.
3. **Perfect hard pass (n=19):** only `meta/muse-spark-1.3-contributor` remains at 1.000 (`openai/gpt-oss-120b` drops to 0.947 after failing org chart). Muse Glimmer / Grok sit at 0.947.
4. **C²/$:** `openai/gpt-oss-120b` still leads Value (correctness² ÷ $/task) despite the org-chart fail. Costs for spliced averages use catalog rates × recorded `total_tokens` (85% prompt / 15% completion when the split is unknown).
5. **Pareto plots** regenerate from this JSON (`plot_pareto.py`); MiniMax remains on the board (one known stream-normalizer contract bug on a non-Calc task — see [stream-normalizer-delta-crash.md](stream-normalizer-delta-crash.md)).

## Scoring approach

Structural tasks are scored from the **exported final document** (HTML / Draw tree / Calc grid) via result oracles — not tool-name traces. Creative tasks (resume, logical rewriting, summarization) and the two table tasks use an LLM judge (default `openai/gpt-oss-120b:nitro`) plus gold references in `gold_standards.json` (hand-written from the rubrics).

**Fine-tuning direction:** the same eval signal (correct vs incorrect tool use, minimal vs verbose traces) could train a smaller specialist for this tool distribution—fewer tokens at similar correctness, better Value (C²/$).

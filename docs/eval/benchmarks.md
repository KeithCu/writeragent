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

## Snapshot ranking (29×19 dual-lane)

Infra-error and tool-round-limit rows were retried with `max_tool_rounds=50`. Full per-task data: [HF dataset KeithCu/writeragent-eval-1](https://huggingface.co/datasets/KeithCu/writeragent-eval-1).

> **Org-chart scoring needs work:** the code oracle for `org_chart_gen` is too loose (many false PASSes vs gallery PNGs). This snapshot uses **hand visual overrides** ([org-chart hand scores](org-chart-hand-scores-2026-09-28.md)); a vision/scoring model may replace or augment the oracle later.

**Complete 29×19 dual-lane board** with **hand-scored gallery overrides** ([org-chart hand scores](org-chart-hand-scores-2026-09-28.md)): flag-18 tasks + `org_chart_gen` for every catalog model (`n_examples=19`). Eval-1 backends: `string` = in-memory; `lo` = native headless UNO; `auto` = dual-lane mix. Tasks 18–19 **need native**; headed eval-2 is separate — native is not eval-2-only.

**Catalog:** `openai/gpt-6-luna`, `cohere/command-a-plus`, `prism-ml/ternary-bonsai-2-27b` aboard; no `openai/gpt-5.6-luna` / `deepseek/deepseek-v4-flash-0731`. MiMos omitted (aborted string runs). **Org-chart hard PASS (hand-scored visual): 5/26** — catalog oracle PASS overridden by gallery grades; soft/borderline also FAIL. Ternary Bonsai hang → hard=0 kept. See [org-chart hand scores](org-chart-hand-scores-2026-09-28.md).

`--backend string` still skips `python_shapes_flag` and `org_chart_gen`. Do not average a pure-string 17-pack with a mixed `--backend auto` 19-pack without saying so.

Artifacts: [`scripts/prompt_optimization/benchmark_results.json`](../../scripts/prompt_optimization/benchmark_results.json) and `benchmark_results_details.json`. Pareto: [pareto-fronts.svg](pareto-fronts.svg), [pareto-distance.svg](pareto-distance.svg). Nemotron Super vs Ultra: [nemotron3-super-vs-ultra-string-pack.md](nemotron3-super-vs-ultra-string-pack.md).

Ranked by **hard pass → agent score → metric**. **Hard pass** = document substring + result oracles + process oracles, no API error. **Agent** = same gate including tool-process checks. **Quality** = LLM judge among creative/table passes only. Every row is **n=19** (flag + org chart included).

| Rank | Model | Hard pass | Agent | Correctness | Quality | Tokens/task | $/task | C²/$ |
| ---- | ---- | ------- | ------- | ------- | ------- | ------- | ------- | ------- |
| 1 | meta/muse-spark-1.3-contributor | 1.000 | 1.000 | 0.981 | 0.93 | 56766 | 0.00653 | 64.4 |
| 2 | nvidia/nemotron-3.5-super-vl | 0.947 | 0.947 | 0.932 | 0.94 | 54511 | 0.01104 | 34.1 |
| 3 | deepseek/deepseek-v4.1-flash | 0.947 | 0.947 | 0.921 | 0.93 | 139971 | 0.01025 | 36.4 |
| 4 | openai/gpt-oss-120b | 0.947 | 0.947 | 0.921 | 0.90 | 18554 | 0.00106 | 584.2 |
| 5 | meta/muse-glimmer-30b | 0.895 | 0.895 | 0.923 | 0.96 | 32858 | 0.01429 | 29.5 |
| 6 | x-ai/grok-4.6 | 0.895 | 0.895 | 0.918 | 0.94 | 36460 | 0.09480 | 4.9 |
| 7 | qwen/qwen3.8-27b | 0.895 | 0.895 | 0.917 | 0.92 | 120710 | 0.08978 | 3.8 |
| 8 | inception/mercury-3 | 0.895 | 0.895 | 0.906 | 0.89 | 43867 | 0.00603 | 52.4 |
| 9 | reflection/beam-1.0-501b-a23b | 0.842 | 0.842 | 0.879 | 0.93 | 148577 | 0.05386 | 7.7 |
| 10 | openai/gpt-6-luna | 0.842 | 0.842 | 0.873 | 0.93 | 34348 | 0.00550 | 71.1 |
| 11 | qwen/qwen3.8-flash | 0.842 | 0.842 | 0.865 | 0.89 | 56566 | 0.01120 | 19.3 |
| 12 | bytedance-seed/seed-2.0-mini | 0.842 | 0.842 | 0.861 | 0.90 | 28592 | 0.00415 | 93.3 |
| 13 | google/gemma-4-31b-it | 0.842 | 0.842 | 0.861 | 0.90 | 17594 | 0.00224 | 236.2 |
| 14 | z-ai/glm-5.3-flash | 0.842 | 0.842 | 0.856 | 0.90 | 83249 | 0.00843 | 40.2 |
| 15 | prism-ml/ternary-bonsai-2-27b | 0.842 | 0.842 | 0.843 | 0.93 | 39831 | 0.00553 | 46.9 |
| 16 | poolside/laguna-xs-2.1 | 0.842 | 0.842 | 0.838 | 0.81 | 28418 | 0.00196 | 183.0 |
| 17 | nvidia/nemotron-3-ultra-550b-a55b | 0.789 | 0.789 | 0.840 | 0.74 | 161718 | 0.16172 | 1.4 |
| 18 | poolside/laguna-s-2.1 | 0.789 | 0.789 | 0.777 | 0.92 | 33308 | 0.00345 | 87.5 |
| 19 | minimax/minimax-m3 | 0.789 | 0.789 | 0.738 | 0.57 | 127380 | 0.05541 | 4.3 |
| 20 | nvidia/nemotron-3-super-120b-a12b | 0.737 | 0.789 | 0.861 | 0.91 | 119112 | 0.01575 | 15.4 |
| 21 | openai/gpt-oss-20b | 0.737 | 0.737 | 0.759 | 0.89 | 24456 | 0.00110 | 349.7 |
| 22 | ibm-granite/granite-4.2-8b | 0.737 | 0.737 | 0.757 | 0.93 | 69218 | 0.00744 | 20.4 |
| 23 | upstage/solar-pro4 | 0.737 | 0.737 | 0.716 | 0.90 | 33501 | 0.00146 | 178.0 |
| 24 | google/gemma-4-26b-a4b-it | 0.684 | 0.684 | 0.701 | 0.89 | 20530 | 0.00227 | 125.9 |
| 25 | google/gemini-3.5-flash-lite | 0.632 | 0.632 | 0.708 | 0.93 | 16160 | 0.01018 | 34.4 |
| 26 | cohere/command-a-plus | 0.632 | 0.632 | 0.694 | 0.94 | 59928 | 0.02877 | 6.3 |
| 27 | liquid/lfm-2.5-24b-a2b | 0.579 | 0.579 | 0.589 | 0.89 | 33805 | 0.00499 | 30.0 |
| 28 | mistralai/mistral-small-2603 | 0.579 | 0.579 | 0.583 | 0.85 | 24553 | 0.00534 | 39.2 |
| 29 | nvidia/nemotron-3.5-lightning | 0.368 | 0.421 | 0.395 | 0.68 | 34466 | 0.00338 | 17.0 |

## Key insights

1. **Org-chart-19 + hand visual grades:** All 26 catalog models have `python_shapes_flag` + `org_chart_gen` (`n_examples=19`). Catalog oracle had 19 PASS / 7 FAIL; **hand gallery overrides** leave **5 visual HARD PASS**: `meta/muse-spark-1.3-contributor`, `qwen/qwen3.8-flash`, `qwen/qwen3.8-27b`, `z-ai/glm-5.3-flash`, `openai/gpt-6-luna`. Soft/borderline (Grok 4.6, Muse Glimmer, Laguna S) and other oracle false PASSes score org hard=0. Catalog FAILs (gpt-oss-120b, Nemotron 3.5 Lightning, Mistral Small 2603, Granite 4.2, DeepSeek V4.1F, Nemotron Ultra, Ternary Bonsai hang → 0). Grades: [org-chart hand scores](org-chart-hand-scores-2026-09-28.md).
2. **Catalog:** MiMos omitted.
3. **Perfect hard pass (n=19):** only `meta/muse-spark-1.3-contributor` remains at 1.000. `openai/gpt-oss-120b` stays 0.947 (catalog org fail). Muse Glimmer / Grok drop to 0.895 after visual org FAIL.
4. **C²/$:** `openai/gpt-oss-120b` still leads Value (correctness² ÷ $/task). Costs for spliced averages use catalog rates × recorded `total_tokens` (85% prompt / 15% completion when the split is unknown).
5. **Pareto plots** regenerate from this JSON (`plot_pareto.py`); MiniMax remains on the board (one known stream-normalizer contract bug on a non-Calc task — see [stream-normalizer-delta-crash.md](stream-normalizer-delta-crash.md)).
6. **Org-chart oracle gap:** structural box/connector floors still admit false PASSes (blobs, stacks, overlaps). Hand grades override for this board; tighten code scoring or add a vision judge later — see [org-chart hand scores](org-chart-hand-scores-2026-09-28.md).

## Scoring approach

Structural tasks are scored from the **exported final document** (HTML / Draw tree / Calc grid) via result oracles — not tool-name traces. Creative tasks (resume, logical rewriting, summarization) and the two table tasks use an LLM judge (default `openai/gpt-oss-120b:nitro`) plus gold references in `gold_standards.json` (hand-written from the rubrics).

**Fine-tuning direction:** the same eval signal (correct vs incorrect tool use, minimal vs verbose traces) could train a smaller specialist for this tool distribution—fewer tokens at similar correctness, better Value (C²/$).

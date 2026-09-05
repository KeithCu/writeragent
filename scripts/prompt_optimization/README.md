# Writer prompt optimization with DSPy

This folder implements the DSPy-based optimization of `DEFAULT_CHAT_SYSTEM_PROMPT` for WriterAgent (see plan in repo). String harness: live `ToolRegistry.get_schemas` + `get_chat_system_prompt_for_document` plus eval note — [`docs/eval/string-harness-upgrade.md`](../../docs/eval/string-harness-upgrade.md). Phase F `=PY()` dest rows are in the dataset.

## Benchmarks from repo root

```bash
git clone …/writeragent && cd writeragent
uv sync
make eval-deps                    # uv pip install dspy-ai (eval + optimize only)
export OPENROUTER_API_KEY=sk-…   # or OPENAI_API_KEY / WRITERAGENT_API_KEY
make run_eval-smoke               # one model, one example
make run_eval EVAL_ARGS="--models openai/gpt-oss-120b:nitro -n 2 -j 1"
```

Local OpenAI-compatible (Ollama, vLLM, etc.):

```bash
export OPENAI_API_BASE=http://127.0.0.1:11434/v1
make run_eval EVAL_ARGS="--model llama3.2 --allow-unknown-model -n 1 -j 1"
# Judge defaults to the same model on non-OpenRouter endpoints.
```

Wrapper: [`scripts/benchmark.py`](../benchmark.py). Credentials: [`eval_auth.py`](eval_auth.py) (CLI/env → `LlmClient` config; judge uses same HTTP stack as chat).

## Setup (this directory)

```bash
uv pip install -r requirements.txt   # or: make eval-deps from repo root
```

**Defaults: OpenRouter** with **openai/gpt-oss-120b:nitro** (see `DEFAULT_EVAL_STUDENT_MODEL` in `model_configs.py`; `:nitro` is OpenRouter routing, same prices as `openai/gpt-oss-120b`). API key (first match wins):

- `--api-key` / `-k`, then `WRITERAGENT_API_KEY`, `OPENAI_API_KEY`, `OPENROUTER_API_KEY`

Endpoint:

- `--api-base` / `WRITERAGENT_API_BASE`, `OPENAI_API_BASE` — default `https://openrouter.ai/api/v1`

Judge model (`run_eval_multi.py`):

- `--judge` / `WRITERAGENT_JUDGE_MODEL`, then `openai/gpt-oss-120b:nitro` on OpenRouter, else first `--models` id on other endpoints
- `--no-judge` — substring checks only

Override model for optimize:

- `python run_optimize.py --model google/gemini-3.5-flash-lite` / `--api-base ...` / `--api-key ...`

## Run

**Eval only (see per-example success without optimizing):**

```bash
export OPENROUTER_API_KEY="your-key"
python run_eval.py                          # all examples (needs a key; default --student llm)
python run_eval.py -e table_from_mess       # one task_id
python run_eval.py -n 2                     # first 2 examples
python run_eval.py -v                       # verbose: print every tool call as it runs
python run_eval.py --compare-with optimized_writer_prompt.json   # compare current vs optimized
python run_eval.py --no-bust-cache   # disable cache-busting (default: on)
python run_eval.py --backend string --student scripted -v   # no API key; full pack
python run_eval.py --backend lo --student scripted --no-bust-cache -v   # headless LO, no key
# or from repo root: make run_eval-lo-scripted
```

Shows for each example: task_id, expected/reject/oracle pass or miss, correctness, tokens, score, and a short doc snippet. Pytest covers the string pack (`tests/scripts/test_scripted_eval_pack.py`). The LO pack is skipped unless `soffice` and real `uno` are importable. Do **not** set `WRITERAGENT_TESTING=1` for LO eval. Do not use `tests/eval_runner.py`. Use `-v`/`--verbose` to print each tool call. Use `--compare-with` to run both the current prompt and the prompt from a DSPy JSON file, then report which scores higher. Cache-busting is enabled by default (unique suffix per example) to avoid OpenRouter prompt cache; use `--no-bust-cache` to disable.

**Full optimization (MIPROv2):**

```bash
export OPENROUTER_API_KEY="your-key"
python run_optimize.py
```

Pick a different model:

```bash
python run_optimize.py --model google/gemini-3.5-flash-lite
python run_optimize.py -m openai/gpt-oss-120b:nitro -k sk-...
```

This runs MIPROv2 in **0-shot instruction-only** mode: it proposes alternative system prompts and keeps the one that scores best on the **judge-based metric** (same LLM-as-a-Judge as `run_eval_multi`, plus token penalty). Output is saved to `optimized_writer_prompt.json`.

- **`--judge`** / **`-J`**: Judge model for grading (default `openai/gpt-oss-120b:nitro`). Same dataset and `gold_standards.json` as run_eval_multi. Golds are hand-written from the rubrics; `--generate-golds` is an optional teacher merge, not a ranking prerequisite.
- **`-j N`** / **`--jobs N`**: parallel evals (default 4).
- **`--auto light|medium|heavy`**: exploration level (default `light`). Use `medium` or `heavy` for more tries when your prompt is complicated.
- **`-t N`** / **`--trials N`**: explicit number of Bayesian optimization trials (overrides `--auto`; uses more exploration).

## Metric

Optimization and multi-model eval use **result oracles** for structural tasks (`oracles.py` on the exported final document). **LLM-as-a-Judge** (default **`openai/gpt-oss-120b:nitro`**) is for quality after the hard gate.

- **Dual-Mode Scoring**: Hard gate first (substring + result oracles + process oracles). A quality judge runs after that gate for resume, rewriting, summarization, and the two table tasks:
    - **Creative** (resume, rewriting, summarization): 50% accuracy, 20% formatting, 30% naturalness.
    - **Tables** (`table_from_mess`, `table_engineering`): 20% accuracy, 80% formatting.
    - Other structural tasks stay oracle-only (no judge).
- **Chain-of-Thought**: Judges output a `thought_process` before assigning 1-5 sub-scores for each dimension.
- **Internal Normalization**: Sub-scores are normalized and weighted into a final 0.0–1.0 score.
- **Token penalty**: `score -= 0.01 * (total_tokens / 1000)` so fewer tokens improve the score.

## Dataset

`dataset.py` `ALL_EXAMPLES` is **17 tasks**: 12 Writer (the original 8 plus `style_consistency`, `smart_summarization`, `section_refactor`, `comment_management`) plus `flowchart_gen` (Draw), `data_sorting` / `tax_column` (Calc), and two Phase F `=PY` dest rows (`py_refuse_overlap`, `py_no_bulk_read`). Each has fixed `document_content` and `user_question` so runs are comparable. Kind is keyed by `task_id` (`task_kind()`), not question keywords.

Hard pass is the **exported final document** plus process oracles (`oracles.py` / `process_oracles.py`). A quality LLM judge runs **after** that gate for resume, rewriting, summarization, and the two table tasks. Eval does not set sampling temperature. `gold_standards.json` is hand-written from the rubrics (no live teacher API unless `--generate-golds`). Specialized Draw/Calc tools are reached through an inner `LlmClient` loop (`domain="shapes"` / `"ranges"`), not SmolAgents.

## Tool subset

`--backend string` (default) is an in-memory simulator (`string_eval_tools.py`). `--backend lo` is **headless UNO**: `tools_lo.py` starts `soffice --headless`, serializes all UNO onto `_lo_thread` via `LOBackend.call`, and executes production tools with `bypass_thread_guard=True`. Do not use `tests/eval_runner.py` or `make lo-start` for this path.

`--student scripted` replays `scripted_student.SCRIPTS` (no `LlmClient`, no API key, result oracles + honest substring checks). `--student llm` (default) uses a live model and still needs a key. `--no-judge` skips the quality judge.

`-j N` in `run_eval_multi.py` is **ThreadPoolExecutor** over **models** (default **20**; each model still runs its 17 tasks serially). UNO is already serialized on `_lo_thread`. Do **not** `ProcessPoolExecutor` against one soffice. Scripted green runs use `-j 1`. Per-task banners include `model=` so interleaved workers are readable.

DSPy `build_program()` can still pass `tool_names` to restrict which tools the model sees (for “how many tools is too many” sweeps).

## Applying the result

After a run, open `optimized_writer_prompt.json` and copy the optimized instruction text into `core/constants.py` as `DEFAULT_CHAT_SYSTEM_PROMPT` (or merge with `FORMAT_RULES` as in the current prompt). Then test in WriterAgent with the same evaluation tasks.

## Multi-model evaluation (intelligence per dollar)

You can also run the same fixed dataset and current system prompt across **multiple models** and compare their performance and estimated cost.

Models and prices live in `model_configs.py` (one `ModelConfig` per model with context window and list prices in USD per 1M input/output tokens).

```bash
export OPENROUTER_API_KEY="your-key"

# Required: --models (or --yes-all-models for the full catalog)
python run_eval_multi.py --models openai/gpt-oss-120b,openai/gpt-4o-mini

# Fewer examples (faster, cheaper)
python run_eval_multi.py --models openai/gpt-oss-120b:nitro -n 2

# Selection runs: three repeats
python run_eval_multi.py --models openai/gpt-oss-120b:nitro --repeats 3

# 20 models in parallel (default); use -j 1 for sequential
python run_eval_multi.py --models openai/gpt-oss-120b,openai/gpt-4o-mini -j 20
```

For each model, `run_eval_multi.py` reports **hard pass %**, **agent score**, **quality** (among judged passes), then historical avg correctness / cost / C²/$. Rank is hard pass, then agent, then quality; C²/$ is secondary.

Use `--out path.json` or `--out path.csv` to write results (format by extension). Details files include missing/reject/oracle/process failures and `judge_error`. Results are written after each model completes so partial data is saved if the run is interrupted.

To merge a selective model run back into the master dataset without overwriting existing data (and retire older superseded generations like Mercury 2 in favor of Mercury 2.5):

```bash
python merge_benchmark_results.py \
  --base benchmark_results.json \
  --update benchmark_results_selective.json \
  --drop "inception/mercury-2,meta/muse-spark-1.2-contributor" \
  --out benchmark_results.json \
  --markdown
```

### Eval framework (summary)

- **Dataset** (`dataset.py`): 17 fixed tasks (12 Writer + Draw flowchart + 2 Calc + 2 `=PY` dest) with assigned `category` (structural or creative).
- **Result oracles** (`oracles.py`): Structural correctness from the exported final doc (table Total, 8% tax, Revenue desc, heading order, …). Not tool-name traces.
- **Gold Standards** (`gold_standards.json`): Hand-written references matching current rubrics. Used only as the quality-judge reference for resume / rewrite / summary / tables. `--generate-golds` can merge a teacher run with `--gold-model` (default `openai/gpt-5.6-luna`; not used during ranking).
- **Program** (`program.py`): DSPy `WriterAssistant` (ReAct) with mock environment.
- **Metric**: Hard gate (document + process); quality judge after the gate for resume/rewrite/summary/tables. Shared via `eval_core` for `run_optimize` (MIPROv2) and `run_eval_multi`.
- **Multi-model**: `run_eval_multi.py` ranks by hard pass / agent / quality; C²/$ is secondary. `--models` is required.

### Benchmark results (2026-09-05, 17-task string harness)

Selective post-#610 re-rank (`data_sorting` + `tax_column` only; other tasks carried from 2026-09-01). Artifacts: `benchmark_results.json`, `benchmark_results_details.json`. Cost–quality charts: [`docs/eval/pareto-fronts.svg`](../../docs/eval/pareto-fronts.svg) (successive fronts) and [`docs/eval/pareto-distance.svg`](../../docs/eval/pareto-distance.svg) (distance to F1); regenerate with `python scripts/prompt_optimization/plot_pareto.py`. Triage: [`docs/eval/benchmark-failure-analysis-2026-09-01.md`](../../docs/eval/benchmark-failure-analysis-2026-09-01.md).

**Excluded from this table (1 of 23 models):**
 
| Model | Reason |
|-------|--------|
| `qwen/qwen3.8-flash` | Infra: OpenRouter upstream 429 on all 17 tasks |

Ranked by **hard pass → agent score → metric**. **C²/$** = metric score squared ÷ avg $/task (`intelligence_per_dollar_metric`). **Quality** = LLM judge average among judged creative/table passes only (`—` if none judged). Models with `n_err` > 0 kept when errors are model-side (empty response, tool-loop limit), not infra/harness.

| Rank | Model | Hard pass | Agent | Correctness | Quality | Tokens/task | $/task | C²/$ | n_err |
| ---- | ---- | ------- | ------- | ------- | ------- | ------- | ------- | ------- | ------- |
| 1 | deepseek/deepseek-v4-flash-0731 | 1.000 | 1.000 | 0.987 | 0.96 | 44832 | 0.00358 | 150.0 | 0 |
| 2 | x-ai/grok-4.6 | 1.000 | 1.000 | 0.982 | 0.94 | 21798 | 0.04881 | 12.0 | 0 |
| 3 | meta/muse-spark-1.3-contributor | 1.000 | 1.000 | 0.979 | 0.93 | 25343 | 0.00272 | 193.6 | 0 |
| 4 | openai/gpt-oss-120b | 1.000 | 1.000 | 0.971 | 0.90 | 13759 | 0.00061 | 1141.4 | 0 |
| 5 | meta/muse-glimmer-30b | 0.941 | 0.941 | 0.928 | 0.96 | 25248 | 0.00979 | 49.2 | 0 |
| 6 | openai/gpt-5.6-luna | 0.941 | 0.941 | 0.922 | 0.94 | 17229 | 0.00406 | 144.3 | 0 |
| 7 | google/gemma-4-31b-it | 0.941 | 0.941 | 0.918 | 0.90 | 15709 | 0.00151 | 416.0 | 0 |
| 8 | inception/mercury-2.5-preview | 0.882 | 0.882 | 0.869 | 0.95 | 32861 | 0.00927 | 34.6 | 0 |
| 9 | z-ai/glm-5.3-flash | 0.882 | 0.882 | 0.854 | 0.90 | 40017 | 0.00404 | 104.8 | 0 |
| 10 | qwen/qwen3.8-27b | 0.824 | 0.824 | 0.864 | 0.92 | 41102 | 0.02417 | 13.1 | 1 |
| 11 | openai/gpt-oss-20b | 0.824 | 0.824 | 0.805 | 0.89 | 15435 | 0.00062 | 747.5 | 0 |
| 12 | bytedance-seed/seed-2.0-mini | 0.824 | 0.824 | 0.800 | 0.90 | 33742 | 0.00530 | 68.9 | 0 |
| 13 | poolside/laguna-xs-2.1 | 0.824 | 0.824 | 0.767 | 0.81 | 19086 | 0.00118 | 297.6 | 2 |
| 14 | ibm-granite/granite-4.2-8b | 0.765 | 0.765 | 0.802 | 0.93 | 65978 | 0.00729 | 26.7 | 1 |
| 15 | minimax/minimax-m3 | 0.706 | 0.706 | 0.761 | 0.94 | 58078 | 0.02057 | 15.1 | 1 |
| 16 | poolside/laguna-s-2.1 | 0.706 | 0.706 | 0.759 | 0.90 | 19040 | 0.00192 | 189.2 | 2 |
| 17 | google/gemini-3.5-flash-lite | 0.706 | 0.706 | 0.747 | 0.93 | 14640 | 0.00509 | 76.7 | 0 |
| 18 | upstage/solar-pro4 | 0.647 | 0.647 | 0.624 | 0.90 | 20408 | 0.00065 | 363.2 | 0 |
| 19 | google/gemma-4-26b-a4b-it | 0.647 | 0.647 | 0.621 | 0.89 | 34203 | 0.00254 | 94.2 | 1 |
| 20 | z-ai/glm-5.3 | 0.588 | 0.588 | 0.644 | 0.97 | 29909 | 0.06128 | 3.2 | 3 |
| 21 | mistralai/mistral-small-2603 | 0.588 | 0.588 | 0.571 | 0.85 | 13720 | 0.00216 | 110.7 | 0 |
| 22 | nvidia/nemotron-3.5-lightning | 0.353 | 0.353 | 0.315 | 0.68 | 41237 | 0.00339 | 13.1 | 0 |
| 23 | qwen/qwen3.8-flash | 0.118 | 0.118 | 0.118 | — | 15473 | 0.00254 | 0.8 | 15 |

Re-run: `make run_eval EVAL_ARGS="--models … -j 20"` or edit `model_configs.py`. User-facing summary: [`docs/eval/benchmarks.md`](../../docs/eval/benchmarks.md).

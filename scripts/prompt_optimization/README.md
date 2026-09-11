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

**Optimize students:** `--student llm` (default) wraps the same `llm_chat_eval` tool loop as `run_eval.py`. `--student react-mock` is the old DSPy ReAct + `tools_lo` path (comparison only; do not paste that JSON into `prompts.py`).

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

**Full optimization (MIPROv2, live student):**

Default `--student llm` proposes replacements for a **named slice** (not the whole ambient system prompt) and scores each candidate by running `run_eval` / `llm_chat_eval` (production tool schemas + sidebar-like tool loop). Instruction-only: `max_bootstrapped_demos=0`, `max_labeled_demos=0`. A length penalty prefers winners under ~2× the current slice. Output is `optimized_slice.json` plus `optimized_slice_slice.json` (plain instruction text). **Do not auto-merge** into `plugin/framework/prompts.py` or `plugin/calc/cells.py` — copy by hand after a ranking re-run.

```bash
export OPENROUTER_API_KEY="your-key"
python run_optimize.py --auto light -j 1 \
  -e data_sorting,tax_column --slice calc_core
```

That is the cheap Calc-slice smoke: light MIPRO, one worker, the two Calc ranking tasks, `CALC_CORE_DIRECTIVES` only.

Other slices: `writer_core`, `sort_range`, `write_formula_range`, `write_formula_range.values`, `full_prompt` (opaque whole-prompt fallback). Legacy ReAct comparison: `--student react-mock` (writes `optimized_writer_prompt.json`).

Pick a different model:

```bash
python run_optimize.py --model google/gemini-3.5-flash-lite
python run_optimize.py -m openai/gpt-oss-120b:nitro -k sk-...
```

- **`--student llm|react-mock`**: live eval loop (default) vs DSPy ReAct mocks.
- **`--slice NAME`**: fragment MIPROv2 rewrites (`calc_core` default).
- **`-e` / `--example`**: comma-separated `task_id` filter (same idea as `run_eval.py`).
- **`--judge`** / **`-J`**: Judge model for grading (default `openai/gpt-oss-120b:nitro`). Same dataset and `gold_standards.json` as run_eval_multi. Golds are hand-written from the rubrics; `--generate-golds` is an optional teacher merge, not a ranking prerequisite.
- **`-j N`** / **`--jobs N`**: parallel evals (default 4). Use `1` for a smoke.
- **`--auto light|medium|heavy`**: exploration level (default `light`). Use `medium` or `heavy` for more tries when your prompt is complicated.
- **`-t N`** / **`--trials N`**: explicit number of Bayesian optimization trials (overrides `--auto`; uses more exploration).
- **`--no-length-penalty`**: disable the ~2× slice-length penalty.

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

DSPy `build_program()` (`--student react-mock`) can still pass `tool_names` to restrict which tools the ReAct mock sees (for “how many tools is too many” sweeps). Live `--student llm` uses `eval_catalog.build_eval_tool_schemas` (same as `run_eval_multi`).

## Applying the result

After a live run, open `optimized_slice_slice.json` and copy **only that slice** into the matching production constant (`CALC_CORE_DIRECTIVES`, `WRITER_CORE_DIRECTIVES`, or the tool description in `plugin/calc/cells.py`). Then re-run `run_eval.py` / `run_eval_multi.py` on the same tasks. Do not paste a ReAct `optimized_writer_prompt.json` into the sidebar prompt.

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
- **Program**: default `program_llm.LiveEvalStudent` injects a named slice then calls `llm_chat_eval` (same student as `run_eval_multi`). Optional `program.py` `WriterAssistant` (ReAct + mocks) behind `--student react-mock`.
- **Metric**: Hard gate (document + process); quality judge after the gate for resume/rewrite/summary/tables; token penalty; slice-length penalty (~2× seed). Shared via `eval_core` / `metric.py` for `run_optimize` (MIPROv2) and `run_eval_multi`.
- **Multi-model**: `run_eval_multi.py` ranks by hard pass / agent / quality; C²/$ is secondary. `--models` is required.

### Benchmark results (2026-09-11, 17-task string harness)

Calc-only selective refresh (`data_sorting` + `tax_column`) for 20 catalog models; Luna / Qwen Flash / DeepSeek V4.1 Flash keep prior Calc rows. Other 15 tasks carried forward. Artifacts: `benchmark_results.json`, `benchmark_results_details.json`, plus `benchmark_results_calc_selective_2026-09-11*.json`. Cost–quality charts: [`docs/eval/pareto-fronts.svg`](../../docs/eval/pareto-fronts.svg) (successive fronts) and [`docs/eval/pareto-distance.svg`](../../docs/eval/pareto-distance.svg) (distance to F1); regenerate with `python scripts/prompt_optimization/plot_pareto.py`. Triage: [`docs/eval/benchmark-failure-analysis-2026-09-01.md`](../../docs/eval/benchmark-failure-analysis-2026-09-01.md).

Ranked by **hard pass → agent score → metric**. **C²/$** = metric score squared ÷ avg $/task (`intelligence_per_dollar_metric`). **Quality** = LLM judge average among judged creative/table passes only (`—` if none judged). Models with `n_err` > 0 kept when errors are model-side (empty response, tool-loop limit), not infra/harness.

| Rank | Model | Hard pass | Agent | Correctness | Quality | Tokens/task | $/task | C²/$ | n_err |
| ---- | ---- | ------- | ------- | ------- | ------- | ------- | ------- | ------- | ------- |
| 1 | meta/muse-glimmer-30b | 1.000 | 1.000 | 0.987 | 0.96 | 27041 | 0.01025 | 50.1 | 0 |
| 2 | deepseek/deepseek-v4-flash-0731 | 1.000 | 1.000 | 0.987 | 0.96 | 46613 | 0.00369 | 138.7 | 0 |
| 3 | x-ai/grok-4.6 | 1.000 | 1.000 | 0.982 | 0.94 | 24613 | 0.05418 | 10.0 | 0 |
| 4 | meta/muse-spark-1.3-contributor | 1.000 | 1.000 | 0.979 | 0.93 | 28777 | 0.00307 | 155.7 | 0 |
| 5 | openai/gpt-5.6-luna | 0.941 | 0.941 | 0.916 | 0.90 | 20031 | 0.00449 | 116.6 | 0 |
| 6 | z-ai/glm-5.3-flash | 0.941 | 0.941 | 0.913 | 0.90 | 43404 | 0.00426 | 105.6 | 0 |
| 7 | deepseek/deepseek-v4.1-flash | 0.882 | 0.882 | 0.935 | 0.97 | 51291 | 0.00944 | 39.2 | 2 |
| 8 | bytedance-seed/seed-2.0-mini | 0.882 | 0.882 | 0.859 | 0.90 | 23817 | 0.00381 | 107.1 | 0 |
| 9 | openai/gpt-oss-120b | 0.882 | 0.882 | 0.853 | 0.90 | 12150 | 0.00056 | 999.4 | 0 |
| 10 | poolside/laguna-xs-2.1 | 0.882 | 0.882 | 0.826 | 0.81 | 38787 | 0.00238 | 155.6 | 1 |
| 11 | qwen/qwen3.8-27b | 0.824 | 0.824 | 0.922 | 0.92 | 54296 | 0.03051 | 9.3 | 2 |
| 12 | ibm-granite/granite-4.2-8b | 0.824 | 0.824 | 0.861 | 0.93 | 69637 | 0.00777 | 25.2 | 1 |
| 13 | inception/mercury-2.5-preview | 0.824 | 0.824 | 0.811 | 0.95 | 30675 | 0.00869 | 33.2 | 0 |
| 14 | qwen/qwen3.8-flash | 0.824 | 0.824 | 0.805 | 0.89 | 45001 | 0.00755 | 37.2 | 1 |
| 15 | google/gemma-4-31b-it | 0.824 | 0.824 | 0.800 | 0.90 | 17235 | 0.00165 | 289.3 | 0 |
| 16 | minimax/minimax-m3 | 0.765 | 0.765 | 0.820 | 0.94 | 63655 | 0.02252 | 13.4 | 1 |
| 17 | openai/gpt-oss-20b | 0.765 | 0.765 | 0.746 | 0.89 | 14664 | 0.00060 | 668.0 | 0 |
| 18 | upstage/solar-pro4 | 0.706 | 0.706 | 0.682 | 0.90 | 29828 | 0.00094 | 301.5 | 0 |
| 19 | google/gemma-4-26b-a4b-it | 0.706 | 0.706 | 0.680 | 0.89 | 19093 | 0.00141 | 197.0 | 0 |
| 20 | poolside/laguna-s-2.1 | 0.647 | 0.647 | 0.700 | 0.90 | 19806 | 0.00200 | 150.7 | 2 |
| 21 | google/gemini-3.5-flash-lite | 0.647 | 0.647 | 0.688 | 0.93 | 15741 | 0.00545 | 60.9 | 0 |
| 22 | mistralai/mistral-small-2603 | 0.588 | 0.588 | 0.571 | 0.85 | 17137 | 0.00267 | 89.5 | 0 |
| 23 | nvidia/nemotron-3.5-lightning | 0.353 | 0.353 | 0.315 | 0.68 | 29840 | 0.00245 | 18.1 | 0 |

Re-run: `make run_eval EVAL_ARGS="--models … -j 20"` or edit `model_configs.py`. User-facing summary: [`docs/eval/benchmarks.md`](../../docs/eval/benchmarks.md).

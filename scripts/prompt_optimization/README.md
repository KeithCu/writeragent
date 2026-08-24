# Writer prompt optimization with DSPy

This folder implements the DSPy-based optimization of `DEFAULT_CHAT_SYSTEM_PROMPT` for WriterAgent (see plan in repo). Future Calc work: `=PY()` destination placement (J1 vs overlapping H1) is sketched as Phase F in [`docs/eval-dev-plan.md`](../../docs/eval-dev-plan.md) — not implemented here yet.

## Benchmarks from repo root

```bash
git clone …/writeragent && cd writeragent
uv sync
make eval-deps                    # uv pip install dspy-ai (eval + optimize only)
export OPENROUTER_API_KEY=sk-…   # or OPENAI_API_KEY / WRITERAGENT_API_KEY
make run_eval-smoke               # one model, one example
make run_eval EVAL_ARGS="--models qwen/qwen3-coder-next -n 2 -j 1"
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

**Defaults: OpenRouter** with **qwen/qwen3-coder-next** (cheap and fast). API key (first match wins):

- `--api-key` / `-k`, then `WRITERAGENT_API_KEY`, `OPENAI_API_KEY`, `OPENROUTER_API_KEY`

Endpoint:

- `--api-base` / `WRITERAGENT_API_BASE`, `OPENAI_API_BASE` — default `https://openrouter.ai/api/v1`

Judge model (`run_eval_multi.py`):

- `--judge` / `WRITERAGENT_JUDGE_MODEL`, then Grok on OpenRouter, else first `--models` id on other endpoints
- `--no-judge` — substring checks only

Override model for optimize:

- `python run_optimize.py --model google/gemini-2.0-flash-001` / `--api-base ...` / `--api-key ...`

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
python run_eval.py --backend lo --student scripted --no-bust-cache -v   # headless LO, no key
# or from repo root: make run_eval-lo-scripted
```

Shows for each example: task_id, expected_contains / reject_contains pass or miss, correctness, tokens, score, and a short doc snippet. Use `-v`/`--verbose` to print each tool call. Use `--compare-with` to run both the current prompt and the prompt from a DSPy JSON file, then report which scores higher. Cache-busting is enabled by default (unique suffix per example) to avoid OpenRouter prompt cache; use `--no-bust-cache` to disable.

**Full optimization (MIPROv2):**

```bash
export OPENROUTER_API_KEY="your-key"
python run_optimize.py
```

Pick a different model:

```bash
python run_optimize.py --model google/gemini-2.0-flash-001
python run_optimize.py -m qwen/qwen3-coder-next -k sk-...
```

This runs MIPROv2 in **0-shot instruction-only** mode: it proposes alternative system prompts and keeps the one that scores best on the **judge-based metric** (same LLM-as-a-Judge as `run_eval_multi`, plus token penalty). Output is saved to `optimized_writer_prompt.json`.

- **`--judge`** / **`-J`**: Judge model for grading (default `x-ai/grok-4.1-fast`). Same dataset and optional `gold_standards.json` as run_eval_multi; run `run_eval_multi.py --generate-golds` once to populate gold for better judge reference.
- **`-j N`** / **`--jobs N`**: parallel evals (default 4).
- **`--auto light|medium|heavy`**: exploration level (default `light`). Use `medium` or `heavy` for more tries when your prompt is complicated.
- **`-t N`** / **`--trials N`**: explicit number of Bayesian optimization trials (overrides `--auto`; uses more exploration).

## Metric

Optimization and multi-model eval both use the same **LLM-as-a-Judge** scoring (default **Grok 4.1 Fast** via `score_with_judge` in `eval_core`).

- **Dual-Mode Scoring**: The judge applies weighted criteria based on the task category:
    - **Structural** (Tables, Cleanup): 60% Accuracy, 40% Formatting.
    - **Creative** (Editing, Resumes): 30% Accuracy, 20% Formatting, 50% **Naturalness**.
- **Chain-of-Thought**: Judges output a `thought_process` before assigning 1-5 sub-scores for each dimension.
- **Internal Normalization**: Sub-scores are normalized and weighted into a final 0.0–1.0 score.
- **Token penalty**: `score -= 0.01 * (total_tokens / 1000)` so fewer tokens improve the score.

## Dataset

`dataset.py` `ALL_EXAMPLES` is **11 tasks**: 8 Writer (table-from-mess, reformat-resume, table-engineering, bulk-cleanup, logical-rewriting, format-preservation, style-application, bullet-consistency) plus `flowchart_gen` (Draw), `data_sorting` and `tax_column` (Calc). Each has fixed `document_content` and `user_question` so runs are comparable. Kind is keyed by `task_id` (`task_kind()`), not question keywords.

## Tool subset

`--backend string` (default) is an in-memory simulator (`string_eval_tools.py`). `--backend lo` is **headless UNO**: `tools_lo.py` starts `soffice --headless`, serializes all UNO onto `_lo_thread` via `LOBackend.call`, and executes production tools with `bypass_thread_guard=True`. Do not use `tests/eval_runner.py` or `make lo-start` for this path.

`--student scripted` replays `scripted_student.SCRIPTS` (no `LlmClient`, no API key, substring checks only). `--student llm` (default) uses a live model and still needs a key. `--no-judge` skips the LLM judge.

`-j N` in `run_eval_multi.py` is **ThreadPoolExecutor** (parallel models in one process). UNO is already serialized on `_lo_thread`. Do **not** `ProcessPoolExecutor` against one soffice. Scripted green runs use `-j 1`.

DSPy `build_program()` can still pass `tool_names` to restrict which tools the model sees (for “how many tools is too many” sweeps).

## Applying the result

After a run, open `optimized_writer_prompt.json` and copy the optimized instruction text into `core/constants.py` as `DEFAULT_CHAT_SYSTEM_PROMPT` (or merge with `FORMAT_RULES` as in the current prompt). Then test in WriterAgent with the same evaluation tasks.

## Multi-model evaluation (intelligence per dollar)

You can also run the same fixed dataset and current system prompt across **multiple models** and compare their performance and estimated cost.

Models and prices live in `model_configs.py` (one `ModelConfig` per model with context window and list prices in USD per 1M input/output tokens).

```bash
export OPENROUTER_API_KEY="your-key"

# Run all default models from model_configs.get_default_models()
python run_eval_multi.py

# Restrict to a subset of models by OpenRouter id
python run_eval_multi.py --models openai/gpt-oss-120b,openai/gpt-4o-mini

# Fewer examples (faster, cheaper)
python run_eval_multi.py -n 2

# 8 models in parallel (default); use -j 1 for sequential with verbose output
python run_eval_multi.py -j 8
```

For each model, `run_eval_multi.py` reports:

- **Average correctness** and **average score** (correctness minus token penalty).
- **Total tokens** used across all examples.
- **Estimated dollar cost**, based on per-million token prices.
- An **“intelligence per dollar”** figure: average correctness divided by total cost (higher is better).

Use `--out path.json` or `--out path.csv` to write results (format by extension). Results are written after each model completes so partial data is saved if the run is interrupted. The final file is sorted by intelligence-per-dollar.

### Eval framework (summary)

- **Dataset** (`dataset.py`): 11 fixed tasks (8 Writer + Draw flowchart + 2 Calc) with assigned `category` (structural or creative).
- **Gold Standards** (`gold_standards.json`): Reference documents pre-generated by a high-tier "Teacher" model (**Claude Sonnet 4.6**). Use `--generate-golds` to update. **Cost:** default is **one example per run** (use `-e task_id` or `-n 1`); use `--yes-multi-gold` only if you intentionally batch several teacher calls in one command.
- **Program** (`program.py`): DSPy `WriterAssistant` (ReAct) with mock environment.
- **Metric**: LLM-as-a-Judge weighted score (Acc/Fmt/Nat) − token penalty. Shared via `eval_core.score_with_judge()` for both `run_optimize` (MIPROv2) and `run_eval_multi`.
- **Multi-model**: `run_eval_multi.py` ranks models by **Corr/USD** (avg judge correctness ÷ total $).

### Benchmark results (best models, combined runs)

From multi-model runs on the default 8-evaluation Writer set, ranked by **Corr/USD** (avg correctness ÷ total cost; higher = better value) and/or **correctness**:

- **openai/gpt-oss-120b** — Top value in run 1 (346.8 Corr/USD, 1.0 correctness, ~$0.003 total).
- **google/gemini-3-flash-preview** — Strong value (161.7 Corr/USD, 0.925 correctness).
- **nvidia/nemotron-3-nano-30b-a3b** — Best value in run 2 (131.2 Corr/USD; 0.725 correctness, very cheap).
- **allenai/olmo-3.1-32b-instruct** — High correctness (0.963) and good value (84.1 Corr/USD).
- **openai/gpt-4o-mini** — Good balance (98.8 Corr/USD, 0.938 correctness).
- **nex-agi/deepseek-v3.1-nex-n1** — Solid (58.9 Corr/USD, 0.925 correctness).

Re-run with different model sets by editing `model_configs.py` (and uncommenting the block you want). Failed runs (0 tokens) usually mean a wrong OpenRouter id or API error; check stderr or logs.

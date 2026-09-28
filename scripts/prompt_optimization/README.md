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

Writer apply/HTML-diff smoke (`WRITER_APPLY_DOCUMENT_HTML_RULES` — not the whole Writer ambient prompt):

```bash
python run_optimize.py --auto light -j 1 \
  -e table_from_mess,table_engineering --slice apply_html
```

Other slices: `writer_core`, `apply_html`, `sort_range`, `write_formula_range`, `write_formula_range.values`, `full_prompt` (opaque whole-prompt fallback). Legacy ReAct comparison: `--student react-mock` (writes `optimized_writer_prompt.json`).

Pick a different model:

```bash
python run_optimize.py --model google/gemini-3.5-flash-lite
python run_optimize.py -m openai/gpt-oss-120b:nitro -k sk-...
```

- **`--student llm|react-mock`**: live eval loop (default) vs DSPy ReAct mocks.
- **`--slice NAME`**: fragment MIPROv2 rewrites (`calc_core` default; Writer HTML contract is `apply_html`).
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

`dataset.py` `ALL_EXAMPLES` is **20 tasks**: 12 Writer (the original 8 plus `style_consistency`, `smart_summarization`, `section_refactor`, `comment_management`) plus `flowchart_gen` / `org_chart_gen` / `solar_sld_gen` (Draw; org chart and solar SLD are `backend=lo`), `data_sorting` / `tax_column` (Calc), two Phase F `=PY` dest rows (`py_refuse_overlap`, `py_no_bulk_read`), and `python_shapes_flag` (Writer, `backend=lo`). Each has fixed `document_content` and `user_question` so runs are comparable. Kind is keyed by `task_id` (`task_kind()`), not question keywords. Rows omit `backend` to stay on the string world. The flag cannot be faked there.

Hard pass is the **exported final document** plus process oracles (`oracles.py` / `process_oracles.py`). `python_shapes_flag` is the exception: hard pass is the harness trace plus exported Writer geometry (`flag_eval.py`). Under-counted stars are soft and do not fail. A quality LLM judge runs **after** that gate for resume, rewriting, summarization, and the two table tasks. Eval does not set sampling temperature. `gold_standards.json` is hand-written from the rubrics (no live teacher API unless `--generate-golds`). Specialized Draw/Calc tools are reached through an inner `LlmClient` loop (`domain="shapes"` / `"ranges"`), not SmolAgents.

## Tool subset

Eval-1 is **not string-only**. `--backend string` (default) is an in-memory simulator (`string_eval_tools.py`). It **skips** rows that declare `backend=lo` (`python_shapes_flag`, `org_chart_gen`, `solar_sld_gen`), so a pure-string board stays 17 tasks. `--backend lo` forces **native headless UNO** for every selected row: `tools_lo.py` starts `soffice --headless`, serializes all UNO onto `_lo_thread` via `LOBackend.call`, and executes production tools with `bypass_thread_guard=True`. `--backend auto` honors each row's `backend` and is the mixed 20-task pack (dual-lane; flag + org chart + solar SLD on LO). LO Draw/Writer rows need native; headed eval-2 is a separate suite — do not treat native LO as eval-2-only. Do not use `tests/eval_runner.py` or `make lo-start` for this path. Do not set `WRITERAGENT_TESTING=1` (that swaps in `QueueExecutor` on the wrong thread).

`--student scripted` replays `scripted_student.SCRIPTS` (no `LlmClient`, no API key, result oracles + honest substring checks). The flag has **no** script. `--student llm` (default) uses a live model and still needs a key. `--no-judge` skips the quality judge.

### Mixed backends (dual lane)

`--backend` used to be run-global. Each example may now set `backend` to `string` or `lo` (omitted means `string`). `auto` uses that field. `string` and `lo` still force one backend for the invocation.

From t=0 the scheduler does **not** finish the string pack and then start LibreOffice:

- A **string pool** (`run_eval.py -j N`, default 1) runs string rows concurrently.
- An **LO agent pool** (`--lo-workers`, default 4, clamped to 1..5; threads `eval-lo-lane-N`) runs that many `lo` example bodies at once, in front of the existing `_lo_thread` / one soffice. LLM waits overlap. UNO stays serial: tools enter only through `LOBackend.call`. `workers=1` is the old full-example FIFO.
- LO work is queued before the string pool waits, so it is in flight while string tasks are still running. Wall clock for native LO rows approaches the overlap of those LLM waits plus serial UNO, not a full-example FIFO.
- A string row never blocks on the LO queue. Only a row whose own backend is `lo` waits for a free pool worker.

`run_eval_multi.py -j N` is still **models** (default 20). Inside one model, string tasks stay serial. Models share **one** LO pool (`--lo-workers`), so flag rows from several models occupy those agent threads while those models' string tasks overlap. Do **not** `ProcessPoolExecutor` against one soffice, and do not start a second soffice. Flag-path `get_ctx()` / Desktop current component / venv `_io_lock` are still a separate pin. Per-task banners include `model=`; an LO row also prints `backend=lo`.

`delegate_tool_domains` on the string world returns `unsupported_in_eval`. There is no fake `run_venv_python_script`. On LO, the harness runs that hop as an inner `LlmClient` loop (not SmolAgents) so `run_venv_python_script` is on the **harness trace**, then executes it through production tools with `python_tool_domain=shapes,core`. Shape mutators stay off the inner LLM list. Headed eval-2 still scores `writeragent_debug.log`; this row does not.

```bash
# Original 17, no soffice. Comparable with the 2026-09-11 boards.
python scripts/prompt_optimization/run_eval.py --backend string --student scripted --no-bust-cache

# Same 17: auto + scripted prints a skip note for the flag (no script).
python scripts/prompt_optimization/run_eval.py --backend auto --student scripted --no-bust-cache

# Live flag only. Needs OPENROUTER_API_KEY (or equivalent) and soffice.
python scripts/prompt_optimization/run_eval.py --backend auto -e python_shapes_flag

# Full mixed pack (17 string + flag). String pool overlaps the LO agent pool
# (default --lo-workers 4). UNO stays on one soffice thread.
python scripts/prompt_optimization/run_eval.py --backend auto -j 4

# Explicit string request for the flag is an error (cannot fake venv/shapes).
python scripts/prompt_optimization/run_eval.py --backend string -e python_shapes_flag
```

OpenRouter-only `--backend string` still cannot run the flag alone. CI without soffice cannot execute that live row; scheduler and oracle unit tests do not need it. Do not average a mixed 18-pack with the September 2026 17-task CSV.

`-j N` on `run_eval.py` is the string-pool width above. Scripted green runs use `-j 1`.

DSPy `build_program()` (`--student react-mock`) can still pass `tool_names` to restrict which tools the ReAct mock sees. Live `--student llm` uses `eval_catalog.build_eval_tool_schemas` (same as `run_eval_multi`) and now accepts the same **tool-count** / **schema-density** knobs as `run_eval.py` (see below). MIPROv2 still cannot search those structural knobs — run a dropper sweep on the live harness instead.

## Tool-count and schema-density sweeps (live `--student llm`)

Production sidebar registration is unchanged. These flags only reshape the **advertised** eval catalog.

**`--tools SPEC`** (default `full`): named preset or comma-separated production tool names. Unknown explicit names raise with the available catalog. Kind-specific presets apply only to that document kind (a mixed-kind run with `--tools calc_minimal` still gives Writer/Draw the full catalog). **Specialized inner loops are not filtered** — `delegate_to_specialized_calc_toolset` still sees `sort_range` / ranges-domain schemas.

| Preset | Kind | Outer tools (production names) |
|--------|------|--------------------------------|
| `full` | any | Today's unfiltered `get_schemas` catalog (Writer 14 / Calc 14 / Draw 19). |
| `calc_minimal` | calc | `write_formula_range`, `get_sheet_summary`, `delegate_to_specialized_calc_toolset` — smallest set that can still pass `data_sorting` + `tax_column`. |
| `calc_core` | calc | The 9 Calc-specific outer tools (drops shared chatbot extras: `web_research`, `upsert_memory`, `get_guidance`, `redo`, `undo`). Not the MIPRO `--slice calc_core` prompt fragment. |
| `writer_minimal` | writer | `apply_document_content`, `get_document_content` — smallest set for apply-HTML Writer tasks (`table_from_mess`, `bulk_cleanup`, …). |

**`--schema-density full|skinny`** (default `full`): `skinny` is a pure transform (after the name filter, before MIPRO `apply_schema_patches`). Tool names and param names/types stay; tool and param **descriptions** are blanked so you can measure whether fat prose confuses small models without removing tools.

Do **not** burn OpenRouter in CI. Scripted smokes (no key) prove plumbing; live droppers are manual.

```bash
# Plumbing smoke (no API key)
python run_eval.py --student scripted --no-judge -e data_sorting \
  --tools calc_minimal --schema-density skinny -v
python run_eval.py --student scripted --no-judge -e tax_column --tools calc_minimal
python run_eval.py --student scripted --no-judge -e table_from_mess --tools writer_minimal

# Cheap live dropper (one small model, two Calc tasks). Compare full vs minimal vs skinny.
# Requires OPENROUTER_API_KEY; do not put this in CI.
python run_eval_multi.py --models openai/gpt-oss-20b -e data_sorting -j 1 --no-judge \
  --tools full --out /tmp/dropper_full.json
python run_eval_multi.py --models openai/gpt-oss-20b -e data_sorting -j 1 --no-judge \
  --tools calc_minimal --out /tmp/dropper_minimal.json
python run_eval_multi.py --models openai/gpt-oss-20b -e data_sorting -j 1 --no-judge \
  --tools calc_minimal --schema-density skinny --out /tmp/dropper_skinny.json

# Same knobs on MIPRO's live student (instruction search; tools/density stay fixed)
python run_optimize.py --auto light -j 1 -e data_sorting,tax_column \
  --slice calc_core --tools calc_minimal --schema-density skinny
```

Repeat the live commands with `-e tax_column` and, if you want a mid-size point, `--tools calc_core`. Compare hard pass / tokens (and cost when priced) across the three `--out` files. That is the “how many tools is too many” / “do fat descriptions hurt” measurement.

## Applying the result

After a live run, open `optimized_slice_slice.json` and copy **only that slice** into the matching production constant (`CALC_CORE_DIRECTIVES`, `WRITER_CORE_DIRECTIVES`, `WRITER_APPLY_DOCUMENT_HTML_RULES`, or the tool description in `plugin/calc/cells.py`). Then re-run `run_eval.py` / `run_eval_multi.py` on the same tasks. Do not paste a ReAct `optimized_writer_prompt.json` into the sidebar prompt.

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

- **Dataset** (`dataset.py`): 20 fixed tasks (12 Writer + Draw flowchart/org-chart/solar-sld + 2 Calc + 2 `=PY` dest + `python_shapes_flag` on LO) with assigned `category` (structural or creative). `--backend string` runs the 17 non-`backend=lo` rows.
- **Result oracles** (`oracles.py`): Structural correctness from the exported final doc (table Total, 8% tax, Revenue desc, heading order, …). Not tool-name traces.
- **Gold Standards** (`gold_standards.json`): Hand-written references matching current rubrics. Used only as the quality-judge reference for resume / rewrite / summary / tables. `--generate-golds` can merge a teacher run with `--gold-model` (default `openai/gpt-6-luna`; not used during ranking).
- **Program**: default `program_llm.LiveEvalStudent` injects a named slice then calls `llm_chat_eval` (same student as `run_eval_multi`). Optional `program.py` `WriterAssistant` (ReAct + mocks) behind `--student react-mock`.
- **Metric**: Hard gate (document + process); quality judge after the gate for resume/rewrite/summary/tables; token penalty; slice-length penalty (~2× seed). Shared via `eval_core` / `metric.py` for `run_optimize` (MIPROv2) and `run_eval_multi`.
- **Multi-model**: `run_eval_multi.py` ranks by hard pass / agent / quality; C²/$ is secondary. `--models` is required.

### Benchmark results (string snapshot → 19-task dual-lane refresh)

Eval-1 backends: **`string`** = in-memory; **`lo`** = native headless UNO; **`auto`** = per-task dual-lane mix. Tasks 18–19 (`python_shapes_flag`, `org_chart_gen`) need native — not eval-2-only.

### 2026-09-28 board refresh (complete 26×19 dual-lane)

Org_chart_gen matrix splice on #941 tip (after 2026-09-27 complete 26×18 flag-18 board). Catalog unchanged: GPT-6 Luna, Command A+, Ternary Bonsai 2 aboard; no 5.6 Luna / ds-v4-flash-0731; MiMos omitted. **Org-chart hard PASS: 19/26** (7 fails kept honestly, including Ternary Bonsai hang → 0). Do not invent scores.

Tasks 18–19 run on the **native** lane (`--backend auto`). **Board is complete: 26/26** models have `n_examples=19`. Perfect hard pass (n=19): only `meta/muse-spark-1.3-contributor` (`openai/gpt-oss-120b` fails org chart → 0.947).

Pareto SVGs regenerated from this JSON: [`docs/eval/pareto-fronts.svg`](../../docs/eval/pareto-fronts.svg), [`docs/eval/pareto-distance.svg`](../../docs/eval/pareto-distance.svg).

Calc fill-down base (`data_sorting` + `tax_column`) from 2026-09-11 after Tip A/B + harness `expand_single_formula` (#733). Artifacts: `benchmark_results.json`, `benchmark_results_details.json`. Cost–quality charts: [`docs/eval/pareto-fronts.svg`](../../docs/eval/pareto-fronts.svg) (successive fronts) and [`docs/eval/pareto-distance.svg`](../../docs/eval/pareto-distance.svg) (distance to F1); regenerate with `python scripts/prompt_optimization/plot_pareto.py`. Triage: [`docs/eval/benchmark-failure-analysis-2026-09-01.md`](../../docs/eval/benchmark-failure-analysis-2026-09-01.md).

Ranked by **hard pass → agent score → metric**. **C²/$** = metric score squared ÷ avg $/task (`intelligence_per_dollar_metric`). **Quality** = LLM judge average among judged creative/table passes only (`—` if none judged). Models with `n_err` > 0 kept when errors are model-side (empty response, tool-loop limit), not infra/harness. **n** is **19** for every catalog model (flag + org chart spliced).

| Rank | Model | Hard pass | Agent | Correctness | Quality | Tokens/task | $/task | C²/$ | n_err |
| ---- | ---- | ------- | ------- | ------- | ------- | ------- | ------- | ------- | ------- |
| 1 | meta/muse-spark-1.3-contributor | 1.000 | 1.000 | 0.981 | 0.93 | 56766 | 0.00653 | 64.4 | 0 |
| 2 | meta/muse-glimmer-30b | 0.947 | 0.947 | 0.975 | 0.96 | 32858 | 0.01429 | 30.9 | 0 |
| 3 | x-ai/grok-4.6 | 0.947 | 0.947 | 0.971 | 0.94 | 36460 | 0.09480 | 4.9 | 0 |
| 4 | openai/gpt-oss-120b | 0.947 | 0.947 | 0.921 | 0.90 | 18554 | 0.00106 | 584.2 | 0 |
| 5 | google/gemma-4-31b-it | 0.895 | 0.895 | 0.913 | 0.90 | 17594 | 0.00224 | 261.3 | 0 |
| 6 | bytedance-seed/seed-2.0-mini | 0.895 | 0.895 | 0.913 | 0.90 | 28592 | 0.00415 | 103.1 | 0 |
| 7 | poolside/laguna-xs-2.1 | 0.895 | 0.895 | 0.890 | 0.81 | 28418 | 0.00196 | 203.2 | 0 |
| 8 | qwen/qwen3.8-27b | 0.842 | 0.842 | 0.917 | 0.92 | 133087 | 0.09898 | 2.9 | 1 |
| 9 | deepseek/deepseek-v4.1-flash | 0.842 | 0.842 | 0.883 | 0.97 | 138573 | 0.01015 | 34.8 | 2 |
| 10 | openai/gpt-6-luna | 0.842 | 0.842 | 0.873 | 0.93 | 34348 | 0.00550 | 71.1 | 0 |
| 11 | inception/mercury-2.5-preview | 0.842 | 0.842 | 0.870 | 0.95 | 36864 | 0.01198 | 25.0 | 0 |
| 12 | z-ai/glm-5.3-flash | 0.842 | 0.842 | 0.856 | 0.90 | 83249 | 0.00843 | 40.2 | 0 |
| 13 | minimax/minimax-m3 | 0.789 | 0.789 | 0.832 | 0.94 | 126882 | 0.05519 | 5.5 | 1 |
| 14 | openai/gpt-oss-20b | 0.789 | 0.789 | 0.812 | 0.89 | 24456 | 0.00110 | 349.7 | 0 |
| 15 | qwen/qwen3.8-flash | 0.789 | 0.789 | 0.792 | 0.89 | 49095 | 0.00972 | 22.8 | 2 |
| 16 | nvidia/nemotron-3-ultra-550b-a55b | 0.789 | 0.789 | 0.787 | 0.74 | 145858 | 0.14586 | 1.4 | 2 |
| 17 | upstage/solar-pro4 | 0.789 | 0.789 | 0.768 | 0.90 | 33501 | 0.00146 | 199.9 | 0 |
| 18 | ibm-granite/granite-4.2-8b | 0.737 | 0.737 | 0.810 | 0.93 | 110856 | 0.01192 | 12.7 | 2 |
| 19 | poolside/laguna-s-2.1 | 0.737 | 0.737 | 0.784 | 0.90 | 32409 | 0.00335 | 86.3 | 2 |
| 20 | google/gemma-4-26b-a4b-it | 0.737 | 0.737 | 0.753 | 0.89 | 20530 | 0.00227 | 145.4 | 0 |
| 21 | nvidia/nemotron-3-super-120b-a12b | 0.684 | 0.737 | 0.907 | 0.91 | 143994 | 0.01904 | 10.7 | 4 |
| 22 | google/gemini-3.5-flash-lite | 0.684 | 0.684 | 0.761 | 0.93 | 16160 | 0.01018 | 39.4 | 0 |
| 23 | cohere/command-a-plus | 0.632 | 0.632 | 0.694 | 0.94 | 66553 | 0.03195 | 5.6 | 6 |
| 24 | mistralai/mistral-small-2603 | 0.579 | 0.579 | 0.583 | 0.85 | 24553 | 0.00534 | 39.2 | 2 |
| 25 | prism-ml/ternary-bonsai-2-27b | 0.474 | 0.474 | 0.543 | 0.97 | 27120 | 0.00376 | 35.0 | 9 |
| 26 | nvidia/nemotron-3.5-lightning | 0.368 | 0.421 | 0.395 | 0.68 | 34466 | 0.00338 | 17.0 | 0 |

Re-run: `make run_eval EVAL_ARGS="--models … -j 20"` or edit `model_configs.py`. User-facing summary: [`docs/eval/benchmarks.md`](../../docs/eval/benchmarks.md).

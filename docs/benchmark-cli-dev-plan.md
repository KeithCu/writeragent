# Benchmark CLI Development Plan

**Status:** Phase 1 implemented (repo-root CLI, `make run_eval`, provider-agnostic auth, LlmClient judge).

## Goal

Simple CLI entry point for the eval suite so developers can run benchmarks without digging into `scripts/prompt_optimization/`.

## Quick start

```bash
git clone …/writeragent && cd writeragent
uv sync
make eval-deps
export OPENROUTER_API_KEY=sk-…
make run_eval-smoke
make run_eval EVAL_ARGS="--models qwen/qwen3-coder-next -n 2 -j 1"
```

## What exists

| Feature | Entry |
|---------|--------|
| Repo-root CLI | [`scripts/benchmark.py`](../scripts/benchmark.py) |
| Make targets | `make eval-deps`, `make run_eval`, `make run_eval-smoke` |
| Multi-model eval | [`run_eval_multi.py`](../scripts/prompt_optimization/run_eval_multi.py) |
| Credentials | [`eval_auth.py`](../scripts/prompt_optimization/eval_auth.py) |
| Judge (LlmClient) | [`eval_core.score_with_judge_llm`](../scripts/prompt_optimization/eval_core.py) |
| Student eval (LlmClient tool loop) | [`llm_chat_eval.py`](../scripts/prompt_optimization/llm_chat_eval.py) |

## API key and endpoint

| | CLI | Environment (in order) |
|---|-----|------------------------|
| Key | `--api-key` | `WRITERAGENT_API_KEY`, `OPENAI_API_KEY`, `OPENROUTER_API_KEY` |
| URL | `--endpoint` / `--api-base` | `WRITERAGENT_API_BASE`, `OPENAI_API_BASE`, OpenRouter default |

`--allow-unknown-model` — local/custom model ids without `model_configs.py` pricing (IpD/cost shown as n/a).

## Judge

- Resolved by `eval_auth.resolve_judge_model` (not hardcoded Grok on local endpoints).
- Runs via **`LlmClient`** (same auth/shims as production chat), not `dspy.LM`.
- `--no-judge` — skip LLM judge.

## Phase 2 (not yet)

- `--task` / `--document` custom ad-hoc examples in `benchmark.py` and `run_eval_multi.py`.

## Testing

```bash
pytest tests/scripts/test_eval_auth.py tests/scripts/test_eval_judge_llm.py tests/scripts/test_benchmark_cli.py
```

(`test_eval_judge_llm` requires `dspy-ai` from `make eval-deps`.)

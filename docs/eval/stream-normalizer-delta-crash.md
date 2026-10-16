# Harness bug: `type(delta) is dict` crash on MiniMax M3 (stream normalizer)

**Status:** Open — confirmed on 2026-09-01 benchmark run  
**Severity:** Harness crash (eval/chat abort, not a model quality failure)

---

## Symptom

Eval run records this as the task `error` string (ANSI coloring from `deal`):

```text
expected type(delta) is dict
```

**Observed case:**

| Field | Value |
|-------|--------|
| Model | `minimax/minimax-m3` (OpenRouter) |
| Task | `format_preservation` (Writer, 17-task string harness) |
| `total_tokens` | `0` |
| `final_document` | empty |
| Run artifact | `scripts/prompt_optimization/benchmark_results_details.json` |

The task never completed tool rounds — the HTTP response parsing path threw before tokens were accumulated.

Same model completed other tasks in the same run (e.g. `table_engineering` passed), so this is **provider response shape / normalizer**, not a blanket MiniMax outage.

---

## What this is not

- **Not** an oracle or rubric bug (`format_preservation` oracle never ran on real output).
- **Not** OpenRouter 429 / rate limit (those rows say `Rate limited (429)`).
- **Not** “model failed format_preservation” — the harness died first.

Eval may still attach substring/oracle noise on empty `final_document` when `error` is set; `hard_pass` is already false because `error is not None` ([`eval_core.py`](../../scripts/prompt_optimization/eval_core.py) ~713–718).

---

## Call path (eval → LlmClient → normalizer)

String harness eval does **not** use LibreOffice. It uses the same production HTTP stack:

```text
scripts/benchmark.py
  → scripts/prompt_optimization/run_eval_multi.py
  → scripts/prompt_optimization/eval_core.run_eval_on_examples_llm
  → scripts/prompt_optimization/llm_chat_eval.run_llm_chat_eval
       client.request_with_tools(..., stream=False)   # sync, not SSE UI stream
  → plugin/framework/client/llm_client.py
       request_with_tools (sync branch)
       → base_provider_shim.parse_sync_response(result)
            → stream_normalizer._normalize_delta(message)   # likely crash site
```

On failure, `llm_chat_eval` catches any `Exception` and stores `str(e)` as the row `error` (~435–437 in `llm_chat_eval.py`).

**Eval always passes `stream=False`** for the main tool loop (`llm_chat_eval.py` ~365). So this is the **sync JSON response** path, not the sidebar SSE drain — but the same `_normalize_delta` helper is shared with streaming chat.

---

## Root cause (code)

### `_normalize_delta` — contract stricter than shim body

File: [`plugin/framework/client/stream_normalizer.py`](../../plugin/framework/client/stream_normalizer.py) (~339–373)

```python
@deal.pre(
    lambda delta: type(delta) is dict
    and len(delta) <= DEAL_MAX_SHAPE_DIM
    ...
)
def _normalize_delta(delta: dict[str, Any]) -> None:
    # Shim path (deal absent): keep the old non-dict no-op. With deal installed, pre rejects.
    if type(delta) is not dict:
        return
    ...
```

**What went wrong:** The function body was written to **no-op** on non-plain dicts (`type(delta) is not dict` → return). The `@deal.pre` runs **before** that body and **rejects** the call with `PreContractError` when `type(delta) is not dict`.

So in dev/typecheck builds where `deal` is active, any caller that passes:

- a non-dict, or
- a `dict` **subclass** (`collections.OrderedDict`, `UserDict`, CrossHair `AttrDict`, etc. — `isinstance(x, dict)` true but `type(x) is not dict`)

will crash instead of skipping normalization.

Comments elsewhere in this file explicitly use `type(...) is dict` instead of `isinstance(..., dict)` to avoid CrossHair `AttrDict` crashes — but here the **pre-contract** turns that into a hard failure in production eval.

### Same pattern elsewhere

[`plugin/framework/async_stream.py`](../../plugin/framework/async_stream.py) `accumulate_delta` (~815–829) uses the same `type(acc) is dict and type(delta) is dict` `@deal.pre`. Streaming tool accumulation would hit the same class of bug if a non-plain dict delta reached it. Eval’s main loop uses `stream=False`, but **sidebar chat** uses streaming + `accumulate_delta`.

### Contrast: functions that handle this safely

In the same module, `accumulate_streaming_thinking` (~71–80) returns early when `type(delta) is not dict` **and** its deal pre uses `Mapping[str, Any]` (not `type is dict`). `_normalize_stream_delta` (~247–262) returns `{}` for non-dicts before further work.

---

## Sync response extraction (caller context)

[`plugin/framework/client/base_provider_shim.py`](../../plugin/framework/client/base_provider_shim.py) `parse_sync_response` (~91–105):

```python
message = choice.get("message") or response_data.get("message") or {}
if not isinstance(message, dict):
    message = {}
_normalize_delta(message)
```

`isinstance(message, dict)` is **true** for dict subclasses, so subclasses are passed to `_normalize_delta` and can trigger the pre-contract even though the shim intended to only normalize plain dicts.

JSON from `safe_json_loads` / `json.loads` is normally a plain `dict`; if MiniMax/OpenRouter returns a shape that gets converted to a subclass somewhere in the stack, or if a future caller passes a wrapped message, this fires.

**Investigation tip for fix agent:** Log `type(message)` and a repr of `result["choices"][0]["message"]` keys on OpenRouter MiniMax sync tool responses before `_normalize_delta`.

---

## Reproduction

```bash
export OPENROUTER_API_KEY=sk-…
cd scripts/prompt_optimization
../.venv/bin/python run_eval.py \
  --backend string \
  --models minimax/minimax-m3 \
  -e format_preservation \
  -j 1 -v
```

Or from repo root:

```bash
python scripts/benchmark.py --model minimax/minimax-m3 -n 1 \
  --examples 1  # if wired; otherwise use run_eval -e format_preservation
```

Expect `error` containing `type(delta) is dict` and `total_tokens: 0` until fixed.

---

## Fix directions (pick one coherent approach)

1. **Align contract with shim (minimal)**  
   Remove or relax `@deal.pre` on `_normalize_delta` so the existing `if type(delta) is not dict: return` body is the guard. Keep postconditions on the dict path only, or split into `_normalize_delta_plain` (with deal) + public wrapper without pre.

2. **Normalize at boundary**  
   Before `_normalize_delta`, coerce with `dict(message)` only when `isinstance(message, dict)` and `type(message) is dict` — or copy plain dict: `{k: message[k] for k in message}` for subclasses.

3. **Call-site skip**  
   In `parse_sync_response` and streaming loop (`llm_client.py` ~768–769), only call `_normalize_delta` when `type(delta) is dict` (matches streaming `if delta and on_delta` which skips empty dict but not subclasses).

4. **Same audit for `accumulate_delta`**  
   Streaming chat tool calls depend on it; consider matching guard or plain-dict coercion at `on_delta` in `llm_client.py` ~943–947.

**Do not** blindly replace all `type(x) is dict` with `isinstance(x, dict)` without reading CrossHair / AttrDict notes in `stream_normalizer.py`.

---

## Tests to add

Extend [`tests/framework/test_stream_normalizer_verification.py`](../../tests/framework/test_stream_normalizer_verification.py):

- `test_normalize_delta_no_crash_on_ordered_dict` — `OrderedDict` message should not raise when `deal` pre is present (either no-op or normalize).
- `test_normalize_delta_no_crash_on_non_dict` — `None`, `str`, `list` should not raise.
- Optional integration: mock `parse_sync_response` input shaped like OpenRouter MiniMax tool response.

Run:

```bash
make pytest   # tests/framework/test_stream_normalizer_verification.py
make typecheck
```

---

## Oracle / scoring after fix

`format_preservation` task ([`dataset.py`](../../scripts/prompt_optimization/dataset.py)):

- Document: `John Doe - Project Lead` + line with `Contact person: John Doe (legacy ID JD-001)`.
- Ask: replace **only** first-line name with `Jane Smith`; second line must stay verbatim.
- Oracle: [`oracles.oracle_format_preservation`](../../scripts/prompt_optimization/oracles.py).

After the harness fix, MiniMax may still **fail the task** on document content — that would be a real model failure, not this crash.

---

## Key files (quick index)

| File | Role |
|------|------|
| `plugin/framework/client/stream_normalizer.py` | `_normalize_delta`, `_normalize_stream_delta`, deal contracts |
| `plugin/framework/client/base_provider_shim.py` | `parse_sync_response` → `_normalize_delta(message)` |
| `plugin/framework/client/llm_client.py` | `request_with_tools` sync + streaming loops |
| `plugin/framework/async_stream.py` | `accumulate_delta` (same `type is dict` pre) |
| `scripts/prompt_optimization/llm_chat_eval.py` | Eval tool loop, `stream=False`, exception → `error` |
| `docs/framework/streaming-and-threading.md` | UI streaming / reasoning replay (related context) |

---

## Background docs

- Eval harness overview: [`docs/eval/eval-dev-plan.md`](eval-dev-plan.md)
- HTTP / LLM module entry: [`plugin/framework/client/llm_client.py`](../../plugin/framework/client/llm_client.py) module doc / [`docs/repo-map.md`](../../docs/repo-map.md)

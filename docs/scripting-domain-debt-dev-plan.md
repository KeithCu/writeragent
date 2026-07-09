# Scripting domain debt reduction — dev plan

Phased cleanup of trusted-helper host glue: declarative registries, shared RPC, and zero-AST worker dispatch.

## Phase 1 — Shared helper headers (shipped)

- [`plugin/scripting/helper_domain.py`](../plugin/scripting/helper_domain.py): `HelperScriptMeta`, header parse/build, RPS outcome helpers.
- Domain modules keep compute/egress; host glue deduplicated.

## Phase 2 — Declarative RPS registry (shipped)

- [`plugin/scripting/domain_registry.py`](../plugin/scripting/domain_registry.py): `WIRING_TABLE`, `get_rps_domains()`, `try_rps_fast_path`.
- [`plugin/scripting/python_runner.py`](../plugin/scripting/python_runner.py) uses registry instead of per-domain `if` blocks.

## Phase 3 — Spec-driven venv dispatchers (shipped)

- Analysis, viz, symbolic, units, quant, optimize, forecast venv entry points use `(spec, data, context)`.
- Host [`plugin/scripting/client.py`](../plugin/scripting/client.py) `_make_spec_runner` uses `run_trusted_action` RPC.

## Phase 4 — Partial action migration (shipped)

- `embedding_client`, `langdetect_service`, and scripting spec runners on `action: "run_trusted_action"`.
- Embeddings index + folder FTS still used fixed stub strings; heartbeat tied to `"maintain_folder_index" in stub_code`.

## Phase 5 — Full trusted-action dispatch (shipped)

**Goal:** Remove all host fixed-stub RPC; single path via registry + direct venv dispatch.

| Component | Change |
|-----------|--------|
| [`trusted_action_registry.py`](../plugin/scripting/trusted_action_registry.py) | Declarative domain → dispatcher wiring |
| [`trusted_rpc.py`](../plugin/scripting/trusted_rpc.py) | Shared `run_trusted_worker_action` host helper |
| [`trusted_dispatch.py`](../plugin/scripting/venv/trusted_dispatch.py) | Scripting / vision / sql / linter domains |
| [`embeddings_index_dispatch.py`](../plugin/embeddings/venv/embeddings_index_dispatch.py) | Index maintain/search RPC |
| [`folder_fts_dispatch.py`](../plugin/embeddings/venv/folder_fts_dispatch.py) | FTS maintain/search RPC |
| [`worker_harness.py`](../plugin/scripting/venv/worker_harness.py) | Registry lookup; heartbeat via `supports_heartbeat` |
| Host clients | `embeddings_service`, `folder_fts_service`, `client.py` — no `_STUB` constants |

**Invariant:** LLM-submitted code still uses AST sandbox; trusted modules run as normal bytecode inside the venv worker.

**Tests:** `tests/scripting/test_trusted_action_registry.py`, `tests/scripting/test_worker_harness_trusted_action.py`, updated embeddings/client contract tests.

See also [enabling_numpy_in_libreoffice.md §Trusted extension code](enabling_numpy_in_libreoffice.md#trusted-extension-code-in-the-venv).

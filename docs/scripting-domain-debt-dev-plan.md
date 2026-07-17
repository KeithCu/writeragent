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

## Phase 6 — Finish registry pattern (shipped)

| Item | Change |
|------|--------|
| [`trusted_dispatch.py`](../plugin/scripting/venv/trusted_dispatch.py) | Per-domain `dispatch_*` handlers; no monolith `if domain` chain |
| [`trusted_action_registry.py`](../plugin/scripting/trusted_action_registry.py) | Each scripting domain points at its `dispatch_*` (math → `dispatch_symbolic`) |
| [`venv_sandbox.py`](../plugin/scripting/venv/venv_sandbox.py) | Removed vision/embeddings string-stub bypass |
| [`embeddings_index_dispatch.py`](../plugin/embeddings/venv/embeddings_index_dispatch.py) | `warm_embedder` helper; [`venv_worker.warm_venv_worker`](../plugin/scripting/venv_worker.py) uses `run_trusted_action` |
| [`analysis.py`](../plugin/scripting/analysis.py) | Templates via `make_template_api` (duckdb SQL templates stay hand-rolled) |
| [`client.py`](../plugin/scripting/client.py) | `run_quant` via `_make_spec_runner` |
| [`venv_worker.py`](../plugin/scripting/venv_worker.py) | Dropped diagnostics/sandbox re-exports |
| [`domain_registry.py`](../plugin/scripting/domain_registry.py) | `PICKER_WIRING` table; quant/optimize/forecast expose `get_*_script_templates` |

## Future ideas (not started)

Follow-ons from the Phase 6 planning pass. Prefer least-code reuse of existing factories (`make_template_api`, `WIRING_TABLE`, `TrustedActionWiring`) over new frameworks.

### Medium — orchestration / UI cleanup

| Idea | Notes |
|------|--------|
| Extract result formatting from [`python_runner.py`](../plugin/scripting/python_runner.py) | Keep runner as orchestration; move Writer/Calc/Draw insert + `format_result_for_writer` to something like `result_formatters.py`. Reuse `try_rps_post_venv`. Add tests for Calc/Draw insert (currently thin). |
| Thin [`venv_diagnostics.py`](../plugin/scripting/venv_diagnostics.py) | Declarative probe-group specs (reuse `_self_check_group_specs` shape); keep orchestration thin. Heavy existing coverage makes this low risk. |
| Targeted unit tests | Direct tests for [`trusted_rpc.py`](../plugin/scripting/trusted_rpc.py); Calc/Draw insert paths; optional [`venv/vale.py`](../plugin/scripting/venv/vale.py) unit coverage. |
| Harper path docs only | Host in-process grammar vs trusted-worker path is intentional: many users never configure a Python venv, and grammar must still work. Do not route realtime Harper through the warm worker. |

### Larger / higher risk — defer unless needed

| Idea | Notes |
|------|--------|
| Shared `DOMAIN_SPECS` generating all registries | One table (or small codegen) feeding trusted-action wiring, RPS `WIRING_TABLE`, and `PICKER_WIRING`. Seed from existing tables; avoid a fourth ad-hoc registry. |
| Careful [`payload_codec.py`](../plugin/scripting/payload_codec.py) split | Extract read-only helpers / Cython loader only. **Do not** split the flatten/unpack hot loop without re-running serialization A/B + worker tests — see [numpy-serialization.md](numpy-serialization.md). |
| Do not merge [`venv/calc_functions_*.py`](../plugin/scripting/venv/) | Alphabet splits keep host import light; Excel-parity risk is high. Optional index doc only. |
| Venv ↔ LO tool RPC | Product feature (`writeragent_api` stubs / [enabling_numpy_in_libreoffice.md](enabling_numpy_in_libreoffice.md) §7), not structural cleanup. |
| Live UNO suites | Multi-range Calc, `=PY()` plot e2e — valuable harness cost, not debt structure. |

### Intentional non-goals

- Separate `plugin/{scripting,embeddings,vision}/venv/` trees and worker pools (isolation + release splits).
- Dual Harper execution paths (host vs worker) as designed today: host for grammar without a venv; worker only for non-grammar trusted callers.- Expanding `make_template_api` for duckdb custom SQL template bodies unless a real second consumer appears.

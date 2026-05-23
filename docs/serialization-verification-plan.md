# Serialization Formal Verification

**Goal:** Apply the formal verification approach from [`docs/formal_verification.md`](formal_verification.md) to the split_grid serialization code in [`plugin/scripting/payload_codec.py`](../plugin/scripting/payload_codec.py).

This is the reference implementation for Tier-0 (pure Python) contract + CrossHair verification in WriterAgent.

---

## Status (2026-05)

| Item | State |
|------|--------|
| `deal` contracts | 8 functions in `payload_codec.py` (see list below) |
| Dev dependencies | `deal`, `crosshair-tool` in [`pyproject.toml`](../pyproject.toml) |
| Release strip | [`scripts/strip_code.py`](../scripts/strip_code.py) removes `@deal.*` decorators and import shim |
| Pytest hook | [`tests/scripting/test_serialization_verification.py`](../tests/scripting/test_serialization_verification.py) |
| Makefile target | `make verify-serialization` |
| Status tracking | [`verification_status.json`](../verification_status.json) |
| CI integration | Not yet wired |

**Functions with contracts:**

1. `_flatten_grid_to_components` — core flatten logic
2. `host_pack_split_grid`
3. `host_pack_data`
4. `host_unpack_split_grid`
5. `child_unpack_split_grid`
6. `child_unpack_data`
7. `child_pack_split_grid`
8. `child_pack_result`

`host_unpack_data` is a thin dispatcher with **no** contracts (delegates to `host_unpack_split_grid`).

Round-trip equality (`host_unpack(host_pack(grid)) == grid`) is validated in pytest, not as `@deal.ensure` (too expensive for CrossHair).

---

## Architecture

```mermaid
flowchart LR
  subgraph dev [Dev / CI]
    deal["deal contracts"]
    crosshair[CrossHair check]
    pytest[pytest invariants]
    deal --> crosshair
    deal --> pytest
  end
  subgraph release [Release OXT]
    strip[strip_code.py]
    lo[LibreOffice runtime]
    strip --> lo
  end
  dev --> strip
```

### Release no-ops (zero runtime cost in LibreOffice)

**Two-layer safety:**

1. **Guarded import** — `_DummyDeal` no-op shim when `deal` is not installed:

   ```python
   try:
       deal = importlib.import_module("deal")
   except ImportError:
       class _DummyDeal:
           def __getattr__(self, name: str) -> Any:
               return lambda *args, **kwargs: lambda f: f
       deal = _DummyDeal()
   ```

2. **Build-time stripping** — [`scripts/strip_code.py`](../scripts/strip_code.py) removes `@deal.*` decorators and the import/shim block from the production bundle. Tests in [`scripts/tests/test_strip_code.py`](../scripts/tests/test_strip_code.py).

---

## Contract design

### Helpers

Shared predicates keep `@deal` lambdas short and CrossHair-friendly:

- `_is_grid_sequence(grid)` — empty, 1D, or 2D list/tuple (jagged 2D allowed; flatten raises `ValueError`)
- `_is_split_grid_envelope(envelope)` — valid split_grid wire dict shape
- `_is_ndarray(obj)` — NumPy ndarray type check without importing NumPy at module load

### Key invariants encoded

- `strings` dict keys are integers; values are strings
- `column_kinds` length matches column count
- Buffer byte length is a multiple of 8 (float64 cells)
- When `strings == {}`, child unpack returns ndarray (pytest); when strings present, returns list (`@deal.ensure` on `child_unpack_split_grid`)
- Jagged 2D grids raise `ValueError` via `@deal.raises` on `_flatten_grid_to_components`

### Dispatch wrappers

`host_pack_data`, `child_unpack_data`, and `child_pack_result` use **minimal** pre/post contracts. Branch-specific guarantees (ndarray vs list vs split_grid dict) live in pytest oracles.

Functions with keyword-only parameters use `@deal.pre(lambda arg, *_, **__: ...)` to avoid Deal/CrossHair `TypeError` on default-arg forwarding.

---

## Workflow

### Local verification

```bash
# Runtime invariant tests (fast)
make verify-serialization

# Or pytest directly (CrossHair test skipped if tool not installed)
pytest tests/scripting/test_serialization_verification.py -v

# CrossHair on core Tier-0 functions (slower)
crosshair check plugin.scripting.payload_codec._flatten_grid_to_components --per_condition_timeout=10
crosshair check plugin.scripting.payload_codec.host_pack_split_grid --per_condition_timeout=10
crosshair check plugin.scripting.payload_codec.host_unpack_split_grid --per_condition_timeout=10
crosshair check plugin.scripting.payload_codec.child_unpack_split_grid --per_condition_timeout=10

# Full module scan with report
crosshair check plugin/scripting/payload_codec.py --per_condition_timeout=8 --report_all
```

**Targeting:** use fully-qualified function names or a file path. There is no `--include` flag in current CrossHair; contracts are auto-discovered from `deal` (no `--contracts` flag needed).

### Interpreting CrossHair output

| Message | Meaning |
|---------|---------|
| `Confirmed over all paths` | Condition proven for explored paths |
| `Not confirmed` | No counterexample found, but not proven (common for complex ensures) |
| `Unable to meet precondition` | CrossHair could not synthesize valid inputs (e.g. ndarray for `child_pack_split_grid`) |
| `: error:` | **Counterexample** — contract violation; must fix |

The pytest CrossHair hook fails only on `: error:` lines (counterexamples), not on `Not confirmed`.

### Existing test coverage

[`tests/scripting/test_serialization_ab.py`](../tests/scripting/test_serialization_ab.py) provides A/B regression tests. [`tests/scripting/test_payload_codec.py`](../tests/scripting/test_payload_codec.py) covers edge cases. Verification tests complement these with formal contracts and optional concolic search.

---

## Known gaps

- Most `@deal.ensure` conditions report `Not confirmed` — expected for complex serialization logic; no counterexamples found to date.
- `child_pack_split_grid` pre may report `Unable to meet precondition` when CrossHair cannot synthesize ndarrays.
- Round-trip and branch-specific oracles remain in pytest, not `@deal.ensure`.
- CI matrix entry not yet added.

---

## Next steps

1. Optional CI job running `make verify-serialization` on a schedule or PR label.
2. Extend contracts to other Tier-0 helpers: `should_use_binary_envelope`, `column_kinds_for_grid`.
3. Consider `scripts/update_verification_status.py` to refresh [`verification_status.json`](../verification_status.json) after CrossHair runs.

---

## Why this module is high value

- Pure Tier 0 logic (no UNO)
- Complex numeric + mixed-type handling with subtle edge cases
- Strong existing test coverage
- Performance-critical path for `=PYTHON()` and chat tools
- Mistakes affect both Calc and LLM observation quality

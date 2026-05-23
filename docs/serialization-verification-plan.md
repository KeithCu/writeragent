# Serialization Formal Verification Plan

**Goal:** Apply the formal verification approach from `docs/formal_verification.md` to the split_grid serialization code in `plugin/scripting/payload_codec.py`.

This gives us high-confidence mathematical checking of the most complex pure-Python data transformation logic in the project.

---

## 1. Release No-Ops Strategy (Critical Constraint)

We must ensure `deal` contracts have **zero runtime cost** inside LibreOffice.

### Chosen Approach

**Two-layer safety:**

1. **Guarded Import** (in source)
   ```python
   try:
       import deal
   except ImportError:
       deal = None
   ```

2. **Build-time Stripping** (in `scripts/build_oxt.py`)
   - Extend `strip_production_code()` to detect and remove `@deal.*` decorators.
   - This is the same mechanism already used for `log.debug`, `print`, and `grammar_obs`.

### Stripper Changes Needed

In `scripts/build_oxt.py`, inside `FindVisitor`:

- Add `visit_FunctionDef` and `visit_AsyncFunctionDef`
- Add helper `_strip_deal_decorators(node)` that walks `node.decorator_list` and removes any decorator where the root is `deal`

This ensures that even if someone accidentally leaves a `deal` import, the decorators themselves disappear in release builds.

---

## 2. First Contracts to Add (payload_codec.py)

We will start with the highest-value functions.

### Target Functions (Phase 1)

1. `host_pack_split_grid`
2. `child_unpack_split_grid`
3. `host_pack_data` / `host_unpack_data` (public entry points)
4. `_flatten_grid_to_components` (core logic)

### Example Contracts (Initial Set)

```python
if deal:
    @deal.pre(lambda grid: isinstance(grid, (list, tuple)))
    @deal.post(lambda result: isinstance(result, dict))
    @deal.ensure(lambda grid, result: result.get("__wa_payload__") == "split_grid")
    def host_pack_split_grid(grid):
        ...
```

```python
if deal:
    @deal.pre(lambda envelope: isinstance(envelope, dict))
    @deal.post(lambda result: result is not None)
    @deal.ensure(lambda envelope, result: 
        (isinstance(result, list) or 
         (hasattr(result, 'shape') and result.shape is not None)))
    def child_unpack_split_grid(envelope):
        ...
```

Key invariants we want to encode:

- `strings` dict only contains integer keys
- When `strings == {}`, pure numeric path returns `ndarray`
- Shape is preserved through pack → unpack
- String values are never turned into floats
- `column_kinds` length matches number of columns

---

## 3. Testing Workflow

### Local Verification

```bash
# 1. Add contracts
# 2. Run type checker
mypy plugin/scripting/payload_codec.py --strict

# 3. Run CrossHair
crosshair check plugin/scripting/payload_codec.py --contracts --per_condition_timeout=10
```

### Integration with Existing Tests

The file `tests/scripting/test_serialization_ab.py` already does excellent A/B testing. We can later add `@deal.ensure` contracts that the A/B tests implicitly validate.

---

## 4. Next Steps After Initial Contracts

1. Add 4–6 contracts to `payload_codec.py`
2. Run CrossHair and fix any counterexamples found
3. Extend the stripper in `build_oxt.py`
4. Add `deal` + `crosshair` as optional dev dependencies in `pyproject.toml`
5. Create `verification_status.json` entry for the module
6. Consider adding contracts to `child_pack_result` and the full ingress/egress cycle helpers

---

## 5. Why This Module Is High Value

- Pure Tier 0 logic (no UNO)
- Complex numeric + mixed-type handling with many subtle edge cases
- Already has strong test coverage (`test_serialization_ab.py`)
- Performance-critical path used by `=PYTHON()` and chat tools
- Mistakes here affect both Calc and LLM observation quality

This is one of the best places in the entire codebase to demonstrate the value of the formal verification approach.
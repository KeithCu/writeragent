---
name: Blank vs NaN — shipped decision (egress-only)
overview: We chose the minimal egress-only path. No wire format change. Computed NaN now surfaces as a real Calc error (cascades). Python None still maps to empty cell. Production wire remains Pickle5 + split_grid.
todos:
  - id: done-codec
    content: Stop collapsing NaN→None uniformly in host_unpack_split_grid and _cell_for_json (payload_codec.py)
    status: completed
  - id: done-calc
    content: to_calc_compatible returns raw NaN (error) instead of "" (python_function.py)
    status: completed
  - id: done-docs
    content: Updated enabling_numpy, numpy-serialization, and this doc with pickle+split_grid clarification and new policy
    status: completed
isProject: false
---

# Blank vs NaN — what shipped

## Decision (locked, implemented)

- **No wire-format change.** No `blank_indices`, no `blank_mask`, no side channel, no Cython changes, no masked arrays.
- **No LLM-vs-non-LLM fork.** The codec is uniform: `float('nan')` is preserved everywhere. Only the terminal renderers differ (Calc cells vs text for the model), which is inherent.
- **Full egress:** every computed NaN (scalar or matrix slot) becomes a real Calc error that **cascades** (`#NUM!` / `#VALUE!` from the add-in bridge). Python `None` continues to map to an empty cell (`""`).
- **Accepted tradeoff:** a Calc blank that flows through a pure-numeric range becomes `np.nan` in the worker and now renders as an error cell on egress (instead of staying empty). This is consistent with the spreadsheet model where a missing numeric value taints dependents.

## Why this, not the bigger design

Microsoft's Python in Excel does not preserve a blank-vs-NaN distinction on ingress (ranges become pandas DataFrames; empty → `NaN`) and renders computed `np.nan` as `#NUM!` on output (with the well-known wart that object-column `None` can become the literal string `'None'`). See [microsoft/python-in-excel#38](https://github.com/microsoft/python-in-excel/issues/38).

We can match or beat that behavior with far less code by fixing **egress only**:

- The error path already existed: `to_calc_compatible` had an explicit `isnan → ""` to *suppress* the error that a raw NaN double produces in Calc. We simply stopped suppressing it.
- The only thing that was hiding computed NaN from Calc was the codec itself collapsing buffer NaN → `None` (in `host_unpack_split_grid` for split_grid results and `_cell_for_json` for small/list results). We removed that collapse uniformly.

A parallel blank side-channel + masked-array ingress would let us:
- keep pass-through numeric blanks as empty cells, and
- make `np.mean(data)` ignore blanks automatically.

We explicitly deferred that. It can be added later without breaking the current contract (the wire already carries NaN slots; we would only be adding provenance about *which* NaN slots were blanks).

## What changed in the code

- [`plugin/scripting/payload_codec.py`](plugin/scripting/payload_codec.py):
  - `host_unpack_split_grid`: buffer NaN values are kept as `float('nan')` (int/bool uniform columns emit NaN for NaN slots; float columns pass the value through). Strings map is unchanged.
  - `_cell_for_json` / `grid_from_nested_list`: only `None` normalizes to `None`; `nan` passes through. This affects small grids and list results below `BINARY_MIN_CELLS`.
  - `grid_from_nested_list` docstring updated; no longer claims "JSON path".
- [`plugin/calc/python/function.py`](plugin/calc/python/function.py):
  - `to_calc_compatible`: `isnan(val)` now returns the raw float (Calc shows error). `None` still becomes `""`.
- No changes to `child_pack_*`, Cython, or the split_grid envelope.

## Wire and serialization notes

- **Production:** length-prefixed **Pickle5** frames. Large grids use the `split_grid` envelope inside the pickle; small grids and some results use plain nested lists. The `buffer` is raw float64 bytes (or a Python `array.array('d')` on the host pack side).
- **No JSON on the wire** for data or results in normal execution. JSON (and Base64 `b64` variants) exist in `scripts/bench_serialization.py` and a few test helpers for comparison and diagnostics.
- The `split_grid` contract (shape + buffer + column_kinds + strings) is unchanged. We only stopped turning NaN slots into `None` on the host unpack side.

## Docs updates

- [`docs/enabling_numpy_in_libreoffice.md`](docs/enabling_numpy_in_libreoffice.md) — "Empty cells vs NaN" policy rewritten; coercion list and gotcha section updated; transport note added.
- [`docs/numpy-serialization.md`](docs/numpy-serialization.md) — production wire statement at top; egress and cell-semantics tables updated; a few narrative "JSON" references clarified as benchmark/historical.
- This file records the decision and the "why".

## Tests (to be expanded)

- `tests/scripting/test_payload_codec.py` and `test_serialization_ab.py`: parity now expects preserved `nan` (not coerced to `None`) on unpack for both split_grid and small list paths.
- `tests/calc/python/test_function*.py` (or equivalent): scalar NaN and matrix NaN slots become error values; `None` remains empty.
- Worker round-trips continue to work.

Run `make test` (and the LO-native suite via `plugin/testing_runner.py` when a soffice is present).

## Deferred (clean upgrade path)

If pass-through numeric blanks must remain empty cells (or you want `np.mean(data)` to auto-ignore Calc blanks like AVERAGE), the prior research in earlier revisions of this document still applies:

- Add a blank side-channel to `split_grid` (mode-tagged: `all_nan_blank` / `no_blanks` / `indices_blank` / `indices_nan` / `bitmap`, with indices as compact `uint32` bytes).
- Materialize pure-numeric ingress as `np.ma.masked_array` (mask derived from the blank metadata).
- Keep `child_pack_split_grid` able to read `.mask` on egress.
- Update `to_calc_compatible` (or a policy switch) so blank-tagged NaN → `""` while bare computed NaN → error.

Because host and worker ship together, we can make that change atomically when we choose to.

## Standardized Missing-Value Checks (`is_missing_value`)

To ensure consistent behavior across data coercion, parsing, and custom spreadsheet functions (like `=PY()`), we centralized checks into `is_missing_value` under [coerce.py](file:///home/keithcu/Desktop/Python/writeragent/plugin/scripting/venv/coerce.py):

* **Standard Missing-Values:** Detects `None`, empty/blank strings `""`, and common LibreOffice error tokens (e.g. `#VALUE!`, `#NUM!`).
* **NaNs:** Detects both native Python float `NaN` and NumPy floating-point `NaN` types.
* **Usage:** Used uniformly by data frame coercion helpers and standard Excel formula replicas (`AVERAGEA`, `ISBLANK`, `ISNA`, `MATCH`, etc.) to prevent divergent cell-handling behavior.

## Summary for authors (and LLM prompts)

- Blanks on ingress are still `np.nan` in numeric arrays — use `np.nansum` / `np.nanmean` when you mean "ignore missing."
- A computed `nan` result is now visible as an error in the sheet and will poison dependents. Return a string if you want a quiet marker.
- `None` (from text columns or explicit) is still the way to produce a true empty cell on egress.
- The wire is Pickle5 + split_grid. There is no JSON in normal data movement between the LibreOffice host and the venv worker.

---

## Future reference: Previously researched designs (deferred)

> This section preserves the detailed thinking and design alternatives that were explored before we chose the minimal egress-only implementation above. It is kept here purely as future reference in case we later decide to distinguish blanks from computed NaNs on the wire (e.g. to keep pass-through numeric blanks as empty cells, or to let `np.mean(data)` behave like Calc `AVERAGE`).

### Notes — user-visible behavior (opinion, not locked)

#### What users actually complain about

The main pain is **ingress**: a numeric range with one empty Calc cell becomes `np.nan` holes in `data`, so `np.mean(data)` returns `nan`, egress coerces that to a **silent blank cell** — no error, no number, hard to debug. The gotcha section in the core guide already documents workarounds (`np.nanmean`); the feature should reduce the need for those in the common case.

#### Semantic model (spreadsheet-first)

Treat two concepts differently:

| Concept | Origin | User mental model | Should behave like |
|---------|--------|-------------------|--------------------|
| **Blank** | Calc empty cell | "no value entered" | Calc `AVERAGE` skips it; cell stays empty on round-trip |
| **NaN** | Python/NumPy computation | "numeric operation failed / undefined" | May propagate or be shown explicitly — not the same as an empty input cell |

Calc never sends IEEE NaN on ingress — only empty cells (`None`). The ambiguity is introduced by the codec and hurts on the way back out.

#### Ingress — what we considered

- **Priority #1.** Large pure-numeric ranges (split_grid path) should not require `np.nanmean` for the obvious `result = np.mean(data)` case when holes are **Calc blanks**, not computed NaNs.
- **Reasonable target:** blanks behave like **missing data** (spreadsheet empty), not like poison values. NumPy’s natural expression is a **masked array** (`data.mean()` ignores masked slots) rather than a raw float64 ndarray full of `np.nan`.
- **Mixed-type ranges** (any text): keep **`None` in nested lists** — already closer to spreadsheet semantics; less change, lower risk.
- **Small ranges** (< threshold, nested list wire): preserve **`None` vs `float('nan')`** if possible.
- **Do not try to fully emulate Calc `SUM` via one numpy call.** Calc SUM treats empty as 0; AVERAGE skips empty. NumPy has no single function that matches both. Document that **`np.nansum` ≠ SUM**; optional sandbox helpers (`wa_sum`, `wa_average`) are fine but not a substitute for fixing the mean case.

#### Egress — what we considered (lower priority at the time)

- **Keep formula safety as the default.** Matrix results that write NaN into neighboring formula ranges should not suddenly start producing `#NUM!` / `#VALUE!` without an explicit author choice.
- **Blank / `None` / masked-out slots → empty cell** always — this matches spreadsheet expectations.
- **Computed `np.nan` (no blank tag) → empty cell** was acceptable for matrix slots in the old policy.
- **Scalar `result = float('nan')` → empty cell** was the confusing case. A modest UX win: scalar computed NaN → visible **`"NaN"` text** (or documented opt-in), while matrix NaN slots stay blank.
- **Round-trip:** if the script returns `data` unchanged, cells that were Calc-empty on the way in should still be **empty in the sheet**.

#### Docs and LLM guidance (user-visible)

- Update the core guides: what type `data` is (ndarray vs masked array vs list), which aggregations “just work”, and what still needs explicit handling.
- If ingress materialization changes (e.g. masked array), update sandbox prefix / prompts so generated scripts use the right API.
- Retire or shorten the “silent blank from NaN poisoning” gotcha once ingress behavior is fixed; keep notes for edge cases (inf, intentional NaN display, SUM vs nansum).

#### What we said we would not optimize for initially

- Round-tripping “real NaN” into Calc as `#NUM!` by default.
- Pandas nullable dtypes as the primary `data` representation.
- Breaking the `frombuffer` fast path for large numeric grids.

### Problem today (the state before the egress-only fix)

The production wire format (`split_grid` inside Pickle5) stores every numeric grid as a dense **float64 buffer** plus an optional sparse **`strings`** index map.

| Cell meaning | `buffer` | `strings` | After child unpack (pure numeric) | After egress to Calc |
|--------------|----------|-----------|-----------------------------------|----------------------|
| Calc empty (`None`) | `NaN` | — | `np.nan` | `""` (blank) |
| Python/NumPy `NaN` | `NaN` | — | `np.nan` | `""` (blank) |
| Text | `NaN` | `{flat_idx: str}` | string in list path | text |

Both “missing” flavors shared the same **NaN slot** in the buffer. That was intentional because:
- Pure numeric ingress needs a homogeneous float64 lane for **`np.frombuffer`** (the main performance win).
- Egress mapped **all** NaN/`None` to empty cells via `to_calc_compatible` to avoid `#NUM!` / `#VALUE!` in matrix blocks.

**User-visible pain:** on ingress, `np.mean(data)` is poisoned by holes that came from Calc blanks; on egress, a computed `nan` silently became a blank cell.

**Important nuance:** Calc ingress never delivers a native float NaN — empty UNO cells become Python `None`. The ambiguity appeared only **after** materialization as `np.nan`, or on **egress** when Python returned `None` vs `float('nan')`.

### Can split_grid store the distinction efficiently?

**Yes.** The format already uses a **parallel side channel** for non-numeric data (`strings`). A second side channel for “blank/missing semantics” fits the same pattern and keeps the float64 fast path intact.

#### Encoding options (ranked)

| Option | Wire shape | Size for N cells, B blanks | Lookup on unpack | Notes |
|--------|------------|----------------------------|------------------|-------|
| **A. Dense bitmap** (`blank_mask: bytes`) | `(N + 7) // 8` bytes | 100k cells → **12.5 KiB** (~1.6% of 800 KiB buffer) | O(1) bit test; vectorized with `np.unpackbits` | Best when blanks are common or sparsity unknown |
| **B. Sparse index list** (`blank_indices: list[int]`) | 8×B bytes (Pickle5) | 1 blank in 10k → **8 B**; 90k blanks → **720 KiB** | O(B) or build bitmap once | Mirrors `strings`; wins for typical data tables |
| **C. Hybrid auto-pick** | A or B based on threshold | min(A, B) | Slightly more codec logic | e.g. use list when `B * 8 < (N+7)//8` |
| **D. Reuse `strings` with sentinel** | e.g. `{idx: ""}` or magic | Awful | Collides with real empty strings | **Not recommended** |
| **E. Separate buffer dtype / object lane** | object array or second buffer | Large; kills `frombuffer` path | — | **Defeats the purpose of split_grid** |

**Recommendation:** **Option C (hybrid)** (or a clean mode-tagged variant) with a simple rule:

- **`blank_indices`** when blank count is small (typical: a few holes in a numeric column).
- **`blank_mask`** packed bytes when blanks dominate (sparse sheets, mostly-empty ranges).
- When `B == 0`: emit **`blank_mask: b""`** (zero-length bitmap) — keeps unpack logic uniform.

A later refinement proposed a single **mode-tagged** side channel that always picks the cheapest representation and inverts the meaning when blanks dominate:

- `all_nan_blank` — every NaN-slot is blank (0 extra bytes). Ingress default + the common egress case.
- `no_blanks` — every NaN-slot is a computed NaN (0 extra bytes).
- `indices_blank` — raw `uint32` flat indices of blanks.
- `indices_nan` — raw `uint32` flat indices of computed NaNs (the inversion: mostly-blank sparse sheet with a few real NaNs).
- `bitmap` — packed bits, 1 = blank.

Encoder picks `argmin(4·B, 4·C, ⌈N/8⌉)` where B = blanks, C = computed NaNs among NaN-slots, N = cells. Indices stored as compact bytes (e.g. `array('I').tobytes()`).

#### Updated split_grid envelope (proposed at the time)

```python
{
  "__wa_payload__": "split_grid",
  "dtype": "float64",
  "shape": [rows, cols],
  "column_kinds": ["int", "float", ...],
  "buffer": b"...",           # unchanged: NaN = non-numeric or missing slot
  "strings": {7: "banana"},    # unchanged
  # NEW (required — one of these, never both):
  "blank_mode": "all_nan_blank" | "no_blanks" | "indices_blank" | "indices_nan" | "bitmap",
  "blank_data": b"",           # uint32 LE indices, or np.packbits; b"" for the two 0-byte modes
}
```

**Invariants (proposed):**
- Blank bit/set membership means **“Calc-style blank / missing-for-stats”**; buffer may still be `NaN` at those indices.
- Buffer NaN **without** blank bit = **computed NaN** (Python/NumPy semantic).
- **`strings` indices and blank indices are disjoint** in normal packing (text cells are not blank).
- Pickle5 cost for small bitmaps/indices is negligible.

### Where metadata would be set and consumed

Pack/unpack touchpoints (all in `payload_codec.py` unless noted):

| Stage | Function | Change |
|-------|----------|--------|
| Host ingress pack | `_flatten_grid_to_components` / `host_pack_split_grid` | Already tracks `column_has_none[c]` per column; extend to **per-cell blank bit** when `val is None`. Also distinguish `float('nan')` in nested-list path if ever present. |
| Cython fast path | `native/writeragent_vec/pack.pyx` | Same blank-bit logic in `_flatten_cell`. |
| Child ingress unpack | `child_unpack_split_grid` | After `frombuffer`, apply blank metadata. |
| Child egress pack | `child_pack_split_grid` | Read blank bits from **`np.ma.masked_array` mask**, or from list `None` vs `nan` when packing lists back to split_grid. |
| Host egress unpack | `host_unpack_split_grid` | Use blank bit to choose `None` vs leave `float('nan')` in nested lists. |
| Calc write | `to_calc_compatible` | Policy-dependent: blank bit → `""`; bare NaN without bit → `""` (old) or `#NUM!` / `"NaN"` (future). |

Smaller grids (`< BINARY_MIN_CELLS`) stay on nested Pickle lists — they **already distinguish** `None` vs `float('nan')` (subject to the `_cell_for_json` rules at the time).

### Consumer-side choices (policy — not decided at the time)

#### Ingress — make stats spreadsheet-like

| Approach | Needs blank metadata? | `np.mean(data)` without user change? |
|----------|-----------------------|--------------------------------------|
| Status quo + docs (`np.nanmean`) | No | No |
| Unpack to **`np.ma.masked_array`** (mask from blank bits) | Yes | **`data.mean()`** yes; bare **`np.mean(data)`** still wrong — document `.mean()` or provide `wa_mean()` helper |
| Unpack to ndarray but inject **`data = np.ma.masked_invalid(...)`** with mask only on blank bits | Yes | Same as above |
| Auto-wrap in pandas with nullable NA | Yes + dependency | Different API |

**Spreadsheet alignment:** Calc `AVERAGE` skips empty cells; `SUM` treats empty as 0. NumPy has no single equivalent — **`np.nansum` ≠ SUM** (nansum skips NaN but sum treats empty as 0 in Calc). Blank metadata lets you build **`wa_average`** that matches Calc if desired.

#### Egress — show computed NaN vs intentional blank

| Policy | blank bit set | buffer NaN, no blank bit |
|--------|---------------|---------------------------|
| Today (pre-fix) | `""` | `""` |
| Strict display | `""` | leave as IEEE NaN → Calc `#NUM!` or stringify `"NaN"` |
| Excel-like | `""` | `"NaN"` text (visible, no formula poison) |

### Implementation phases (when you choose to proceed)

#### Phase 0 — Design gate (no code)

- Pick ingress policy, egress policy, or both.
- Decide hybrid threshold (e.g. `B * 8 < (N+7)//8 → indices else bitmap`).

#### Phase 1 — Wire codec (minimal, test-first)

- Add blank tracking to `_flatten_grid_to_components` (stdlib + Cython).
- Add **required** `blank_mode`/`blank_data` (or `blank_indices`/`blank_mask`) to envelope; helpers for choosing and applying.
- Update `@deal` contracts to require blank fields; update tests and legacy test helpers that construct split_grid envelopes by hand.
- Extend A/B suite with blank/nan oracles once policy is chosen.

#### Phase 2 — Consumer behavior (policy-specific)

- `child_unpack_split_grid`: materialize mask (likely `np.ma.masked_array` for pure numeric + blanks).
- `child_pack_split_grid` / list pack path: encode blank bits on egress.
- `host_unpack_split_grid` + `to_calc_compatible`: apply egress policy.

#### Phase 3 — Docs and author guidance

- Update core guides for the new `data` type and egress policy.
- Adjust LLM/sandbox prompts in `import_policy.py` if `data` type changes (e.g. masked array).

#### Phase 4 — Benchmark regression

- Re-run the serialization benchmark with blank-heavy fixtures; expect pack loop + small wire bump, **frombuffer path unchanged**.

### Alternatives that avoid wire-format change

| Alternative | Pros | Cons |
|-------------|------|------|
| Documentation only (`np.nanmean`, gotcha section) | Zero cost | Does not fix silent egress blank |
| Auto-coerce `data` to masked array in worker inject without persisting on wire | Helps ingress only; no envelope change | Loses distinction after user copies/reassigns; egress unchanged |
| Return helper API (`wa_mean`, `wa_nansum`) in sandbox prefix | Simple UX | Not automatic; doesn’t fix egress |
| Always use nested lists (disable split_grid for ranges with blanks) | Preserves `None` vs `nan` in Python lists | Destroys performance on large numeric grids |

**Conclusion from that era:** if the goal is **correctness at scale** (≥10 cells, split_grid path), a **parallel blank side channel** is the efficient, architecturally consistent fix. If the goal is **author education only**, docs suffice.

### Risk notes (from the earlier design work)

- **No backward-compat burden:** simultaneous deploy means pack and unpack can be updated atomically; remove legacy “NaN slot = ambiguous” branches rather than keeping fallbacks.
- **Formal verification:** split_grid is Tier-0; new required fields need CrossHair/deal contract updates and fixture updates.
- **Multi-range:** `PAYLOAD_MULTI_DATA` wraps multiple split_grid items — blank metadata is per-item, no change to outer envelope.
- **Chat tool / MCP paths** using the same codec inherit behavior automatically.
- **Do not** store blank state only in `column_has_none` — it is column-level and already discarded; insufficient for per-cell semantics.
- **Benchmark harness:** update any hand-built envelopes in the same change set.
- **Masked-array ingress risk:** changing `data` from `ndarray` to `MaskedArray` is the biggest behavioral risk (some libs ignore masks; `@`/`np.dot`, pandas, sklearn, matplotlib edge cases). Mitigations: keep decode vectorized; consider a config escape hatch if real scripts break.

---

*End of future reference appendix. The sections above are preserved for when (or if) we decide the extra power is worth the complexity.*
# Calc `=PY()` data shapes

**Glossary:** `=PY()` and `=PYTHON()` are the same Calc add-in (`XPythonFunction`). Formulas below use `=PY`; either name works.

This doc is the authoritative **behavior** contract for range arguments: what `data` / `ranges` look like in Python, blank vs NaN, dates, logicals, and multi-range varargs. The author intro (how to write `=PY()`, session modes, spill/matrix) is [hub §6](../enabling_numpy_in_libreoffice.md#6-the-py-calc-function); use this file for the complete range/`data` tables.

Related:

| Doc | Owns |
| --- | --- |
| [Enabling NumPy & Python](../enabling_numpy_in_libreoffice.md) | User guide, session modes, spill/matrix UX, architecture overview |
| [Venv IPC & serialization](../scripting/numpy-serialization.md) | Pickle5 wire, `split_grid`, benchmarks, codec invariants |
| [Microsoft `=PY` design stance](../scripting/ms-py-compatibility.md) | Why Calc keeps explicit `data` args; Excel packages already bind ranges as trailing `_xlws.PY` args that the rewriter maps onto `data` / `ranges` ([§5.8](../scripting/ms-py-compatibility.md#58-ooxml--xlfnpy-import)) |

Code: [`plugin/scripting/calc_range.py`](../../plugin/scripting/calc_range.py), [`plugin/calc/calc_addin_data.py`](../../plugin/calc/calc_addin_data.py), [`plugin/calc/python/function.py`](../../plugin/calc/python/function.py) (`to_calc_compatible`).

## Table of contents

1. [Data handoff and shaping](#data-handoff-and-shaping)
2. [Why CalcRange, not DataFrame by default](#why-calcrange-not-dataframe-by-default)
3. [Multi-range support (varargs)](#multi-range-support-varargs)
4. [Empty cells vs NaN](#empty-cells-vs-nan)
5. [Cell types and logicals](#cell-types-and-logicals)
6. [Dates and datetimes](#dates-and-datetimes)
7. [Pandas egress (DataFrame / Series)](#pandas-egress)
8. [Rectangular shape rules](#rectangular-shape-rules)
9. [Deferred upgrades](#deferred-upgrades)

---

## Data handoff and shaping {#data-handoff-and-shaping}

**Where does `data` come from?** In an IDE, referencing `data` looks like a `NameError`. In `=PY()`, `data` is **injected at runtime** when you pass a range (or cell) as a trailing formula argument.

When you write `=PY(code; range)`, the add-in:

1. Resolves the range in Calc and reads cell values as a **rectangular 2D grid** (orientation preserved).
2. Packs the grid in a `calc_range` wire envelope (`split_grid` is a private transport optimization — see [serialization](../scripting/numpy-serialization.md#strategy-3-split-grid-serialization-detail)).
3. Materializes [`CalcRange`](../../plugin/scripting/calc_range.py) values and injects **`ranges`** (always a `list`) plus polymorphic **`data`** (one arg → that `CalcRange`; two or more → the same list as `ranges`).
4. Runs your script with `data` / `ranges` already bound.

| Range you pass in Calc | Structure of `data` in Python | Example usage |
| --- | --- | --- |
| **Single cell** (e.g. `B1`) | `CalcRange` shape `(1, 1)` — supports arithmetic (`+ - * / // % **`), unary (`+ - abs`), comparisons, scalar coercions (`float`, `int`, `round`), and formatting directly | `data + 3` or `data * 4` (or explicit `data.values[0][0]`) |
| **Row** (e.g. `B1:D1`) | `CalcRange` shape `(1, N)` | `np.mean(data)` (via `__array__`) |
| **Column** (e.g. `B1:B10`) | `CalcRange` shape `(N, 1)` | `np.mean(data)` |
| **2D rectangle** (e.g. `B1:C5`) | `CalcRange` shape `(rows, cols)` | `data.to_pandas()` or `data.to_numpy()` |

Builtin `sum` / `min` / `max` iterate **rows**, not cells (`sum(data)` on a column is `0 + [10] + …` → TypeError). Use `np.sum(data)` / `np.mean(data)` (via `__array__`).

**API (explicit conversions)** — when `data` is a single `CalcRange`:

```python
data.values                                      # exact list[list] (None for blanks)
data.to_numpy()                                  # ndarray (None → nan for numeric dtype)
data.to_pandas()                                 # header_row=0 by default
data.to_pandas(header_row=None)                  # all rows are data; columns col_0…
data.to_pandas(parse_strings=True)               # opt-in currency/percent/date string parsing
data.to_pandas(date_cols=['Date of Birth'])      # parse specific date column(s)
data.to_pandas(date_cols=True)                   # auto-detect and parse all date columns
ranges                                           # always list[CalcRange]; len 1 when one formula arg
```

Returning a **pandas DataFrame** spills/writes with its **column header row** included. Returning a list/ndarray writes values only.

**Vectorized lightweight helpers:** Trusted elementwise helpers (e.g. `convert_quantity`, `format_currency`, `format_percent`) accept `data` directly and preserve orientation ($N \times 1$ column in $\to$ $N \times 1$ list out; $1 \times N$ row in $\to$ $1 \times N$ list out; $1 \times 1$ single cell $\to$ scalar).

Payload size cap: `scripting.python_max_data_cells` ([serialization config](../scripting/numpy-serialization.md#subprocess-module-map-and-config)). Host↔venv pipeline: [Current pipeline](../scripting/numpy-serialization.md#current-pipeline-and-costs).

**Gaps vs LibrePythonista (workarounds):** chat tool still single `data_range` (use multiple `=PY` cells or formula varargs); no `collapse` (tighter range or strip `None` in Python); DataFrame conversion is explicit via `data.to_pandas()` — [why that is the default](#why-calcrange-not-dataframe-by-default).

---

## Why CalcRange, not DataFrame by default {#why-calcrange-not-dataframe-by-default}

**Locked for now.** Ingress `data` / `ranges` stay [`CalcRange`](../../plugin/scripting/calc_range.py). Pandas is an **explicit conversion** (`data.to_pandas(…)`) and a **return type** ([pandas egress](#pandas-egress)). Do not inject a DataFrame as `data`. Do not add a Settings flag that switches the injected type.

LibrePythonista’s `lp("A1:B10")` and Excel Python’s `xl(…)` usually hand the script a DataFrame. That is a reasonable default for *their* product (pandas-in-the-sheet). It is the wrong default for *this* one (numpy-in-Calc, with pandas as a door). The two documented happy paths cannot share one DataFrame ingress:

```calc
=PY("np.mean(data)"; A1:A10)
=PY("df = data.to_pandas(); result = float(df['Sales'].mean())"; A1:C10)
```

A sheet range **is** a rectangular grid (`None` blanks, orientation preserved, 1×1 scalar-ish ops). `CalcRange` is that grid plus `__array__` so `np.mean(data)` works. A DataFrame is a grid **plus a header policy**. There is no header policy that serves both formulas.

### Silent breaks if `data` were a DataFrame

| Code | Today (`CalcRange`) | If `data` were a DataFrame |
| --- | --- | --- |
| `np.mean(data)` on `A1:A10` | Mean of the column (`__array__`) | `header_row=0` turns the first cell into a **column name**; `header_row=None` yields `col_0` and a different `mean` shape |
| `data[0]` | First **row** | Column labeled `0`, or `KeyError` |
| `len(data)` | Row count | **Column** count |
| `=PY("data * 4"; B1)` | 1×1 arithmetic | 1×1 DataFrame, not a scalar |
| `data.to_pandas()` | Opt-in, with an explicit header policy | Already a DataFrame — the call is noise or a second conversion |

`to_pandas()` defaults `header_row=0` (first row → names). That is correct for a table and wrong for a numeric column. `header_row=None` is correct for `np.mean` and wrong for `df['Sales']`. LibrePythonista papers over that with `headers=True` **on the call**. We already have the same flag: **type it at the conversion**, not on the ingress object. The Excel rewriter follows that rule: `xl(…, headers=True/False)` → `.to_pandas(…)` / `.to_pandas(header_row=None)`; omitted headers stay bare `data`.

### Other reasons the grid stays the injected type

- **Host stays NumPy/pandas-free.** Pandas is imported only inside `to_pandas()`, in the venv ([`calc_range.py`](../../plugin/scripting/calc_range.py) module doc). Collabora’s compute path is JSON grids. Injecting a DataFrame makes pandas a hard dependency of **every** range eval, including Online and “I only wanted `np.mean`.”
- **Recalc cost.** Every dirty `=PY` would build a DataFrame. Fine once for a table; noisy for a geometric chain of numeric columns ([geometric recalc](geometric-recalc-order.md)).
- **Polymorphic `data`.** One formula arg → that `CalcRange`; two or more → the same list as `ranges`. `data[i]` already means **binding index** on the multi-arg path and **row** on a single `CalcRange`. A DataFrame would add a third reading (column label). Geometric predecessor strip ([§4](geometric-recalc-order.md#4-data-binding--do-not-shadow-data)) assumes the injected value is still a grid list, not a pandas object.

### What to do instead

| Need | Do this |
| --- | --- |
| Table / `df['Sales']` | `df = data.to_pandas()` (or `header_row=None` / `date_cols=…`) |
| Numeric column / numpy | Use `data` (or `data.to_numpy()`) |
| Excel `xl(…, headers=True)` import | Rewriter already emits `.to_pandas()` |
| LibrePythonista `lp("A1:B10")` import (researched, not shipped) | Emit `data.to_pandas(header_row=None)`; `headers=True` → `data.to_pandas()`. Do **not** change global ingress to make that import shorter. |
| Authoring UX | Snippet / Monaco “insert as DataFrame” that writes `df = data.to_pandas()` **and** the range arg — not a second injected type |

A Settings switch “inject DataFrames” would be two dialects, two meanings of `data[i]`, and a split tutorial. File converters (Excel, future LP) rewrite **call sites**; they do not move the ingress type.

---

## Multi-range support (varargs) {#multi-range-support-varargs}

**Status:** Shipped. `ranges` is always a `list[CalcRange]`. `data` is **polymorphic**: one formula arg → that `CalcRange`; two or more → the same list object as `ranges` (`data is ranges`). Wire envelope: [Multi-range wire format](../scripting/numpy-serialization.md#multi-range-wire-format). Chat-tool multi `data_range` remains future work.

`=PY()` accepts **one or more** optional data arguments after `code`. Calc packs trailing arguments into a single `sequence<any>` (UNO varargs).

**IDL (shipped):**

```idl
// extension/idl/XPythonFunction.idl
interface XPythonFunction : com::sun::star::uno::XInterface
{
    any python( [in] string code, [in] sequence< any > data );
};
```

Rebuild after IDL changes: `scripts/rebuild_xprompt_rdb.sh` → [`extension/XPythonFunction.rdb`](../../extension/XPythonFunction.rdb).

| Formula | `data` | `ranges` |
| --- | --- | --- |
| `=PY("…"; A1:A5)` | `CalcRange` for `A1:A5` | `[data]` |
| `=PY("…"; A1:A5; C1:C5)` | same list as `ranges` | `[range0, range1]` |

**Example — weighted average across regions** (multi-arg: index with `data[i]` or loop `ranges`):

```text
=PY("result = (np.mean(data[0]) + np.mean(data[1])*2 + np.mean(data[2])) / 4"; A1:A10; C1:C10; E1:E10)
```

```python
result = float(np.mean([np.mean(r) for r in ranges]))
```

Under multi-arg, prefer `data[i]` / `ranges[i]` for a single binding — do **not** use bare `data.to_pandas()` (that is for the one-arg `CalcRange` case). On a single `CalcRange`, `data[i]` means **row** `i`, not another formula argument.

---

## Empty cells vs NaN {#empty-cells-vs-nan}

### Locked decision (shipped)

- **No wire-format change** for blank vs NaN provenance. Empty Calc cells and Python/NumPy NaN both use NaN slots in the `split_grid` float64 buffer (or `None` in small/mixed list results).
- **Egress:** every computed `nan` becomes a real Calc error that **cascades** (`#NUM!` / `#VALUE!`). Python `None` maps to an empty cell (`""`).
- **Accepted tradeoff:** a Calc blank that flows through a pure-numeric path becomes `np.nan` in the worker; if you return that NaN, the sheet shows an error (not a silent blank). Matches the spreadsheet model where a missing numeric value taints dependents.
- Production transport: length-prefixed **Pickle5** + `split_grid` (or nested lists below threshold). No JSON on the runtime wire.

Microsoft Python in Excel also collapses empty → `NaN` on ingress and renders computed `np.nan` as `#NUM!` ([microsoft/python-in-excel#38](https://github.com/microsoft/python-in-excel/issues/38)). We match that with an egress-only fix (`to_calc_compatible` no longer collapses NaN → `""`).

### Ingress (Calc → Python)

| Grid type in the venv | Empty Calc cell becomes | Notes |
| --- | --- | --- |
| **Mixed** (any text in range) | `None` in `list` / `list[list]` (inside `CalcRange.values`) | Same as small-list path. Real `float('nan')` in a mixed grid is also `None` — the wire has no blank-vs-NaN bit. |
| **Pure numeric** (≥100 cells, split_grid) | `np.nan` when using `data.to_numpy()` / `__array__` | Use `np.nansum`, `np.nanmean`, or `np.isnan`. Real NaN and blanks are both `np.nan` here. |
| **Small range** (<100 cells, nested list) | `None` in `.values` | May promote to ndarray only if reloaded as clean numeric |

Ingress blanks can poison naive `np.sum` / `np.mean` — prefer `nan*` helpers when blanks should be ignored.

### Egress (Python → Calc)

- Python `None` → `""` (empty cell).
- `float('nan')` / `np.nan` → raw NaN → cascading error cell.
- `±inf` passes through (may also error in formulas). **Not a missing-value sentinel.**
- `decimal.Decimal` and `fractions.Fraction` → `float` (precision loss is accepted; Calc only has doubles). Column kind must stay `"float"`, not `"int"` (truncation is not accepted). A text cell earlier in the same grid does not turn a later Decimal or Fraction into text.
- **Int fidelity:** the `split_grid` buffer is float64. Integers outside ±2^53 round on pack and unpack. Account numbers and 64-bit IDs should travel as strings. There is no int64 wire lane (Calc cells are doubles anyway).
- For a visible non-error marker, return a string:

```python
val = np.mean(data)
result = "NaN" if (isinstance(val, float) and math.isnan(val)) else val
```

```python
# Ingress
result = np.nansum(data)          # ignores blanks/NaNs
result = np.sum(data)             # poisons on blanks/NaN (returns NaN)

# Egress
result = None                     # empty cell
result = float("nan")             # #NUM! / #VALUE! (cascades)
result = [[1.0, np.nan, 3.0]]     # 1, error, 3
```

**We do not round-trip "real NaN" as a special visible sentinel.** `±inf` is never coerced to empty.

### Author / LLM summary

- Blanks on ingress are `np.nan` in numeric arrays — use `np.nansum` / `np.nanmean` when you mean "ignore missing."
- A computed `nan` is a sheet error and poisons dependents. Return a string for a quiet marker.
- `None` is the way to produce a true empty cell on egress.
- Shared helper: `is_missing_value` in [`plugin/scripting/venv/coerce.py`](../../plugin/scripting/venv/coerce.py) (None, `""`, LO error tokens, float/NumPy NaN) — used by dataframe coercion and Excel-parity formula helpers.

Codec details: [numpy-serialization — Split-Grid encoding](../scripting/numpy-serialization.md#strategy-3-split-grid-serialization-detail).

---

## Cell types and logicals {#cell-types-and-logicals}

What Python sees after UNO unwrap / pack ([`calc_addin_data.py`](../../plugin/calc/calc_addin_data.py)):

| Calc / UNO | In `CalcRange.values` (before NumPy conversion) |
| --- | --- |
| Empty cell / `""` | `None` |
| Number | `int` or `float` |
| Logical constant (`TRUE`/`FALSE` in sheet) | Usually **`1.0`/`0.0`** from the add-in bridge (VALUE cells) |
| UNO boolean (rare on range args) | `bool` |
| Text | `str` (including literal `"True"` until string-logical coercion) |

**Logical string coercion (shipped):** text that looks like a logical or formula after import/paste (`"TRUE"`, `"=WAHR()"`, `"True"`, plus localized names from `XFormulaOpCodeMapper`) is coerced to Python `bool` in `_unwrap_cell` before packing. Typed Calc logicals that arrive as `1.0`/`0.0` are left numeric.

| What the user sees | Typical Python value |
| --- | --- |
| Logical typed in Calc | `1.0` / `0.0` |
| Formula / plain / Python-style text logicals | `True` / `False` (after coercion) |

**Egress:** Python `True` / `False` map to `1.0` / `0.0` (UNO double) in `to_calc_compatible` and `_coerce_spill_value`. Calc's Add-In bridge unpacks doubles and strings; returning doubles preserves truthiness across Calc formulas (e.g. `IF(...)`, filters, matrix operations).


---

## Dates and datetimes {#dates-and-datetimes}

Calc stores dates as float serials (days since `1899-12-30`). Detecting “is this a date?” requires per-cell `NumberFormat` on the main thread — too slow for range reads — so the bridge **does not** auto-coerce on ingress.

There is **no Settings checkbox** and no worker-side string guesser (`dateparser` / `pd.to_timedelta` on every cell). A former `scripting.python_convert_datetime` option did that; it is removed. Typed dates in `=PY()` / `run_venv_python_script` are opt-in via `to_pandas` below.

LLM/MCP sheet reads are a **different, always-on** path: `read_cell_range` enriches formatted serials to ISO / `PT…` — [date-time-handling.md](date-time-handling.md). That does not mutate `data` in the Python worker.

- **Ingress:** serials arrive as floats (or strings if stored as text).
- **Convenience coercion in `to_pandas()`:**
  - `data.to_pandas(date_cols=True)` — automatically detects date-like columns (by name or serial value range) and parses them to `datetime64[ns]`.
  - `data.to_pandas(date_cols=["OrderDate", 1])` — parses specific columns by name or index.
  - `data.to_pandas(date_cols=True, date_origin="1904-01-01")` — for workbooks using a custom `NullDate`.
  - Manual coercion: `pd.to_datetime(df["date_col"], unit="D", origin="1899-12-30")` or `pd.to_datetime(df["date_col"])` for text.
- **Text stays text** by default (`"00123"` remains a string). Opt in with `to_pandas(parse_strings=True)`.

### Egress (locked — do not add a datetime wire lane)

`=PY()` never puts first-class datetime objects on the UNO bridge. Conversion happens at the edges:

| Python value | Cell / spill |
| --- | --- |
| `datetime` / `date` / `time` | Naive ISO-8601 string (`YYYY-MM-DD` / `THH:MM:SS`) for formula returns; converted to serial float + `NumberFormat` during deferred spill |
| tz-aware `datetime` / `Timestamp` | **Drop tzinfo**, then naive ISO / serial. Calc does not parse `+HH:MM` or `Z` as dates ([date/time handling](date-time-handling.md)). |
| `pd.Timestamp` | Same as `datetime` (`Timestamp` subclasses it) |
| `np.datetime64` / datetime columns | Converted in the **venv** to stdlib `datetime`, then ISO / serial — **not** Unix-epoch floats from `astype(float64)` |
| `timedelta` / `pd.Timedelta` | Fractional days (`1.5` = 36 hours) with `[HH]:MM:SS` duration formatting during spill |
| `pd.NaT` / `pd.NA` | Empty cell (`""`), same as `None` |

**Deferred spill formatting:** When `=PY()` returns a DataFrame or grid with temporal values that spills via `perform_deferred_spill`, the spilled cells are written as numeric day-serial floats and formatted with date/time `NumberFormat` keys across coalesced rectangular blocks, enabling native Calc sorting, filtering, and date math.

**Do not implement** a `split_grid` datetime mask, Calc-serial conversion on the float64 buffer, or Unix-epoch day counts as the `=PY()` date representation. The numeric fast path stays `i`/`u`/`f`/`b` only. Rationale: [Dates on the wire](../scripting/numpy-serialization.md#dates-on-the-wire).

Wire note (large mixed grids): above the `split_grid` threshold, stdlib `datetime` values become ISO strings in the sparse `strings` map.

---

## Pandas egress (DataFrame / Series) {#pandas-egress}

Returning a DataFrame (or named Series) uses the existing `dataframe` envelope (column labels + rectangular body). This is **shipped**. Do not add payload kinds for MultiIndex, Categorical, or empty frames.

| Return | What spills |
| --- | --- |
| DataFrame with rows | Header row + body (`dataframe_to_labeled_grid`) |
| **0-row** DataFrame | **Header only** (column names, no body) |
| Named empty Series | Header-only one-column grid |
| Unnamed empty Series | Empty (`""`) |
| MultiIndex **columns** | Flattened labels `A / x` (not `"('A', 'x')"`) |
| MultiIndex **rows** / index | **Dropped** (`itertuples(index=False)`). Call `reset_index()` in the script if the index should appear as a column. |
| Categorical columns | Category **labels** (strings or codes already in the Series), not a special dtype on the wire |

Hierarchical Calc tables, object cards, and “include index by default” are **not** wire-codec work — see [Calc UX backlog](../enabling_numpy_in_libreoffice.md#calc-ux-backlog).

---

## Rectangular shape rules {#rectangular-shape-rules}

- **2D data must be rectangular:** every row the same length. Calc range args always arrive that way; empty cells are `None` in a full-width row, not missing list elements.
- **Jagged nested lists** (tool/LLM payloads) are **unsupported** at pack time: [`_flatten_grid_to_components`](../../plugin/scripting/payload_codec.py) raises `ValueError`. We do not pad short rows on the wire path.
- Orientation is preserved via `ensure_rectangular_2d`: a single row stays `[[a, b, c]]`; a single column stays `[[a], [b], [c]]`; a scalar becomes `[[v]]`. User scripts see this as `CalcRange.shape`, not a flat 1D list.
- A `str` or `bytes` row is one cell. `[['a', 'b'], 'cd']` becomes `[['a', 'b'], ['cd', None]]`, not a row of letters. A flat list of strings (`['ab', 'cd']`) is still one row of two cells.
- `to_pandas()` column labels are unique. A generated `name_1` suffix skips a later header that is already `name_1` (`['a', 'a', 'a_1']` → `a`, `a_1`, `a_1_1`).

---

## Deferred upgrades {#deferred-upgrades}

Not planned unless a real product need appears. **Do not treat the list below as open codec work**, and do not propose new `split_grid` payload kinds for inf / NaN / Decimal / datetime64 / empty DataFrames / MultiIndex — those are covered above.

- Do **not** switch ingress `data` to pandas DataFrame by default — [Why CalcRange](#why-calcrange-not-dataframe-by-default).
- Blank side-channel on `split_grid` + masked-array ingress so pass-through blanks stay empty and `np.mean` auto-ignores Calc blanks (upgrade can be atomic; wire already carries NaN slots).
- Formula parameters: 3rd arg `extras` for recalc deps; `collapse` on conversion; host `lp()` bridge; per-formula `timeout_sec`.
- Range alignment helper for mismatched multi-range shapes before `np.corrcoef` / element-wise math — see [Calc UX backlog](../enabling_numpy_in_libreoffice.md#calc-ux-backlog).
- `_MATRIX_SCALAR_SESSIONS` keys use `repr(worker_data)` (`WorkerResultSession` in [`function.py`](../../plugin/calc/python/function.py)). A packed-payload digest + cell count would avoid building a full `repr` of large grids; do not use `id()`. Not codec work.

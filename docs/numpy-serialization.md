# NumPy Serialization

Back to the [core NumPy and Python guide](enabling_numpy_in_libreoffice.md).

This page collects the serialization details for WriterAgent's out-of-process NumPy bridge: the unified `split_grid` standard, benchmark results, pipeline costs, optimization tiers, future profiling work, and native host-extension notes. The core guide stays focused on setup, execution, safety, and Calc usage.

## Serialization optimization opportunities

The compute bridge is **asymmetric by design**: LibreOffice’s embedded Python (host) must stay ABI-safe and ships **without NumPy**; the user venv (child) may use NumPy, pandas, and other C extensions. Serialization is therefore the main lever for large-range performance — not importing NumPy into LibreOffice ([docs/vector-search-design.md](vector-search-design.md) §3, “NumPy tax”). The host can still ship **small vendored binaries** (a few MB, like [audio](audio-architecture.md) or future `sqlite-vec`) to pack/unpack payloads faster than pure stdlib, while the child keeps the heavy numeric stack in the user venv.

### What we implemented (Pickle5 + Split-Grid)

For numeric and mixed-type grids, the compute bridge implements high-performance serialization directly over standard length-prefixed bytes carrying pickled dictionaries with zero-copy binary arrays. We unified all out-of-process binary serialization under a single production standard: **Pickle5 + Split-Grid**.

| Piece | Purely Numeric Split-Grid | Mixed-Type Split-Grid |
|-------|----------------------------|-----------------------|
| **Wire Payload** | `{ "__wa_payload__": "split_grid", "dtype": "float64", "column_kinds": ["int", ...], "shape": [r, c], "buffer": b"...", "strings": {} }` (Pickled Protocol 5) | `{ "__wa_payload__": "split_grid", "dtype": "float64", "column_kinds": ["int", ...], "shape": [r, c], "buffer": b"...", "strings": {"idx": "val"} }` (Pickled Protocol 5) |
| **Host Packing** | Flattens grid cells to float64; empty cells become `math.nan`. Identifies per-column `column_kinds` (`int`/`float`). Converts directly to standard `array.array` and packs its raw `.tobytes()` binary buffer. Sparse `strings` dict is empty `{}`. | Flattens grid; numbers become float64, empty cells/strings become `math.nan` in binary array. Strings are registered in parallel in a sparse `strings` index map. Identifies `column_kinds`. |
| **Child Unpacking** | **Optimized C-Speed Path**: Sees that the sparse `strings` dictionary is empty and materializes a NumPy `ndarray` directly using `np.frombuffer` in one step. Restores `int64` types if `column_kinds` are all-int. Bypasses all Python list/loop transpositions and Base64 decoding! | Decodes the raw binary buffer directly via `np.frombuffer`. Converts to Python list via C-level `.tolist()`, replaces `nan` with `None`, restores `int` types using `column_kinds`, and overlays sparse strings from the index map. |
| **Compatibility** | Namespace receives a NumPy ndarray (ideal for math operations). | Namespace receives a standard nested list of lists (fully backward compatible for all sheets and scripts). |
| **Threshold** | `BINARY_MIN_CELLS = 10` — 2D grids with ≥ 10 cells use `split_grid`. | `BINARY_MIN_CELLS = 10` — 2D grids with ≥ 10 cells use `split_grid`. |
| **Fallback** | Grids < 10 cells fall back to standard Pickle lists. | Grids < 10 cells or non-2D arrays fall back to standard Pickle lists. |
| **Debug log tag** | `payload_codec host_pack split_grid` | `payload_codec host_pack split_grid` |

---

### Strategy 3: Split-Grid Serialization (Detail)

Split-Grid represents a highly-optimized, asymmetric serialization strategy designed for spreadsheet columns or tables containing mixed data types (such as standard Calc ranges with headers, labels, or text mixed with numeric metrics).

#### Why Column-Wise was replaced:
Historically, a column-wise transposition approach (Strategy 1) was analyzed. It divided grids into individual columns, packing numeric columns as `f64_blob` and text columns as standard JSON lists. However:
1. Column transposing in pure Python on the host creates massive object structures and column-slice overhead.
2. Ingesting multiple chunks requires multiple base64 decodes and nested list pointer reconstructions, leading to serialization bottlenecks on mixed sheets.

#### The Split-Grid Solution:
Instead of dividing columns, Split-Grid serializes the **entire grid as a single flat binary float64 array** plus a **parallel sparse strings dictionary** and **per-column type metadata**, packed as raw binary data in a length-prefixed Pickle5 stream.

```mermaid
flowchart TD
  subgraph host [LibreOffice Host stdlib]
    Input[2D Mixed Grid] --> Flatten[Flatten grid row-major]
    Flatten -->|Numbers / None| Array[array.array 'd']
    Flatten -->|Strings| Dict[Sparse Dict strings]
    Flatten -->|Types| Kinds[Identify column_kinds int/float]
    Array --> tobytes[tobytes binary buffer]
    Kinds --> Envelope[Envelope Dict]
    tobytes --> Envelope
    Dict --> Envelope
    Envelope --> Pickle[pickle.dumps protocol 5]
  end
  subgraph child [Child Process venv]
    Pickle --> Unpickle[pickle.loads]
    Unpickle --> Envelope2[Envelope Dict]
    Envelope2 -->|"buffer" (bytes)| Buff[np.frombuffer float64]
    Buff --> List[tolist C-level]
    List --> Patch[Patch math.nan to None]
    Kinds --> Patch2[Restore int types using column_kinds]
    Dict --> Patch3[Overlay strings using sparse indexes]
    Patch --> Slice[Slice row-major to 2D lists]
    Patch2 --> Slice
    Patch3 --> Slice
    Slice --> Output[Standard nested list of lists]
  end
```

1. **Host Packing**:
   - Flat double-precision binary `array` preserves all numeric values (`int`, `float`, `bool`).
   - String values or empty/None cells are encoded as `math.nan` in the binary array.
   - Any string cell is registered in the parallel `strings` dictionary keyed by its flat cell index (e.g. `{"7": "banana", "12": "apple"}`).
   - Per-column `column_kinds` (`int` or `float`) are identified to allow precise type restoration in the child.
   - Avoids expensive type-coercion testing by mapping strings immediately, ensuring 100% preservation of zip codes (`"02138"` remains a string) and preventing float conversion bugs.
   - The entire dictionary payload is serialized using `pickle.dumps(request, protocol=5)`, completely avoiding Base64 encoding.
2. **Child Unpacking**:
   - Decodes the length-prefixed stream and deserializes the request with `pickle.loads(payload)`.
   - Maps the binary buffer (`"buffer"` key) directly into memory using `np.frombuffer`.
   - For mixed grids containing strings, utilizes NumPy's fast C-level `.tolist()` to generate a flat Python list in a single pass.
   - Reconstructs the grid by running a highly optimized single-pass loop in Python, replacing remaining `nan` values with `None`, restoring integer types using `column_kinds`, and overlaying string values from the sparse dict.
   - Slices the flat list back into a row-major 2D nested list.
   - For purely numeric grids, completely bypasses Python list reconstructions and returns `ndarray` directly.

#### Performance Impact:
- **~20x Speedup** over Column-Wise mixed grids.
- Binary materialization is done at C-speed via `frombuffer` + `.tolist()`, and pure Python loops only process the small fraction of cells that actually contain string text.

**Benchmarked** outside LO ([`scripts/bench_serialization.py`](../scripts/bench_serialization.py)) — [results](#benchmark-results-2026-05). Defer Tier 2b (vendored msgpack), Tier 3 (mmap), Tier 5 (payload cache) unless real Calc profiles disagree. Tier 0 (scalars, two-phase tools, matrix `ROW()` index) stays complementary.

### Benchmark results (2026-05)

Asymmetric simulation: **host** = stdlib pack/serialize; **child** = deserialize + materialize. Timings are median values; the automatic `split_grid` envelope is triggered when **at least 10 cells** (smaller grids fall back to standard JSON lists). We compare three main strategies:
1. `json_list` (standard nested lists over JSON wire, materializing using `np.array(list)`).
2. `split_grid` (JSON-based: flattened float64 bytes encoded in Base64 over JSON wire, materializing using `np.frombuffer`).
3. `pickle5` (Split-Grid inside Pickle: zero-Base64 binary Split-Grid packing raw binary float64 buffers directly in the dictionary envelope, using Python pickle protocol 5 over a length-prefixed stream, materializing at C-speed via `np.frombuffer`).

Additionally, for child-side materialization comparison, we also preserve historical results for **`pure_pickle`** (standard Python pickle of nested lists, which still requires expensive `np.array(list)` conversions in the child) to demonstrate why pure pickle is not enough compared to Split-Grid inside Pickle.

#### 1. End-to-End Serialization Timings (Ingress & Egress)

timings in milliseconds (including packing, serialization, IPC transit, deserialization, and peer materialization):

| Direction | Kind | Shape | Cells | Format | Total (ms) | Wire Size | vs. JSON Size | Speedup vs. JSON | E2E Winner |
|-----------|------|-------|-------|--------|------------|-----------|---------------|------------------|------------|
| **Ingress** | grid | 3×3 | 9 | `json_list` | 0.027 ms | 209 B | baseline | - | |
| **Ingress** | grid | 3×3 | 9 | `split_grid` | 0.022 ms | 258 B | 123% | 1.21x | |
| **Ingress** | grid | 3×3 | 9 | `pickle5` | 0.015 ms | 129 B | **62%** | **1.83x** | **★ pickle5** |
| **Egress** | grid | 3×3 | 9 | `json_list` | 0.018 ms | 212 B | baseline | - | |
| **Egress** | grid | 3×3 | 9 | `split_grid` | 0.019 ms | 260 B | 123% | 0.90x | |
| **Egress** | grid | 3×3 | 9 | `pickle5` | 0.006 ms | 131 B | **62%** | **3.16x** | **★ pickle5** |
| **Ingress** | grid | 10×10 | 100 | `json_list` | 0.139 ms | 2.02 KiB | baseline | - | |
| **Ingress** | grid | 10×10 | 100 | `split_grid` | 0.070 ms | 1.26 KiB | 63% | 2.00x | |
| **Ingress** | grid | 10×10 | 100 | `pickle5` | 0.042 ms | 0.95 KiB | **47%** | **3.34x** | **★ pickle5** |
| **Egress** | grid | 10×10 | 100 | `json_list` | 0.103 ms | 2.02 KiB | baseline | - | |
| **Egress** | grid | 10×10 | 100 | `split_grid` | 0.033 ms | 1.27 KiB | 63% | 3.15x | |
| **Egress** | grid | 10×10 | 100 | `pickle5` | 0.012 ms | 0.96 KiB | **47%** | **8.79x** | **★ pickle5** |
| **Ingress** | grid | 100×100 | 10 000 | `json_list` | 11.898 ms | 198.15 KiB| baseline | - | |
| **Ingress** | grid | 100×100 | 10 000 | `split_grid` | 4.770 ms | 105.18 KiB| 53% | 2.49x | |
| **Ingress** | grid | 100×100 | 10 000 | `pickle5` (Split-Grid in Pickle) | 3.980 ms | **78.48 KiB**| **40%** | **2.99x** | **★ pickle5** |
| **Egress** | grid | 100×100 | 10 000 | `json_list` | 10.066 ms | 198.19 KiB| baseline | - | |
| **Egress** | grid | 100×100 | 10 000 | `split_grid` | 1.426 ms | 105.18 KiB| 53% | 7.06x | |
| **Egress** | grid | 100×100 | 10 000 | `pickle5` (Split-Grid in Pickle) | 0.503 ms | **78.48 KiB**| **40%** | **20.01x** | **★ pickle5** |
| **Ingress** | grid | 1×1000 | 1000 | `json_list` | 1.176 ms | 19.82 KiB | baseline | - | |
| **Ingress** | grid | 1×1000 | 1000 | `split_grid` | 0.513 ms | 10.56 KiB | 53% | 2.29x | |
| **Ingress** | grid | 1×1000 | 1000 | `pickle5` | 0.267 ms | 8.82 KiB | **45%** | **4.40x** | **★ pickle5** |
| **Egress** | grid | 1×1000 | 1000 | `json_list` | 0.982 ms | 19.80 KiB | baseline | - | |
| **Egress** | grid | 1×1000 | 1000 | `split_grid` | 0.335 ms | 19.34 KiB | 98% | 2.93x | |
| **Egress** | grid | 1×1000 | 1000 | `pickle5` | 0.082 ms | 8.83 KiB | **45%** | **12.01x** | **★ pickle5** |
| **Ingress** | grid | 1000×1 | 1000 | `json_list` | 1.841 ms | 21.80 KiB | baseline | - | |
| **Ingress** | grid | 1000×1 | 1000 | `split_grid` | 0.773 ms | 10.56 KiB | **48%** | **2.38x** | **★ split_grid**|
| **Ingress** | grid | 1000×1 | 1000 | `pickle5` | 0.945 ms | 11.75 KiB | 54% | 1.95x | |
| **Egress** | grid | 1000×1 | 1000 | `json_list` | 1.002 ms | 19.83 KiB | baseline | - | |
| **Egress** | grid | 1000×1 | 1000 | `split_grid` | 0.149 ms | 10.56 KiB | 53% | 6.72x | |
| **Egress** | grid | 1000×1 | 1000 | `pickle5` | 0.079 ms | 8.83 KiB | **45%** | **12.73x** | **★ pickle5** |

#### 2. Child-Only Peer Materialization (np.array vs frombuffer)

timings in milliseconds (measuring CPU time required to deserialize and instantiate the array in the child):

| Shape | Cells | `json_list` (np.array) | `split_grid` (frombuffer) | `pure_pickle` (np.array) [Historical] | `pickle_split_grid` (frombuffer) [New] | `split_grid` Speedup | `pure_pickle` Speedup | `pickle_split_grid` Speedup |
|-------|-------|------------------------|---------------------------|---------------------------------------|---------------------------------------|----------------------|-----------------------|-----------------------------|
| **Scalar** | 1 | 0.0020 ms | 0.0053 ms | 0.0011 ms | 0.0010 ms | 0.38x | 1.84x | 2.00x |
| **3×3** | 9 | 0.0052 ms | 0.0062 ms | 0.0024 ms | 0.0015 ms | 0.83x | 2.13x | 3.47x |
| **4×4** | 16 | 0.0072 ms | 0.0063 ms | 0.0030 ms | 0.0018 ms | 1.16x | 2.44x | 4.00x |
| **10×10** | 100 | 0.0309 ms | 0.0090 ms | 0.0081 ms | 0.0031 ms | 3.42x | 3.80x | 9.97x |
| **100×100** | 10 000 | 2.837 ms | 0.234 ms | 0.608 ms | **0.017 ms** | **12.13x** | **4.85x** | **168.50x** |
| **1×1000** | 1000 | 0.2816 ms | 0.0269 ms | 0.0622 ms | **0.0038 ms** | **10.46x** | **4.53x** | **74.11x** |
| **1000×1** | 1000 | 0.4504 ms | 0.0268 ms | 0.2489 ms | **0.0039 ms** | **16.83x** | **1.81x** | **115.49x** |

#### 3. Warm Process Executions (with dynamic format switching)

Side-by-side warm worker execution times comparing JSON and Pickle IPC dynamic loops (10 iterations):

* **Task 1: 1000th Prime** (via `sympy`):
  * **In-Process**: Avg: `0.001042s` | Min: `0.000962s`
  * **JSON Mode**: Cold: `0.792939s` | Warm Avg: `0.042475s` | Warm Min: `0.036271s`
  * **Pickle Mode**: Cold: `0.766803s` | Warm Avg: `0.047892s` | Warm Min: `0.038229s`

* **Task 2: 1000×1000 Matrix Dot Product** (via `numpy`):
  * **In-Process**: Avg: `0.041175s` | Min: `0.032525s`
  * **JSON Mode**: Cold: `0.777692s` | Warm Avg: `0.079107s` | Warm Min: `0.075480s`
  * **Pickle Mode**: Cold: `0.808579s` | Warm Avg: `0.076709s` | Warm Min: `0.072407s`

#### Key Insights

1. **Why Pure/Standard Pickle is Not Enough**:
   Standard Python pickle on standard list structures (e.g. standard float list-of-lists) is extremely fast to deserialize back to standard Python objects, but it **completely lacks memory layout optimization**. The unpickled result is still a list-of-lists, which forces the child process to perform a heavy, slow, cell-by-cell Python object traversal (`np.array(lists)`) to construct a NumPy array. In our `100x100` benchmarks, this pure/standard pickle unpickling yielded only a **4.85x** materialization speedup (`0.608 ms`).

2. **Split-Grid inside Pickle is the Ultimate Champion**:
   By packing the flat numeric buffer (`array.array('d')` on the host, or `np.ndarray` on the child) *directly inside the Pickle dictionary envelope* as raw binary bytes, we eliminate Base64 encoding/decoding, JSON serialization overhead, AND standard Python list pointer reconstructions!
   - **Wire size reduction**: Payloads shrink by **60%** (a 100x100 grid takes only **78.48 KiB** compared to 198 KiB for JSON lists), bypassing all Base64 size expansion.
   - **Egress speedup**: E2E egress time for `100x100` cells drops from `10.066 ms` to `0.503 ms` (a massive **20.01x E2E speedup**).
   - **Materialization speedup**: Peer materialization is an unbelievable **168.50x faster** (`0.017 ms` vs `2.837 ms` for standard list mapping), mapping the memory directly via zero-copy C-speed `np.frombuffer`.

3. **Production Implementation (May 2026)**:
   This proposal has been fully implemented! The production codebase has been standardized exclusively on Split-Grid inside Pickle (direct raw bytes under the `"buffer"` dictionary key, completely bypassing Base64 and JSON encoding overhead). All JSON/Base64 serialization remnants have been removed from the production path (while the historical JSON/Base64 Split-Grid codec is retained locally in benchmark and test scripts for performance comparisons).

To run benchmarks yourself:
```bash
.venv/bin/python scripts/bench_serialization.py --direction both
.venv/bin/python scripts/bench_serialization.py --child-only
.venv/bin/python scripts/bench_warm_numpy.py
```

### Unified `split_grid` Serialization

Production wiring (2026-05 refactor):

The implementation was simplified in May 2026 to unify the 1D and 2D packing paths into a single robust iteration loop in `host_pack_split_grid`, removing several redundant helper functions while preserving the same high-performance wire format and C-speed materialization in the child process.

| Location | Role |
|----------|------|
| [`plugin/scripting/payload_codec.py`](../plugin/scripting/payload_codec.py) | Single source: unified pack/unpack, threshold, `describe_wire_value` for logs |
| [`plugin/calc/calc_addin_data.py`](../plugin/calc/calc_addin_data.py) | `pack_calc_data_for_wire()` after range read; `count_cells()` understands split_grid envelopes |
| [`plugin/scripting/python_worker_manager.py`](../plugin/scripting/python_worker_manager.py) | `_normalize_response`: `host_unpack_data` on worker `result` (all callers); respects `column_kinds` |
| [`plugin/calc/python_function.py`](../plugin/calc/python_function.py) | `=PYTHON()` ingress pack + matrix/session flattening (result already unpacked) |
| [`plugin/calc/venv_python.py`](../plugin/calc/venv_python.py) | Chat tool ingress pack |
| [`plugin/scripting/venv_sandbox.py`](../plugin/scripting/venv_sandbox.py) | `child_unpack_data` before inject; `child_pack_result` in `serialize_result` |
| [`tests/scripting/test_payload_codec.py`](../tests/scripting/test_payload_codec.py) | Unit tests (threshold, round-trip, mixed text → lists) |
| [`tests/scripting/test_run_venv_code.py`](../tests/scripting/test_run_venv_code.py) | Harness integration with split_grid payloads |

**Policy:** `BINARY_MIN_CELLS = 10` — 2D grids with **≥ 10 cells** use `split_grid`; smaller grids use standard Pickle lists.

**Still deferred:** Tier 2b vendored codecs, mmap (Tier 3), payload cache (Tier 5), venv tool RPC.

### Current pipeline and costs

```text
Calc UNO range
  → calc_addin_data_to_python (host: list / list[list])
  → pack_calc_data_for_wire → host_pack_data (host: split_grid or nested list)
  → pickle.dumps(request)        (host: binary payload, protocol 5; split_grid contains raw bytes)
  → pickle.loads(payload)        (child: parse request dict)
  → child_unpack_data → ndarray  (child: frombuffer + reshape when split_grid and strings is empty)
  → send_variables({"data": ...}) (child: ndarray or list in fresh namespace)
  → user code (NumPy/pandas)
  → serialize_result → child_pack_result (child: split_grid or list for large numeric result)
  → pickle.dumps(response)       (child: binary response, protocol 5)
  → pickle.loads(response)       (host)
  → _normalize_response → host_unpack_data (host: split_grid → nested lists for Calc / LLM / UI)
  → finalize_python_return / write_formula_range
```

| Stage | Module | What happens | Large dense numeric `data` (shipped path) |
|-------|--------|--------------|-------------------------------------------|
| Range read | [`calc_addin_data.py`](../plugin/calc/calc_addin_data.py) | Cell scalars in nested lists; cap 250 000 cells | O(cells) once at read |
| Host pack | [`payload_codec.py`](../plugin/scripting/payload_codec.py) | `array` buffer envelope when ≥10 numeric cells | One pass; wire ~40% size vs JSON list (bench) |
| Host encode | [`python_worker_manager.py`](../plugin/scripting/python_worker_manager.py) | `pickle.dumps` of request dict | Small binary payload; completely avoids Base64 |
| Child unpack | [`venv_sandbox.py`](../plugin/scripting/venv_sandbox.py) | `frombuffer` + `reshape` → ndarray | ~168× faster materialize vs `np.array(list)` at 10⁴ cells (bench) |
| Return | [`serialize_result`](../plugin/scripting/venv_sandbox.py) | `child_pack_result` for ndarray/list; DataFrame still `to_dict(orient="records")` | Large ndarray egress as binary buffer, not `.tolist()` |
| Host decode | [`python_worker_manager.py`](../plugin/scripting/python_worker_manager.py) | `_normalize_response` → `host_unpack_data` on `result` | Nested lists for LLM, smol observations, Calc matrix/session |
| Calc return | [`python_function.py`](../plugin/calc/python_function.py) | `finalize_python_return` / session flattening | Per-cell scalars for legacy add-in bridge |

**Pickle Protocol 5:** Standardized as the exclusive production serialization protocol on the worker path. Opaque msgpack, mmap, and shared memory remain deferred. [`SafeSerializer`](../plugin/contrib/smolagents/serialization.py) (`__type__: ndarray`) is **not** on the worker path — only [`payload_codec.py`](../plugin/scripting/payload_codec.py).

**Fresh namespace every call** ([core strategy](enabling_numpy_in_libreoffice.md#2-strategy-decision)): there is no worker-side variable cache; the same `A1:Z1000` range is re-serialized on every `=PYTHON()` or `run_venv_python_script` invocation unless the product adds an explicit cache ([core roadmap](enabling_numpy_in_libreoffice.md#7-deferred-roadmap)).

### Design constraints

- **Host stays NumPy-free** — do not vendor full NumPy/pandas into LibreOffice ([vector-search-design.md](vector-search-design.md) §3). That is unrelated to shipping **small, purpose-built binaries** (a few MB per platform total) when stdlib is too slow.
- **Host may use small vendored natives** — same precedent as audio ([audio-architecture.md](audio-architecture.md): `sounddevice` / CFFI wheels under `vendor/` / `plugin/vendor/`, injected from [`plugin/main.py`](../plugin/main.py)) and future vector search (`sqlite-vec` `vec0`, ~1 MB per OS in [vector-search-design.md](vector-search-design.md)). A serialization codec wheel or tiny custom `.so` is acceptable if it stays in the **few‑MB** budget and is pruned per OS/Python ABI like audio — not a 50–100 MB science stack.
- **Wire format uses length-prefixed binary streams carrying Pickle5 payloads** — this standard provides extremely fast, out-of-band zero-copy buffer sharing between processes without any Base64 encoding or JSON parsing overhead. Since we package both the extension host and the sandboxed child worker together inside the OXT, backward compatibility is not a constraint, allowing us to evolve the IPC protocol to be as fast as possible.
- **Sandbox must not grant arbitrary filesystem access** — [`LocalPythonExecutor`](../plugin/contrib/smolagents/local_python_executor.py) blocks `os` / `pathlib` in user code; temp files and mmap paths must be **host-allocated, host-trusted paths** passed in the request envelope, not paths chosen by LLM-generated scripts.
- **LLM and Calc still need JSON-safe or scalar outputs** eventually — even an optimized ingress path usually ends with compact `result` (scalar, short list, summary stats) or a second-phase host tool (`write_formula_range`) for sheet output ([core user guide](enabling_numpy_in_libreoffice.md#3-user-guide)).

### Optimization tiers (what to consider)

#### Tier 0 — Keep JSON; reduce crossings (no protocol change)

- Return **scalars or small summaries** from the venv (`result = float(np.mean(arr))`) instead of full arrays when the LLM only needs a number.
- Use the **two-phase workflow**: compute in venv, insert via existing Calc tools with a compact payload — avoid shipping a 10⁵-element list through chat JSON twice.
- For **matrix `=PYTHON()`**, prefer the `ROW()-1` index form so one worker run fills a session cache ([`finalize_python_return`](../plugin/calc/python_function.py)), not N full round-trips with the same `data`.
- Tighten ranges (`collapse`-style) in the sheet or strip `None` in Python before heavy work.

Best when: mixed types (strings, blanks, dates), small ranges, or logic dominates runtime.

#### Tier 1 — Typed JSON envelope (metadata + payload)

Extend request/response objects with a tagged shape, e.g. `{"__wa_payload__": "ndarray", "dtype": "float64", "shape": [1000, 100], "data": ...}` where `data` is still JSON list **or** base64 (Tier 2). On the venv side: `np.array(data, dtype=...).reshape(shape)` or `np.frombuffer(...)`.

- Reuse ideas from [`SafeSerializer`](../plugin/contrib/smolagents/serialization.py) (`__type__: ndarray`) but implement a **small, worker-specific** codec in [`venv_sandbox.py`](../plugin/scripting/venv_sandbox.py) / host mirror — do not pull the full smolagents serializer into the hot path without measuring import and dependency cost.
- Host without NumPy: decode envelope to nested lists only when Calc/LLM need lists; otherwise pass the envelope through opaquely.

Best when: you need dtype/shape preserved but payloads are still moderate; stepping stone before binary wire.

#### Tier 2 — Asymmetric Fast Path via Split-Grid inside Pickle5 (Production Standard)

**Shipped** in [`payload_codec.py`](../plugin/scripting/payload_codec.py).

Host (stdlib only) packs a 2D numeric/mixed range:

```python
# Host sketch (embedded Python): row-major float64, empty/None → NaN, strings registered in strings dict
import array
buf = array.array("d")
strings = {}
# (flatten and fill buf / strings; identify column_kinds)
payload = {
    "__wa_payload__": "split_grid",
    "dtype": "float64",
    "column_kinds": ["int", "float", ...],
    "shape": [nrows, ncols],
    "buffer": buf.tobytes(),  # Packed as raw binary bytes directly!
    "strings": strings,
}
# Sent over a length-prefixed stream using: pickle.dumps(payload, protocol=5)
```

Child (venv):

```python
import numpy as np
# Deserialized instantly via: payload = pickle.loads(payload_bytes)
raw = payload["buffer"]
# If strings dict is empty, deserialize directly to ndarray at microsecond C-speed
if not payload.get("strings"):
    arr = np.frombuffer(raw, dtype=np.float64).reshape(payload["shape"])
    # (restore int64 types using column_kinds)
else:
    # Overlay strings on flat list, restore int types, reconstruct nested lists
    pass
```

- **Pros:** Microsecond binary transport; no Base64 wire bloat or CPU overhead; NumPy only on child; avoids million-element Python float objects on the wire. Fully supports mixed types via sparse strings index.
- **Cons:** Host still walks cells once to pack.

Best when: standard Python objects and serialization dominate numeric processing (practically all spreadsheet ranges).

#### Tier 2b — Vendored host codec (few MB, no NumPy)

If stdlib `json` + `array` + base64 is still too slow on the **LibreOffice host**, vendor a **small** binary-backed library into the OXT (parallel to audio, not parallel to NumPy):

| Candidate | Rough size / role | Host (LO Python) | Child (venv) |
|-----------|-------------------|------------------|--------------|
| **msgpack** or **cbor2** | Small C extension per platform; compact binary for `data` / `result` blobs | `packb` grid metadata + float bytes; one line still `base64(pack(...))` or length-prefixed frame | `unpackb` → `np.frombuffer` (NumPy already in venv) |
| **orjson** | Fast JSON only | Faster `json.dumps`/`loads` if wire stays JSON | Optional; child can keep stdlib `json` |
| **lz4** (bind via **lz4** wheel or stdlib **zlib**) | Compress large blobs before base64 or temp file | Shrink stdin payload when JSON/text dominates | Decompress then `frombuffer` |
| **Custom `vec_pack` (Cython)** | Smallest if scope is fixed: row-major `float64`/`float32` + optional mask for `None` | Fast pack over buffer after Python UNO read — see [Building host natives](#building-host-native-extensions-cython) | N/A (child only decodes bytes) |
| **pyarrow** | Usually **too large** for this tier | Defer unless benchmarks justify multi‑MB per arch | User venv may already have Arrow |

**Packaging pattern (reuse audio):**

- **Native extensions** (msgpack, orjson, custom Cython): prebuilt wheels under **`plugin/contrib/…`** (e.g. [`plugin/contrib/audio/`](../plugin/contrib/audio/)), with `sys.path` injection like [`panel_factory.py`](../plugin/chatbot/panel_factory.py) — **not** the pure-Python [`vendor/`](../vendor/) tree from [`make vendor`](../Makefile).
- **Python version + arch matrix** — prune unused ABI tags in the OXT like [audio-architecture.md](audio-architecture.md) (March 2026 binary pruning).
- **Linux** may still need a system package for some natives (audio’s PortAudio case); always **graceful degrade** to Tier 2 stdlib [`payload_codec.py`](../plugin/scripting/payload_codec.py) on `ImportError`.

**Asymmetric benefit:** host uses vendored **pack** only; child uses **NumPy + optional msgpack** from the user venv without vendoring NumPy into LibreOffice. Worst case: host packs binary, child decodes with `np.frombuffer` — still faster than JSON lists on **both** sides.

**Not a substitute for Tier 3:** mmap/temp files help when payload size exceeds practical stdin; a 1 MB msgpack wheel does not remove the need for mmap at 250 k-cell scale if the line itself is the bottleneck.

Best when: profiles show host `json.dumps` or list construction dominates; you accept OXT size + release matrix cost for a bounded codec (target **≤ few MB** extra per platform set, not NumPy-scale).

For a **custom Cython** host module (not NumPy), see [Building host native extensions (Cython)](#building-host-native-extensions-cython) below.

### Building host native extensions (Cython)

**Status: not shipped** — reference for a future `writeragent_vec` (or similar) module. **Never vendor NumPy** into LibreOffice; the user **venv** remains where full NumPy/pandas live ([core strategy](enabling_numpy_in_libreoffice.md#2-strategy-decision)). A small Cython extension only accelerates **host-side pack** (and optionally other tight loops) inside the embedded interpreter.

#### Policy summary

| Do | Don’t |
|----|--------|
| Ship **tagged `.so` / `.pyd`** per ABI in `plugin/contrib/vec_pack/` (mirror audio) | Import NumPy/pandas/scipy in-process in LO |
| **Fallback** to stdlib `split_grid` on `ImportError` | Link against LibreOffice or call UNO from C |
| Build matrix with **cibuildwheel + CI** | Expect one Arch laptop to produce Windows/macOS wheels |
| Profile in LO before adding OXT weight | Ship pyarrow-scale stacks |

#### How audio does it (model to copy)

Audio does **not** compile C in this repo for release. [`scripts/update_audio_contrib.py`](../scripts/update_audio_contrib.py) **downloads prebuilt wheels** from PyPI and copies artifacts into [`plugin/contrib/audio/`](../plugin/contrib/audio/):

- **Python tags:** 3.11–3.14 (`PYTHON_VERSIONS` in the script; 3.9/3.10 and free-threaded `314t` pruned per [audio-architecture.md](audio-architecture.md))
- **Platforms:** `win_amd64`, `win_arm64`, macOS x86_64/arm64/universal2, `manylinux2014` / `musllinux_1_1` x86_64 and aarch64 (`PLATFORMS` in the script)
- **Tagged binaries** sit **flat** in one directory, e.g. `_cffi_backend.cpython-312-x86_64-linux-gnu.so` (~**28** files for cffi today)
- **Runtime:** [`panel_factory.py`](../plugin/chatbot/panel_factory.py) prepends `plugin/contrib/audio` to `sys.path`; Python imports the module whose tag matches **LO’s** `sys.version` and platform

[`vendor/`](../vendor/) + [`requirements-vendor.txt`](../requirements-vendor.txt) (`make vendor`) is for **pure-Python** deps (snowballstemmer, etc.) — a different path from contrib natives.

#### Custom Cython: you become the wheel publisher

For **your** module (e.g. `writeragent_vec`), PyPI will not have wheels unless **you** build them. Two equivalent maintainer workflows:

| Approach | Workflow | OXT contents |
|----------|----------|--------------|
| **A — cibuildwheel (recommended)** | CI builds wheels on tag; extract into contrib | Flat tagged `.so` / `.pyd` in `plugin/contrib/vec_pack/` |
| **B — pip download your wheels** | Publish to GitHub Releases / PyPI; [`update_vec_contrib.py`](../scripts/update_audio_contrib.py) mirrors audio’s download script | Same |

You do **not** need every developer machine to compile the full matrix.

#### Supported ABI matrix (match audio)

| Dimension | Values ([`update_audio_contrib.py`](../scripts/update_audio_contrib.py)) |
|-----------|-------------------------------------------------------------------------------|
| CPython | 3.11, 3.12, 3.13, 3.14 |
| Windows | `win_amd64`, `win_arm64` |
| macOS | `macosx_10_9_x86_64`, `macosx_11_0_arm64`, `macosx_10_9_universal2` |
| Linux glibc | `manylinux2014_x86_64`, `manylinux2014_aarch64` |
| Linux musl | `musllinux_1_1_x86_64`, `musllinux_1_1_aarch64` |

A Cython package with the same policy ships on the order of **~28** native artifacts (one per ABI), not one universal binary.

**What you cannot do on a single machine:** produce the full matrix natively. Typical split:

1. **Local smoke test** — one ABI matching **LibreOffice’s** embedded `python` on your box.
2. **Linux manylinux + musl** — `cibuildwheel --platform linux` (uses **Docker** on Arch/Linux).
3. **Windows + macOS** — GitHub Actions (`windows-latest`, `macos-*`) or other CI; not practical to cross-build all of those on Arch alone.

**Rough maintainer effort (first time):** package + `pyproject.toml` / `cibuildwheel.toml` (~half day); CI workflow + `update_vec_contrib.py` (~1 day); first green matrix + strip/prune (~1 day); rebuild when bumping supported Python range.

#### Recommended project layout (future)

```text
native/writeragent_vec/
  pyproject.toml              # setuptools + Cython
  src/writeragent_vec/
    __init__.py
    pack.pyx                  # coerce + row-major float64 pack
  tests/                      # pytest vs host_pack_split_grid
scripts/update_vec_contrib.py # extract wheels → plugin/contrib/vec_pack/
plugin/contrib/vec_pack/        # git-tracked .so/.pyd like audio
```

Wire into [`calc_addin_data.py`](../plugin/calc/calc_addin_data.py) or [`payload_codec.py`](../plugin/scripting/payload_codec.py) with `try: import writeragent_vec` and stdlib fallback.

#### Build pipeline (cibuildwheel)

1. Pin ABI to the table above (`pyproject.toml` + `[tool.cibuildwheel]` or GitHub Actions matrix).
2. On release tag: `pip install cibuildwheel && cibuildwheel native/writeragent_vec` → `wheelhouse/`.
3. `update_vec_contrib.py`: unzip wheels; copy `writeragent_vec/*.so` (+ `__init__.py` once) into `plugin/contrib/vec_pack/`.
4. **Strip** with `llvm-strip` (reuse `strip_binary` from [`update_audio_contrib.py`](../scripts/update_audio_contrib.py)).
5. Optional: prune musl tags if you only target glibc LO builds; optional `NO_VEC_PACK=1` in [`build_oxt.py`](../scripts/build_oxt.py) (parallel to `NO_RECORDING`).

#### On Arch Linux (local dev and Linux wheels)

**Packages:**

```bash
sudo pacman -S base-devel gcc llvm docker   # docker or podman for cibuildwheel linux
uv sync --group dev
uv pip install cython build cibuildwheel
```

**Discover LibreOffice’s embedded Python** (build and test against this ABI, not only `/usr/bin/python`):

```bash
/usr/lib/libreoffice/program/python -c "import sys; print(sys.version); print(sys.implementation.cache_tag)"
```

**Dev install for local smoke tests** (once `native/writeragent_vec` exists):

```bash
cd native/writeragent_vec
/usr/lib/libreoffice/program/python -m pip install --user cython build   # if pip available on LO python
/usr/lib/libreoffice/program/python -m pip install -e .
```

**Linux wheel matrix from Arch** (glibc manylinux + musl; requires Docker):

```bash
cd native/writeragent_vec
uv run cibuildwheel --platform linux
```

Distro LibreOffice on glibc Arch typically needs **`manylinux2014_*`** tags. **Musl** wheels matter only for musl-linked LO builds (some minimal/Flatpak-style layouts).

**Not from Arch alone:** `win_*` and `macosx_*` wheels — add `.github/workflows/build-vec-wheels.yml` (or similar) on tag push.

#### Runtime integration (LO host)

```python
_vec_dir = os.path.join(ext_root, "plugin", "contrib", "vec_pack")
if _vec_dir not in sys.path:
    sys.path.insert(0, _vec_dir)
try:
    import writeragent_vec as _wv
except ImportError:
    _wv = None  # unknown LO Python minor or arch → stdlib payload_codec
```

Log `sys.version`, `sys.platform`, and import success at debug level once per session when diagnosing missing tags.

#### UNO boundary (important)

A Cython extension **must not** link against LibreOffice or call UNO from C. PyUNO stays in Python.

```mermaid
flowchart TB
  subgraph py_host [Python_in_LO]
    UNO[getDataArray_or_cell_loop]
    Bridge[pass_buffer_to_native]
  end
  subgraph native [writeragent_vec.so]
    Pack[pack_split_grid]
  end
  UNO -->|"memoryview_array_or_flat_list"| Bridge --> Pack
```

- **Python:** one UNO read ([`calc_addin_data_to_python`](../plugin/calc/calc_addin_data.py) or `getDataArray`) — still required.
- **Cython:** fast **coerce + pack** (row-major `float64`, `None`/empty → NaN) over a contiguous buffer — replaces the hot loop in [`host_pack_split_grid`](../plugin/scripting/payload_codec.py).
- **Wire:** unchanged Tier 2 `split_grid` envelope (or Tier 2b msgpack later); **venv** child still uses `np.frombuffer`.

#### Cython vs plain C vs prebuilt PyPI codecs

| Option | Pros | Cons |
|--------|------|------|
| **Cython** | Fast to write numeric loops; same wheel matrix as any C ext | You build/publish all ABIs |
| **Plain C API module** | Minimal runtime | More boilerplate; same ABI matrix |
| **Vendored orjson/msgpack** | Wheels exist on PyPI; download like audio cffi | Does not remove UNO→Python cell loop; different bottleneck |
| **NumPy in LO** | — | **Rejected** — ABI + size ([core ABI section](enabling_numpy_in_libreoffice.md#1-the-problem-abi-and-embedded-python)) |

#### Verification before shipping in OXT

1. Unit tests: `writeragent_vec` output matches [`host_pack_split_grid`](../plugin/scripting/payload_codec.py) for sample grids.
2. [`scripts/bench_serialization.py`](../scripts/bench_serialization.py) — optional `native_vec` row when import succeeds (planned `--candidates` extension).
3. LO profile legs A–B ([Future work — Priority 1](#priority-1--profile-inside-libreoffice-gate-for-everything-else)) on 100×100+ numeric ranges.
4. Cold import cost in LO (extension startup) — compare to audio’s cffi load.

#### Tier 3 — Host-managed temp file + mmap (large payloads)

For ranges approaching `MAX_PYTHON_DATA_CELLS` or multi‑MB matrices:

1. Host writes a **trusted** temp file (e.g. `tempfile.mkstemp` under LO profile or system temp), row-major binary (`float64` / `float32`), plus JSON metadata: `{"__wa_payload__": "mmap", "path": "/…", "dtype": "float64", "shape": […], "writable": false}`.
2. Child opens with `np.memmap(path, dtype=..., mode="r", shape=...)` or `np.fromfile` — **no full read into RAM** until the script touches data.
3. Host deletes the file after the response line is read (or on worker timeout/kill); child must not retain handles across requests.

- **Pros:** Avoids giant stdin strings; can skip base64 expansion; good for “read once, compute many” if combined with a **payload id** cache ([core roadmap](enabling_numpy_in_libreoffice.md#7-deferred-roadmap)).
- **Cons:** Protocol and lifecycle complexity (Windows file locking, crash cleanup, security of path leakage in logs); must not expose `open(path)` to arbitrary user code — only harness-decoded `data` replacement.
- **Not** “let the user script mmap arbitrary paths”; whitelist imports stay as today.

Best when: payload size makes JSON impractical and benchmarks show copy/parse cost >> disk I/O.

#### Tier 4 — Return path and downstream consumers

| Consumer | Needs | Implication |
|----------|-------|-------------|
| Chat / LLM | JSON-serializable `result` | Prefer summaries, small lists, or “wrote range X1:Y10 via tool” after RPC ([core roadmap](enabling_numpy_in_libreoffice.md#7-deferred-roadmap)) |
| `=PYTHON()` scalar | Single double/string/bool | Large arrays already use session + index ([`python_function.py`](../plugin/calc/python_function.py)); returning a blob handle does not help per-cell bridge |
| `write_formula_range` | Nested lists on host | Host must decode binary envelope → lists once, or RPC streams from host without round-tripping through venv JSON |

Large **egress** arrays: same tiers as ingress (binary envelope or temp file + host reads into Calc), or skip egress entirely via tool RPC writing directly to the sheet.

#### Tier 5 — Session / payload cache (product + protocol)

Optional ([core roadmap](enabling_numpy_in_libreoffice.md#7-deferred-roadmap)): host sends `data_id` + hash of range contents instead of full `data` when unchanged since last execute; worker keeps a bounded LRU of decoded arrays **inside the warm process** (not in user namespace). Requires explicit opt-in and invalidation on sheet edit/recalc.

### Benchmark checklist (regression / future tiers)

Re-run when changing [`payload_codec.py`](../plugin/scripting/payload_codec.py) or considering Tier 2b/3. **Standalone bench (outside LO):** [`scripts/bench_serialization.py`](../scripts/bench_serialization.py) — asymmetric host (stdlib) vs child (NumPy), ingress and egress, scalar/list/ndarray sizes up to 10 000 cells; compares JSON lists vs `split_grid` (`np.frombuffer` + `reshape` for numeric). Production policy matches bench defaults (`BINARY_MIN_CELLS = 10`).

```bash
python scripts/bench_serialization.py --direction both
python scripts/bench_serialization.py --child-only   # isolate np.array vs frombuffer
# Planned: --candidates for orjson/msgpack/zlib vs split_grid (see Tier 2b table)
```

Checklist (same legs the script runs):

1. **Baseline (list path)** — host pack list + `json.dumps` + child `json.loads` + `np.array(data)`.
2. **With envelope (target)** — host `split_grid` + same wire + child `frombuffer`; compare `mat` column and `wire_B`.
2b. **Tier 2b** — same payload with vendored **msgpack** (or custom packer) on host only vs stdlib; measure OXT size and cold-import cost in LO.
3. **Tier 3** — host writes temp binary file; child `np.memmap`; measure with `N×M` at 10⁴, 10⁵, 10⁶ cells (under cap).
4. **Egress** — `result = large_ndarray`: compare `.tolist()` + JSON vs compact binary envelope vs scalar-only return.
5. **Matrix formulas** — count worker invocations per recalc with and without `ROW()-1` index arg.
6. **Cross-platform** — temp file delete on timeout ([`python_worker_manager.py`](../plugin/scripting/python_worker_manager.py) process-group kill), Windows path length, UTF-8 JSON for non-ASCII cells (keep JSON branch for mixed data).

Record: cells/sec host→child, cells/sec child→host, bytes on wire, and whether timeout (`scripting.python_exec_timeout`) fires due to serialization alone.

### Recommendation summary

**Pickle5 + Split-Grid** is the unified binary wire format shipped for both numeric and mixed-type grids (dense numeric arrays use split-grid with `strings: {}` for C-speed `np.frombuffer` loading in child). Keep **Standard Pickle lists** for <10 cells, small 1D mixed arrays, and scalars.

| Situation | Prefer |
|-----------|--------|
| Dense numeric or mixed numeric/strings 2D grids (≥10 cells) | **Pickle5 + Split-Grid envelope** (shipped) |
| Small ranges (<10 cells), 1D mixed types, scalars | **Standard Pickle lists** (no envelope) |
| LLM chat with huge outputs | **Tier 0** summaries + **tool RPC** / `write_formula_range` (Tier 4), not giant `result` JSON |
| Host still slow after split_grid in LO profiles | **Tier 2b** vendored msgpack/orjson (few MB OXT) |
| Very large ranges / stdin size limits | **Temp file + mmap** (Tier 3) + optional **payload cache** (Tier 5) |
| **Next optimizations** | See [Future work — serialization performance](#future-work--serialization-performance) (profile in LO first, then Tier 0 → host paths → defer 2b/3) |

**Vendoring policy:** avoid NumPy/pandas in the OXT; **do** consider a few MB of focused binaries only if Tier 2 stdlib is insufficient after measurement. Keep pack/unpack logic in **`plugin/scripting/`** (host + [`venv_sandbox.py`](../plugin/scripting/venv_sandbox.py)).

### Future work — serialization performance

Tier 2 fixed the **child** hot path (`frombuffer` vs `np.array(list)`). Remaining cost is mostly **host work** (UNO read → Python objects → pack), **extra crossings** (same range sent every recalc), and **downstream consumers** that force blob → nested lists. Do not add Tier 2b/3 OXT weight until **LibreOffice profiles** show serialization dominates compute.

**Suggested next sprint:** (1) LO profile → (2) Tier 0 product/prompt fixes → (3) host opaque blob or single-pass UNO→bytes **only if** step 1 points there.

#### Priority 1 — Profile inside LibreOffice (gate for everything else)

[`scripts/bench_serialization.py`](../scripts/bench_serialization.py) is asymmetric and **skips** the production step that still costs most on the host: **UNO range read → one Python object per cell** in [`calc_addin_data_to_python`](../plugin/calc/calc_addin_data.py), then [`pack_calc_data_for_wire`](../plugin/calc/calc_addin_data.py).

Add timing (debug menu, `testing_runner`, or temporary logs) on realistic sheets:

| Leg | What to measure |
|-----|-----------------|
| A | `calc_addin_data_to_python` only |
| B | A + `pack_calc_data_for_wire` |
| C | B + `json.dumps` + worker round-trip |
| D | Response + `host_unpack_data` (matrix `=PYTHON()` is often hot here) |

**Stop rule:** If NumPy compute dominates, serialization work has low ROI. If **read + host pack + JSON line** dominates, pursue host optimizations below. If **host_unpack → nested lists** dominates on matrix formulas, fix egress pass-through before msgpack/mmap.

Possible deliverable: minimal LO harness (debug menu or UNO test) that prints legs A–D for one `=PYTHON()` call on a large numeric range.

#### Priority 2 — Tier 0: less data on the wire (best ROI, no protocol change)

Often beats another codec — product, prompts, and formula patterns:

| Area | Action |
|------|--------|
| **Chat / LLM** | Prompts + tool behavior: return scalars/summaries (`result = float(np.mean(...))`), two-phase “compute in venv → `write_formula_range`”, not 10⁵-element lists in `result`. |
| **`=PYTHON()` matrix** | Prefer **`ROW()-1`** index form — one worker run + [`_WorkerResultSession`](../plugin/calc/python_function.py); avoid N recalcs each resending the same `data`. |
| **Ranges** | Tighter sheet ranges; strip `None` in script; no `collapse` on host yet (LibrePythonista gap) but same intent. |

See [Tier 0](#tier-0--keep-json-reduce-crossings-no-protocol-change) and [core two-phase workflow](enabling_numpy_in_libreoffice.md#two-phase-llm-workflow).

#### Priority 3 — Host: pack closer to UNO cells (code, if profiling says read/pack hurts)

**Today:** every cell → Py scalar in nested lists → second scan for `split_grid`.

**Idea:** one pass **UNO → row-major bytes** during range read, or a **Cython pack** over a buffer after the Python UNO read — see [Building host native extensions (Cython)](#building-host-native-extensions-cython). Avoids a million heap floats before base64. Child path unchanged (`frombuffer`).

#### Priority 4 — Host: opaque `split_grid` pass-through (if egress/unpack hot)

[`host_unpack_split_grid`](../plugin/scripting/payload_codec.py) expands split_grid to nested lists for Calc matrix/session paths. If leg D dominates:

- Keep **`split_grid` opaque** through more of the pipeline; decode to lists only when emitting per-cell UNO values, or  
- Insert via **`write_formula_range`** from host after one decode (pairs with [Tier 4](#tier-4--return-path-and-downstream-consumers) and future **tool RPC**).

Larger architectural slice than “faster JSON.”

#### Priority 5 — Smaller wire and pandas egress (experiments)

| Idea | Notes |
|------|--------|
| **`float32` envelope** | Optional `dtype` in wire dict; ~half bytes when precision allows; policy + round-trip tests. |
| **Pandas egress** | Large `DataFrame` still `to_dict(orient="records")` in [`venv_sandbox.py`](../plugin/scripting/venv_sandbox.py); route numeric blocks through `child_pack_result` / blob where possible. |

#### Priority 6 — Tier 5: worker payload cache (same range, many recalcs)

Fresh namespace per call stays ([core strategy](enabling_numpy_in_libreoffice.md#2-strategy-decision)); the **warm worker** can still hold a bounded LRU of decoded arrays keyed by `data_id` + range content hash — host sends id instead of 250 k cells when unchanged since last execute. High impact for repeated `=PYTHON(code; B1:Z1000)` on recalc; needs invalidation on sheet edit/recalc. See [Tier 5](#tier-5--session--payload-cache-product--protocol).

#### Priority 7 — Defer unless LO profiles disagree with bench

| Tier | Invest when |
|------|-------------|
| **2b** — orjson / msgpack on host | Whole-line `json.dumps` / `json.loads` dominates after Priority 1 |
| **3** — mmap temp file | Payloads near [`MAX_PYTHON_DATA_CELLS`](../plugin/calc/calc_addin_data.py) (250 k); stdin size or base64 RAM spikes |
| **Tool RPC** ([core RPC roadmap](enabling_numpy_in_libreoffice.md#venv--libreoffice-tool-rpc)) | Sheet output should not round-trip huge `result` through JSON at all |

#### Completed: Split-Grid inside Pickle Optimization

**Status: Fully Implemented and Standardized (May 2026)**

We have successfully combined the microsecond C-speed memory materialization of **Split-Grid** (`np.frombuffer`) with the zero-overhead raw binary streaming of **Pickle Protocol 5**.

By placing the raw binary numeric buffer (as a Python `array.array('d')` on the host side, or `bytes` on the child side) *directly inside the Pickle envelope* as an unencoded field under the `"buffer"` key, we have eliminated Base64 encoding/decoding and JSON parsing CPU cycles entirely.

##### How it is designed and implemented:
1. **Host-Side Pack (OOB/Direct array serialization)**:
   Instead of Base64 encoding the contiguous float64 array into a `"b64"` ASCII string inside a JSON dictionary, we wrap it natively inside a standard Python dictionary under the `"buffer"` key (holding the raw binary bytes natively):
   ```python
   payload = {
       "__wa_payload__": "split_grid",
       "dtype": "float64",
       "column_kinds": column_kinds,
       "shape": shape,
       "buffer": buf.tobytes(),  # Direct raw bytes
       "strings": strings,
   }
   ```
2. **IPC Binary Transport**:
   When unpickling using Pickle Protocol 5 on the peer side (host or child), Pickle natively deserializes the raw `bytes` object with **zero Base64 parsing or string decoding overhead**.
3. **Child-Side Materialize (C-Speed Direct Buffer Ingestion)**:
   In the child, if the sparse `strings` dictionary is empty, NumPy instantly materializes the buffer:
   ```python
   arr = np.frombuffer(payload["buffer"], dtype=np.float64).reshape(payload["shape"])
   ```
   This gives the **exact C-speed buffer mapping** of `split_grid` but completely gets rid of the **33% Base64 wire bloat** and **base64 CPU cycles**!

##### Architectural and Performance Benefits:
- **No Base64 overhead**: Base64 enlarges binary footprints by ~33% and consumes CPU cycles encoding/decoding. Eliminating this reduces both RAM and CPU latency.
- **Zero-copy out-of-band unpickling**: Uses Pickle 5's out-of-band buffer sharing features, allowing zero-copy memory reads between IPC layers.
- **No dynamic JSON fallback**: To keep production code fast, clean, and completely streamlined, all historical JSON/Base64 serialization code pathways have been moved out of production execution paths and placed strictly in the benchmark and unit testing suites.

#### Remaining pipeline costs (reference)

Even with Pickle5 + Split-Grid shipped, these stages still exist:

```text
UNO read → Py list per cell → host_pack (array.tobytes) → pickle.dumps (protocol 5)
  → pickle.loads → frombuffer → … compute …
  → child_pack_result → pickle.dumps → pickle.loads → host_unpack → nested lists (Calc matrix)
```

| Bottleneck | Future lever |
|------------|--------------|
| Per-cell Py objects at read | Priority 3 (Cython pack / UNO→bytes) — [Building host natives](#building-host-native-extensions-cython) |
| Same range every recalc | Priority 2 (ROW index), Priority 6 (cache) |
| Giant `result` in chat | Priority 2 (Tier 0), tool RPC |
| DataFrame `.to_dict` | Priority 5 |

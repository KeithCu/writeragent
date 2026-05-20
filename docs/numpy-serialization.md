# NumPy Serialization

Back to the [core NumPy and Python guide](enabling_numpy_in_libreoffice.md).

This page collects the serialization details for WriterAgent's out-of-process NumPy bridge: the shipped `f64_blob` fast path, benchmark results, pipeline costs, optimization tiers, future profiling work, and native host-extension notes. The core guide stays focused on setup, execution, safety, and Calc usage.

## Serialization optimization opportunities

The compute bridge is **asymmetric by design**: LibreOffice’s embedded Python (host) must stay ABI-safe and ships **without NumPy**; the user venv (child) may use NumPy, pandas, and other C extensions. Serialization is therefore the main lever for large-range performance — not importing NumPy into LibreOffice ([docs/vector-search-design.md](vector-search-design.md) §3, “NumPy tax”). The host can still ship **small vendored binaries** (a few MB, like [audio](audio-architecture.md) or future `sqlite-vec`) to pack/unpack payloads faster than pure stdlib, while the child keeps the heavy numeric stack in the user venv.

Historically every crossing used **nested Python lists inside JSON text** on stdin/stdout. That remains the fallback; **Tier 2 `f64_blob` is now shipped** for dense numeric grids (see [below](#tier-2-f64_blob-shipped)).

### What we implemented (Tier 2)

For **dense numeric** `data` and large numeric `result`, the bridge uses a **stdlib binary envelope** inside the existing JSON line ([Tier 2](#tier-2--base64-binary-blob-inside-json-asymmetric-fast-path)) — not a new wire protocol, not vendored msgpack, not mmap.

| Piece | Approach |
|-------|----------|
| **Wire** | One JSON object per line on stdin/stdout ([`python_worker_manager.py`](../plugin/scripting/python_worker_manager.py)) |
| **Heavy payload** | Tagged dict: `{"__wa_payload__": "f64_blob", "dtype": "float64", "shape": [...], "b64": "..."}` — row-major IEEE float64 bytes; `None`/empty → NaN |
| **Host** | [`host_pack_data`](../plugin/scripting/payload_codec.py) / [`host_unpack_data`](../plugin/scripting/payload_codec.py) — stdlib `array` + `base64`; **no NumPy import** on the LO side |
| **Child** | [`child_unpack_data`](../plugin/scripting/payload_codec.py) → `np.frombuffer` + `reshape` before `send_variables`; [`child_pack_result`](../plugin/scripting/payload_codec.py) on egress instead of `.tolist()` for large ndarrays |
| **Threshold** | `BINARY_MIN_CELLS = 10` — **≥ 10 cells** and all cells numeric-coercible → blob; otherwise nested JSON lists |
| **Fallback** | Mixed types, text, dates, formulas with strings, and grids **&lt; 10 cells** stay nested lists |
| **Debug** | With agent/debug logging enabled, look for `payload_codec host_pack f64_blob` / `child_unpack f64_blob` / `below_threshold` in `writeragent_debug.log` |

**Benchmarked** outside LO ([`scripts/bench_serialization.py`](../scripts/bench_serialization.py)) — [results](#benchmark-results-2026-05). **Defer** Tier 2b (vendored msgpack), Tier 3 (mmap), Tier 5 (payload cache) unless real Calc profiles disagree. Tier 0 (scalars, two-phase tools, matrix `ROW()` index) stays complementary.

### Benchmark results (2026-05)

Asymmetric simulation: **host** = stdlib list pack + `json.dumps`; **child** = `json.loads` + either `np.array(list)` (**json_list**) or `np.frombuffer` + `reshape` (**f64_blob**). Median timings; auto blob when **at least 10 cells** (e.g. 4×3 and 10×1 qualify; 3×3 does not) — [`payload_codec.py`](../plugin/scripting/payload_codec.py).

| Case | Wire size | Materialize speedup | End-to-end speedup |
|------|-----------|---------------------|-------------------|
| **100×10 000 ingress** | **~53%** of json (~104 KiB vs ~198 KiB) | **~13×** (`frombuffer` vs `np.array`) | **~2×** |
| **100×10 000 egress** | **~53%** of json | **~1.6×** (host unpack) | **~4.7×** |
| **1×1000 / 1000×1 ingress** | **~48–53%** of json | **~11–17×** | **~2×** |
| **10×10 ingress** | **~56%** of json | **~4.6×** | **~1.7×** |
| **< 10 cells** | Blob often **larger** or similar on wire | Marginal | Often **json_list** wins — keep list fallback |

**Conclusions (why Tier 2 shipped):** `f64_blob` is what NumPy reads fast (`frombuffer` dominates `np.array` on large dense grids). Wire size roughly halves for 10⁴ cells. Grids **&lt; 10 cells** should stay **json_list** (blob often larger on wire). Host pack + `json.dumps` for blob is still cheaper than list+json at 10⁴ scale. **No msgpack/mmap** for the current 250 k cell cap based on these numbers — production uses stdlib Tier 2 only.

Re-run: `python scripts/bench_serialization.py --direction both`

### Tier 2 `f64_blob` (shipped)

Production wiring (2026-05):

| Location | Role |
|----------|------|
| [`plugin/scripting/payload_codec.py`](../plugin/scripting/payload_codec.py) | Single source: pack/unpack, threshold, `describe_wire_value` for logs |
| [`plugin/calc/calc_addin_data.py`](../plugin/calc/calc_addin_data.py) | `pack_calc_data_for_wire()` after range read; `count_cells()` understands blob envelopes |
| [`plugin/calc/prompt_function.py`](../plugin/calc/prompt_function.py) | `=PYTHON()` ingress pack + `host_unpack_data` on large list results for matrix/session |
| [`plugin/calc/venv_python.py`](../plugin/calc/venv_python.py) | Chat tool ingress pack |
| [`plugin/scripting/venv_sandbox.py`](../plugin/scripting/venv_sandbox.py) | `child_unpack_data` before inject; `child_pack_result` in `serialize_result` |
| [`tests/scripting/test_payload_codec.py`](../tests/scripting/test_payload_codec.py) | Unit tests (threshold, round-trip, mixed text → lists) |
| [`tests/scripting/test_run_venv_code.py`](../tests/scripting/test_run_venv_code.py) | Harness integration with blob payloads |

**Policy:** `BINARY_MIN_CELLS = 10` — numeric grids with **≥ 10 cells** use `f64_blob`; smaller or non-numeric grids use JSON nested lists.

**Still deferred:** Tier 2b vendored codecs, mmap (Tier 3), payload cache (Tier 5), venv tool RPC.

### Current pipeline and costs

```text
Calc UNO range
  → calc_addin_data_to_python (host: list / list[list])
  → pack_calc_data_for_wire → host_pack_data (host: f64_blob or nested list)
  → json.dumps(request)          (host: one JSON line; blob is base64 inside JSON)
  → json.loads(request)          (child: parse envelope dict)
  → child_unpack_data → ndarray  (child: frombuffer + reshape when f64_blob)
  → send_variables({"data": ...}) (child: ndarray or list in fresh namespace)
  → user code (NumPy/pandas)
  → serialize_result → child_pack_result (child: f64_blob or list for large numeric result)
  → json.dumps(response)         (child: text line to host)
  → json.loads(response)         (host)
  → host_unpack_data where needed (host: blob → nested lists for Calc matrix / LLM)
  → finalize_python_return / write_formula_range
```

| Stage | Module | What happens | Large dense numeric `data` (shipped path) |
|-------|--------|--------------|-------------------------------------------|
| Range read | [`calc_addin_data.py`](../plugin/calc/calc_addin_data.py) | Cell scalars in nested lists; cap 250 000 cells | O(cells) once at read |
| Host pack | [`payload_codec.py`](../plugin/scripting/payload_codec.py) | `array` + base64 envelope when ≥10 numeric cells | One pass; wire ~half size vs JSON list (bench) |
| Host encode | [`python_worker_manager.py`](../plugin/scripting/python_worker_manager.py) | `json.dumps` of request dict | Smaller line for blob; still O(cells) base64 |
| Child unpack | [`venv_sandbox.py`](../plugin/scripting/venv_sandbox.py) | `frombuffer` + `reshape` → ndarray | ~13× faster materialize vs `np.array(list)` at 10⁶ cells (bench) |
| Return | [`serialize_result`](../plugin/scripting/venv_sandbox.py) | `child_pack_result` for ndarray/list; DataFrame still `to_dict(orient="records")` | Large ndarray egress as blob, not `.tolist()` |
| Calc return | [`prompt_function.py`](../plugin/calc/prompt_function.py) | `host_unpack_data` when expanding matrix results | Lists only where Calc needs per-cell scalars |

**Not used:** pickle, msgpack, mmap, shared memory. [`SafeSerializer`](../plugin/contrib/smolagents/serialization.py) (`__type__: ndarray`) is **not** on the worker path — only [`payload_codec.py`](../plugin/scripting/payload_codec.py).

**Fresh namespace every call** ([core strategy](enabling_numpy_in_libreoffice.md#2-strategy-decision)): there is no worker-side variable cache; the same `A1:Z1000` range is re-serialized on every `=PYTHON()` or `run_venv_python_script` invocation unless the product adds an explicit cache ([core roadmap](enabling_numpy_in_libreoffice.md#7-deferred-roadmap)).

### Design constraints

- **Host stays NumPy-free** — do not vendor full NumPy/pandas into LibreOffice ([vector-search-design.md](vector-search-design.md) §3). That is unrelated to shipping **small, purpose-built binaries** (a few MB per platform total) when stdlib is too slow.
- **Host may use small vendored natives** — same precedent as audio ([audio-architecture.md](audio-architecture.md): `sounddevice` / CFFI wheels under `vendor/` / `plugin/vendor/`, injected from [`plugin/main.py`](../plugin/main.py)) and future vector search (`sqlite-vec` `vec0`, ~1 MB per OS in [vector-search-design.md](vector-search-design.md)). A serialization codec wheel or tiny custom `.so` is acceptable if it stays in the **few‑MB** budget and is pruned per OS/Python ABI like audio — not a 50–100 MB science stack.
- **Wire format stays line-oriented JSON** until a protocol version bump — easy to log, grep, and extend; binary payloads ride *inside* JSON fields (base64), as a **msgpack/CBOR body** on the same stdin line, or via temp-file metadata; not a raw byte stream without framing on day one.
- **Sandbox must not grant arbitrary filesystem access** — [`LocalPythonExecutor`](../plugin/contrib/smolagents/local_python_executor.py) blocks `os` / `pathlib` in user code; temp files and mmap paths must be **host-allocated, host-trusted paths** passed in the request envelope, not paths chosen by LLM-generated scripts.
- **LLM and Calc still need JSON-safe or scalar outputs** eventually — even an optimized ingress path usually ends with compact `result` (scalar, short list, summary stats) or a second-phase host tool (`write_formula_range`) for sheet output ([core user guide](enabling_numpy_in_libreoffice.md#3-user-guide)).

### Optimization tiers (what to consider)

#### Tier 0 — Keep JSON; reduce crossings (no protocol change)

- Return **scalars or small summaries** from the venv (`result = float(np.mean(arr))`) instead of full arrays when the LLM only needs a number.
- Use the **two-phase workflow**: compute in venv, insert via existing Calc tools with a compact payload — avoid shipping a 10⁵-element list through chat JSON twice.
- For **matrix `=PYTHON()`**, prefer the `ROW()-1` index form so one worker run fills a session cache ([`finalize_python_return`](../plugin/calc/prompt_function.py)), not N full round-trips with the same `data`.
- Tighten ranges (`collapse`-style) in the sheet or strip `None` in Python before heavy work.

Best when: mixed types (strings, blanks, dates), small ranges, or logic dominates runtime.

#### Tier 1 — Typed JSON envelope (metadata + payload)

Extend request/response objects with a tagged shape, e.g. `{"__wa_payload__": "ndarray", "dtype": "float64", "shape": [1000, 100], "data": ...}` where `data` is still JSON list **or** base64 (Tier 2). On the venv side: `np.array(data, dtype=...).reshape(shape)` or `np.frombuffer(...)`.

- Reuse ideas from [`SafeSerializer`](../plugin/contrib/smolagents/serialization.py) (`__type__: ndarray`) but implement a **small, worker-specific** codec in [`venv_sandbox.py`](../plugin/scripting/venv_sandbox.py) / host mirror — do not pull the full smolagents serializer into the hot path without measuring import and dependency cost.
- Host without NumPy: decode envelope to nested lists only when Calc/LLM need lists; otherwise pass the envelope through opaquely.

Best when: you need dtype/shape preserved but payloads are still moderate; stepping stone before binary wire.

#### Tier 2 — Base64 binary blob inside JSON (asymmetric fast path)

**Shipped** in [`payload_codec.py`](../plugin/scripting/payload_codec.py).

Host (stdlib only) packs a dense numeric block:

```python
# Host sketch (embedded Python): row-major float64, None/empty → NaN or sentinel
import array, base64, struct
buf = array.array("d", (float(x) if x is not None else float("nan") for x in flat))
payload = {"__wa_payload__": "f64_blob", "shape": [nrows, ncols], "b64": base64.b64encode(buf.tobytes()).decode("ascii")}
```

Child (venv):

```python
import base64, numpy as np
raw = base64.b64decode(payload["b64"])
arr = np.frombuffer(raw, dtype=np.float64).reshape(payload["shape"])
```

- **Pros:** One JSON line still; no pickle; NumPy only on child; avoids million-element Python float objects on the wire.
- **Cons:** ~33% base64 overhead; host still walks cells once to pack; not ideal for sparse/mixed columns without a separate “sparse” or “json_list” branch.

Best when: benchmarks show JSON list encode/decode dominates NumPy compute for mostly-numeric ranges.

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
| **Fallback** to stdlib `f64_blob` on `ImportError` | Link against LibreOffice or call UNO from C |
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
  tests/                      # pytest vs host_pack_f64_blob
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
    Pack[pack_f64_row_major]
  end
  UNO -->|"memoryview_array_or_flat_list"| Bridge --> Pack
```

- **Python:** one UNO read ([`calc_addin_data_to_python`](../plugin/calc/calc_addin_data.py) or `getDataArray`) — still required.
- **Cython:** fast **coerce + pack** (row-major `float64`, `None`/empty → NaN) over a contiguous buffer — replaces the hot loop in [`host_pack_f64_blob`](../plugin/scripting/payload_codec.py).
- **Wire:** unchanged Tier 2 `f64_blob` envelope (or Tier 2b msgpack later); **venv** child still uses `np.frombuffer`.

#### Cython vs plain C vs prebuilt PyPI codecs

| Option | Pros | Cons |
|--------|------|------|
| **Cython** | Fast to write numeric loops; same wheel matrix as any C ext | You build/publish all ABIs |
| **Plain C API module** | Minimal runtime | More boilerplate; same ABI matrix |
| **Vendored orjson/msgpack** | Wheels exist on PyPI; download like audio cffi | Does not remove UNO→Python cell loop; different bottleneck |
| **NumPy in LO** | — | **Rejected** — ABI + size ([core ABI section](enabling_numpy_in_libreoffice.md#1-the-problem-abi-and-embedded-python)) |

#### Verification before shipping in OXT

1. Unit tests: `writeragent_vec` output matches [`host_pack_f64_blob`](../plugin/scripting/payload_codec.py) for sample grids.
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
| `=PYTHON()` scalar | Single double/string/bool | Large arrays already use session + index ([`prompt_function.py`](../plugin/calc/prompt_function.py)); returning a blob handle does not help per-cell bridge |
| `write_formula_range` | Nested lists on host | Host must decode binary envelope → lists once, or RPC streams from host without round-tripping through venv JSON |

Large **egress** arrays: same tiers as ingress (binary envelope or temp file + host reads into Calc), or skip egress entirely via tool RPC writing directly to the sheet.

#### Tier 5 — Session / payload cache (product + protocol)

Optional ([core roadmap](enabling_numpy_in_libreoffice.md#7-deferred-roadmap)): host sends `data_id` + hash of range contents instead of full `data` when unchanged since last execute; worker keeps a bounded LRU of decoded arrays **inside the warm process** (not in user namespace). Requires explicit opt-in and invalidation on sheet edit/recalc.

### Benchmark checklist (regression / future tiers)

Re-run when changing [`payload_codec.py`](../plugin/scripting/payload_codec.py) or considering Tier 2b/3. **Standalone bench (outside LO):** [`scripts/bench_serialization.py`](../scripts/bench_serialization.py) — asymmetric host (stdlib) vs child (NumPy), ingress and egress, scalar/list/ndarray sizes up to 10 000 cells; compares JSON lists vs `f64_blob` (`np.frombuffer` + `reshape`). Production policy matches bench defaults (`BINARY_MIN_CELLS = 10`).

```bash
python scripts/bench_serialization.py --direction both
python scripts/bench_serialization.py --child-only   # isolate np.array vs frombuffer
# Planned: --candidates for orjson/msgpack/zlib vs f64_blob (see Tier 2b table)
```

Checklist (same legs the script runs):

1. **Baseline (list path)** — host pack list + `json.dumps` + child `json.loads` + `np.array(data)`.
2. **With envelope (target)** — host `f64_blob` + same wire + child `frombuffer`; compare `mat` column and `wire_B`.
2b. **Tier 2b** — same payload with vendored **msgpack** (or custom packer) on host only vs stdlib; measure OXT size and cold-import cost in LO.
3. **Tier 3** — host writes temp binary file; child `np.memmap`; measure with `N×M` at 10⁴, 10⁵, 10⁶ cells (under cap).
4. **Egress** — `result = large_ndarray`: compare `.tolist()` + JSON vs compact binary envelope vs scalar-only return.
5. **Matrix formulas** — count worker invocations per recalc with and without `ROW()-1` index arg.
6. **Cross-platform** — temp file delete on timeout ([`python_worker_manager.py`](../plugin/scripting/python_worker_manager.py) process-group kill), Windows path length, UTF-8 JSON for non-ASCII cells (keep JSON branch for mixed data).

Record: cells/sec host→child, cells/sec child→host, bytes on wire, and whether timeout (`scripting.python_exec_timeout`) fires due to serialization alone.

### Recommendation summary

**Tier 2 `f64_blob` is shipped** (ingress + egress for dense numeric arrays). Keep **JSON nested lists** for &lt;10 cells, mixed types, and scalars. Benchmarks ([above](#benchmark-results-2026-05)) justified the threshold and format; no Tier 2b/3 needed at current caps.

| Situation | Prefer |
|-----------|--------|
| Dense numeric `data` / large numeric `result` (≥10 cells) | **`f64_blob` envelope in JSON** (Tier 2, shipped) |
| Small ranges, mixed types, formulas with strings/`None` | **JSON nested lists** (no envelope) |
| LLM chat with huge outputs | **Tier 0** summaries + **tool RPC** / `write_formula_range` (Tier 4), not giant `result` JSON |
| Host still slow after Tier 2 in LO profiles | **Tier 2b** vendored msgpack/orjson (few MB OXT) |
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
| **`=PYTHON()` matrix** | Prefer **`ROW()-1`** index form — one worker run + [`_WorkerResultSession`](../plugin/calc/prompt_function.py); avoid N recalcs each resending the same `data`. |
| **Ranges** | Tighter sheet ranges; strip `None` in script; no `collapse` on host yet (LibrePythonista gap) but same intent. |

See [Tier 0](#tier-0--keep-json-reduce-crossings-no-protocol-change) and [core two-phase workflow](enabling_numpy_in_libreoffice.md#two-phase-llm-workflow).

#### Priority 3 — Host: pack closer to UNO cells (code, if profiling says read/pack hurts)

**Today:** every cell → Py scalar in nested lists → second scan for `f64_blob`.

**Idea:** one pass **UNO → row-major bytes** during range read, or a **Cython pack** over a buffer after the Python UNO read — see [Building host native extensions (Cython)](#building-host-native-extensions-cython). Avoids a million heap floats before base64. Child path unchanged (`frombuffer`).

#### Priority 4 — Host: opaque `f64_blob` pass-through (if egress/unpack hot)

[`host_unpack_f64_blob`](../plugin/scripting/payload_codec.py) expands blobs to nested lists for Calc matrix/session paths. If leg D dominates:

- Keep **`f64_blob` opaque** through more of the pipeline; decode to lists only when emitting per-cell UNO values, or  
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

Bench at ≤10 k cells in [`bench_serialization.py`](../scripts/bench_serialization.py) did **not** justify 2b/3 at current caps; production stays stdlib Tier 2 until LO says otherwise.

#### Remaining pipeline costs (reference)

Even with Tier 2 shipped, these stages still exist:

```text
UNO read → Py list per cell → host_pack (array+base64) → json.dumps(line)
  → json.loads → frombuffer → … compute …
  → child_pack_result → json.dumps → host_unpack → nested lists (Calc matrix)
```

| Bottleneck | Future lever |
|------------|--------------|
| Per-cell Py objects at read | Priority 3 (Cython pack / UNO→bytes) — [Building host natives](#building-host-native-extensions-cython) |
| Base64 in JSON line | float32, Tier 3 mmap, or 2b binary frame |
| Same range every recalc | Priority 2 (ROW index), Priority 6 (cache) |
| Giant `result` in chat | Priority 2 (Tier 0), tool RPC |
| DataFrame `.to_dict` | Priority 5 |

# Enabling NumPy & Python in LibreOffice

WriterAgent can run scientific Python — **NumPy**, **pandas**, **scipy**, and similar C-extension stacks — **without** loading those packages into LibreOffice’s embedded interpreter. Point **Settings → Python** at a **user-provided virtual environment**, then use **Run Python Script…**, Calc `=PY()` / **Edit Python in Cell…**. Code runs in a sandboxed child process and returns serializable results to your sheet or script UI.

**Glossary:** `=PY()` and `=PYTHON()` are the same Calc add-in (`XPythonFunction`). **User-facing examples in this file use `=PY()`.** The registered alias `PYTHON` works the same (some ODS fixtures use `=PYTHON()` because XLSX import lowercases custom add-in names).

These Python / NumPy features also now ship in **LibrePy.oxt**. The WriterAgent extension covers the same Python surfaces plus a prototype Calc to =PY() spreadsheet conversion, chat and related tools — install **one** OXT at a time (see [extension packaging](scripting/librepy-split.md)).

## Table of contents

1. [The problem: ABI and embedded Python](#1-the-problem-abi-and-embedded-python)
2. [Strategy decision](#2-strategy-decision)
3. [User guide](#3-user-guide)
4. [Architecture](#4-architecture)
5. [Developer reference](#5-developer-reference)
6. [The `=PY()` Calc function](#6-the-py-calc-function)
  - [Session modes and recalc semantics](#session-modes-and-recalc-semantics)
  - [Keyboard shortcuts and recalc](#keyboard-shortcuts-and-recalc)
  - [Calc formula lexer quirks (inline code)](#calc-formula-lexer-quirks-inline-code)
  - [Data shapes (`data` / blanks / varargs)](calc/py-data-shapes.md)
7. [Deferred roadmap](#7-deferred-roadmap)
  - [Calc UX backlog](#calc-ux-backlog)
8. [Collabora Online and jail-safe execution](scripting/numpy-jailsafe.md)
9. [Implementation status](#9-implementation-status)

### Related Documents

| Document | Description / Notes |
| :--- | :--- |
| [Calc `=PY()` data shapes](calc/py-data-shapes.md) | `CalcRange`, ingress/egress, blanks vs NaN, dates, multi-range |
| [Venv subprocess IPC & NumPy serialization](scripting/numpy-serialization.md) | Warm worker, protocol, wire formats, benchmarks |
| [Why not copy Microsoft’s `=PY()`](scripting/ms-py-compatibility.md) | `xl()` + co-volatility costs vs native `=PY(code, data?)` |
| [NumPy domain helpers](scripting/numpy-domains.md) | Analysis, Viz, Symbolic, Units, Text, Forecasting |
| [DuckDB / folder SQL](calc/duckdb-dev-plan.md) | Read-only SQL over sibling files + live ranges; 200-row result cap (`truncated`/`warning`); prefer `run_sql` / `session_duckdb()` from `=PY()` |
| [Extension packaging](scripting/librepy-split.md) | LibrePy vs WriterAgent packaging |
| [LibrePy-surface live QA plan](librepy-manual-qa-plan.md) | Real-scenario Calc/RPS/domain checks (`=PY("1 + 1")` upward). Either OXT; do not test chat/`=PROMPT()`. |
| [Monaco editor dev plan](scripting/monaco-editor-dev-plan.md) | IPC, phases 2B–2F |
| [Collabora Online / jail-safe](scripting/numpy-jailsafe.md) | Thin C++ Add-In + compute service |
| [Collabora engine spill](calc/collabora-engine-spill-plan.md) | Single-cell `=PY()` spill in Core Calc |
| [Collabora engine plot egress](calc/collabora-engine-plot-plan.md) | Single & multiple plot/image egress in Collabora Online Calc |
| [Calc spreadsheet → Python import](calc/spreadsheet-to-python-import.md) | Prototype / low priority — convert formulas to `=PY()` |
| [Jupyter notebook import](writer/jupyter-notebook-import.md) | Writer `.ipynb` import + ▶ run (shared `notebook:…` kernel; not Calc `=PY()`) |

---



## 1. The problem: ABI and embedded Python

`numpy` is not pure Python; it ships compiled C/C++ extensions that must match the **exact** Python ABI they were built for.

- **The problem:** If a user runs `pip install numpy` with system Python 3.12 and the extension loads that build into LibreOffice’s embedded Python (often 3.8–3.11), LibreOffice can **fatally crash** — the extensions are binary-incompatible.
- **The requirement:** NumPy (and similar wheels) must be installed into the **same** `python` executable that runs the code, or execution must stay in a **separate** interpreter that never shares memory with LibreOffice.

All design choices below follow from that constraint.

---



## 2. Strategy decision


| Approach                                      | Status       | Summary                                                                                                                                                                                                     |
| --------------------------------------------- | ------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **1 — Pip bootstrap inside LibreOffice**      | **Rejected** | Ship `pip` and install packages into LO’s runtime at startup (LibrePythonista-style). Requires heavy path/sandbox handling (Flatpak, macOS, Windows) and couples the extension to the embedded interpreter. |
| **2 — User-provided venv + subprocess**       | **Chosen**   | User points `scripting.python_venv_path` at an existing `.venv`. The extension never imports NumPy in-process.                                                                                              |




### Chosen: warm worker + session-aware sandbox

1. **Persistent worker:** `[PythonWorkerManager](plugin/scripting/venv_worker.py)` spawns the venv’s `python` once per executable path and keeps it alive.
2. **Namespace per request (configurable):** `[worker_harness.py](plugin/scripting/venv/worker_harness.py)` → `[venv_sandbox.py](plugin/scripting/venv/venv_sandbox.py)` uses a `[LocalPythonExecutor](plugin/contrib/smolagents/local_python_executor.py)`. Default **Isolated** mode gives each `=PY()` cell a fresh namespace (init script still seeds once). **Shared kernel** mode (`[session_manager.py](plugin/scripting/session_manager.py)`) keeps one workbook namespace across cells — see [§6 Session modes](#session-modes-and-recalc-semantics).
3. **Length-prefixed Pickle5 IPC:** `[PythonWorkerManager](plugin/scripting/venv_worker.py)` ↔ `[worker_harness.py](plugin/scripting/venv/worker_harness.py)` exchange framed request/response dicts; `data` / `result` use `[split_grid](scripting/numpy-serialization.md#strategy-3-split-grid-serialization-detail)` when dense. Protocol detail: [Venv subprocess IPC](scripting/numpy-serialization.md#worker-protocol). Bidirectional **tool RPC** (`import writeragent as wa` → `wa.writer.apply_content(...)`) is wired on the same pipe for **Run Python Script** / chat; it is **disabled** during `=PY()` recalc ([§7](#venv--libreoffice-tool-rpc)).

**Pros:** Sidesteps ABI issues; any Python version in the venv; avoids spawn overhead on every call; optional shared-kernel mode for multi-cell pipelines.  
**Cons:** User must create and maintain a venv; in **Isolated** mode, re-pass data via `data` / `data_range` or cell references unless Shared kernel is enabled.

---



## 3. User guide



### Vision

You can run Monte Carlo simulations, statistics, plots, and other library-heavy work **without leaving LibreOffice**. Configure a dedicated Python venv once, then:

- **Run Python Script…** in Writer, Calc, or Draw (Python editor — CodeMirror by default, Monaco optional — or a simple dialog)
- `=PY()` formulas and **Edit Python in Cell…** in Calc
- **Edit Initialization Script…** for workbook-wide helpers (Calc)

No terminal is required after the venv is set up. An optional [chat assistant](#using-the-chat-assistant-optional) (WriterAgent) can generate and run the same kind of code.

### Settings → Python


| Setting                         | Description                                                                                                                                                                               | Example               |
| ------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------- |
| `scripting.python_venv_path`    | Absolute path to an existing venv directory                                                                                                                                               | `~/.writeragent_venv` |
| `scripting.python_session_mode` | `isolated` (default) or `shared` (Shared kernel for `=PY()` cells)                                                                                                                        | `isolated`            |
| `scripting.python_exec_timeout` | Wall-clock limit (seconds) for Run Python Script and `=PY()` (default **10**, max **600**). Trusted long-running helpers (OCR, spaCy, SymPy, embeddings, …) use a longer internal budget. | `10`                  |
| `scripting.python_auto_spill`   | **On by default.** Single-cell `=PY()` returning a list, 2D array, or DataFrame **auto-spills** into adjacent cells. Blocked cells → `#SPILL!`. Disable for matrix-only workflows.        | `true`                |
| `scripting.python_allow_network` | Allow network access (`socket`, `http`, `urllib`, `ssl` and their submodules). Default `false`. | `false` |
| `scripting.python_allow_files`   | Allow file access (`open`, `pathlib`, `shutil`, `io`, `tempfile`, `glob`, and filtered `os` file proxy). Reads and writes anywhere; default blocks both. | `false` |
| `scripting.python_allow_os`      | Allow OS access (real `os`, `subprocess`, `multiprocessing`, `pty`, e.g. `os.system`, `subprocess.run`). Implies file access. Default `false`. | `false` |
| `scripting.python_extra_modules` | Comma-separated extra allowed modules (each name also grants `name.*`). Overrides every built-in blocklist, including `os`, `socket` and `subprocess`. | `""` |



- **Empty path:** Run Python Script and `=PY()` fall back to LibreOffice’s embedded Python — usually **stdlib-only**. **Use a dedicated venv for NumPy.**
- **No automatic venv creation** — you bring your own environment.
- **Path paste:** Paste the path **without** surrounding quotes (Windows Explorer “Copy as path” adds them — strip them). Spaces, parentheses, and non-ASCII home dirs are fine; spawn uses argv lists, not a shell.
- **Layouts:** uv / Poetry / stdlib `venv` → `bin/python` or `Scripts\python.exe`. Windows conda / pyenv-win → env folder or `python.exe` at the env root also works.
- **Test button:** Checks that the path resolves to a `python` executable and reports which package groups are Present/Missing (**Scientific**, **Data Analysis / EDA**, **UI / Editor**, **Vision**, **Embeddings**, **Audio Recording**). After Test finishes, if packages are still Missing, the message ends with copy-paste **`uv pip install …`** then **`pip install …`** for those packages (not shown during progressive refresh). Vision marks paddle/ultralytics/skimage as optional when OCR is already ready. Cold Vision/Embeddings imports can take ~30s on first Test. Domain package lists: [scripting/numpy-domains.md](scripting/numpy-domains.md), [Image Recognition](images/recognition.md), [Embeddings](embeddings.md#embeddings-venv-packages). Microphone capture uses the same venv (`uv pip install sounddevice`) — see [chat/audio-architecture.md](chat/audio-architecture.md).

**Creating the venv (uv recommended in 2026):**

```bash
# Create a dedicated venv (choose a Python close to what you develop with)
uv venv ~/.writeragent_venv --python 3.12

# Activate (optional for uv pip) and install what you need
source ~/.writeragent_venv/bin/activate
uv pip install numpy pandas scipy scikit-learn matplotlib sympy spacy textdescriptives
python -m spacy download xx_sent_ud_sm

# Point Settings → Python at the venv root (~/.writeragent_venv) or the bin/ dir.
```

For the Python editor UI (CodeMirror by default, Monaco optional), also install `pywebview` (on Linux: `PyQt6 PyQt6-WebEngine qtpy`; add `rocher` only if you select Monaco).

### Ways to run Python


| What you use                                  | Where                     | Notes                                                                                                   |
| --------------------------------------------- | ------------------------- | ------------------------------------------------------------------------------------------------------- |
| **LibrePy Python sidebar** (Calc)             | LibrePy **and** WriterAgent | Cell list, diagnostics (stdout/errors), Reset / Edit Init / Run Script / Settings — Python editor stays separate; Calc-only **Python** deck |
| **Run Python Script…**                        | Writer / Calc / Draw menu | Python editor (CodeMirror by default, Monaco optional; **Run** / **Save** / script picker), or a plain multiline dialog if pywebview is missing |
| **Edit Python in Cell…** / `=PY(code, data?)` | Calc                      | Python editor (CodeMirror by default, Monaco optional) when pywebview is available; otherwise a native dialog with the same Save / Data / **Save without =PY()** chrome. Dual save as `=PY("…")` or plain text for `=PY($A$1; …)` |
| **Edit Initialization Script…**               | Calc (sidebar or editor)  | Workbook startup script; seeds helpers for every `=PY()` cell                                           |
| Shared warm worker                            | All of the above          | One subprocess per venv path (`[venv_worker.py](plugin/scripting/venv_worker.py)`)                      |


Scripts compute and return values. **Run Python Script…** (and chat `python_run_script`) can also call WriterAgent tools from the venv via `import writeragent as wa` (same Pickle5 pipe; host runs UNO). `=PY()` recalc does **not** — formula evaluation stays side-effect free.


### Run Python Script & the Python editor {#run-python-script--monaco}

The extension ships a **Python editor (CodeMirror by default, Monaco optional)** (pywebview child in the configured venv) for Calc formulas and ad-hoc scripts. Theme sync with LibreOffice light/dark is shipped. IPC and remaining editor backlog: [scripting/monaco-editor-dev-plan.md](scripting/monaco-editor-dev-plan.md).


| Feature                                                     | Status      | Notes                                                                                            |
| ----------------------------------------------------------- | ----------- | ------------------------------------------------------------------------------------------------ |
| **Edit Python in Cell…** (Calc menubar + cell context menu) | **Shipped** | Dual save (`=PY("…")` or plain text for `=PY($A$1; …)`); editable **Data:** range. Experimental: **Settings → Python → Rewrite xl() ranges** (default off; beside auto-spill) lifts static `xl("A1:…")` onto formula data args and rewrites call sites to polymorphic `data` / `data[i]` / `.to_pandas()` on any Edit Python in Cell save (Monaco, CodeMirror or the built-in dialog), including `=PY($A$1; …)` saves that keep the code in the referenced cell ([`xl_static_rewrite.py`](../plugin/calc/python/xl_static_rewrite.py)). |
| **Run Python Script…** (Writer/Calc/Draw)                   | **Shipped** | **Run** / **Save** / script picker. In Calc, structured results (dicts, tables, DataFrames) format as rich HTML tables and insert via controller transferable paste, registering a native `ScUndo` action for single-step **Ctrl+Z** undo. Save reports an error and leaves the buffer dirty when no script is selected; Run still executes that buffer without writing a library script. |
| **Document-attached scripts**                               | **Shipped** | **This Document** vs **My Scripts** in the picker — scripts can travel with `.odt`/`.ods`/`.odg` |
| **Edit Initialization Script…** (Calc)                      | **Shipped** | Workbook startup script in document properties; LibrePy sidebar button + Monaco                  |
| **LibrePy Python sidebar** (Calc deck)                      | **Shipped** | Cell list, filtered diagnostics, session/actions — not an embedded Monaco editor                 |
| Syntax squiggles, range picker, full Jedi                   | **Backlog** | [Monaco dev plan §8](scripting/monaco-editor-dev-plan.md#8-next-development-plan-detailed)          |


**Requirements:** Settings → Python → venv path with `pywebview` installed (Linux also needs `PyQt6 PyQt6-WebEngine qtpy`; add `rocher` if you select Monaco) for the Python editor UI. **Edit Python in Cell…** and **Run Python Script…** both fall back to native LibreOffice dialogs when pywebview is missing or `scripting.force_internal_script_editor` is true. Native cell edit does not need a venv.

**Document-attached scripts:** Named scripts live in document properties so they travel with the file. New and Save As can attach a script to the document. On a read-only document, Save fails with the document's read-only message and nothing is written to My Scripts; use Save As with the attach box unchecked to keep a copy. Save As starts from the script's stored name, without a `[Doc]` or helper prefix. If the stored envelope is damaged or uses a version this release does not read, it is left unchanged.

**Undo behavior:** In Calc, running a Python script that inserts tabular or structured results registers a native undo action in LibreOffice Calc's internal undo manager. Pressing **Ctrl+Z** (`Edit → Undo`) immediately removes all inserted titles, headers, and rows in a single step.


### Assign `result` {#assign-result}

Prefer `result = …` for the value that should appear in the sheet or script UI. If you never assign `result`, `=PY()` uses the **last expression** value (same Jupyter-style fallback as Excel). In shared-kernel mode a successful `result = …` stays in the namespace so later cells can use it (`result * 1.1`); leftover `result` is used as this cell’s sheet value only when this cell rebound it, so last-expression cells are not hijacked. A failed cell restores the previous `result`. NumPy arrays and pandas objects are serialized in the worker. `print()` is diagnostics only (LibrePy Python sidebar) — it does not become the cell value.

### Using the chat assistant (optional) {#using-the-chat-assistant-optional}

> **WriterAgent only** — not part of the core Python/NumPy extension; see [extension split](scripting/librepy-split.md).

You can also ask the sidebar chat to run the same venv Python. The model uses the specialized tool `python_run_script` (domain `python`) — same warm worker as the menus and `=PY()`. Chat runs are always **isolated** (they do not share the Calc workbook kernel).

The chat model still typically uses a two-phase workflow (compute in venv, then host tools). User scripts can also call tools **from the venv** via `import writeragent as wa` ([§7 tool RPC](#venv--libreoffice-tool-rpc)).

1. **Compute:** Call `python_run_script` with numpy/pandas code; read serialized `result`.
2. **Insert:** Call existing Calc/Writer tools (`write_range`, `set_style`, `create_chart`, etc.), or have the script call `wa.writer.apply_content(...)` itself.


| Context                             | `data` / `data_range`? | Injected in subprocess?                |
| ----------------------------------- | ---------------------- | -------------------------------------- |
| Calc chat, `domain=python`          | Yes                    | Yes, when provided                     |
| Writer / Draw chat, `domain=python` | No                     | Never — use document tools for content |
| `=PY(code, range)`                  | 2nd arg is the range   | Yes                                    |


**What you see:** ask for analysis → the model may show generated Python in Thinking → status *Running Python script…* → results return and the model updates the document via normal tools (or retries on error).

Wall-clock limit is still **Settings → Python** (`scripting.python_exec_timeout`); it is not a tool-schema parameter. Tool schema detail: [§5](#tool-schema-reference) / `[venv.py](plugin/calc/python/venv.py)`.

---



## 4. Architecture

```
┌──────────────────────────────────────────────────────────┐
│                    LibreOffice Process                    │
│                                                          │
│  ┌──────────────────┐   ┌─────────────────────────────┐  │
│  │ Run Python Script │──▶│ run_code_in_user_venv       │  │
│  │ =PY() / Edit Cell │  │ (shared entry)              │  │
│  └──────────────────┘   └──────────┬──────────────────┘  │
│  ┌──────────────────┐              │                     │
│  │ Chat (optional)   │──────────────┘                     │
│  │ run_venv_python…  │                                   │
│  └──────────────────┘                                    │
│                     ┌──────────▼───────────────────────┐ │
│                     │  PythonWorkerManager             │ │
│                     │  warm venv process               │ │
│                     │  worker_harness → venv_sandbox   │ │
│                     └──────────┬───────────────────────┘ │
│                                │ Pickle5 stream         │
│                     ┌──────────▼───────────────────────┐ │
│                     │  User venv Python (subprocess)   │ │
│                     │  LocalPythonExecutor + blocklist │ │
│                     └──────────┬───────────────────────┘ │
│                                │ result / stdout         │
│                     ┌──────────▼───────────────────────┐ │
│                     │  Sheet / script UI / (chat tools)│ │
│                     └──────────────────────────────────┘ │
└──────────────────────────────────────────────────────────┘
```

LibreOffice’s embedded Python and the user’s venv are **different interpreters** ([§1](#1-the-problem-abi-and-embedded-python)). Venv execution uses the venv’s `ast` and packages; the subprocess boundary is the hard safety line for C extensions.

Subprocess lifecycle, worker protocol, Linux pipe performance, and serialization wire formats: **[Venv subprocess IPC & NumPy serialization](scripting/numpy-serialization.md)**.

---



## 5. Developer reference

Host↔venv plumbing (module map, worker protocol, `python_max_data_cells`, benchmarks): **[scripting/numpy-serialization.md](scripting/numpy-serialization.md)**.

### Safety model


| Layer                     | Mechanism                                                                                                                                                                                                   | Protects against                                          |
| ------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------- |
| **Restricted executor**   | `LocalPythonExecutor` in subprocess — AST walk, dunder guards (`__version__`, `__name__`, `__qualname__`, `__doc__`, and `__module__` reads allowed; `operator.attrgetter` hop-checked with `os`/`posix`/`nt`/`sys` callables rejected, `operator.methodcaller`, `str.format` / `format_map`, and `string.Formatter.get_field` use the same dunder allowlist as attribute syntax; private wrapper attributes hidden), iteration/operation limits | `eval`/`exec`, dunder escapes, infinite loops             |
| **Import blocklist**      | Default-allow with a short blocklist (`DANGEROUS_MODULES` in `[local_python_executor.py](../plugin/contrib/smolagents/local_python_executor.py)` and `[sandbox.py](plugin/scripting/sandbox.py)`). `PUBLIC_PLUGIN_IMPORTS` is the exact set of public plugin modules for `plugin.*` / `writeragent.*` mapping. | `subprocess`, `socket`, `pickle`, dynamic import side doors, arbitrary host escape |
| **Subprocess isolation**  | Separate interpreter, no shared memory with LO. On Linux the worker runs in its own transient systemd user scope (`systemd-run --user --scope --collect`), so a worker OOM stops only its scope, not soffice. The scope is skipped under Flatpak, when `systemd-run` or `XDG_RUNTIME_DIR` is missing, or when a probe fails, and the worker then spawns plainly. | ABI crashes, segfaults in C extensions, UNO corruption, worker OOM taking down LibreOffice |
| **Environment scrubbing** | Strip secret-like env vars from child; credential names match a whole `_` or `-` segment (`KEY`, `TOKEN`, `PASSWORD`, `PASSWD`, `PASSPHRASE`, `PAT`, `PWD`, `WEBHOOK`, …) or a glued compound as a substring (`APIKEY` matches `OPENAI_APIKEY` and `OPENAIAPIKEY`; same for the other `_GLUED_CREDENTIAL_TOKENS`). `PWD`, `XAUTHORITY`, `TOKENIZERS_PARALLELISM`, `MONKEY`, `SECRETARY`, and `KEYWORD` are kept. Flatpak host spawn passes that scrubbed mapping with `--clear-env` and `--env=KEY=VALUE`. Spawn calls that omit `env` still inherit the host session. | Credential exfiltration via generated code                |
| **User-provided venv**    | Explicit opt-in                                                                                                                                                                                             | User controls installed packages                          |
| **Timeout**               | Standard: user `scripting.python_exec_timeout` for scripts + quick helpers. Long trusted ops (OCR, spaCy text analytics, SymPy, embeddings, ...) use one internal `LONG_TRUSTED...` budget. Warm uses ~30s. | Runaway computation                                       |


Missing packages fail when code imports them (no import pre-check at executor init).

> The AST sandbox is not a perfect security boundary; **subprocess isolation** is the real guarantee. Untrusted script strings (including LLM-generated code) are the threat model, not arbitrary hostile users with shell access.



#### Import policy (sandboxed scripts) {#import-policy-sandboxed-scripts}

Prompt text is generated from `[plugin/scripting/import_policy.py](../plugin/scripting/import_policy.py)` (policy and blocklists in `[sandbox.py](../plugin/scripting/sandbox.py)`). It always leads with a **sandbox context prefix** before module lists so models know they are in an AST **Python sandbox** inside a **same-user** venv subprocess (not LibreOffice/UNO). Host pickle frames reconstruct builtin types only.

`writeragent.X` is the script alias for `plugin.X` (`AliasImporter`). It is allowed only when `plugin.X` is on `PUBLIC_PLUGIN_IMPORTS`, or that exact alias is listed (`writeragent.vision`, `writeragent.scripting.duckdb_sql`). A blanket `writeragent.*` is not used: it loaded `plugin.framework.config` and `LlmClient`. `duckdb` is on no blocklist, so `import duckdb` passes without an entry. `plugin.scripting.duckdb_sql` stays off it. The =PY blurb also omits `writeragent.scripting.payload_codec`, `writeragent.scripting.writeragent_api`, and `writeragent.scripting.writeragent_namespace`; those three stay on `PUBLIC_PLUGIN_IMPORTS`. `__future__` and `collections.abc` are exact `BASE_BUILTIN_MODULES` entries. A bare `*` entry is ignored.


| Category                                              | Modules                                                                                                                                                                                                                                                                   |
| ----------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Pre-imported** (no import needed; importing again is harmless) | `np`, `pd`, `sp`, `st` (`scipy.stats`), `plt` (`matplotlib.pyplot`), `math`, `dt` (`datetime`), `re`, `random`, `statistics`, `collections`, `itertools`, `json`, `csv`, `xl` |
| **Blocked host escape** (unless Allow OS access for process/os) | `subprocess`, `_posixsubprocess`, `ctypes`, `_ctypes`, `cffi`, `pty`, `multiprocessing`, `_multiprocessing`, `builtins`, `importlib`, `_frozen_importlib`, `_frozen_importlib_external`, `zipimport`, `runpy`, `pickle`, `marshal`, `shelve`, `dill`, `cloudpickle`, `posix`, `nt`, plugin internals (`plugin`, `writeragent.framework`) |
| **Blocked networking** (unless Allow network access)  | `socket`, `requests`, `urllib`, `urllib3`, `http`, `httpx`, `ssl`, `_socket`, `_ssl`, `httpcore`, `h11`, `h2`, `aiohttp`, `websockets`, `websocket`, `paramiko`, `ftplib`, `smtplib`, `poplib`, `imaplib`, `nntplib`, `telnetlib`, `xmlrpc`, `socketserver`, `dns` |
| **Blocked file read/write access** (unless Allow file access) | `shutil`, `tempfile`, `sqlite3`, `open` (all modes, including builtin, `io`, `codecs`, `_pyio`), `pathlib` reads and mutations, `glob` / `fileinput` / `linecache`, path opens in `gzip` / `bz2` / `lzma` / `zipfile` / `tarfile`, `logging` file handlers, and `os` `listdir` / `scandir` / `stat` / `walk` |
| **Allowed by default**                                | Safe stdlib (`io`, `logging`, `glob`, `zipfile`, `gzip`, `bz2`, `lzma`, `pathlib`, `asyncio`, `inspect`, `sys` reads, `signal`, `concurrent.futures`, `json`, `csv`, `math`, `datetime`, …; file operations gated) and all installed packages (numpy, pandas, scipy, matplotlib, etc.) |

##### Sandbox settings & permissions {#sandbox-settings-permissions}

By default, every flag is off. Users can selectively enable capabilities under **Settings → Python** (blocked-import errors name the matching setting):

- **Allow network access (`scripting.python_allow_network`)**: Blocks network imports (`socket`, `requests`, `http`, `urllib`, `ssl`, …) unless checked.
- **Allow file access (`scripting.python_allow_files`)**: Blocks file read/write access (`open`, `io`, `pathlib`, `shutil`, `tempfile`, `sqlite3`, `glob`, and `os` file calls such as `listdir` and `stat`) unless checked. In-memory buffers (`io.BytesIO`, `io.StringIO`) stay available.
  - The `_FileOsProxy` exposes path and filesystem operations. By default (when `python_allow_os` is false), it exposes `os.path`, cwd, environ, and constants; listing and `stat` require the file flag; mutating calls require it too. `os.path` existence checks stay available.
- **Allow OS access (`scripting.python_allow_os`)**: Allows raw `os`, `subprocess`, `multiprocessing`, and `pty` (run other programs and processes); includes file access. Process spawning via `subprocess.run`, `multiprocessing` (e.g. `Process`, `Pool`), `pty`, `os.system`, `asyncio.create_subprocess_*`, and `ProcessPoolExecutor` is enabled when checked, and blocked at call-level and import-level when unchecked.
- **Extra allowed modules (`scripting.python_extra_modules`)**: Comma-separated names (each also grants `name.*`). Overrides the sandbox blocklist, including `os`, `socket`, and `subprocess`. Rejects invalid identifiers, bare `*`, and names over 62 characters.
- `builtins`, `ctypes`, `importlib`, and serialization modules stay blocked under all flags unless explicitly overridden in extra modules.

##### Known residuals

- **Library-level file I/O**: File I/O through pandas (`read_csv`, `to_csv`), numpy (`load`, `save`), matplotlib (`plt.savefig` to a path), PIL (`Image.open`, `Image.save`), and DuckDB file I/O stay library-level. Stdlib path helpers such as `configparser.read`, `netrc`, `dbm`, `mailbox`, and `wave` are not gated either.
- **Library-level network**: The network flag gates listed network modules (`socket`, `requests`, `urllib`, `http`, `ssl`, etc.). Allowed third-party libraries that embed compiled network clients can perform network I/O.
- Subprocess isolation remains the primary security boundary.




### Trusted extension code in the venv {#trusted-extension-code-in-the-venv}

The **AST sandbox** (`LocalPythonExecutor` + default-allow blocklist and `PUBLIC_PLUGIN_IMPORTS` alias gate) applies only to **user-submitted Python source** — Run Python Script, Calc `=PY()`, optional chat `[python_run_script](../plugin/calc/python/venv.py)`, and similar. It is **not** a blanket restriction on everything that runs inside the warm venv child process.

**Shipped extension code** can use the full venv interpreter (including `open()`, `sqlite3`, `sqlite_vec.load()`, and other modules blocked for sandboxed scripts) when implemented as **shipped, reviewed modules** under `plugin/scripting/`, invoked from the **LibreOffice host** — not from untrusted script strings.


| Layer                    | Interpreter                | Sandbox?                                  | Typical use                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        |
| ------------------------ | -------------------------- | ----------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **LibreOffice host**     | Embedded Python in-process | No NumPy; stdlib + UNO                    | UNO, config, enqueue **maintain** RPC                                                                                                                                                                                                 |
| **User venv worker**     | User’s venv subprocess     | **Yes** for user `code` strings           | `=PY()`, Run Python Script, chat tool                                                                                                                                                                                                 |
| **Trusted venv modules** | Same subprocess            | **No** (normal CPython inside the module) | Reviewed modules such as `[payload_codec.py](../plugin/scripting/payload_codec.py)`, `[calc_functions.py](../plugin/scripting/calc_functions.py)`, embeddings index/search, langdetect RPC |


#### How trusted venv code runs

1. **Ship a normal module** under `plugin/scripting/venv/` (implementation) with a public facade at `plugin/scripting/*.py`, or under `plugin/embeddings/venv/`.
2. **Host calls** `[run_trusted_worker_action](../plugin/scripting/trusted_rpc.py)` with `action: "run_trusted_action"` and a `domain` + `helper` packet — not LLM output.
3. `[worker_harness.py](../plugin/scripting/venv/worker_harness.py)` looks up the domain in `[trusted_action_registry.py](../plugin/scripting/trusted_action_registry.py)` and calls the dispatcher **directly** (zero AST). Long-running maintain jobs can stream heartbeats (`allow_heartbeat`).
4. **Run Python Script templates** execute visible helper calls (e.g. `convert_quantity(data, "m/s", "km/h")`). The host injects `data` / `text` / `image` before execution; results use insert handlers in `[domain_registry.py](../plugin/scripting/domain_registry.py)`.
5. **Bulk data** travels in the action `data` dict (Pickle5). Trusted code opens host-supplied paths (for example the per-folder `corpus.db`).

**Embeddings worker pool:** Folder maintain, hybrid search, and grammar **Local (langdetect)** use `WORKER_POOL_EMBEDDINGS` — a second warm venv child isolated from Calc `=PY()` ([embeddings.md](embeddings.md#dedicated-embeddings-subprocess)). Registry / dispatch detail: [scripting-domain-debt-dev-plan.md](scripting-domain-debt-dev-plan.md).

#### What not to do

- **Do not** tell sandboxed scripts (or the chat model) to `open()` index paths or import `sqlite3` — blocked by design ([import policy](#import-policy-sandboxed-scripts)).
- **Do not** widen the script blocklist or alias gate to “fix” embeddings; add a trusted module instead.
- **Do not** run sqlite-vec or NumPy encode in LibreOffice’s embedded interpreter — stay on the venv side ([embeddings](embeddings.md#why-numpy-stays-in-the-venv)).

### Specialized domain

Tool: `python_run_script` with `specialized_domain = "python"`. Registered for Calc; exposed in Writer/Draw via cross-cutting delegation when the LLM activates the python toolset (`delegate_to_subagent(domain="python")`), same pattern as other specialized domains.

### Tool schema (reference) {#tool-schema-reference}

See `[plugin/calc/python/venv.py](plugin/calc/python/venv.py)` — parameters `code`, optional `data` / `data_range` (Calc); `long_running` / async execution.

---



## 6. The `=PY()` Calc function

You can run Python from Calc via `=PY()`. Same warm worker as **Run Python Script…** (`[venv_worker.py](plugin/scripting/venv_worker.py)`). Configure **Settings → Python** → `scripting.python_venv_path` ([§3](#3-user-guide)).

### Formula parameters

IDL: `any python( [in] string code, [in] any data );` in `[extension/idl/XPythonFunction.idl](../extension/idl/XPythonFunction.idl)`. Rebuild `[extension/XPythonFunction.rdb](../extension/XPythonFunction.rdb)` and `[extension/XPromptFunction.rdb](../extension/XPromptFunction.rdb)` after IDL changes (`scripts/rebuild_xprompt_rdb.sh` — one `.rdb` per interface).


| Arg | Name   | Required | Role                                                                         |
| --- | ------ | -------- | ---------------------------------------------------------------------------- |
| 0   | `code` | Yes      | Python source; evaluated result is returned                                  |
| 1   | `data` | No       | Optional range(s) → `data` / `ranges` ([Data shapes](calc/py-data-shapes.md)) |




### Session modes and recalc semantics {#session-modes-and-recalc-semantics}

Settings → Python → `scripting.python_session_mode`:


| Mode                   | Behavior                                                                                                                                                                                                                                |
| ---------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Isolated** (default) | Each `=PY()` evaluation gets a **fresh** namespace. The **initialization script** still runs once per workbook and its imports/helpers are **seeded** into every cell — but variables assigned in one cell do **not** leak to the next. |
| **Shared kernel**      | One **persistent global namespace** per workbook (`calc:…` session). Any cell can read or overwrite any name set by any other cell. **Reset Python Session** clears it (including the session DuckDB catalog). Run Python Script uses the same Calc session, or `rps:…` on Writer/Draw, so `wa.scripts` / `wa.doc` library caches stay on that document. |


Optional chat runs always use isolated execution (not workbook session mode) — see [Using the chat assistant](#using-the-chat-assistant-optional).

> [!IMPORTANT]
> **Mental model (Shared kernel):** Calc may recalculate cells in **any order** — not row-major. The only ordering guarantee you get for free is **init script before any cell**. Assume each cell **can run zero, one, or many times** per workbook session. Write **idempotent** code (safe to re-run).



#### Excel vs shared kernel: persistence and ordering

Microsoft Python in Excel keeps one global namespace per workbook. F9 / auto-recalc does **not** reset it — globals persist until **Reset Runtime**. Excel compensates with **co-volatility**: when any `=PY` cell recalculates, **all** PY cells re-execute in row-major order ([co-volatility overview](https://fastexcel.wordpress.com/2023/11/01/python-in-excel-py-calculation-globals-co-volatility/)).

**Shared kernel** matches persistence (no auto-reset on F9) but **does not** co-volatile all Python cells. Calc uses its native **dependency DAG**: only dirty cells and their dependents recalculate. That sounds weaker than Excel until you use `data` **as a dependency edge** — see below.


| Approach                        | Ordering mechanism              | Partial recalc              |
| ------------------------------- | ------------------------------- | --------------------------- |
| Excel `=PY` + globals           | Row-major + all PY cells re-run | Heavy (co-volatility)       |
| Shared kernel + `data` **refs** | Calc DAG (precedents first)     | Only dirty subgraph recalcs |




#### Why pass upstream cells as `data`

Excel's shared globals depend on **sheet position** and co-volatility. Here the `data` **argument declares a Calc dependency.** When cell `B1` uses a global set in `A1`, still write:

```calc
=PY("result = x + 1"; A1)
```

Calc tracks that `B1` depends on `A1` and runs `A1` **before** `B1` — no Python string parsing, no co-volatility tax. Chain pipelines (load → clean → aggregate → plot) by passing each stage as `data`, even when cells are not adjacent or not in row-major order.

Excel’s *saved* static bridges already put ranges on `_xlws.PY` trailing args (Excel→PY dirtying works); co-volatility is still how Excel orders **PY↔PY** / shared globals, not a substitute for DAG partial recalc among PY cells. Prefer `=PY(code, data)` over a naive UI-shaped `xl()`-inside-string Calc port ([§7 comparison](#microsoft-python-in-excel-vs-writeragent); package details in [ms-py §5.8](scripting/ms-py-compatibility.md#58-ooxml--xlfnpy-import)).

#### Rules of the shared namespace


| Rule                                 | Meaning                                                                                                                                                                                                                                                    |
| ------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **One namespace per workbook**       | The `calc:…` session lives until **Reset Python Session**, worker crash/restart, init-script change (re-seed) or removal, or **document unload**. The first `=PY()` for a workbook registers [`workbook_lifecycle.py`](../plugin/calc/python/workbook_lifecycle.py) so close/reopen runs the init script again. |
| **Any cell can clobber any name**    | True shared mutable globals — cell B1 can overwrite a name from A1.                                                                                                                                                                                        |
| **Order via** `data`**, not layout** | `result = x + 1` with **no** second arg has **no** ordering guarantee. Pass upstream cells/ranges as `data`. Init script always runs before any cell.                                                                                                      |
| **Runs any time**                    | Partial recalc, matrix spill, manual F9 — treat every cell as **restartable**.                                                                                                                                                                             |
| **Escape hatch**                     | **Reset Python Session** clears workbook namespace, wipes worker executors, closes the session DuckDB connection, and re-seeds helper functions.                                                                                                           |
| **Yellow recalc contract**           | `=PY()` formula recalculations run in a synchronous host dispatch context (`sync_host_dispatch`). Off-main recalc worker threads never query UNO desktop or document components; session IDs and init kwargs are resolved from UI-thread cached state.          |



| When state clears                               | When state persists                               |
| ----------------------------------------------- | ------------------------------------------------- |
| **Reset Python Session** (namespace + session DuckDB) | F9 / automatic / partial recalc                   |
| Worker subprocess restart or crash              | Names from cells that did not recalc this pass    |
| Init script changed (re-seed) or removed        | Until user resets (matches Excel: no reset on F9) |
| Document close (`OnUnload` — sends `reset_session` only when pool child is alive) | No child means kernel is already gone; close does not start one |




#### Authoring guidelines

1. **Wire dependencies with** `data` — pass upstream cells/ranges as the second arg so Calc's DAG runs precedents first; keep one-off setup in the **initialization script** (runs once per workbook — see [Initialization scripts](#initialization-scripts)).
2. **Avoid unbounded accumulation** — `mylist.append(x)` every recalc grows forever unless intentional.
3. **One-time expensive work** belongs in the init script, not repeated in every cell.
4. **Side effects** (sheet writes, files, shapes) should be idempotent or clearly intentional on re-run.



#### Initialization scripts and helper re-seeding {#initialization-scripts}

Workbook initialization scripts can be defined via **Edit Initialization Script…** (Calc only). This stores a workbook startup script in document properties (`calc:…:init`). It runs once per workbook session even when the session mode is **Isolated**, making its imports and helper functions available to all `=PY()` cells.

Saving in the init editor only updates the document property. The new script takes effect on the next `=PY()` evaluation, when the worker sees the new script hash and re-seeds; saving does not reset the workbook session. Blanking or removing the script also takes effect on the next `=PY()`: in Shared kernel the worker drops the old init names together with the workbook namespace and its DuckDB catalog, as a changed script does. Cells that do not recalculate keep their displayed values. If the stored scripts cannot be read, nothing is dropped.

In the worker sandbox:
- Functions and variables defined in the init script are executed in the companion `calc:…:init` session and snapshot into cell executors.
- The init executor does not receive data, ranges, xl, host bindings, scoped_dir, or the injected names session_duckdb, run_sql, and invalidate_session_tables. Those are applied to the cell after the snapshot is copied. An init assignment to one of those names is not copied into the cell.
- A shared-kernel init script that needs the workbook catalog imports session_duckdb from writeragent.scripting.duckdb_sql. The sandbox session is already the workbook id while init runs, so that call uses the same catalog as later cells. An isolated execute has no persistable session id, so a connection opened in init is not the cell's connection.
- A class statement, and any last value that cannot cross the pickle boundary (a helper instance, a file, a lock), does not fail the init script or clear the init session. The names stay and are seeded into cells. The init result itself is still discarded. A cell whose value is a class, including `result = SomeClass`, returns `None`, same as a `def`. A cell whose value is an instance errors when no pyplot figure is open, and that cell's prints are kept. If that discarded init value leaves a pyplot figure open, the figure is closed and the init session is kept. The next cell does not return that figure.
- An open pyplot figure replaces a non-image cell result, including one that cannot cross the pickle boundary (the return value of `plt.hist`, `plt.boxplot`, `plt.pie` or `plt.colorbar`). result = 42 in a cell that also leaves a figure open returns the figure.
- A def in the init script or a cell is stored on executor state, which is what the cell calls. After the script finishes, callables whose names are not cell-owned (data, ranges, xl, result, scoped_dir, session_duckdb, run_sql, invalidate_session_tables) are copied into custom_tools. Named libraries (wa.scripts / wa.doc) evaluate with a fresh state and that dict, so they can call those helpers. Those tools are not written back onto state.
- When **Reset Python Session** is invoked, both the base session (`calc:…`) and the companion `:init` session are dropped in the worker process, and the initialization script is immediately re-executed and re-seeded so helpers remain available. If the workbook has a file URL and still stores an `unsaved:` id, Reset also drops that leftover kernel (`calc:`, `rps:`, `notebook:`, and `ppt_master:`) before the URL session. A workbook that has never started a worker session shows a dialog and does not create one.

Example helpers (no special `excel` module needed; use auto-imported `np`/`pd`/`st`/`plt`/`dt`/`xl` or explicit imports):

```python
def format_currency(series):
    return series.apply(lambda x: f"${x:,.2f}")

def kpi_summary(df, metrics):
    return df[metrics].agg(["mean", "min", "max"]).round(2)
```



#### What “idempotent” means

A cell is **idempotent** when running it again (F9, edit elsewhere, partial recalc) produces the **same intended outcome** — it does not keep adding unwanted changes.


| Pattern                                 | Idempotent? | Why                               |
| --------------------------------------- | ----------- | --------------------------------- |
| `result = data * 2`                     | Yes         | Same inputs → same `result`.      |
| `result = df.groupby("col").sum()`      | Yes         | Derives from current `data` only. |
| `runs += 1; result = runs`              | No          | Counter grows on every recalc.    |
| `cache.append(x)` with no reset         | No          | List grows each invocation.       |
| Write a file / insert a shape every run | Usually no  | Re-run duplicates side effects.   |


Deliberate accumulation (running totals, etc.) is fine — treat it as a choice, not an accident. When in doubt, compute `result` from `data` and init helpers; use **Reset Python Session** for a clean slate.

**Not reset automatically:** F9, Ctrl+Shift+F9, or editing one cell does **not** clear the shared kernel.

#### Session DuckDB (Phase D)

In **Shared kernel**, `session_duckdb()` (injected; also `from writeragent.scripting.duckdb_sql import session_duckdb`) returns one in-memory DuckDB connection per workbook. Injection is the cell executor only. The init script uses from writeragent.scripting.duckdb_sql import session_duckdb, which sees this workbook catalog during a shared-kernel execute. Isolated init does not share that connection with the cell. `query_folder_sql` uses that connection when called from a `=PY()` / RPS cell so later cells can `SELECT` previously registered tables. Isolated mode and chat `query_folder_sql` still open-and-close a connection per request.

Registered tables are snapshots: a cell that passes `data` and re-registers refreshes that name; a query-only cell does not. **Reset Python Session** or `invalidate_session_tables()` drops the catalog. Full staleness table: [calc/duckdb-dev-plan.md § Phase D](calc/duckdb-dev-plan.md#phase-d--shared-kernel-session-cache).

**Implementation:** `[session_manager.py](../plugin/scripting/session_manager.py)`, `[venv/venv_sandbox.py](../plugin/scripting/venv/venv_sandbox.py)` (`SessionStore` / `SESSIONS`), `[venv/duckdb_sql.py](../plugin/scripting/venv/duckdb_sql.py)` (session catalog).

#### `result` vs `print()` (egress model) {#result-vs-print-egress-model}


|                        | Microsoft Python in Excel                 | Calc `=PY()`                                                                                                                               |
| ---------------------- | ----------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------ |
| **Cell value**         | Last evaluated expression (Jupyter-style) | Prefer `result = …`; if unset, last expression (same fallback)                                                                             |
| `print()` **/ stdout** | Diagnostics pane only; cell gets `None`   | Captured in worker response; shown in **LibrePy Python sidebar** diagnostics (shipped); not written into the cell |
| **Top-level** `return` | Syntax error in Excel                     | Use `result = …` instead                                                                                                                   |




### Keyboard shortcuts and recalc {#keyboard-shortcuts-and-recalc}



#### LibreOffice Calc recalc (native)

These are **Calc** shortcuts. They recalculate formulas; they do **not** clear the Python shared kernel unless noted.


| Shortcut          | Calc action                     | Effect on Python session                                                                                                                                                                      |
| ----------------- | ------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **F9**            | Recalculate changed cells       | Re-runs dirty `=PY()` cells; **Shared kernel** globals **persist**                                                                                                                            |
| **Ctrl+Shift+F9** | Hard recalculate (all formulas) | Re-runs all `=PY()` cells; globals **persist**. Use after worker crash/`NameError` to rebuild DAG state ([shared-kernel soft timeout](#calc-ux-backlog)) |
| **Shift+F9**      | Recalculate current sheet       | Same persistence rules as F9, sheet-scoped                                                                                                                                                    |


**Clear Python memory:** **Reset Python Session** (menu). Excel’s analogue is **Ctrl+Alt+Shift+F9** (Reset Runtime) — see target mapping below.

**Matrix formulas:** Confirm multi-cell blocks with **Ctrl+Shift+Enter** ([Matrix formulas](#2-normal-single-cell-formulas-vs-matrix-array-formulas)).

#### Microsoft Python in Excel shortcuts (reference)

Parity targets for Calc `=PY()` UX (Microsoft Python in Excel product docs).


| Excel shortcut                 | Excel action                    | Today                                                                                                        | Target / notes                                                                                                                                              |
| ------------------------------ | ------------------------------- | ------------------------------------------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **=PY** / **Ctrl+Alt+Shift+P** | Start Python cell / editor      | **Shipped** — **Ctrl+Alt+Shift+P** opens **Edit Python in Cell…** (also menu / cell context menu) | Same chord as Excel                                                                                                                                          |
| **Ctrl+Enter**                 | Commit code in editor           | Monaco **Save** (Calc cell editor)                                                                           | Same pattern in Monaco when editing multi-line scripts                                                                                                      |
| **Ctrl+Alt+Shift+F9**          | **Reset Python runtime**        | **Shipped** — **Ctrl+Alt+Shift+F9** → **Reset Python Session**                                               | WriterAgent: [`extension/Accelerators.xcu`](../extension/Accelerators.xcu); LibrePy: [`extension-core/Accelerators.xcu`](../extension-core/Accelerators.xcu) |
| **Ctrl+Alt+Shift+F2**          | Toggle Python editor pane       | Monaco window (separate process)                                                                             | Optional: dock/focus toggle                                                                                                                                 |
| **Ctrl+Alt+Shift+C**           | Toggle plot float vs embedded   | Plots insert as sheet images only                                                                            | Floating plot layer → backlog                                                                                                                               |
| **Ctrl+Alt+Shift+M**           | Toggle Value vs Object return   | Always value egress today; object cards → [Calc UX backlog](#calc-ux-backlog) |                                                                                                                                                             |
| **Ctrl+Shift+F5**              | Open object card preview        | Not shipped                                                                                                  | [Object cards](#calc-ux-backlog)                                                                                                                                                     |
| **Ctrl+Shift+U**               | Expand formula bar              | Calc native (multi-line formula bar)                                                                         | Formula-bar Jedi → [Monaco 2D](scripting/monaco-editor-dev-plan.md#phase-2d--jedi-autocompletion-child-only-performance-sensitive)                                                                                                                                  |
| **F2**                         | Edit vs point mode (range pick) | Calc native cell edit                                                                                        | Monaco **range picker** should use same Enter/Point idea ([Monaco 2C](scripting/monaco-editor-dev-plan.md#phase-2c--calc-range-picker-medium-risk-high-value)) |
| **Ctrl+F2**                    | Focus formula bar ↔ grid        | Calc native                                                                                                  | —                                                                                                                                                           |


WriterAgent **chat** shortcuts (Writer/Calc): **Ctrl+Q** extend selection, **Ctrl+E** edit selection (`[Accelerators.xcu](../extension/Accelerators.xcu)`) — unrelated to `=PY()`, but often used alongside the sidebar.

#### Excel error codes (reference for diagnostics)

Errors surface as **cell text** (and worker `traceback` in logs). LibrePy Python sidebar already shows structured diagnostics; optional glanceable cell traceback remains in [Calc UX backlog](#calc-ux-backlog).


| Excel code                 | Typical cause                     | Today                                                                                   |
| -------------------------- | --------------------------------- | --------------------------------------------------------------------------------------- |
| **#PYTHON!**               | Syntax/runtime error              | Error string in cell; Monaco on manual edit                                             |
| **#SPILL!**                | Blocked dynamic array spill       | Shipped — sets formula cell to `#SPILL!` and highlights blocking cell                   |
| **#TIMEOUT!**              | Exceeded runtime limit            | Timeout message; Settings → `scripting.python_exec_timeout`                             |
| **#BUSY!** / **#CONNECT!** | Cloud kernel (N/A locally)        | Worker spawn failure / venv misconfig                                                   |
| **#CALC!**                 | Payload too large / volatile deps | `python_max_data_cells` cap; avoid `RAND()`/`NOW()` in `data` precedents if problematic |




### Return Types, Coercion, and Matrix (Array) Formulas

The return type in the IDL is declared as `any` to allow a dynamic union of return types, maximizing compatibility with both standard (single-cell) and matrix formulas.

#### 1. The LibreOffice Type-Coercion Quirk (The `#VALUE!` Trap)

LibreOffice Calc operates strictly on double-precision floats (`double`/`float`), strings (`string`/`str`), and booleans (`boolean`/`bool`) for cell values.

- **The issue:** Python integers (`int`) returned from a script are marshaled by PyUNO as a sequence of `long`s (e.g. `sequence<sequence<long>>`).
- **The consequence:** Calc's formula engine lacks type coercion for integer matrices, immediately throwing a `#VALUE!` error in the sheet.
- **The resolution:** Every return value from `=PY()` is recursively filtered through a coercion pipeline (`to_calc_compatible`):
  - `int` -> `float` (coerced to UNO `double`)
  - `None` / `pd.NaT` / `pd.NA` -> `""` (empty cell)
  - `float('nan')` / `np.nan` -> raw NaN (the Calc add-in bridge renders this as a cascading error, typically `#NUM!` or `#VALUE!`)
  - `±inf` and `decimal.Decimal` -> `float` (`±inf` shows `#NUM!` in Calc; Decimal precision loss is accepted)
  - `datetime` / `Timestamp` / `datetime64` -> naive ISO-8601 (tz offset stripped). Timedelta -> fractional days.
  - Other `bool`, `float`, and `str` values are preserved as-is. `py_addin_cell_value` then maps the eight tokens above to formula errors, and `to_calc_compatible` itself still returns those strings.
  - Lists and tuples are recursively converted to tuples of these Calc-supported types.

These coercions are the **complete** Calc type contract. Do not add wire payload kinds for inf, NaT, Decimal, or datetime — see [data shapes](calc/py-data-shapes.md#dates-and-datetimes).

**Note on transport:** `=PY()` and `python_run_script` cross the host↔venv boundary via length-prefixed **Pickle5** frames carrying either a `split_grid` envelope (dense numeric/mixed 2D grids) or plain nested lists (small grids). There is no JSON on the production wire for these payloads (JSON appears only in benchmarks and a few legacy test paths).

#### Empty cells vs NaN

Calc empty cells and Python/NumPy NaN are **not distinguished on the wire**. Ingress blanks become `None` or `np.nan`; egress `None` → empty cell, computed `nan` → cascading Calc error. Prefer `np.nansum` / `np.nanmean` when blanks should be ignored.

Full tables, decision rationale, and author/LLM summary: **[Empty cells vs NaN](calc/py-data-shapes.md#empty-cells-vs-nan)**.

#### 2. Normal (Single-Cell) Formulas vs. Matrix (Array) Formulas

`=PY()` supports both **dynamic auto-spill** from a single cell and **matrix (array) formulas** over a selected block. Every argument after `code` is an input (`data` / `ranges`); there is no special index argument.

- **Auto-spill (Enter, recommended):** Enter a single formula returning a 1D list or 2D grid, e.g. `=PY("result = [sp.prime(x) for x in range(1000, 1006)]")`. When `scripting.python_auto_spill` is enabled (default), Calc spills the result into neighbouring cells automatically.
- **Matrix / array block (Ctrl+Shift+Enter):** Select the output range (e.g. `A1:A6`), enter `=PY("result = [sp.prime(x) for x in range(1000, 1006)]")`, and confirm with **Ctrl+Shift+Enter** (curly braces `{=…}`). Calc evaluates the add-in once for the block and `=PY()` returns a native UNO matrix (`matrix_result`), populating every cell in the block; cells beyond the result length show `#N/A`. This needs LibreOffice 26.8 or later. On 24.8 to 26.2, PyUNO cannot return a matrix, so `=PY()` returns the top-left value: every cell of a `{=PY()}` block shows it and `=SUM(PY(list))` sums only it. Use auto-spill, which works on every version. `write_range(array=true)` refuses `=PY` there. Use auto-spill when mixed text and numbers will be compared or looked up, because Calc does not intern strings in mixed array returns. (Desktop renders a flat list as an N×1 column whereas Collabora Online renders it as a 1×N row, so portable vertical array blocks should return `[[x] for x in …]`.)
- **Scalar return (Enter):** e.g. `=PY("result = 3 ** 8")` or `=PY("result = str([2, 3, 5])")`.

#### Dynamic auto-spill {#dynamic-auto-spill}

Desktop spill is the `scripting.python_auto_spill` setting. Collabora Online single-cell spill: [engine spill plan](calc/collabora-engine-spill-plan.md). Each spill is tracked as a single rectangle from the formula cell. When rows or columns are inserted or deleted before the block, the modify listener confirms the move when the sheet's used-area end shifts by the same delta and remaps the registry without clearing cells. A multi-cell selection does not suppress auto-spill when the formula origin is unique and is not itself a multi-cell array formula. A `{=PY()}` / `{=PYTHON()}` array formula (Ctrl+Shift+Enter, `setArrayFormula`) skips spill because `collapseToCurrentArray` on that origin spans the block, and returns a native matrix directly to Calc (top-left value before 26.8). When the origin cannot be located, spill is not written and `matrix_result` is returned. When a formula seen this session is deleted (origin emptied, used-area end unchanged, zero matches elsewhere, and no user formula inside), the whole rectangle is cleared along with hard number formats; overwriting the origin with a constant, cut/paste, or duplicate formulas drop the record and leave the old block as values. A cell filled after the formula returned but before the spill write blocks the write; the next recalculation shows `#SPILL!`. Edited values inside a spill's rectangle are overwritten on the next spill, while a user formula inside the rectangle blocks the spill (`#SPILL!`). An emptied cell is refilled.

### Usage

```text
=PY("3 ** 8")
=PY("str([sp.prime(x) for x in range(1000, 1006)])")   (Returns as single-cell string)
=PY("np.mean(data)"; A1:A10)
=PY("result = [sp.prime(x) for x in range(1000, 1006)]")  (Auto-spills from one cell, or Ctrl+Shift+Enter over A1:A6 on 26.8+)
=PY("df = data.to_pandas(); result = float(df['Sales'].mean())"; A1:C10)
```



### Sharing Code via Cell References

Instead of typing Python code directly as a string literal inside the `=PY()` formula, **you can pass a cell reference containing the code** (e.g., `=PY(A1; B1:B10)`).

Because the first parameter of `=PY()` is defined in the IDL (`XPythonFunction.idl`) as `string code`, **the LibreOffice Calc formula engine automatically handles evaluation and type coercion of cell references out-of-the-box.** 

No code changes or new APIs (such as `PythonCell()`) are required.

#### Advantages of passing a cell reference for code:

1. **Code Reusability / Single Source of Truth**: You can write a script once in cell `A1` and reference it in dozens of other cells (e.g., `=PY(A1; B1:B10)`, `=PY(A1; C1:C10)`). Updating the logic in `A1` recalculates all dependent cells automatically.
2. **Clean Syntax (No Quote Doubling)**: Inside Calc formulas, double quotes must be doubled to escape them (e.g., `""result = ...""`). Putting code in a cell lets you write clean, standard Python syntax without escaping pain.
3. **Multi-line Scripts**: The standard Calc cell editor supports multi-line text blocks (using `Alt+Enter` to insert newlines). This allows users to write readable, commented Python scripts of arbitrary length.
4. **Dynamic Formulas**: You can use Calc formulas to construct Python code dynamically based on other spreadsheet variables! For example:
  - Cell `A1`: `= "import numpy as np; result = np." & B1 & "(data)"`
  - Changing `B1` from `"mean"` to `"std"` dynamically changes the script executed by `=PY(A1; C1:C10)`.



#### Gotchas & Design Invariants:

- **Empty Code Cells**: If the referenced code cell evaluates to an empty string, the script runner returns `Error: No code provided.`
- **Implicit Intersection**: If a user passes a multi-cell range as the first argument (e.g., `=PY(A1:A2; B1:B10)`), Calc performs implicit intersection using the active row/column. Always pass a **single cell** for the code argument (e.g. `$A$1`).

### Calc formula lexer quirks (inline code) {#calc-formula-lexer-quirks-inline-code}

Calc’s formula compiler parses the cell **before** the `=PY()` add-in runs. Failures here are **not** venv/NumPy/sandbox errors — Python never executes.

ASCII `"…"` string literals are **opaque**. Identifiers like `float` and nested `()` *inside* a proper ASCII-quoted string do **not** become spreadsheet function tokens: `=PY("float(1)")` and `=LEN("float(1)")` succeed when `PY` is registered.

| Symptom | Typical cause | What users see |
| -------- | ------------- | -------------- |
| **#NAME?** | **Unquoted** name + `(` treated as unknown spreadsheet function | `=PY(float(1))` or `=float(1)` → `#NAME?`; `=PY("float(1)")` works |
| **Err:513** | One formula **symbol** exceeds Calc `MAXSTRLEN` (**1024**) | Long inline Python in `=PY("…")` fails before the add-in |
| **Err:508** | Wrong **argument separator** (`;` vs `,`), or **curly quotes** `“…”` used instead of ASCII `"` | XLSX/locale mismatch; pasted smart quotes |
| **Err:510** | Cell text starts with `=` (e.g. section label `=== normal ===`) | Use plain labels like `[normal]`, not leading `=` |
| **#NAME?** | XLSX import lowercases the add-in name; lookup failed on display-only name | Prefer `=PY(...)` (uppercase). The add-in also accepts `python` / `PYTHON`. |

Do not wrap returns in `float()` / `int()` / `str()` for Calc’s sake — [`to_calc_compatible`](../plugin/calc/python/addin_values.py) already coerces NumPy scalars and Python `int` to Calc `double`. `#NAME?` on `float` means the call is **outside** quotes (or quotes were lost / became curly). That is not proof the lexer scans inside ASCII strings.

```text
=PY(float(1))                 → #NAME?   (unquoted float = unknown function)
=PY("float(1)")               → OK       (ASCII string opaque)
=LEN("float(np.sum(data))")   → OK
=LEN(“float(1)”)              → Err:508  (U+201C/U+201D not string seps)
=LEN("<1023+ char string>")   → Err:513  (MAXSTRLEN)
```

#### Recommended patterns (today)

| Pattern | When to use | Example |
| ------- | ----------- | ------- |
| **Bare NumPy / expression** | Default for short inline code | `=PY("np.sum(data)"; B1:B10)` |
| **Code in a cell** | Multi-line / huge scripts (avoids `MAXSTRLEN`) | `A1` = script text (**Save without =PY()**); `=PY($A$1; B1:B10)` (or `Sheet2.A1` / `'My Sheet'.$B$2`). Opening the formula cell edits the code cell; **Data:** on the formula stays editable. |
| **ASCII quotes only** | Always when pasting / generating formulas | Normalize curly `“”` → `"` (WriterAgent does this on parse) |
| **Comma vs semicolon** | Match file/locale | XLSX OOXML → commas; Calc UI often `;` |
| **XLSX test sheets** | Manual serialization regression | See [`scripts/generate_serialization_spreadsheet.py`](../scripts/generate_serialization_spreadsheet.py) |
| **Excel Python-in-Excel `.xlsx`** | Open in Calc with the Python/`=PY` extension | Auto-rewrites to DAG `=PY` on load; `=PY(...)` formulas **>1000 chars** parked on visible `py_code_<Sheet>` at the same A1, shorter stay inline ([`script_bank.py`](../plugin/calc/excel_py_convert/script_bank.py)) — see [ms-py §5.8](scripting/ms-py-compatibility.md#58-ooxml--xlfnpy-import) |

The **Code in a cell** pattern: the chat prompt does not ask the model to emit this two-cell pattern: auto-imports and helpers usually fit in Calc's `MAXSTRLEN` (1024). Revisit `CALC_FORMULA_SYNTAX` if `Err:513` shows up for generated `=PY("…")`.

**XLSX input cells must be numeric, not text:** if the sheet stores values as strings (e.g. `"1.0"` from `str()` in a generator), Calc passes them as text, `split_grid` lands them in the `strings` map, and `np.sum(data)` fails with a Unicode dtype `TypeError`. Regenerate [`serialization_tests.xlsx`](../tests/fixtures/serialization_tests.xlsx) after fixing the generator so ints/floats are written as native cell types.

#### Future product directions (WriterAgent)

These are **not** implemented; kept so design discussions do not rediscover the same traps.

1. **Cell-reference-first UX** — Shipped for the editor: opening `=PY($A$1; …)` edits A1’s text and saves back to A1 (formula stays a ref). A Settings/wizard default to *create* that pattern is still not implemented. Best mitigation for huge scripts until LO raises `MAXSTRLEN`.
2. **LLM / `=PROMPT()` guardrails** — Deferred. Auto-imports and helpers usually fit in 1024 chars; do not lengthen `CALC_FORMULA_SYNTAX` unless generated `=PY("…")` hits `Err:513`. Monaco already follows cell-ref formulas.
3. **Native ODS fixtures** — Shipped: [`tests/fixtures/numpy_domains_demo.ods`](../tests/fixtures/numpy_domains_demo.ods). Use ODS for manual `=PY()` QA (preserves uppercase add-in name; semicolon args). Serialization fixtures use bare `np.sum` / `np.max` (no `float()` wrapper).

### Future LibreOffice formula-string work {#future-libreoffice-formula-string-work}

> **Deferred upstream work** — not in WriterAgent. Schedule as a LibreOffice/Collabora Calc patch when long inline `=PY("…")` / Excel import becomes a product priority.

ASCII-quoted opacity is already correct. Remaining compiler gaps:

1. **Raise / grow string-symbol limit** — `sc/inc/compiler.hxx` `#define MAXSTRLEN 1024` and fixed `cSymbol[MAXSTRLEN+1]` in `sc/source/core/tool/compiler.cxx` (`ScCompiler::NextSymbol`, `ssGetString` / `ssSkipString`). Prefer a growable `OUStringBuffer` for **string literals** with a high soft cap; idents can stay shorter. Symptom today: `Err:513` on long symbols.
2. **Curly / smart quotes** — Normalize U+201C/U+201D (and ideally U+2018/U+2019) to ASCII `"` before or during compile, **or** treat them as `CharString` / `StringSep`. Symptom today: `=LEN(“float(1)”)` → `Err:508`.
3. **Core tests** (e.g. `sc/qa/unit/ucalc_formula.cxx`): `=LEN("float(1)")` / `=LEN("a(b(c))")` stay OK; long string (>1024) succeeds after (1); curly-quoted `LEN` succeeds after (2). Use `LEN`/`CONCATENATE` — no PY add-in required.
4. **Optional Bugzilla** — Attach the three `LEN` reproducers after a patch lands or when filing.

Until then: keep scripts in cells for large Python; WriterAgent normalizes curly quotes on formula parse and may sanitize `float(` when emitting Calc formulas defensively.

### How it runs

Uses the same warm worker as Run Python Script ([§2](#2-strategy-decision)). **Isolated** mode (default): variables do **not** persist across cells. **Shared kernel**: one workbook namespace until reset — [§6 Session modes](#session-modes-and-recalc-semantics).

### Code Oracle (`=PROMPT()` + `=PY()`)

**WriterAgent only.** `=PROMPT("Write a Python formula using numpy for the 95th percentile of B1:B100")` can yield a pasteable `=PY("…")` string — natural-language bridge to data-science formulas without leaving the sheet. Topic hub (shipped args + proposed live ranges): [calc/prompt-function.md](calc/prompt-function.md).

### Comparison with LibrePythonista (`PY.C` and `lp()`)

[LibrePythonista](https://github.com/Amourspirit/python_libre_pythonista_ext) stores code **outside** the formula (`=PY.C(SHEET(), CELL("ADDRESS"), extras?)`) and runs in **LO embedded Python** with pip bootstrap. This design keeps code **in the formula** and runs in the **user venv**.

```mermaid
flowchart LR
  subgraph ourPy [Calc =PY]
    F1["=PY(code, data?)"]
    F1 --> Venv["venv subprocess"]
    Venv --> Inject["inject CalcRange as data"]
  end
  subgraph librePythonista [LibrePythonista PY.C]
    F2["=PY.C(SHEET(), CELL(...), extras?)"]
    F2 --> LO["LO embedded Python"]
    LO --> Editor["code from cell editor"]
    Editor --> LP["lp('range', collapse=...)"]
  end
```

| Capability                     | `data` / `ranges`                      | LibrePythonista             |
| ------------------------------ | ----------------------------------------- | --------------------------- |
| Pass one range                 | Yes — `CalcRange` (always 2D)             | `lp("A1:B10")`              |
| Multiple ranges in one formula | Yes — `data[i]` or `ranges[i]` / loop `ranges` | Multiple `lp()` calls       |
| Named ranges                   | Only as formula args                      | `lp("MyRange")`             |
| Trim empty rows (`collapse`)   | No                                        | `collapse=True` on `lp()`   |
| Typed date columns             | Raw Calc values (user coerces)            | `column_types` + pandas     |
| Return type for ranges         | `CalcRange` / explicit `.to_pandas()`     | `pandas.DataFrame`          |
| Cell context                   | Not exposed                               | `sheetIdx` + `cAddress`     |
| Execution                      | User venv                                 | LO embedded + pip bootstrap |

**What we kept:** formula args + venv NumPy; rectangular orientation via [`CalcRange`](../plugin/scripting/calc_range.py). **What we did not copy:** `PY.C` metadata formula, in-LO pandas bootstrap, mandatory `lp()` for every read.

|                    | `=PY()`                                                             | LibrePythonista                                                 |
| ------------------ | ------------------------------------------------------------------- | --------------------------------------------------------------- |
| Where users edit   | Formula bar, **Edit Python in Cell…** (Monaco), or code-in-cell ref | LibrePythonista menu / Edit Code; cell shows short `=PY.C(...)` |
| Where source lives | In the `.ods` formula (or a referenced cell)                        | Document-side store (`PySourceManager`, etc.)                   |

**Design stance:** treat each `=PY` cell as a **pure function** (`data` in → `result` out). Monaco / code-in-cell helps for long scripts ([Calc UX backlog](#calc-ux-backlog); [Monaco plan](scripting/monaco-editor-dev-plan.md)). Flatpak/Snap spawn patterns for the Monaco child remain tracked in [scripting/monaco-editor-dev-plan.md](scripting/monaco-editor-dev-plan.md) (phase 2F).

### Data shapes (`data` / blanks / varargs)

Trailing formula arguments become `CalcRange` values: `ranges` is always the full list; `data` is that `CalcRange` when there is one arg, or the same list as `ranges` when there are several. Orientation is always 2D (`(1,1)`, `(1,N)`, `(N,1)`, or `(rows, cols)`). Blank vs NaN, dates, pandas DataFrame spill, logicals, and multi-range examples: **[calc/py-data-shapes.md](calc/py-data-shapes.md)**.

### Optional: Python edit dialog (deferred UX)


| Tier      | User sees                          | Code location           | Effort       |
| --------- | ---------------------------------- | ----------------------- | ------------ |
| 0 (today) | Formula bar + Monaco cell editor   | Inside `=PY("…")` or code cell | Done   |
| 1         | Modal XDL edit dialog              | Still in formula        | Small–medium |
| 2         | Short formula + document store key | Outside formula         | Medium       |
| 3         | LibrePythonista-like IDE surface   | LP-scale infrastructure | Very large   |


Tier 1 reuses existing `DialogProvider` / XDL patterns (`[plugin/chatbot/dialogs.py](plugin/chatbot/dialogs.py)`); execution unchanged. Tier 3 is only justified if Calc-native Python becomes a primary product pillar.

---



## 7. Deferred roadmap

### Microsoft Python in Excel vs Calc `=PY()` {#microsoft-python-in-excel-vs-writeragent}

Microsoft runs Python in **cloud containers**. Authors type `xl("…")` literals in the editor; the package stores `pythonScripts.xml` with `xl(%Pn%)` and `_xlws.PY(scriptIndex, returnType, …deps)`. Calc uses a **local venv** with `=PY(code, data?)` — explicit formula args for DAG dependencies (the rewriter maps Excel’s trailing deps onto that shape).

| Feature dimension | Microsoft Excel (`=PY`) | Calc `=PY()` |
| --- | --- | --- |
| **Data ingress** | UI `xl("A1:B10")` → package `%Pn%` + trailing `_xlws.PY` deps | Range as formula arg → `data` / `ranges` |
| **Output egress** | Last expression | Prefer `result = …`; last expression if unset |
| **Dependency tracking** | **Excel→PY:** trailing formula deps (literals rewritten at edit/save; no Python parse at recalc). **PY↔PY:** co-volatility ([ms-py §5.8](scripting/ms-py-compatibility.md#58-ooxml--xlfnpy-import), [jailsafe](scripting/numpy-jailsafe.md)) | Native Calc DAG on `data` args |
| **Multi-range** | Multiple `%Pn%` / trailing deps | Varargs → `ranges` ([data shapes](calc/py-data-shapes.md#multi-range-support-varargs)) |
| **Shared state** | Globals + row-major co-volatility | Opt-in shared kernel + `data` refs ([§6](#session-modes-and-recalc-semantics)) |
| **Runtime** | Cloud sandbox | User venv (offline, any pip packages) |
| **Editor** | Monaco task pane | Monaco via pywebview ([§3](#run-python-script--monaco)) |

**Design stance:** keep explicit `data` + `result`. Deep dive for Collabora/LibreOffice (why not copy co-volatility as the default; file rewrite for Excel packages): **[scripting/ms-py-compatibility.md](scripting/ms-py-compatibility.md)**.

**Excel parity (summary):** dynamic spill, plots, Monaco cell editor, shared kernel + init scripts, and LibrePy sidebar diagnostics are **shipped**. Object cards, rich DataFrame tables, names/tables labels → [Calc UX backlog](#calc-ux-backlog).

### Competitive landscape (pointer)

Google Sheets does not run Python in cells (Apps Script = JS; external Python via APIs). Calc `=PY()` is closer to Excel / Neptyne than to Sheets. Non-goals: gspread, Sheets API sync, Apps Script runtime, cloud `=AI()` cell parity.

Apps Script’s sheet object model remains a useful **API design reference** for future venv↔LO tool RPC sugar — not a runtime to embed. Remaining UX / landscape items: [Calc UX backlog](#calc-ux-backlog).

### Venv ↔ LibreOffice tool RPC

> **Status: Shipped** for **Run Python Script…** and chat `python_run_script`. **Disabled** during Calc `=PY()` recalc (`python_tool_domain=""`). LibrePy omits the generated proxy (`writeragent_api.py`).

[`writeragent_api.py`](../plugin/scripting/writeragent_api.py) is generated from tool metadata (`scripts/generate_tool_proxies.py --output PATH` / `make proxy-stubs`, validated by `--check PATH` / `make proxy-stubs-check`). Generation fails closed: compilation must succeed and `DOMAIN_TOOLS` must be non-empty with `document_list_open` in `DOMAIN_TOOLS["core"]` before writing atomically. Each method docstring is the full tool description plus a Google-style `Args` block (schema description, enum values, and required or optional) so a script or a python specialized agent can inject the proxy without a separate schema dump. Proxy tool names follow `<domain>_<verb>[_<object>]` where `<domain>` is the longest matching entry in `API_DOMAINS` (`scripts/generate_tool_proxies.py`), or omit the app prefix for single-app tools (`read_range`, `apply_content`, `get_tree`). Domain-prefixed tools land on `wa.<domain>.<rest>` (keyword methods get a trailing underscore, e.g. `style_import` → `wa.style.import_`); app-level tools without a domain prefix land on `wa.writer`, `wa.calc`, or `wa.draw` from the app they are registered for (`wa.calc.read_range`, `wa.writer.apply_content`, `wa.draw.get_tree`). LLM tool names never carry `writer_`, `calc_`, or `draw_`. `TOOL_METHODS` maps each tool name to `(namespace, method)`; the inner catalog and `wa.help()` resolve every tool, including `wa.writer` / `wa.calc` / `wa.draw`, through that map only. `DOMAIN_TOOLS` keys are the raw `specialized_domain` strings (or `writer` / `calc` / `draw` / `core` for core tools); one allowlist key can span multiple `wa` namespaces (e.g. `ranges` holds `sort_range` and `named_range_*`). The inner script catalog prints those attribute paths (`wa.sql.query_folder`, `wa.style.import_`) under the raw domain heading (`python/sql:`, `styles:`). `make proxy-stubs` raises `ValueError` on a `writer_`/`calc_`/`draw_` prefix, a cross-app tool with no `API_DOMAINS` prefix, a reserved/builtin/import-shadowing domain in `API_DOMAINS | APP_NAMESPACES`, or two tools in one namespace producing the same method name. Sidebar and chat-mode domains (`writing_plan`, `deep_research`, `brainstorming`, `document_research`, `ppt-master` / `ppt_master`) and the LLM specialized-toolset orchestration tools (`delegate_to_subagent`, `workflow_finished`) are omitted from `DOMAIN_TOOLS` and the proxy methods; those tools stay registered for WriterAgent chat. Copied tool descriptions may still name a delegate as a breadcrumb. `web_research` stays on `wa.web` (in `DOMAIN_TOOLS["core"]`) for scripts. In the venv child (`WRITERAGENT_IS_WORKER=1`) proxies send a `tool_call` Pickle5 frame on stdout; [`PythonWorkerManager`](../plugin/scripting/venv_worker.py) dispatches via [`host_rpc.execute_tool`](../plugin/scripting/host_rpc.py) → `ToolRegistry.execute()` on the LibreOffice main thread and replies on stdin until the code result frame. Chat `python_run_script` pins `ctx.doc` for the turn (`doc:…` in [`session_manager.pin_script_document`](../plugin/scripting/session_manager.py)). That id is host-only, so the child namespace stays isolated, and `wa.draw` / `wa.shape` bind to the chat document instead of the focused window. Optional kwargs default to `None` and are **omitted on the wire**, so tool defaults apply (`apply_content` is a real edit, not a silent `dry_run`, and its `content` accepts a string or a list of strings). NumPy arrays and scalars in proxy arguments are converted to plain lists and scalars before the call.

**Collections:** The five unscoped document-wide namespaces in `COLLECTION_NAMESPACES` (`bookmark`, `frame`, `pivot_table`, `section`, `sheet`) are generated as Python containers (`for item in wa.<ns>`, `len(wa.<ns>)`, `name in wa.<ns>`, `wa.<ns>[name]`, `del wa.<ns>[name]`) instead of flat `list` / `get` / `get_info` / `delete` methods; other verbs on those namespaces stay methods, and scoped (`image`, `named_range`) or positional/multi-key (`slide`, `field`, `table`) families stay plain methods. Each container operation makes one host RPC returning a fresh snapshot of `_Item(dict)` entries whose keys also read as attributes. Families without a `get` tool (`section`, `sheet`, `pivot_table`) read `wa.<ns>[name]` from their `*_list` response, and `pivot_table`'s optional `sheet` filter is omitted from the container form (filter the returned items in Python). On an error from `[]` or `del`, one extra `*_list` call confirms whether the name is absent (`KeyError`) or still present (`RuntimeError` with `.code`). The script catalog and `wa.help()` display `for item in wa.<ns>`, `wa.<ns>[key]`, and `del wa.<ns>[key]` for those tools.

**Results and errors:** Every flat `wa.*` method unwraps its response through `_result`: when `status == "ok"` that key is dropped (other statuses such as `"skipped"` or `"partial"` stay readable), and the dict is wrapped in `_Item` so keys also read as attributes (`result.text == result["text"]`). When `status == "error"` or the response is not a dict, `_tool_failure` raises `RuntimeError` with `.code` (or `plugin.framework.ipc.UserStopped` when `code == "USER_STOPPED"` so `except Exception` cannot swallow Stop). `to_wire` serializes `_Item` back to a plain `dict` on the pickle pipe so host unpickling never sees `_Item`.

**Paged lists:** Non-collection `list` tools whose optional integer start parameter is in `PAGED_START_PARAMS` (`start_index` or `offset`: `comment`, `page_comment`, `tracking`) keep their `list(...)` method and also get a namespace `__iter__` (`for c in wa.comment`). Iteration fetches one page per host RPC lazily via `_iter_pages` (each page is a fresh snapshot; collect `list(wa.<ns>)` before deleting in a loop) and passes no optional arguments.

**Usage:**

```python
import writeragent as wa

wa.writer.apply_content(
    content=["<h1>Hello</h1>", "<p>Inserted from the venv.</p>"],
    target="end",
)
```

The shipped **Universal Sample** script (default in Run Python Script…) does the same: Writer HTML, Calc `wa.calc.insert_html`, plus a shape — each call on one line, top-level (no `def run()`). The sandbox already sets `__name__ == "__main__"` (`LocalPythonExecutor`). `python_run_script` cannot be called from a script (it would re-enter the warm worker).

**Named libraries:** `wa.scripts.Helpers.add(1, 2)` loads defs from a My Scripts title; `wa.doc.Cover.format(...)` from This Document. Titles are massaged to identifiers (`python_identifier_from_script_name`); use `wa.scripts["odd name"]` for the stored title. Only `def` / `class` / imports / name assignments are imported, plus a module-level unpack that only binds names (`a, b = 1, 2`, `a, *rest = xs`, nested name tuples or lists). Attribute and subscript targets are rejected. A call or `:=` that runs while the library loads — including a class base, keyword, class-body value, class-body assignment target, default, or annotation — is rejected. A class-body `obj.x = 1`, `obj.x: int = 1`, or `data[0]: int = 1` stays when the target expression has no call or `:=`. `make().x = 1`, `make().x: int`, `make()[0]: int = 1`, and `data[make()]: int = 1` are rejected. Decorators are rejected, including `@staticmethod` and `@property`. `async def`, a nested class, and a class-body unpack are rejected (class-body subscript assignment is supported). A class docstring is the first statement only. Bare module-level `x: int` is rejected; `x: int = 1` is kept. Calls and `:=` inside defs and lambdas stay. Top-level expressions such as `wa.writer…` are omitted when the library still has a definition, and they are an error when they are the whole library. The cached namespace is the same object for the life of that executor, so an attribute write is visible to later cells on the shared session until the script hash changes. The host sends the body once per library per Run; later calls in that execute are local. The next Run rechecks a content hash (no body if unchanged) so Shared kernel picks up edits without Reset. `=PY()` recalc uses an empty allowlist, so it cannot list or read named-script source. **Shared kernel** (`scripting.python_session_mode`) keeps compiled defs on a document-keyed session (`calc:…` or `rps:…`) so two Writer files do not share libraries. Isolated mode still caches for a single Run. Domain-scoped allowlists (`python_tool_domain="writer"` / `"footnotes"`) are enforced on the host; they are not a new IPC type. When `writeragent_api` is absent (LibrePy), `resolve_allowed_tools` returns `None` (unrestricted) for a domain string instead of an empty set that would disable every tool call. `python_tool_domain=""` still disables tool RPC for `=PY()`. Follow-on sugar (`sheet.range("A1:B2").values = matrix`) only after this RPC stays stable.

### Calc UX backlog {#calc-ux-backlog}

Not shipped unless noted. Monaco editor gaps live in [scripting/monaco-editor-dev-plan.md](scripting/monaco-editor-dev-plan.md) (2B–2F, Phase 3 formula-bar polish).

#### Object cards

Complex returns (DataFrame, dict, class) should show a compact cell label (e.g. `[DataFrame 150×4]`) and an inspect dialog — not `#VALUE!`.

- Object references in shared-kernel session (`__pyobj_N__`; object stays in worker namespace)
- Worker `inspect_object` → shape, dtypes, `head(5)`
- XDL preview dialog ([`dialogs.py`](../plugin/chatbot/dialogs.py) patterns); optional Spill to Grid on top of matrix formulas
- Touch: [`venv_sandbox.py`](../plugin/scripting/venv_sandbox.py), [`worker_harness.py`](../plugin/scripting/venv/worker_harness.py), [`function.py`](../plugin/calc/python/function.py)

#### AI code synthesis

- Context-aware `=PY()` generation (nearby ranges/headers when chat builds formulas)
- Formalize `=PROMPT()` → pasteable `=PY("…")` template
- Stronger multi-step analysis prompts (clean → stats → chart)
- Touch: [`venv.py`](../plugin/calc/python/venv.py), [`prompt_function.py`](../plugin/calc/prompt_function.py)

#### Unphased items

| Item | Notes / touch |
|------|----------------|
| **DataFrame → rich Calc table** | Headers, formats, filters — distinct from object cards |
| **JSON `result` envelope** | Multi-cell agent updates via `__wa_payload__`; `payload_codec` + host apply |
| **Inline result preview** | Stdout/thumbnail under cell |
| **Formula-bar Jedi** | [Monaco 2D](scripting/monaco-editor-dev-plan.md#phase-2d--jedi-autocompletion-child-only-performance-sensitive) |
| **Named ranges / structured tables / `headers` in `data`** | [`calc_addin_data.py`](../plugin/calc/calc_addin_data.py) |
| **Label preservation** | First row/column as pandas Index when requested |
| **Spreadsheet → Python import** | Prototype / low priority — [calc/spreadsheet-to-python-import.md](calc/spreadsheet-to-python-import.md) |
| **Worker idle shutdown / per-formula `timeout_sec`** | Global timeout is Settings → Python; per-cell timeout and idle worker teardown are not shipped |
| **Python edit dialog tiers 1–3** | [§6 deferred UX](#optional-python-edit-dialog-deferred-ux) |
| **Range alignment for multi-range NumPy** | Mismatched shapes before `np.corrcoef` — [data shapes deferred](calc/py-data-shapes.md#deferred-upgrades) |
| **Shared-kernel soft timeout / invalidation** | Prefer `SIGINT` then `SIGKILL`; user **Ctrl+Shift+F9** rebuilds DAG after worker wipe — [`session_manager.py`](../plugin/scripting/session_manager.py), [`venv_worker.py`](../plugin/scripting/venv_worker.py) |
| **Blank side-channel** | Pass-through blanks stay empty — [data shapes deferred](calc/py-data-shapes.md#deferred-upgrades) |
| **Cell-level traceback snippet** | Short trace in cell error string; full trace already in LibrePy sidebar |
| **Mito-style action recorder** | GUI → pandas in Monaco; low priority |
| **Dynamic sidebar controls from sheet context** | A2UI-style; low priority |
| **Shared-kernel memory bounds** | LRU large DataFrames; defer until OOM reports |

### Other deferred pointers

- Serialization performance: [scripting/numpy-serialization.md — Future work](scripting/numpy-serialization.md#future-work--serialization-performance)
- Jupyter Writer `.ipynb` import + ▶ run: [writer/jupyter-notebook-import.md](writer/jupyter-notebook-import.md)
- Monaco remaining phases: [scripting/monaco-editor-dev-plan.md](scripting/monaco-editor-dev-plan.md)
- Domain roadmaps: [scripting/numpy-domains.md](scripting/numpy-domains.md)

---

## 8. Collabora Online and jail-safe execution

See [scripting/numpy-jailsafe.md](scripting/numpy-jailsafe.md) for Collabora Online / jail-safe NumPy. Desktop LibrePy **reads** Collabora-saved `=PY()` by rewriting `GETPY` OriginalNames on open ([`collabora_formula.py`](../plugin/calc/python/collabora_formula.py)); it does not register Collabora's UNO package.

---



## 9. Implementation status

Remaining work lives in the child doc (or [§7 backlog](#calc-ux-backlog)). Do **not** add `split_grid` kinds for inf, NaT, Decimal, datetime64, empty DataFrames, or MultiIndex — [calc/py-data-shapes.md](calc/py-data-shapes.md).

| Area | Where to look |
| --- | --- |
| Calc UX (object cards, named ranges, soft timeout, edit-dialog tiers, …) | [§7 backlog](#calc-ux-backlog) |
| Domain helpers (Geospatial / Audio remaining; Analysis, Viz, SymPy, Units, Forecast, Text, Optimize, Quant shipped or partial) | [scripting/numpy-domains.md](scripting/numpy-domains.md); SageMath: [sagemath-integration-dev-plan.md](sagemath-integration-dev-plan.md) |
| Serialization / Cython `vec_pack` download | [scripting/numpy-serialization.md](scripting/numpy-serialization.md#future-work--serialization-performance) |
| Venv ↔ LO tool RPC | [§7](#venv--libreoffice-tool-rpc) (Run Python Script / chat shipped; `=PY()` disabled) |
| Collabora Online (Steps A–C landed; plot / Monaco remain) | [scripting/numpy-jailsafe.md](scripting/numpy-jailsafe.md), [online#16010](https://github.com/CollaboraOnline/online/issues/16010) |
| Monaco editor remaining phases | [scripting/monaco-editor-dev-plan.md](scripting/monaco-editor-dev-plan.md) |
| Jupyter Writer import (Phase 1: import + ▶ run, ATX markdown, output replace) | [writer/jupyter-notebook-import.md](writer/jupyter-notebook-import.md) |


## Open

- `[feature M many]` Object cards: show a compact cell label such as `[DataFrame 150×4]` for complex `=PY()` returns and an inspect dialog with shape, dtypes and head, instead of `#VALUE!`. (§ Calc UX backlog > Object cards)
- `[feature M many]` Context-aware `=PY()` generation from nearby ranges and headers, a pasteable `=PY("…")` template from `=PROMPT()`, and stronger multi-step analysis prompts. (§ Calc UX backlog > AI code synthesis)
- `[feature M few]` Rich Calc table formatting/AutoFilter when returning a DataFrame from `=PY()`, and optional pandas `Index` preservation on egress (`CalcRange.to_pandas` in `plugin/scripting/calc_range.py` already supports `header_row`/`index_col` on ingress, and `result_to_calc_grid` spills headers + body as a plain grid, while `plugin/scripting/venv/to_wire.py` drops the index with `index=False` and `plugin/calc/python/spill.py` applies only temporal number formats). (§ Calc UX backlog > Unphased items)
- `[feature M few]` JSON `result` envelope (`__wa_payload__`) so one `=PY()` or agent script can update several cells through `payload_codec` and a host apply step. (§ Calc UX backlog > Unphased items)
- `[feature S many]` Short traceback snippet in the cell error string and an inline stdout or thumbnail preview under the cell. (§ Calc UX backlog > Unphased items)
- `[feature M few]` Per-formula `timeout_sec` and idle-worker teardown; today only the global Settings → Python timeout exists. (§ Calc UX backlog > Unphased items)
- `[eng M few]` Shared-kernel soft timeout that sends `SIGINT` then `SIGKILL` and invalidates the DAG state, plus LRU bounds on large DataFrames kept in the shared namespace. (§ Calc UX backlog > Unphased items)
- `[feature M few]` Named ranges, structured tables and `headers` passed into `data` for `=PY()`. (§ Calc UX backlog > Unphased items)
- `[fidelity M few]` Align mismatched multi-range shapes before NumPy calls such as `np.corrcoef`, and keep pass-through blanks empty via a blank side-channel. (§ Calc UX backlog > Unphased items)
- `[feature L few]` Python edit dialog: a modal XDL editor (tier 1), a short formula plus a document-store key (tier 2), and a LibrePythonista-style IDE surface (tier 3). (§ Optional: Python edit dialog (deferred UX))
- `[feature S few]` Settings or wizard default that creates the code-in-a-cell `=PY($A$1; …)` pattern for long scripts. (§ Future product directions (WriterAgent))
- `[feature L few]` Python editor gaps: syntax squiggles, a Calc range picker and full Jedi autocompletion in the CodeMirror/Monaco child. (§ Run Python Script & the Python editor)
- `[fidelity L few]` LibreOffice/Collabora compiler patch: grow the 1024-character `MAXSTRLEN` string-symbol limit and accept curly quotes, with `LEN` core tests. (§ Future LibreOffice formula-string work)
- `[feature M few]` Floating plot layer and the unshipped shortcuts for plot float vs embedded, Value vs Object return, and object card preview. (§ Keyboard shortcuts and recalc)
- `[eng M few]` Remaining serialization performance work (the Cython `vec_pack` download and accelerator loading are shipped via `run_vec_pack_download` in [`plugin/scripting/native_binaries.py`](../plugin/scripting/native_binaries.py) and `load_cython_accelerator` in [`plugin/scripting/payload_codec.py`](../plugin/scripting/payload_codec.py); profiling inside LibreOffice, opaque `split_grid` pass-through, `float32` envelope, and worker payload caching remain open in [numpy-serialization.md](scripting/numpy-serialization.md)). (§ Other deferred pointers)
- `[feature L few]` Remaining domain helpers for geospatial and audio. (§ 9. Implementation status)
- `[feature L few]` Collabora Online jail-safe `=PY()` plot and Monaco support. (§ 9. Implementation status)
- `[idea M few]` Low-priority ideas: a Mito-style GUI-to-pandas action recorder, A2UI-style dynamic sidebar controls from sheet context, spreadsheet-to-Python import, and `sheet.range("A1:B2").values = matrix` sugar once the tool RPC is stable. (§ Calc UX backlog > Unphased items)

# WriterAgent: A Professional-Grade AI Platform for LibreOffice

**Author**: WriterAgent Team
**Date**: August 2026

---

## Executive Summary

WriterAgent is a sophisticated, high-performance platform that bridges the gap between modern Artificial Intelligence and the complex, legacy environment of LibreOffice (Writer, Calc, and Draw).

While many AI tools struggle to interact with desktop software reliably, WriterAgent uses advanced systems engineering—similar to a mini-operating system—to provide a seamless, robust, and semantically-aware assistant. The project ships as **three standalone extension packages** (`WriterAgent.oxt`, `LibrePy.oxt`, `LibreHarper.oxt`), installed one at a time:

- **WriterAgent** — the full stack: AI sidebar chat, multi-turn tool calling, `=PROMPT()`, autonomous web research, Calc → Python converter, and an embedded MCP server.
- **LibrePy** — the stable Python/NumPy compute core: `=PY()`, warm venv worker, Monaco editor, Jupyter `.ipynb` import, domain helpers, and OCR, with no AI or API keys.
- **LibreHarper** — a standalone offline Harper grammar engine for Writer.

![State Machine Architecture](../Showcase/full_super_unified_complete.png)

*Unified state machine for AI tool interactions. Related: [chat sidebar](chat/sidebar-implementation.md), [streaming & threading](framework/streaming-and-threading.md), [formal verification](framework/formal-verification.md), [LLM hacks](chat/llm-hacks.md), [test architecture](archive/test_architecture_analysis.md).*

The remainder of this document explains *how* the platform works under the hood: the state machine that governs every interaction, the threading model that keeps LibreOffice responsive, the subprocess bridge that safely runs NumPy, and the engineering standards (formal verification, static analysis, evaluation) that keep a codebase of this complexity tractable.

---

## Why WriterAgent is a Sophisticated Product

### 1. The "Platform" Architecture (The Nervous System)

#### A Pure State-Machine Core

Every interaction in WriterAgent is driven by a **pure finite-state machine (FSM)**. Each state is a `frozen=True` dataclass subclassing `BaseState`; each transition is a pure function `next_state(state, event) -> FsmTransition` that returns the next state plus a list of **effects** (side-effect *descriptions*, interpreted outside the FSM). The invariant is strict: `next_state` performs **no UNO calls and no I/O**—it only computes. Side effects (UI updates, document work, HTTP) are executed later by an interpreter (`tool_loop_actions.py`) against session handles.

This pattern is applied to the hardest orchestration paths, each with its own typed state machine:

| Machine | File | States / Events |
|---------|------|-----------------|
| Tool loop | `plugin/chatbot/tool_loop_state.py` | `ToolLoopState` (`round_num`, `pending_tools`, `is_stopped`, `max_rounds`), `EventKind` (`STOP_REQUESTED`, `STREAM_DONE`, `NEXT_TOOL`, `TOOL_RESULT`, `FINAL_DONE`, `ERROR`) |
| Send button | `plugin/chatbot/send_state.py` | `SendButtonState` (`is_busy`, `is_recording`, `has_text`, `has_audio`), events for text/record/send/stop lifecycle |
| Send handler | `plugin/chatbot/state_machine.py` | Chat-send orchestration |
| MCP | `plugin/mcp/mcp_state.py` | Tool-call routing, error and response effects |
| Audio recorder | `plugin/chatbot/audio_recorder_state.py` | Record/stop/auto-stop lifecycle |
| MCP tunnel | `plugin/mcp/tunnel_state.py` | Reconnection with explicit `StartProcessEffect`/`ScheduleRetryTimerEffect`/`NotifyUrlAcquiredEffect` |

Previously these paths mixed implicit instance-field state with threads, UNO, and I/O—hard to reason about and expensive to test. Pure transitions are now unit-testable without a running office, and the "Stop vs. stream completion" and "Send/Stop mutual exclusion" races are enforced as contracts.

#### Formal Verification of State Invariants

The FSMs are not merely hand-tested; they carry **design-by-contract** assertions verified with **Hypothesis** (property-based) and **CrossHair** (concolic execution). Contracts use `@deal.pre` / `@deal.post` / `@deal.ensure` (via a no-op `deal_shim.py` at LibreOffice runtime, the real `deal` under pytest). Examples of shipped `@deal.ensure` contracts on `next_state`:

- `STOP_REQUESTED` must always produce an `ExitLoopEffect` and set `is_stopped`.
- `is_stopped` is **sticky**; pending tools never shrink while stopped, and never spawn a `SpawnToolWorkerEffect` after stop.
- `round_num` is bounded by `max(round_num + 1, max_rounds)`.
- Send/Stop mutual exclusion: never `is_busy and is_recording` simultaneously.

CrossHair runs `check` (contract-violation search) and `cover` (coverage example generation) as budgeted partial exploration; the `next_state` functions are marked `# crosshair: off` and verified via `@deal.ensure` + Hypothesis oracles instead. Contracts and strategies live in `tests/chatbot/fsm_hyp_support.py` and `tests/chatbot/test_fsm_verification.py` (`make verify` for light, `make vhs` for deep fuzz).

#### Threading Model & The Three-Layer UNO Safety Defense

Everything above is helpful, but not sufficient. While the pure state machine and formal contracts guarantee logical correctness—ensuring that transitions and event lifecycles are mathematically sound—they only describe *what* should happen in the abstract. Getting a multi-threaded codebase genuinely correct in a complex host application like LibreOffice requires multiple layers of defense: a formal threading model to keep the application responsive, and strict enforcement layers to ensure that PyUNO's thread boundaries are never breached.

##### The Core Constraint: PyUNO Thread Affinity
PyUNO is **not thread-safe**. UNO objects are thin Python proxies over LibreOffice's internal C++ objects. The underlying VCL (Visual Class Library) framework is single-threaded; touching a UNO object from a background worker thread races internal C++ state. Crucially, thread-affinity violations do not throw clean Python exceptions—instead, they silently corrupt the document undo stack, freeze the UI event loop, or crash `soffice` nondeterministically under load. The VCL main thread is the *only* thread that may legally touch UNO; there is no lock you can acquire to make off-thread UNO access safe.

##### The Formal Threading Model
To run AI generation, network I/O, and sub-agents asynchronously without violating thread affinity, the platform defines **colored functions**: RED (main-thread / UNO), BLUE (background workers), and YELLOW (synchronous host dispatch — `=PY()`/`=PROMPT()`, remote PyUNO bridges). All background work flows through `run_in_background(func, ..., dedicated=, daemon=)` (`plugin/framework/worker_pool.py`):

- **Daemon pool** — a fixed pool of 2 daemon threads (`wa-bg-0…1`, `BACKGROUND_POOL_MAX_WORKERS`) over an unbounded `queue.SimpleQueue`. Bounded in *worker count*, not queue length; pooled threads are daemon so they never block `soffice` exit (CPython `ThreadPoolExecutor` workers are non-daemon since 3.9).
- **Dedicated threads** — `dedicated=True` (or `daemon=False`) spawns a single `threading.Thread` for servers, pipe drains, infinite loops, and any job another thread will `join()`. The rule: never `join()` a pooled job from another pooled job.
- **`AsyncProcess` & pipe safety** — wraps `subprocess.Popen` with dedicated stdout/stderr drain threads; `terminate()` degrades to `kill()` on timeout. Every long-lived child with `stderr=PIPE` needs a continuous drain (`start_stderr_drain`/`AsyncProcess`) or stderr redirected, or the ~64 KiB kernel pipe buffer fills and deadlocks the parent. `StderrTail` keeps a bounded tail; `optimize_popen_pipes` expands Linux pipe size via `F_SETPIPE_SZ`.

##### Main-Thread Marshalling & Async Streaming: The Drain Loop
Marshalling results and UI updates back to the RED main thread relies on two coordinated mechanisms:

- **`QueueExecutor`** (`execute_on_main_thread` / `post_to_main_thread`) — pokes the VCL main thread via `com.sun.star.awt.AsyncCallback` and blocks workers on a per-item `threading.Event` with timeout. It **refuses** blocking marshal from YELLOW context (deadlock hazard #402).
- **The Stream Drain Loop** — streaming AI tokens works by a strict **worker produces, UI drains** split. Background work pushes tuples onto a `queue.Queue` whose first element is always a `StreamQueueKind` enum member (`CHUNK`, `THINKING`, `TOOL_CALL`, `TOOL_RESULT`, `APPROVAL_REQUIRED`, `STREAM_DONE`, `ERROR`, …). The UI thread runs `run_stream_drain_loop`, which blocks up to 0.1 s for an item, drains any immediately available extras, and when idle calls `pump_ui_idle` — draining the `QueueExecutor` and pumping VCL via `toolkit.processEventsToIdle()`.
  - **`job_done[0]` ownership**: Owned exclusively by the drain loop. The worker's `finally` posts the `(STREAM_DONE, None)` sentinel but never writes `job_done` directly, preventing the loop from exiting before `on_done` runs (which would skip `leaveUndoContext()` and corrupt the undo stack).
  - **`BatchingStreamQueue`**: Batches `CHUNK`/`THINKING` deltas on the producer side for up to 250 ms (a one-shot timer armed on the first fragment), flushing before any control item to keep the UI smooth without main-thread sleeps.
  - UNO `XTimerListener` is deliberately **not** used for sidebar streaming: the drain loop owns VCL pumping, guarded by `drain_owner_scope` (nested/differing drain owners raise). Direct `toolkit.processEventsToIdle()` outside approved chokepoints is banned by Opengrep.

##### Enforcing Thread Affinity: The Three-Layer Defense
Architectural rules alone are conventions—nothing in Python natively stops an engineer or tool from accidentally invoking `doc.getText()` inside a BLUE background worker. WriterAgent enforces thread affinity across three concentric layers:

- **Layer A (Runtime tripwire, shipped):** `thread_guard.py` wraps every UNO object in a viral `_UnoThreadGuardProxy` via `guard_uno()`, which asserts the main thread on every attribute access, call, and property write. `assert_main_thread` raises (dev) or logs (release stub) on violation; enabled by default (`WRITERAGENT_UNO_THREAD_GUARD=1`), stubbed off in release OXTs. The proxy unwraps for `__setattr__`/`__call__`/`queryInterface` so proxies never leak into C++.
- **Layer B (Test-time verification):** `ThreadAffineMock` and `set_designated_main_thread`/`set_force_marshal_mode` let affinity tests run under `make lo-test-threadguard` to prove marshal boundaries hold before code touches a real office build.
- **Layer C (Static analysis):** Opengrep taint rules (`tests/semgrep/uno_thread_safety.yml`, `--taint-intrafile`) trace BLUE roots to RED sinks with sanitizers (`execute_on_main_thread`, `post_to_main_thread`, `if on_main_thread():`); a custom AST linter (`scripts/lint_thread_safety.py`) and a call-graph deadlock analyzer (`scripts/analyze_thread_deadlocks.py`) catch `blocking-marshal-in-sync-dispatch`.

The context discipline is equally strict: use the extension's `self.ctx` / `get_ctx()` (a cached, wrapped context), never a fresh `uno.getComponentContext()` — a fresh call can return a *different* context that quietly breaks package/dialog lookups or segfaults test runners.

#### Parsing Layer: JSON Repair and Robust Tool Recovery

Model output is messy, so `safe_json_loads` (`plugin/framework/json_utils.py`) tries, in order: standard `json.loads`, `strict=False`, `ast.literal_eval` (Python reprs like `True`/`None`), then vendored `json_repair` (truncated JSON, trailing commas, unquoted keys). A pre-step repairs LaTeX sequences (`\times` → `\\times`) that collide with JSON escapes. Streamed SSE is normalized by `iterate_sse`; leaked chat-template control tokens (`<|...|>`) are stripped; and a Hermes-inspired client-side tool-call parser registry (`plugin/contrib/tool_call_parsers/`) recovers `<tool_call>` fragments for Hermes/Qwen/DeepSeek/Mistral/Llama/Kimi/GLM models without any VLLM dependency.

#### Transport Layer: Two Runtimes, One HTTP Client

WriterAgent intentionally keeps **two separate agent runtimes** sharing a single `LlmClient`:

- **Main chat** — the streaming FSM above, OpenAI-shaped multi-turn history.
- **Smol / Librarian ReAct** — a vendored smolagents `ToolCallingAgent` (ReAct steps: `ActionStep`, `ToolCall`, `FinalAnswerStep`) used for web research, librarian, PPT-Master, and specialized delegation. Its model is `WriterAgentSmolModel`, which delegates to the same `LlmClient`; its sync tools are marshalled to the main thread via `SmolToolAdapter`.

Merging the runtimes would change prompts, stops, and transcripts; merging the HTTP client does not. `LlmClient` is constructed **per job** (sidebar send, grammar worker, `=PROMPT()`, smol), so each owns its persistent keep-alive `http.client` connection and provider shims—chat and grammar hitting the same Ollama use two sockets, not one shared conn. The transport is deliberately unlocked: the Stop button closes the socket (`stop()`) while a worker may be blocked in `getresponse()`, which is how a hung stream is aborted. A `RequestPacer` enforces a 50 ms inter-request interval; local HTTPS falls back from verified to unverified TLS only after a genuine certificate-verification failure on a local host.

#### Integration Layer: MCP (LibreOffice as a First-Class AI Citizen)

WriterAgent embeds an **MCP (Model Context Protocol) HTTP server** (`plugin/mcp/`) so external agents (Cursor, Claude Desktop, LM Studio) can remote-control LibreOffice over `http://localhost:18765/mcp`. It implements JSON-RPC 2.0 (`initialize`, `tools/list`, `tools/call`) with a **custom stdlib server** (no official SDK/Pydantic), marshalling every UNO operation to the main thread. Two concurrency layers prevent corruption: a **global semaphore** serializes fast tools (overload → HTTP 429), and a **per-document mutation gate** serializes mutating tools per document while read-only work stays concurrent. By default, tools are exposed through a single `delegate_to_specialized_*_toolset` gateway that runs a **nested smolagents sub-agent on WriterAgent's own configured LLM** — the MCP host's model never touches LibreOffice directly. Optional Cloudflare/Bore/Ngrok/Tailscale tunnels and a stdio bridge (`scripts/mcp_bridge.py`) round out remote access.

### 2. Deep Semantic Understanding (The Eyes)

Most AI tools see a document as a flat "wall of text." WriterAgent sees the **structure**, via an LO-DOM (LibreOffice Document Object Model) that extracts LibreOffice's hierarchy into semantic JSON instead of relying on screenshots.

- **Draw/Impress `get_draw_tree`** translates raw UNO shapes into semantic nodes with hierarchy (grouped shapes → nested children), spatial geometry (`x`, `y`, `width`, `height`), attributes (`text`, `name`, `alt_title`), and style (`FillColor`, `ZOrder`). For `ConnectorShape`s it extracts `StartShape` and `EndShape` directly — so in a flowchart the agent knows *which boxes are connected to which*, not just the labels.
- **Writer `writer_tree`** builds a navigable index of headings/body paragraphs from `OutlineLevel`, letting the model "skim" a TOC and zoom into sections rather than read 100 pages linearly.
- **Writer `get_page_objects`** jumps the view cursor to a specific physical page and returns a multimodal snapshot: exact visible paragraph text, anchored images/tables, and embedded draw shapes (cross-referenced from Writer's hidden draw page against physical page boundaries). This solves the "what am I looking at on page 12?" needle-in-a-haystack problem.

**Proximity awareness** lets the agent navigate by headings, sections, and bookmarks; **tracked-changes integration** (`track_changes_start/stop/list`, `manage_tracked_changes`) respects the editorial process — and `get_string_without_tracked_deletions()` in `text_helpers` ensures prompts and edits never see text the author has already deleted.

### 3. Advanced AI Pipelines (The Specialized Skills)

#### Reviewable, Format-Preserving Edits

Writer's edit paths support a **review mode** (`doc.agent_edit_review_mode`: `off`/`record`/`wait`) where agent changes land as native LibreOffice tracked changes (redlines) the user accepts or rejects. `EditReviewSession` (`plugin/writer/edit_review.py`) snapshots pre-edit redline identifiers, runs the edit, and tags every *new* redline with a `wa-review:<session>:<n>` token — **fail-closed**: if the scan is incomplete, edits apply but redlines stay untagged so "Accept All" can never touch a misclassified user redline. Word-level diff splitting (`word_diff_split.py`) splits large replacements into tight Delete+Insert pairs when the changed-word fraction is below a threshold, producing one reviewable change per sub-edit with per-change outcomes (`accepted`/`rejected`/`modified`/`pending`) and `final_text` previews. A dedicated review toolbar and inline accept/reject popups scope resolution strictly to `wa-review` changes.

Format preservation is the default: edits flow through LibreOffice's native `HTML (StarWriter)` import, with a symmetric **`data-lo-style`** convention — `get_document_content` exports each block with its style name as a compact token (`Heading 1` → `Heading1`), and `apply_document_content` resolves that token back to the real paragraph style, layering inline `style="..."` as direct overrides. Bold, italics, highlights, font sizes, tables, and nested lists survive the round-trip.

#### Specialized Tool Tiers

The tool registry (`plugin/framework/tool.py`) tiers tools as `core` (main chat + MCP default lists), `specialized`, `specialized_control` (nested sets, hidden from default lists), and `mcp`. Matching is by `uno_services` first, then `doc_types` — anything advertised by `get_schemas` must be runnable via `execute`. Specialized domains are reached through a single gateway (`delegate_to_specialized_writer_toolset` / `..._draw_toolset`) that runs a nested sub-agent with only that domain's tools: styles, page layout, text frames, embedded OLE, images, shapes (`shape_upsert`, `shape_connect`, `shape_group`), charts, indexes, fields, tracking, bookmarks, footnotes, tables, structural navigation (`section_list`, `nav_goto_page`), and forms. Writer `charts`/`shapes` share tool **names** with Calc/Draw, so the Writer classes declare the **union** of those services or execution rejects the document.

#### Real-Time Grammar Engine

Grammar checking is a native `XProofreader` service (`WriterAgentAiGrammarProofreader`) with a Lightproof-style registry. Its `doProofreading` returns a cache-first result: normalize locale → split the paragraph into sentence candidates → return cached errors for *all* sentences immediately → enqueue only the active sentence for async checking. Sentence splitting is Unicode-aware, using LibreOffice's `com.sun.star.i18n.BreakIterator` as the gold standard with whitespace-run fallback for Thai/Lao/Khmer and CLDR abbreviation suppressions. Results are cached in two levels — an in-memory LRU (2048 entries) and a document-embedded `.odt` user property (900 KB cap, saved on save/unload). Backends are pluggable: LLM (default, with a `grammar_llm_request_gate` shared with chat), Harper, LanguageTool, and Vale. Mixed-language paragraphs are detected (LLM or local `langdetect`) and re-localed before re-queueing.

#### Calc Intelligence

Beyond `=PY()` (below), Calc supports pivot-table analysis, logical-error detection, and specialized toolsets (conditional formatting, sheet filters, charts). The **spreadsheet → Python converter** rewrites classic formulas as `=PY("…"; …)` through a **259-helper `calc.*` parity library** (auto-imported as `calc`), preserving constants, dates, and cell formats while targeting ~90% formula-cell coverage and ≥99% value fidelity against an oracle.

#### Multimodal Mastery

- **Image generation** (`image_generate`) routes to OpenRouter's image endpoint (with text-to-image and img2img), inserting into Writer/Calc/Draw/Impress.
- **OCR / vision** (`extract_text`, `extract_structure`) runs Docling/PaddleOCR in the user venv via the trusted-module pattern, with an HTML insert pipeline (Docling → `css_inline` → heading/body style augmentation → StarWriter import).
- **Audio** uses a dedicated user-venv subprocess (not the warm Python worker) capturing 16 kHz mono PCM with `sounddevice`, with silence auto-stop and two LLM paths (native `input_audio` or STT transcription).
- **Math** converts LaTeX/MathML into native, editable LibreOffice Math objects (StarMath) via `latex2mathml` and LO's MathML import, and exports back via `mathml-to-latex`.

### 4. Scientific Python Integration (The Compute Bridge)

LibreOffice ships its own embedded Python. Compiled libraries such as **NumPy** must match that interpreter's ABI — loading a system `pip install numpy` inside the extension can **crash the whole office suite**. WriterAgent's answer is to never mix interpreters in memory.

#### User-Provided Venv, Out-of-Process Warm Worker

- **Config**: In **Settings → Python**, point `scripting.python_venv_path` at an existing `.venv` you created (no automatic pip bootstrap inside LibreOffice). Empty path disables venv execution and falls back to the embedded stdlib-only interpreter.
- **Warm worker**: A persistent child process runs the venv's `python` over **length-prefixed Pickle5 frames** (`plugin/scripting/ipc.py`), spawned once per resolved executable by `PythonWorkerManager` (`venv_worker.py`) and respawned on crash/timeout. The chain is `run_code_in_user_venv` → `PythonWorkerManager` → `venv/worker_harness.py` → `venv/venv_sandbox.py` → `LocalPythonExecutor`. Each call gets a **fresh `LocalPythonExecutor`** (isolated mode) — fast reuse without notebook-style state leaking between cells or chat turns.
- **AST sandbox (shipped in the OXT)**: the vendored smolagents `LocalPythonExecutor` AST-walks user code against a fixed `VENV_AUTHORIZED_IMPORTS` allowlist (not "whatever is pip-installed"), blocks `os`/`subprocess`, strips nested `os`/`sys` on allowed modules, guards dunders, and caps runaway loops. **Subprocess isolation remains the hard boundary**: the AST sandbox is a convenience, not a security proof — LibreOffice never shares memory with C extensions.
- **One execution path**: the chat tool `run_venv_python_script` and Calc `=PY()` / `=PYTHON()` both go through `run_code_in_user_venv`. Assign JSON-serializable output to **`result`**; optional range data is injected as **`data`** (Calc).
- **Serialization**: `payload_codec.py` packs grids ≥100 cells as a `split_grid` envelope (float64 buffer + sparse string index, `np.frombuffer` on the child) and DataFrames/images as dedicated envelopes, ~5–21× faster than JSON with `@deal` + Hypothesis round-trip oracles and A/B tests.
- **Trusted modules**: reviewed helper code (analysis, viz, embeddings, OCR, DuckDB) bypasses the AST sandbox via `run_trusted_worker_action` — only *user/LLM-submitted source* is sandboxed.

#### `=PY()` and `=PYTHON()` Add-Ins

The Calc add-in executes Python expressions with **extension-owned array spill** (not engine spill): a single cell returning a list/2D array auto-spills into neighbors via a deferred task, `#SPILL!` on collision, and an `XModifyListener` clears orphans — all inside an undo-isolated context so Ctrl+Z removes formula and spill together. **Shared kernels** (`calc:…` sessions) persist one namespace per workbook for fast recalc; the init script seeds once. Cell ranges map to a `CalcRange` object with `.values`, `.to_numpy()`, `.to_pandas(date_cols=True)`; NumPy/pandas results are serialized back for both Calc and the LLM. A **matrix fast path** (`WorkerResultSession`) caches a formula's result on the host so repeated cells reuse one IPC call (~99.9% IPC reduction for large matrix columns).

#### Two-Phase Orchestration

Python computes in the venv; the agent still uses existing Calc tools (`write_formula_range`, `create_chart`, etc.) to place results — no UNO inside the child process today. The in-process `execute_python_script` remains a separate stdlib-only sandbox for light logic without a venv.

#### Domain Helpers

Built-in scientific domains run as trusted modules: Analysis (EDA, outliers via scikit-learn, OLS via statsmodels, KMeans, Monte Carlo), Viz (matplotlib), Symbolic (SymPy), Units (Pint — `convert_quantity(60, "mph", "m/s")` → `26.8224 m/s`), Forecast, Optimize, Quant, Text Analytics, and Vision. Each ships as a host facade + venv compute + RPC stub, registered in `domain_registry.py` / `trusted_action_registry.py`.

Full design, security model, LibrePythonista comparison, and roadmap: **[Enabling NumPy & Python in LibreOffice](enabling_numpy_in_libreoffice.md)**.

### 5. Professional Engineering Standards (The Foundation)

The quality of a product is hidden in the details. WriterAgent includes:

#### Static Analysis & Type Checking

`make typecheck` runs **seven tools in parallel**: `ty` (Astral, primary checker), `basedpyright` (strict), `mypy` (second opinion), `bandit` (security SAST), `opengrep` (Semgrep-style rules including UNO thread-safety taint), `pyspector` (AI/taint SAST), and a custom **thread-safety linter** (AST + call-graph deadlock analysis). The configured include set (`plugin/`, `compute_service/`) is green; a documented cleanup fixed ~141 issues across 40+ files from an initial 1000+ diagnostics (mostly vendored/test code).

#### A Disciplined Exception Policy

Errors flow through a typed `WriterAgentException` hierarchy (24+ codes, from `CONFIG_ERROR` to `DISPOSED_OBJECT` and `PAYLOAD_CODEC_ERROR`) and `format_error_payload`. Tools fail via `_tool_error`/`make_tool_error`. The subtle rule that protects document integrity: **never catch `com.sun.star.uno.Exception` to "avoid" `DisposedException`** — disposal subclasses `RuntimeException` → `uno.Exception`, so such a catch still swallows it. UI lifecycle uses `suppress_disposed`; document tools re-raise via `is_disposed_exception`/`DocumentDisposedError`.

#### Automated Localization

A gettext pipeline (`i18n.py`, `_()`) serves **35 locale catalogs**. String extraction is automated (Python + XDL + module YAML), and an AI-assisted translation script (`scripts/translate_missing.py`) fills missing/empty/fuzzy `.po` entries against an OpenAI-compatible API. It is genuinely **multi-threaded**: a `ThreadPoolExecutor` (default 5 workers, `--jobs`, staggered by `--delay` between starts) fans translation batches — and the `--review` pass's per-entry critiques — across all languages concurrently, preserving whitespace and parsing results with `safe_json_loads`. System prompts stay English; the model is instructed to match the user's language in free-form chat.

#### Beyond UI Strings: Deep Linguistic Infrastructure

Internationalization in WriterAgent extends far beyond translating user-interface strings. Robust document processing across 36+ languages demands script-specific systems engineering (`plugin/writer/locale/grammar_proofread_locale.py`): full Unicode 15.1 sentence support across 30+ non-Latin scripts, and specialized whitespace segmentation for unspaced languages like Thai. Can auto-detect and set language on text as it's typed, and then run the appropriate grammar checking.

#### The "Lab": Internal Evaluation

An in-LibreOffice **LLM Evaluation Suite** benchmarks models on real Writer/Calc/Draw tasks, scoring structural tasks against **result oracles** (exported HTML/Draw-tree/Calc grid) and creative tasks with an **LLM judge**. Models are ranked by **Value (C²/$)** — average correctness² ÷ average dollars per run using live OpenRouter pricing. A **DSPy MIPROv2** loop (`scripts/prompt_optimization/run_optimize.py`) is an **offline** optimizer: default student is the live `llm_chat_eval` tool loop, rewriting a named prompt/tool slice (not ReAct on the sidebar). Winning text is copied by hand after a ranking re-run.

#### Cross-Document Intelligence

An optional **embeddings + FTS** engine (`writeragent_embeddings/corpus.db`, sqlite-vec) enables hybrid search over a document's sibling files: BM25/FTS5 and semantic vectors fused with **reciprocal-rank fusion**, optional cross-encoder reranking — all heavy compute (sentence-transformers, sqlite-vec) confined to the user venv. Experimental **memory** (`USER.md` + `MEMORY_GUIDANCE`) persists cross-session agent knowledge, and the **librarian** mode maintains a per-profile ReAct transcript.

---

## Where the Detail Lives

This document is a map, not the territory. For every area above there is a topic doc that goes deeper, and the repo-root `AGENTS.md` holds the invariants that keep the whole thing coherent:

| Need | Start |
|------|--------|
| Full entry-point & topic catalog | [`docs/repo-map.md`](repo-map.md) |
| Formal verification & FSM | [`framework/formal-verification.md`](framework/formal-verification.md) |
| Streaming & threading | [`framework/streaming-and-threading.md`](framework/streaming-and-threading.md), [`framework/threading.md`](framework/threading.md) |
| UNO thread safety | [`framework/uno-thread-safety.md`](framework/uno-thread-safety.md) |
| Exception policy | [`framework/exception-policy.md`](framework/exception-policy.md) |
| NumPy & Python bridge | [`enabling_numpy_in_libreoffice.md`](enabling_numpy_in_libreoffice.md) |
| LibrePy / WriterAgent split | [`scripting/librepy-split.md`](scripting/librepy-split.md) |
| MCP protocol | [`mcp-protocol.md`](mcp-protocol.md) |
| Embeddings & search | [`embeddings.md`](embeddings.md) |
| Benchmarks & eval | [`eval/benchmarks.md`](eval/benchmarks.md) |

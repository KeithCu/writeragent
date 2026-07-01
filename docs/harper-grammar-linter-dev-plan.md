# Development Plan: Offline Harper Grammar Linter Integration

This document outlines the detailed development plan to integrate **Harper**, a privacy-first, offline, and high-performance grammar checker written in Rust, into WriterAgent as a local proofreading option.

---

## 1. Why Harper? (Strategic Value)

1. **Rust Performance (Lightning Fast):** Harper is written in Rust, executing checks in sub-milliseconds with a near-zero memory footprint.
2. **Zero Dependencies (No JVM):** Unlike LanguageTool, which requires a Java Runtime Environment (JRE) to run its local server, Harper compiles to a single native binary.
3. **No AI Overhead:** It utilizes legible, rule-based logic, saving API tokens and running entirely offline.
4. **Rich Language Rules:** Harper provides built-in linters for common grammatical mistakes, spacing issues, spell-checking, and typographical errors.

---

## 2. Configuration & Integration Design

### UI Schema (`plugin/doc/module.yaml`)
We will add `harper` as an option in the grammar checker select dropdown:
```yaml
      - value: "harper"
        label: "Harper (Local Rust)"
```

### Config Coercion (`plugin/framework/config.py`)
Update `get_grammar_provider()` and `is_grammar_enabled()` to support the new `harper` string value:
```python
def is_grammar_enabled():
    val_str = str(get_config("doc.grammar_proofreader_enabled")).strip().lower()
    return val_str in ("llm", "languagetool", "vale", "harper", "true")
```

---

## 3. Dependency Management (Binary Fetching)

To avoid compiling Rust from source, WriterAgent fetches the official precompiled `harper-ls` binary based on the host architecture:
1. **GitHub Releases API:** Fetch binaries from `https://github.com/Automattic/harper/releases`.
2. **Platform Resolution:**
   * **Linux x86_64:** `harper-ls-x86_64-unknown-linux-gnu.tar.gz`
   * **macOS Arm64:** `harper-ls-aarch64-apple-darwin.tar.gz`
   * **Windows x86_64:** `harper-ls-x86_64-pc-windows-msvc.zip`
3. **First-run Auto-Install:** The worker will automatically download and unpack the correct `harper-ls` binary into the user's WriterAgent config folder if not already present.

---

## 4. Worker-Side Harper Helper (`plugin/scripting/venv/harper.py`)

Rather than spawning a new process and writing temporary files on every grammar check, WriterAgent runs `harper-ls --stdio` as a persistent background process inside the venv worker. Communication occurs over standard input/output streams using the JSON-RPC Language Server Protocol (LSP).

### Persistent LSP Client implementation (`HarperLSClient`)
The class `HarperLSClient` manages:
- **Process Lifecycle:** Starts `harper-ls --stdio` and keeps it alive. Auto-restarts the process if it goes offline.
- **Handshakes:** Sends standard LSP `initialize` and `initialized` payloads.
- **Request Interception:** Captures and automatically replies to server-initiated requests like `workspace/configuration`.
- **Text Sync:** Sends `textDocument/didOpen` notifications to lint text segments and listens for `textDocument/publishDiagnostics` containing lint rules and location spans.
- **Action Queries:** Query the `textDocument/codeAction` endpoint for each diagnostic to fetch and parse quickfix suggestions (e.g. spelling corrections).
- **Position Mapping:** Converts LSP 0-indexed line/column pairs back into absolute character offset spans expected by `WriterAgent`.

---

## 5. Queue Dispatcher Integration (`grammar_work_queue.py`)

Harper runs on **one sentence per `run_harper_check` call**. Upstream, [`ai_grammar_proofreader.py`](../plugin/writer/locale/ai_grammar_proofreader.py) splits each paragraph Writer hands in via [`split_into_sentences()`](../plugin/writer/locale/grammar_proofread_text.py) and enqueues each uncached sentence as its own `GrammarWorkItem` (see [realtime grammar checker plan](realtime-grammar-checker-plan.md) — sentence-sized scheduling). Unlike the LLM provider, Harper does not batch multiple sentences into one request.

Integrate Harper directly into the linter work queue:
```python
        if provider == "harper":
            from plugin.scripting.client import run_harper_check
            from plugin.framework.config import user_config_dir

            cfg_dir = user_config_dir() or ""

            for item, text in chunk:
                try:
                    request_start = time.monotonic()
                    res = run_harper_check(ec.ctx, text, cfg_dir)
                    elapsed_ms = int((time.monotonic() - request_start) * 1000)

                    errors = res.get("errors", [])
                    results = [errors]

                    _process_grammar_results([(item, text)], results, bcp47, original_bcp47, elapsed_ms, ec)
                    grammar_obs("worker_harper_done", chunk_len=1, results_len=len(errors), elapsed_ms=elapsed_ms, bcp47=bcp47)
                except Exception as ex:
                    log.error("[grammar] Harper check failed: %s", ex)
            return
```

---

## 6. Implementation Status (Completed)

The Harper Rust linter integration is fully implemented and optimized:
1. **Persistent Daemon Pattern:** Upgraded from one-shot `harper-cli` process spawning to a persistent `harper-ls` background daemon running inside the virtual environment worker process. This eliminates process startup and disk I/O overhead.
2. **Standard LSP Protocol:** Implemented handshake, configuration negotiation, diagnostics handling, and code actions queries natively over stdin/stdout streams.
3. **Integration Testing:** Verified via [`scripts/test_harper.py`](../scripts/test_harper.py) and added mocks and range-mapping unit tests in [`tests/scripting/test_harper.py`](../tests/scripting/test_harper.py).

Primary implementation: [`plugin/scripting/venv/harper.py`](../plugin/scripting/venv/harper.py) (`HarperLSClient`, `run_harper_check`). Host RPC: [`plugin/scripting/client.py`](../plugin/scripting/client.py) (`run_harper_check`). Queue wiring: [`plugin/writer/locale/grammar_work_queue.py`](../plugin/writer/locale/grammar_work_queue.py).

---

## 7. Known Limitations

These are accepted trade-offs for the current Harper-only integration. None block normal grammar proofreading use.

### LSP client scope

`HarperLSClient` is a **purpose-built client for `harper-ls`**, not a reusable general LSP library. It implements only the methods Harper needs: `initialize`, `textDocument/didOpen`, `textDocument/publishDiagnostics`, `textDocument/codeAction`, `textDocument/didClose`, and `workspace/configuration`. Other server-initiated requests receive a generic `result: null` reply.

#### Diagnostic and request waiting

The client waits for responses with a wall-clock timeout of 5 seconds by utilizing a background reader thread pushing to a thread-safe `queue.Queue`. If the server hangs or crashes, the client fails fast with a `TimeoutError` and restarts.

### Quickfix round trips

Code actions are fetched with **one `textDocument/codeAction` request per diagnostic**. A single sentence with many issues therefore incurs N sequential LSP round trips after diagnostics arrive. In practice this is usually a small N because each Harper call lints one sentence, not a whole paragraph.

### Language and configuration

- `languageId` is hardcoded to `"markdown"` for every segment, regardless of document locale or content type.
- `workspace/configuration` replies with `[{}]` — Harper-specific settings (dialect, rule toggles) are not forwarded from WriterAgent config.

### Position encoding

Range mapping uses Python string indices (`lsp_range_to_offset`) and assumes Harper's LSP columns align with UTF-8 code units in the segment text. Mixed surrogate-pair or complex Unicode edge cases are untested.

### Test coverage

Tests cover range mapping, mocked happy-path lint (including stable URI and didChange notifications), and diagnostic timeout verification. There are no automated tests for process restart, interleaved server requests, or malformed framing.

### Queue granularity

Harper is **sentence-scoped end to end**:

1. **Proofreader** — Writer passes paragraph-like `aText`; WriterAgent splits it into sentences and enqueues one work item per uncached sentence (plus partial-sentence drafts while typing).
2. **Worker** — The Harper branch loops `for item, text in chunk` and calls `run_harper_check` once per sentence. It does not use LLM-style multi-sentence batching (`doc.grammar_proofreader_batch_sentences` applies only to the AI provider).
3. **Observability** — `worker_harper_done` logs `chunk_len=1` because each Harper invocation handles a single queue item (one sentence string).

This matches LanguageTool and Vale and keeps memory bounded. LSP overhead scales with the number of **sentences** checked (e.g. a long paragraph yields several Harper calls, one per sentence), not with paragraph count alone.

---

## 8. Future Work

### 8.1 Reuse a stable document with `didChange` (Completed)

We implemented the proposed `didChange` pattern. We reuse a stable URI per client instance, send `didOpen` once, and use `didChange` with full text replacement on subsequent lints. Monotonic document versioning ensures we reject stale, in-flight diagnostics, and `didClose` is sent cleanly upon shutdown.

### 8.2 Other improvements (lower priority)

| Item | Rationale | Status |
|------|-----------|--------|
| **Timed diagnostic wait** | Replace the fixed 50-message loop with a monotonic budget (e.g. 5s per lint) so hung servers fail fast. | Completed |
| **stderr drain** | Background thread reading stderr, or `stderr=subprocess.DEVNULL` if Harper guarantees silence. | Completed (stderr redirected to DEVNULL) |
| **Harper config via LSP** | Map WriterAgent grammar/locale settings into `workspace/configuration` responses so Harper rule sets match user expectations. | Open |
| **Dynamic `languageId`** | Derive from document BCP47 or content type instead of hardcoded `markdown`. | Open |
| **Protocol helpers** | Extract Content-Length framing and JSON-RPC envelope builders (similar to Hermes `agent/lsp/protocol.py`) if Harper client grows or a second LSP consumer appears. | Open |
| **Edge-case tests** | Process death + restart, interleaved `workspace/configuration` during `codeAction`, empty diagnostic list, timeout path. | Open |
| **Batch code actions** | Investigate whether `harper-ls` supports range-wide or document-wide code actions to cut round trips when one sentence has many diagnostics. | Open |

### 8.3 Not planned: general-purpose LSP port

WriterAgent does **not** need Hermes's full multi-language LSP stack (`pyright`, `gopls`, workspace delta baselines, etc.) for Harper grammar. A future **separate** feature — semantic lint of user Python scripts or macros — would warrant porting selected pieces from [Hermes `agent/lsp`](file:///home/keithcu/.hermes/hermes-agent/agent/lsp/) under a new module, not extending `HarperLSClient`.

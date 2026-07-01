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
3. **Integration Testing:** Verified via [scripts/test_harper.py](file:///home/keithcu/Desktop/Python/writeragent/scripts/test_harper.py) and added comprehensive mocks and range mapping unit tests inside [tests/scripting/test_harper.py](file:///home/keithcu/Desktop/Python/writeragent/tests/scripting/test_harper.py). All checks and tests pass successfully.

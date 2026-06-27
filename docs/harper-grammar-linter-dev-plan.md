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

To avoid compiling Rust from source, WriterAgent can fetch the official precompiled `harper-cli` binary based on the host architecture:
1. **GitHub Releases API:** Fetch binaries from `https://github.com/elijahpotter/harper/releases`.
2. **Platform Resolution:**
   * **Linux x86_64:** `harper-cli-x86_64-unknown-linux-gnu.tar.gz`
   * **macOS Arm64:** `harper-cli-aarch64-apple-darwin.tar.gz`
   * **Windows x86_64:** `harper-cli-x86_64-pc-windows-msvc.zip`
3. **First-run Auto-Install:** The worker will automatically download and unpack the correct binary into the user's WriterAgent config folder if not already present.

---

## 4. Worker-Side Harper Helper

We will create a worker script `plugin/scripting/venv/harper.py` that writes text segments to a temp file, runs `harper-cli`, and parses the stdout.

### CLI JSON Format Analysis
When running `harper-cli --format json <file>`, Harper returns a JSON array of issues:
```json
[
  {
    "check": "SentenceCapitalization",
    "message": "This sentence does not start with a capitalized letter.",
    "span": { "start": 0, "end": 4 },
    "suggestions": [
      {
        "type": "Replace",
        "text": "This"
      }
    ]
  }
]
```

### Target Script: `plugin/scripting/venv/harper.py`
```python
import os
import sys
import json
import subprocess
import tempfile
from pathlib import Path

def _get_harper_binary(user_config_dir: str) -> str:
    # Resolves local binary path (or downloads it if missing)
    bin_dir = Path(user_config_dir) / "bin"
    suffix = ".exe" if os.name == "nt" else ""
    binary_path = bin_dir / f"harper-cli{suffix}"
    
    if not binary_path.exists():
        # TODO: Implement platform-specific auto-download from GitHub releases
        # For development, fall back to PATH or throw exception
        pass
        
    return str(binary_path)

def run_harper_check(text: str, user_config_dir: str) -> dict:
    """Run harper-cli on text segment and return parsed errors."""
    try:
        harper_bin = _get_harper_binary(user_config_dir)
    except Exception as e:
        raise RuntimeError(f"Harper binary not available: {e}")

    with tempfile.NamedTemporaryFile(suffix=".txt", delete=False, mode="w", encoding="utf-8") as temp_file:
        temp_file.write(text)
        temp_file_name = temp_file.name

    try:
        cmd = [harper_bin, "--format", "json", temp_file_name]
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
        
        if proc.returncode != 0 and not proc.stdout:
            raise RuntimeError(f"Harper failed with code {proc.returncode}: {proc.stderr}")

        output_data = json.loads(proc.stdout or "[]")
        errors = []
        
        for err in output_data:
            span = err.get("span", {})
            start = span.get("start", 0)
            end = span.get("end", 0)
            length = max(1, end - start)
            
            rule = err.get("check", "Grammar")
            msg = err.get("message", "")
            
            # Map suggestions
            suggestions = []
            for sug in err.get("suggestions", []):
                if sug.get("type") == "Replace":
                    suggestions.append(sug.get("text", ""))
            
            correct = suggestions[0] if suggestions else ""

            errors.append({
                "wrong": text[start:start+length] if start+length <= len(text) else "",
                "correct": correct,
                "n_error_start": start,
                "n_error_length": length,
                "short_comment": f"[Harper] {msg}",
                "full_comment": msg,
                "rule_identifier": f"harper||{rule}",
                "suggestions": suggestions,
                "reason": msg,
                "type": f"Harper ({rule})"
            })
            
        return {"errors": errors}
        
    finally:
        if os.path.exists(temp_file_name):
            os.remove(temp_file_name)
```

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

The Harper Rust linter integration is fully implemented and verified:
1. **Dropdown UI & Config:** Added `harper` under the dynamic dropdown select option `grammar_proofreader_enabled` inside `module.yaml`, with configuration mapping in `config.py`.
2. **Global PATH & Fallback Auto-Download:** Implemented `shutil.which("harper-cli")` in `harper.py` to prioritize global path binaries, falling back to downloading and unpacking precompiled releases from the official `Automattic/harper` GitHub repository.
3. **Structured JSON Parser:** Handled nested file-relative lints arrays (`output_data[0].get("lints")`) and parsed span coordinates (`char_start`, `char_end`) + string-formatted suggestions (stripping `"Replace with: “...”` decorations).
4. **Integration Testing:** Created [scripts/test_harper.py](file:///home/keithcu/Desktop/Python/writeragent/scripts/test_harper.py) to test functionality locally, and verified that all 3,500+ unit/integration tests and Bandit security scans pass successfully.

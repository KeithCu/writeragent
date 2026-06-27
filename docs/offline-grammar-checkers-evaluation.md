# Technical Evaluation: Offline, Cross-Process & Pip-Installable Grammar Checkers

This document provides a detailed evaluation of offline, non-LLM grammar and style checking solutions. It outlines their capabilities, language support, installation footprints (with a preference for `pip` packages), and integration designs within the WriterAgent architecture.

---

## Executive Summary & Comparison

| Tool / Engine | Primary Installation | Multilingual Support | Key Strength | Ideal Audience / Use Case |
| :--- | :--- | :--- | :--- | :--- |
| **LanguageTool (`language-tool-python`)** | `pip install language-tool-python` (with local Java runtime) | **Very High** (30+ languages: EN, DE, FR, ES, PT, PL, RU, etc.) | Industry standard, massive rule set, correction suggestions | Users needing robust, multilingual grammar and spelling checks offline. |
| **Vale** | System package manager (or binary download) + `vale sync` | **Language-Agnostic** (Core engine is regex-based; styles are mostly EN) | Enforces style guides, highly customizable, markup-aware | Teams matching strict editorial guides (Microsoft, Google) in prose. |
| **Harper** | `cargo install` or precompiled binaries | **Low** (English only: US, UK, CA, AU, IN dialects) | High performance, lightweight Rust codebase, privacy-first | Users wanting a lightweight, local English-only checker. |
| **Proselint** | `pip install proselint` | **Low** (English only) | Zero-dependency, pure Python, fast stylistic checks | Users wanting a quick, style-focused pre-pass with zero environment setup. |

---

## 1. LanguageTool via `language-tool-python`

This wrapper manages a local instance of the Java-based LanguageTool engine, allowing full offline checks via standard Python calls.

### Technical Details & Installation
*   **Command:** `pip install language-tool-python`
*   **Initialization (Python):**
    ```python
    import language_tool_python
    # Starts the local background Java server (auto-downloads JARs on first run)
    tool = language_tool_python.LanguageTool('de-DE') 
    matches = tool.check("Es gibt ein Fehler hier.")
    ```
*   **Language Support:** **Excellent (Native Multilingual)**. Fully supports English, Spanish, French, German, Polish, Portuguese, Russian, and 20+ other languages natively out-of-the-box.
*   **Licensing & Hosting:** Local server JARs are loaded into the user's home directory. Data remains 100% local.

### PM & Dev Considerations
*   > [!IMPORTANT]
    > **Dependency:** Requires a Java Runtime Environment (JRE) installed on the system. Since LibreOffice often depends on Java for certain features (e.g., Base database, specific macro features), many users already have JRE installed.
*   **Memory Footprint:** Starts a JVM process. Warmed-up memory usage can be 200MB-500MB, but response latency is low (usually sub-50ms per sentence).

---

## 2. Vale (Prose Linter with Pre-made Styles)

Vale is a Go-based command-line tool. Rather than doing classical NLP-based grammar parsing, it acts as a **style linter** that parses files (HTML, Markdown, XML) and applies regex-based rules.

### Pre-Made Style Guides (Ready-to-use)
You do not have to write rules from scratch. Vale officially maintains packaged style guides:
*   **Microsoft:** Rules from the *Microsoft Writing Style Guide* (acronyms, capitalization, word choice).
*   **Google:** Rules from the *Google Developer Documentation Style Guide*.
*   **write-good:** Enforces the common prose checks (cliches, passive voice, wordiness) from the popular `write-good` linter.
*   **proselint / alex:** Packages carrying rules from `proselint` and `alex` (non-inclusive language).

### Integration & Usage
1.  **Configuration (`.vale.ini`):**
    ```ini
    StylesPath = styles
    MinAlertLevel = suggestion

    Packages = Microsoft, Google, write-good

    [*]
    BasedOnStyles = Microsoft, Google, write-good
    ```
2.  **Download styles:** Run `vale sync` to automatically download the styles.
3.  **Language Support:** **Language-agnostic engine**, but pre-made packages are almost exclusively English. Writing rules for other languages requires custom regex sets.

### PM & Dev Considerations
*   **Installation:** Cannot be installed purely via `pip`. It is a compiled Go binary that must be downloaded or installed via a system package manager (e.g., `apt`, `brew`).
*   **Pro:** Extremely fast, parses document markup cleanly, very low resource footprint.

---

## 3. Harper (Rust-backed Grammar Engine)

Harper is a fast, memory-safe, offline-first grammar checker written in Rust.

### Technical Details & Installation
*   **Command:** Installed via Cargo (`cargo install harper-cli`) or native binaries.
*   **Language Support:** **English Only** (supports US, UK, Canadian, Australian, and Indian dialects). 
*   **Performance:** Built specifically to be faster and consume less memory than JVM-based alternatives.

### PM & Dev Considerations
*   Cannot be installed via `pip`.
*   Still in active development; has a smaller community and fewer rules compared to LanguageTool.

---

## 4. Proselint (Pure Python Style Linter)

Proselint is a pure-Python library designed to lint prose for writing quality, cliches, and stylistic issues.

### Technical Details & Installation
*   **Command:** `pip install proselint`
*   **Usage:**
    ```python
    import proselint
    errors = proselint.tools.lint("This is a very unique sentence.")
    ```
*   **Language Support:** **English Only**.

### PM & Dev Considerations
*   **Pro:** Simplest path for Python developers. Installing in the venv requires zero system dependencies (no Java, Go, or Rust compilers needed).
*   **Con:** Checks are limited to stylistic guidelines and common word-choice traps; it lacks a robust syntactic grammar rules database or spelling checker.

---

## Architecture Design for WriterAgent Integration

If integrating one of these local checkers as an alternative to the LLM path, the following architecture is recommended:

```mermaid
flowchart TD
    LO[LibreOffice / XProofreader] -->|doProofreading| Host[WriterAgent Host]
    Host -->|Queue Item| Queue[Grammar Work Queue]
    Queue -->|IPC via Pickle5| Venv[Python Venv Worker]
    Venv -->|Local Call| Checker[language_tool_python / proselint]
    Checker -->|Run Engine| Process[Background Process / JVM / Local Server]
    Process -->|Errors & Spans| Venv
    Venv -->|Parsed JSON Results| Host
```

---

## 5. Integrated Solution: LanguageTool in WriterAgent

As of June 2026, **LanguageTool** has been fully integrated into WriterAgent as a local, offline grammar linter.

### Implemented Architecture
1. **Host-Worker Split:** The LibreOffice process communicates with the warm Python `venv` subprocess via `_run_trusted_helper`. This isolates the JVM and `language-tool-python` server execution from LibreOffice.
2. **Worker Cache:** The worker script caches initialized `LanguageTool(bcp47)` instances in memory to eliminate JVM boot costs on successive checks.
3. **Queue Interface:** Request tasks are dispatched via the asynchronous `GrammarWorkQueue`. Results are normalized and cached locally in `grammar_proofread_cache`.

### Key Performance Characteristics
* **First-run Download:** The initial execution downloads the LanguageTool server binaries (~259MB) from the internet. Subsequent runs are fully offline.
* **Warm-up Speed:** Subsequent check times typically register around **1.5s** (including cross-process RPC serialization and JVM execution) and are fully non-blocking to the main LibreOffice thread.


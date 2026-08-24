# WriterAgent, LibrePy & LibreHarper

![WriterAgent logo](https://raw.githubusercontent.com/KeithCu/writeragent/master/extension/assets/logo.jpg)

[![License: GPL v3+](https://img.shields.io/badge/License-GPL%20v3%2B-blue.svg)](https://www.gnu.org/licenses/gpl-3.0.html)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![LibreOffice 7.0+](https://img.shields.io/badge/LibreOffice-7.0%2B-green.svg)](https://www.libreoffice.org/)
[![Release](https://img.shields.io/github/v/release/KeithCu/writeragent)](https://github.com/KeithCu/writeragent/releases)

**Python, NumPy, and Agentic AI for LibreOffice (Writer, Calc, and Draw)**

Run Python and scientific compute directly in spreadsheet formulas, edit documents with private local-first AI, conduct autonomous web research, generate diagrams, and automate office workflows — without cloud lock-in.

The project is distributed as three standalone extension packages (*install only one at a time*):

| Package | What's Included | Best For |
| :--- | :--- | :--- |
| 🤖 **[WriterAgent](docs/features.md)** (`WriterAgent.oxt`) *(Full stack)* | Everything in LibrePy and LibreHarper + AI sidebar, `=PROMPT()`, web research, Calc → Python converter, MCP server | Users wanting the complete AI assistant, spreadsheet converter, and scientific compute suite |
| 🐍 **[LibrePy](docs/scripting-librepy-split.md)** (`LibrePy.oxt`) | Python runtime, `=PY()`, NumPy, pandas, SymPy, Monaco code editor, domain helpers, OCR | Users who want Python and Data Science in Calc/Writer without AI or API keys |
| ✍️ **LibreHarper** (`LibreHarper.oxt`) | Standalone offline [Harper](https://github.com/Automattic/harper) grammar engine for Writer | Users who only want fast, local grammar checking without AI or Python stacks |

**[Download .oxt Releases](https://github.com/KeithCu/writeragent/releases/latest)** · [Feature Index](docs/features.md) · [NumPy in LibreOffice Guide](docs/enabling_numpy_in_libreoffice.md) · [Discussions](https://github.com/KeithCu/writeragent/discussions)

---

## Key Capabilities

### 🐍 Python & Scientific Computing (Calc & Writer)

- **Native `=PY()` Spreadsheet Formulas** — Execute Python, NumPy, and pandas expressions directly inside Calc cells with automatic array spill, shared workbook kernels, and persistent scripts.
  - `=PY("np.mean(data)"; A1:A10)` — Calculate array statistics directly on Calc ranges.
  - `=PY("data.to_pandas(date_cols=True)"; A1:C10)` — Load sheet data into pandas with automatic type and date parsing.
  - [NumPy in LibreOffice Guide](docs/enabling_numpy_in_libreoffice.md) · [Data Shapes & Type Mapping](docs/calc-py-data-shapes.md)
- **Embedded Monaco Code Editor** — Write, test, and debug multi-line Python scripts directly inside cells or through the **Tools → Run Python Script** environment with syntax highlighting, autocomplete, and diagnostics.
- **Built-in Scientific & Analytics Domains** — Ready-to-use helpers for EDA, outlier detection, OLS regression, KMeans clustering, Monte Carlo simulations, symbolic algebra (SymPy), plotting, and physical unit conversions (`convert_quantity(60, "mph", "m/s")` → `26.8224 m/s`). [Domain Reference](docs/scripting-numpy-domains.md) · [Analysis Helpers](docs/calc-analysis-tools.md)
- **Spreadsheet → Python Converter *(WriterAgent)*** — Translate 235+ classic Calc/Excel formulas into clean Python expressions using the built-in `calc.*` parity library while preserving constants, dates, and cell formats. [Details](docs/calc-spreadsheet-to-python-import.md)
- **Local Vision & OCR** — Extract text from embedded images or scanned documents directly into Writer and Calc via offline Docling OCR. [Vision Guide](docs/image-recognition.md)

### 🤖 Local-First Agentic AI & Writing (Writer)

- **Sidebar Chat with Multi-turn Tool Calling** — Edit, restructure, or expand documents using natural language. 9 core tools plus dozens of [specialized sub-agents](docs/writer-specialized-toolsets.md) for page layout, footnotes, bookmarks, revisions, and forms.
- **Format-Preserving Edits** — Surgical redlines and section rewrites maintain your existing formatting (bold, italics, highlights, font sizes, tables, and nested lists) without clobbering styles.
- **Autonomous Web Research** — Integrated private [smolagents](https://github.com/huggingface/smolagents) loop with DuckDuckGo. Synthesizes multiple web sources and updates open documents with real-time facts and citations. [Agent Search](docs/agent-search.md)
- **Real-Time Grammar & Proofreading** — Local, privacy-preserving grammar checking via [Harper](https://github.com/Automattic/harper) (fast, auto-installing), [LanguageTool](https://languagetool.org), or LLM endpoints with mixed-language sentence detection. [Details](docs/writer-grammar-checker-plan.md)
- **Math & LaTeX Import** — Converts LaTeX and MathML into native, editable LibreOffice Math objects. [Math Guide](docs/writer-math-tex.md)

### 📊 Diagrams, Slides & Multi-Modal (Draw & Impress)

- **Diagram & Presentation Generation** — Generate, adjust, and style flowcharts, shapes, connectors, speaker notes, and slide transitions through chat commands or Python scripts. [Details](docs/draw-impress-specialized-toolsets.md)
- **LO-DOM Semantic Tree** — Structural understanding of headings, sections, tables, and relationships across entire documents. [Semantic Tree](docs/writer-lo-dom-semantic-tree.md)
- **Cross-Document Search & Memory** — Query other documents in the same folder via local embeddings / hybrid search, with persistent cross-session agent memory. [Embeddings](docs/embeddings.md) · [Memory](docs/hermes-agent-patterns.md)

### 🔌 Integrations & Extensibility

- **Model Context Protocol (MCP) Server** — Connect external IDEs and agents (Cursor, Claude Desktop, LM Studio) to read and edit open LibreOffice documents over `http://localhost:18765/mcp`. [MCP Protocol](docs/mcp-protocol.md)
- **Pluggable Agent Backends** — Switch the chat engine to external agents such as [Hermes](https://github.com/NousResearch/hermes-agent), [Claude Code](https://docs.anthropic.com/en/docs/claude-code), [Mistral Vibe](https://github.com/mistralai/mistral-vibe), [Grok Build](https://zed.dev/acp/agent/grok-build), or [OpenCode](https://opencode.ai/docs/acp/) via ACP. [Cursor Plugin](https://github.com/KeithCu/cursor-libreoffice) · [LO Skill](https://github.com/KeithCu/libreoffice-skill)

Full catalog of capabilities: **[docs/features.md](docs/features.md)**.

---

## Installation & Setup

1. **Download** your chosen `.oxt` package from **[Latest Releases](https://github.com/KeithCu/writeragent/releases/latest)** and double-click to install (or open LibreOffice and go to **Tools → Extension Manager → Add**). *Remember to install only one extension package.*
2. **Restart** LibreOffice.
3. **Quick Configuration:**
   - **Python / LibrePy users:** Open Calc, check **Tools → LibrePy (or WriterAgent) → Settings → Python**, and click **Test** to verify your environment and NumPy/pandas availability.
   - **AI / WriterAgent users:** Open **WriterAgent → Settings** and enter your endpoint (e.g. `http://localhost:11434` for local [Ollama](https://ollama.com/), or an [OpenRouter](https://openrouter.ai/) / [Together.AI](https://www.together.ai/) API key). Open the sidebar via **View → Sidebar → WriterAgent** or press **Ctrl+Q** / **Ctrl+E**.

> **UI Modes:** In classic toolbar mode, access tools through the top menubar. In tabbed/ribbon interfaces, use the **WriterAgent** chat sidebar and/or the **Python** sidebar (Writer + Calc): Settings `⚙`, Python `🐍`, LaTeX math or Edit cell, search `🔍` (WriterAgent chat only), and full menus via `☰`.

For detailed setup instructions, see the **[Install and Troubleshooting Guide](docs/install-troubleshooting.md)**.

---

## Showcase

**Python in LibreOffice Writer**

![Python in LibreOffice](Showcase/PythonLibreOffice.png)

**Spreadsheet Analytics & Dashboard**

![Chat Sidebar with Dashboard](Showcase/Sonnet46Spreadsheet.png)

**Hermes + Opus 4.6 (Autonomous Web Research)**

![Hermes-Agent / Opus-4.6 Akihabara](Showcase/HermesAkihabara.png)

**Math Expressions & LaTeX**

![Math Expressions](Showcase/Math.png)

**Arch Linux Resume**

![Opus 4.6 Resume](Showcase/Opus46Resume.png)

**Diagrams in Draw**

![Sonnet 4.6 Visual](Showcase/Sonnet46ArchDiagram.jpg)

---

## Benchmarks & Evaluation

An in-LibreOffice **LLM Evaluation Suite** benchmarks models on real Writer, Calc, and Draw tasks and ranks them by **Value (C²/$)** — average correctness squared ÷ average dollars per run, using live OpenRouter pricing.

Apr 2026 snapshot — slugs and prices may be stale. Current default eval models live in [`scripts/prompt_optimization/model_configs.py`](scripts/prompt_optimization/model_configs.py).

| Rank | Model                                  | Avg correctness | Avg score | Avg tokens | Avg cost ($) | Value (C²/$) |
| ---- | -------------------------------------- | --------------- | --------- | ---------- | ------------ | ------------ |
| 1    | openai/gpt-oss-120b                    | 0.980           | 0.942     | 3767.1     | 0.00025      | 3827.240     |
| 2    | google/gemini-3-flash-preview          | 0.890           | 0.860     | 2957.2     | 0.00035      | 2234.257     |
| 3    | qwen/qwen3.5-9b                        | 0.730           | 0.691     | 4645.0     | 0.00050      | 1068.806     |
| 4    | nvidia/nemotron-3-nano-30b-a3b         | 0.922           | 0.851     | 7195.5     | 0.00082      | 1037.536     |
| 5    | mistralai/devstral-2512                | 0.980           | 0.950     | 3000.8     | 0.00154      | 623.434      |
| 6    | inception/mercury-2                    | 0.948           | 0.896     | 5150.9     | 0.00160      | 562.405      |
| 7    | minimax/minimax-m2.7                   | 0.990           | 0.943     | 4671.9     | 0.00191      | 512.581      |
| 8    | deepseek/deepseek-v3.2                 | 0.985           | 0.909     | 7575.4     | 0.00206      | 470.222      |
| 9    | qwen/qwen3.5-35b-a3b                   | 0.990           | 0.933     | 5671.1     | 0.00220      | 445.760      |
| 10   | x-ai/grok-4.1-fast                     | 0.950           | 0.886     | 6431.9     | 0.00204      | 442.733      |
| 11   | qwen/qwen3.5-27b                       | 0.993           | 0.942     | 5049.9     | 0.00259      | 380.538      |
| 12   | qwen/qwen3.5-122b-a10b                 | 0.990           | 0.950     | 3958.8     | 0.00308      | 318.312      |
| 13   | nvidia/nemotron-3-super-120b-a12b:free | 0.757           | 0.696     | 6388.4     | 0.00181      | 317.859      |
| 14   | allenai/olmo-3.1-32b-instruct          | 0.323           | 0.306     | 1912.4     | 0.00046      | 226.704      |
| 15   | z-ai/glm-5.1                           | 0.890           | 0.843     | 4677.8     | 0.00524      | 151.141      |

See [docs/eval-benchmarks.md](docs/eval-benchmarks.md) for scoring methodology and insights.

---

## Documentation & Architecture

| Topic | Documentation Link |
| :--- | :--- |
| **Feature Index** | [docs/features.md](docs/features.md) |
| **NumPy & Python in Calc** | [docs/enabling_numpy_in_libreoffice.md](docs/enabling_numpy_in_libreoffice.md) · [docs/calc-py-data-shapes.md](docs/calc-py-data-shapes.md) |
| **LibrePy Core Architecture** | [docs/scripting-librepy-split.md](docs/scripting-librepy-split.md) |
| **Domain Helper Functions** | [docs/scripting-numpy-domains.md](docs/scripting-numpy-domains.md) · [docs/calc-analysis-tools.md](docs/calc-analysis-tools.md) |
| **Full Architecture** | [docs/writeragent-architecture.md](docs/writeragent-architecture.md) · [docs/framework-formal-verification.md](docs/framework-formal-verification.md) |
| **Model Context Protocol (MCP)** | [docs/mcp-protocol.md](docs/mcp-protocol.md) |
| **Embeddings & Search** | [docs/embeddings.md](docs/embeddings.md) |
| **Benchmarks** | [docs/eval-benchmarks.md](docs/eval-benchmarks.md) |
| **Localization (34 Locales)** | [docs/localization.md](docs/localization.md) |
| **Code Explorer** | [DeepWiki](https://deepwiki.com/KeithCu/writeragent) |
| **Cursor / Agent Skills** | [cursor-libreoffice](https://github.com/KeithCu/cursor-libreoffice) · [libreoffice-skill](https://github.com/KeithCu/libreoffice-skill) |

Under the hood, all agentic interactions are governed by a formally verified finite state machine with strict type checking and static analysis.

![State machine architecture](Showcase/full_super_unified_complete.png)

---

## Project Evolution

A chronicle of building a Python runtime and AI suite inside LibreOffice:

- **Week 1**: [Initial fork, sidebar chat, multi-turn tools, and async streaming](https://keithcu.com/wordpress/?p=5060)
- **Week 2 & 3**: [MCP, research sub-agent, voice support, and evaluation dashboard](https://keithcu.com/wordpress/?p=5112)
- **Week 4–6**: [State machines, formal verification, and specialized toolsets](https://keithcu.com/wordpress/?p=5245)
- **Week 6 & 7**: [Async grammar checking and TeX import support](https://keithcu.com/wordpress/?p=5276)
- **Week 8+**: [NumPy compute bridge, `=PY()` Calc add-in, Monaco editor, and LibrePy core split](docs/scripting-librepy-split.md)

---

## Contributing & Development

[![Ask DeepWiki](https://deepwiki.com/badge.svg)](https://deepwiki.com/KeithCu/writeragent)
[Discussions](https://github.com/KeithCu/writeragent/discussions)

**Prerequisites:** Python 3.11–3.13 for development (pinned to **3.13** via [`.python-version`](.python-version)), [uv](https://docs.astral.sh/uv/). Run `make check-setup` to verify. On macOS: install `make`, `gettext`.

```bash
git clone https://github.com/KeithCu/writeragent.git
cd writeragent
uv python install 3.13
uv sync
make deploy          # Builds & installs WriterAgent.oxt (or: make deploy writer)
make test
make help
```

To build and test the standalone extension variants:
```bash
# Standalone Python / NumPy compute suite (LibrePy)
make build-core      # Produces build/LibrePy.oxt
make deploy-core     # Installs LibrePy.oxt (removes WriterAgent)

# Standalone Harper grammar checker (LibreHarper)
make build-harper    # Produces build/LibreHarper.oxt
make deploy-harper   # Installs LibreHarper.oxt
```

See [AGENTS.md](AGENTS.md) (invariants), [docs/repo-map.md](docs/repo-map.md) (entry points), and [docs/scripting-librepy-split.md](docs/scripting-librepy-split.md) for architecture details.

---

## Credits

| Project | Contribution |
| :--- | :--- |
| [localwriter](https://github.com/balisujohn/localwriter) | Original Writer LLM extension (John Balis) |
| [LibreCalc AI Assistant](https://extensions.libreoffice.org/en/extensions/show/99509) | Calc AI foundation and inspiration |
| [LibreOffice MCP Extension](https://github.com/quazardous/mcp-libre) | MCP server patterns, Makefile, tool registry |
| [Hermes Agent](https://github.com/NousResearch/hermes-agent) | Tool-call parsers, JSON repair, memory patterns |
| [latex2mathml](https://github.com/roniemartinez/latex2mathml) | LaTeX → MathML |
| [mathml-to-latex](https://github.com/asnunes/py-mathml-to-latex) | MathML → LaTeX (Writer formula export) |
| [isodate](https://github.com/gweis/isodate) | ISO 8601 duration parse/format (Calc wire) |

---

## License

**GNU GPL v3 (or later)** — see [`LICENSE`](LICENSE). Originally MPL 2.0; relicensed in 2026 for stronger reciprocity and library compatibility.

| Year | Contribution | Contributor |
| :--- | :--- | :--- |
| 2024 | Original release | John Balis |
| 2025–2026 | Config, registries, build system | quazardous |
| 2026 | Calc integration (originally MIT) | LibreCalc AI Assistant |
| 2026 | Modifications and relicensing | KeithCu |

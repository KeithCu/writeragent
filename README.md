# WriterAgent

![WriterAgent logo](https://raw.githubusercontent.com/KeithCu/writeragent/master/extension/assets/logo.png)

[![License: GPL v3+](https://img.shields.io/badge/License-GPL%20v3%2B-blue.svg)](https://www.gnu.org/licenses/gpl-3.0.html)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![LibreOffice 7.0+](https://img.shields.io/badge/LibreOffice-7.0%2B-green.svg)](https://www.libreoffice.org/)
[![Release](https://img.shields.io/github/v/release/KeithCu/writeragent)](https://github.com/KeithCu/writeragent/releases)

A LibreOffice extension (Python + UNO) that adds generative AI editing to Writer, Calc, and Draw.

---

## ⚡ Quick Start

1. **Install**: Download `.oxt` from [Releases](https://github.com/KeithCu/writeragent/releases), double-click to install
2. **Configure**: Open **WriterAgent > Settings** in LibreOffice
3. **Set Backend**: Enter endpoint (e.g., `http://localhost:11434` for Ollama) and model
4. **Chat**: Open sidebar with **View → Sidebar → WriterAgent** or use **Ctrl+Q** / **Ctrl+E** shortcuts

---

## 📑 Table of Contents

- [Local-First & Flexible](#local-first--flexible)
- [Powerful Feature Suites](#powerful-feature-suites)
- [Web Research & Fact-Checking](#web-research--fact-checking)
- [High-Fidelity Editing & Formatting](#high-fidelity-editing--formatting)
- [MCP Server](#mcp-server-optional)
- [Agent Backends](#agent-backends)
- [Architecture](#architecture)
- [Roadmap & Future Vision](#roadmap--future-vision)
- [Credits & Collaboration](#credits--collaboration)
- [Installation & Setup](#installation--setup)
- [Contributing](#contributing)
- [License](#license)

---

## Local-First & Flexible

Unlike proprietary office suites that lock you into a single cloud provider and send all your data to their servers, WriterAgent is local-first. You can run fast, private models locally (via Ollama, LM Studio, or local servers) ensuring your documents never leave your machine. If you choose to use cloud APIs, you can switch between providers (e.g., OpenRouter, Together.AI) in less than 2 seconds, maintaining full control over your data.

---

## Core Features

- **Grammar Checker**: Real-time, local-first editing with persistent storage of good/bad sentences.
- **TeX Import**: Seamless LaTeX and MathML support for scientific documents.


### 🖋️ Writer

- **Real-time Grammar Checker**: An experimental, asynchronous proofreader with a **sentence cache** and **Unicode-aware splitting**. Includes **Token-aware Overlap Repair** to fix "LLM slop" and ensure surgical replacements. [Read the Plan](docs/realtime-grammar-checker-plan.md).
- **Math & LaTeX**: **MathML** and **TeX** delimiters are automatically turned into **editable LibreOffice Math formulas** (OLE objects). [Design Docs](docs/libreoffice-html-math-dev-plan.md) & [Extraction Logic](docs/math-extraction-editing-dev-plan.md).
- **Advanced Editing**: Supports rich text, page layout, shapes, charts, bookmarks, fields, footnotes, and track-changes. [Specialized Toolsets](docs/writer-specialized-toolsets.md)
- **Format Preservation**: Uses a "surgical" replacement method that preserves existing bold, italics, highlights, and font sizes.
- **Extend & Edit Selection**: Quick shortcuts (**`Ctrl+Q`** to extend, **`Ctrl+E`** to rewrite) that act directly on your highlighted text.
- **Reference Guides**: [Footnotes](docs/footnotes-api-reference.md), [Bookmarks](docs/bookmarks-api-reference.md), [Page Layout](docs/page-api-reference.md), [Track Changes](docs/writer-tracking-api-reference.md), and [Section Replace Options](docs/section-replace-options.md).

### 📊 Calc

- **=PROMPT() Function**: Run AI prompts directly within spreadsheet cells.
- **Deep Analysis**: Analyze **pivot tables** and detect **complex logical errors** across massive datasets. [Analysis Tools](docs/calc-analysis-tools.md).
- **Rich Text Cells**: Paste **HTML** (bold, links, breaks) into a **single cell** using advanced StarWriter import paths.
- **Batch Range Edits**: Apply formulas and formatting in bulk. [Specialized Toolsets](docs/calc-specialized-toolsets.md).
- **Advanced Features**: [Conditional Formatting](docs/calc-conditional-formatting.md) and [Sheet Filtering (AutoFilter)](docs/calc-sheet-filter.md).


### 🌐 Multi-modal & Research

- **Web Research**: Powered by a private, vendored **smolagents** loop. [Web Research Loop](docs/agent-search.md) & [Search Integration](docs/search-engine-integration.md).
- **Audio & Voice**: Integrated cross-platform voice recording. [Audio Architecture](docs/audio-architecture.md).
- **Image Generation**: Generate or edit (Img2Img) images. [Image Generation Guide](docs/image-generation.md).

### 🧠 The Intelligence Core (LO-DOM)

- **Document Object Model (LO-DOM)**: A recursive model that understands structural relationships. [LO-DOM Semantic Tree](docs/lo-dom-semantic-tree.md).
- **Specialized Toolsets**: A nested API design that prevents context bloat. [Smol vs. Main Chat Tooling](docs/smol-main-chat-tool-architecture.md).
- **Persistent Memory**: [Agent Memory & Skills](docs/agent-memory-and-skills.md) and [Librarian Onboarding](docs/librarian-agentic-onboarding.md).
- **34 Locales**: Automated AI-driven translation and review pipeline. [Localization Pipeline](docs/localization.md).
- **Multilingual Grammar**: An optional feature that uses the LLM to identify and correct the underlying text language when typing in multiple languages, before running the grammar checker.

### 🎨 Showcase

| Feature | Screenshot |
|---------|------------|
| **Hermes + Opus 4.6 (Web Research)** | ![Hermes-Agent / Opus-4.6 Akihabara](Showcase/HermesAkihabara.png) |
| **Arch Linux Resume** | ![Opus 4.6 Resume](Showcase/Opus46Resume.png) |
| **Spreadsheet Dashboard** | ![Chat Sidebar with Dashboard](Showcase/Sonnet46Spreadsheet.png) |
| **Math Expressions** | ![Math Expressions](Showcase/Math.png) |
| **Sonnet diagram of an Arch Linux deity** | ![Sonnet 4.6 Visual](Showcase/Sonnet46ArchDiagram.jpg) |

---

## Web Research & Fact-Checking

Private, local web searches and fact-checking with citable sources. No data leaves your machine unless you opt into cloud APIs.

Powered by [Hugging Face smolagents](https://github.com/huggingface/smolagents) (vendored and adapted to have zero dependencies, per [this discussion](https://github.com/huggingface/smolagents/issues/1999)). Now you can ask the AI a question and it will search the web and give you the answer—with all requests running directly from your computer. It uses DuckDuckGo for privacy and executes the entire search-and-browse loop locally, ensuring your research stays private.


It's better than a standard Google search box because it understands natural language and can synthesize information from multiple pages.

- **Ask a question**: "What is the current version of Python and when was it released?"
- **Complex Tasks**: "Write a long and pretty summary of After the Software Wars, according to Wikipedia."
- **Real-time Data**: Ask it to find the current price of a specific item and it can update your document with current data.

---

## High-Fidelity Editing & Formatting

- **Two-Layer Editing**: Basic grammar fixes first, then detailed "add comment" feedback.
- **Formatting Preservation**: Maintains styles, tables, and images during edits.

WriterAgent is "format-aware." Unlike simpler plugins that strip away your hard work, our engine is designed to respect your document's visual integrity.


- **Format Preservation**: When fixing typos or rephrasing, WriterAgent uses a "surgical" replacement method. It preserves your existing bold, italics, highlights, and font sizes—even if the AI sends back plain text.
- **HTML-First Architecture**: For complex elements like tables, nested lists, and colored layouts, we use a robust HTML import layer. This ensures that what the AI "sees" and what it "writes" matches the professional standards of LibreOffice.
- **Legacy Support**: Optimized to work perfectly even on older versions of LibreOffice (pre-26.2) where native Markdown support is unavailable.
- **Tracked Changes Support**: Proper handling of tracked deletions, and streamed rewrite with single-undo.

One of the unique challenges of building an AI assistant for a rich word processor, unlike a plain-text code editor, is the multiple ways of applying formatting. Eventually, we will encourage models to output properly classed HTML that maps to your LibreOffice template. See [LLM_STYLES.md](LLM_STYLES.md) and [Styles & Formatting](docs/llm-styles.md).

---

## MCP Server (Optional)

Enable integration with [Model Context Protocol (MCP)](https://github.com/modelcontextprotocol) for advanced AI workflows.

When enabled in **WriterAgent > Settings**, an HTTP server runs on localhost and exposes the same Writer/Calc/Draw tools to external AI clients (Cursor, Claude Desktop, etc.).


- **Real-time Sidebar Monitoring**: All MCP activity (requests and tool results) is logged in real-time in the sidebar.
- **Targeting**: Clients target a document via the `**X-Document-URL**` header.
- **Hybrid AI Orchestrator Model**: This exposes the entire toolset to external agents while maintaining the document as the single source of truth.

---

## Agent Backends

- **Local**: Ollama, LM Studio, or custom servers (e.g., `http://localhost:11434`).
- **Cloud**: OpenRouter, Together.AI, or any OpenAI-compatible API.

You can plug in **external agent backends** so that Chat with Document uses an external process (e.g., Hermes or others) instead of the built-in LLM.


- **[Hermes ACP Integration](https://github.com/NousResearch/hermes-agent)**: Spawns Hermes locally as a subprocess using the Agent Communication Protocol (ACP) via stdio.
- **HITL (Approve/Reject)**: If a backend requests approval for a tool call, a dialog appears for the user.

---

## Architecture

![State Machine Architecture](Showcase/full_super_unified_complete.png)
*Figure 1: Unified state machine architecture for AI tool interactions.*

WriterAgent is engineered for professional-grade reliability, moving beyond simple script-based plugins. [WriterAgent Architecture Overview](docs/writeragent-architecture.md) & [Sidebar Implementation Guide](docs/chat-sidebar-implementation.md).


- **Finite State Machine (FSM)**: All complex AI interactions are managed by a pure FSM. This architecture breaks down the extension's behavior into small, isolated, and testable units of logic. See [Formal Verification](docs/formal_verification.md).
- **JSON Repair**: Uses a multi-stage parsing pipeline (inspired by **Hermes**) and **json-repair** to handle model syntax errors or Python-style literals. [LLM Hacks & Workarounds](docs/llm-hacks.md).
- **Async Threading**: A custom worker-pool and queue system keep the LibreOffice UI responsive during heavy reasoning. [Streaming & Threading](docs/streaming-and-threading.md) & [Threading Architecture](docs/threading_architecture.md).
- **Static Analysis**: [Type Checking](docs/type-checking.md) with (`ty`, `Mypy`, and `Pyright`).

- **Comprehensive Test Suite**: Over 500 tests ensuring stability. [Test Architecture](docs/test_architecture_analysis.md).


---

## Roadmap & Future Vision

Our primary focus is deep **LibreOffice Fidelity**—systematically closing the gap between the AI's capabilities and the full breadth of the UNO API to ensure the agent can manipulate every professional feature the suite offers.

Our application-specific roadmap is focused on closing the remaining gaps in the LibreOffice API surface:

- **🖋️ Writer**: We are expanding from text and style management into complex document automation, including **Mail Merge** (CSV/DB/Sheets), **Bibliographies**, and **Watermark** support. We are also evolving our **Sections** tools from read-only navigation to a full lifecycle suite (multi-column layouts, conditional visibility, and password protection).
- **📊 Calc**: Beyond cell and sheet manipulation, we are targeting advanced data modeling. This includes **Macros & VBA compatibility**, **Scenarios (what-if analysis)**, and **External Data** integration (SQL/Web queries). We are also working toward interactive controls like **Table Slicers**, comprehensive **Sheet Protection**, and experimental **Python/NumPy** support ([Native Python](docs/native_python_in_calc.md), [NumPy](docs/enabling_numpy_in_libreoffice.md)).

- **🎨 Draw & Impress**: We are moving toward full presentation mastery by adding support for **Slide Animations**, **Layer Management**, and **Slide Show Controls**. High-priority multimedia support, including **Audio/Video insertion** and **3D Shape** manipulation, will round out the creative suite.

Building on this foundation, we are eventually working on **Long-Document and Multi-Document Support**. Handling 100+ page documents is a complex engineering challenge; it will require internal caching, a **page-at-a-time navigation** system that allows the agent to move through large files while maintaining awareness of nested elements. For **Multi-Document Support**, we are leveraging the LibreOffice Desktop service to discover and coordinate between all open documents, and eventually expanding the agent's scope to operate on a **directory of files** to synthesize information across Writer reports, Calc sheets, and Draw presentations.

---

## Credits & Collaboration

WriterAgent stands on the shoulders of giants. We'd like to give credit to:

| Project | Contribution |
|---------|--------------|
| **[LibreCalc AI Assistant](https://extensions.libreoffice.org/en/extensions/show/99509)** | AI support for LibreOffice Calc provided the foundation and inspiration for our integration |
| **[LibreOffice MCP Extension](https://github.com/quazardous/mcp-libre)** | Embedded MCP server reference; we used their Makefile system, modular discoverable service registry, and tool registry |
| **[Hermes Agent](https://github.com/NousResearch/hermes-agent)** | Client-side tool call parsers and JSON repair |
| **[latex2mathml](https://github.com/roniemartinez/latex2mathml)** | Converts LaTeX to MathML |

---

## Recent Progress & Benchmarks (Apr 2026)

**LLM Evaluation Suite & Efficiency Rankings**

We have recently integrated an internal **LLM Evaluation Suite** directly into the LibreOffice UI. This allows users and developers to benchmark models across 10 (so far) real-world tasks in Writer, Calc, and Draw, tracking both accuracy and **Intelligence-per-Dollar (IpD)**. By fetching real-time pricing from OpenRouter, the system calculates the exact cost of every AI turn and ranks backends by **Value (C²/$)**—average correctness squared, divided by average dollars per run (higher is better).

<div align="center" style="overflow-x: auto;">

| Rank | Model | Avg correctness | Avg score | Avg tokens | Avg cost ($) | Value (C²/$) |
|------|-------|-----------------|-----------|------------|--------------|--------------|
| 1 | openai/gpt-oss-120b | 0.980 | 0.942 | 3767.1 | 0.00025 | 3827.240 |
| 2 | google/gemini-3-flash-preview | 0.890 | 0.860 | 2957.2 | 0.00035 | 2234.257 |
| 3 | qwen/qwen3.5-9b | 0.730 | 0.691 | 4645.0 | 0.00050 | 1068.806 |
| 4 | nvidia/nemotron-3-nano-30b-a3b | 0.922 | 0.851 | 7195.5 | 0.00082 | 1037.536 |
| 5 | mistralai/devstral-2512 | 0.980 | 0.950 | 3000.8 | 0.00154 | 623.434 |
| 6 | inception/mercury-2 | 0.948 | 0.896 | 5150.9 | 0.00160 | 562.405 |
| 7 | minimax/minimax-m2.7 | 0.990 | 0.943 | 4671.9 | 0.00191 | 512.581 |
| 8 | deepseek/deepseek-v3.2 | 0.985 | 0.909 | 7575.4 | 0.00206 | 470.222 |
| 9 | qwen/qwen3.5-35b-a3b | 0.990 | 0.933 | 5671.1 | 0.00220 | 445.760 |
| 10 | x-ai/grok-4.1-fast | 0.950 | 0.886 | 6431.9 | 0.00204 | 442.733 |
| 11 | qwen/qwen3.5-27b | 0.993 | 0.942 | 5049.9 | 0.00259 | 380.538 |
| 12 | qwen/qwen3.5-122b-a10b | 0.990 | 0.950 | 3958.8 | 0.00308 | 318.312 |
| 13 | nvidia/nemotron-3-super-120b-a12b:free | 0.757 | 0.696 | 6388.4 | 0.00181 | 317.859 |
| 14 | allenai/olmo-3.1-32b-instruct | 0.323 | 0.306 | 1912.4 | 0.00046 | 226.704 |
| 15 | z-ai/glm-5.1 | 0.890 | 0.843 | 4677.8 | 0.00524 | 151.141 |

</div>

### Key Benchmarking Insights (Apr 2026)

**Quadratic utility** (Value = C² ÷ average USD per run) on hardened, realistic Writer tasks highlights a few patterns that raw "accuracy only" tables can hide:

#### 1. Verbosity vs. average cost (Qwen 35B vs 122B)
**Qwen 3.5-35B-A3B** (rank 9) uses more tokens per run on average (**~5,671**) than **Qwen 3.5-122B-A10B** (rank 12, **~3,959**), but its lower **average cost per run** (**~0.00220** vs **~0.00308**) still yields a higher **Value (C²/$)** (**~446** vs **~318**). Token count alone does not determine dollar efficiency; list pricing and usage patterns interact.

#### 2. C² punishes unreliable "cheap" runs
**OLMo 3.1 32B Instruct** (rank 14) shows **~0.32** average correctness—squaring it crushes value despite a low **~0.00046** average cost. **Nemotron 3 Super 120B (free)** (rank 13) sits at **~0.76** correctness with mediocre value, a reminder that "free" or inexpensive is not enough when quality collapses.

#### 3. Value leader: GPT-OSS 120B
**openai/gpt-oss-120b** (rank 1) pairs **~0.98** average correctness with a very low **~0.00025** average cost per run, for **Value (C²/$) ≈ 3827**. **Google Gemini 3 Flash** (rank 2) remains a strong second (**~0.89** correctness, **Value ≈ 2234**) on this snapshot.

#### 4. Near-ceiling accuracy in the mid-pack
**Qwen 3.5-27B** (rank 11) reaches **~0.993** average correctness—among the highest in the table—while **x-ai/grok-4.1-fast** (rank 10) sits at **~0.95** with similar average cost. Both are useful "accuracy-first" options when the ranking metric is dominated by models with even lower **$/run** at the top.

This benchmarking framework is used to tune system prompts and select the best-performing models for local-first office automation. Details: [scripts/prompt_optimization/README.md](scripts/prompt_optimization/README.md).

**Sophisticated LLM-as-a-Judge Scoring.** We have moved beyond simple keyword matching to a nuanced, multi-dimensional evaluation system. A high-tier "Teacher" model (typically **Claude Sonnet 4.6**) generates gold-standard answers, while a specialized "Judge" model (**Grok 4.1 Fast**) evaluates performance using a weighted rubric.

This framework allows us to differentiate between "Flash" models that prioritize speed and "Frontier" models that possess the "taste" and refinement needed for professional documents.

**Fine-tuning.** An interesting direction is to **fine-tune a model** specifically for this tool set and task distribution: the same correctness could potentially be achieved with fewer reasoning steps and fewer tokens, improving both latency and Value (C²/$). The existing eval and dataset are a natural training signal (correct vs incorrect tool use, minimal vs verbose traces).

---

## Installation & Setup

### Installation

1. Download the latest `.oxt` file from the [releases page](https://github.com/KeithCu/writeragent/releases).
2. Double-click the downloaded `.oxt` file to install it in LibreOffice, then restart LibreOffice if prompted.

### Backend Setup

WriterAgent requires an OpenAI-compatible backend. Recommended options:

- **Ollama**: [ollama.com](https://ollama.com/) (easiest for local usage)
- **text-generation-webui**: [github.com/oobabooga/text-generation-webui](https://github.com/oobabooga/text-generation-webui)
- **OpenRouter / OpenAI**: Cloud-based providers

**For the GPU-poor**: If you don't have a powerful GPU or an API key, consider [OpenRouter](https://openrouter.ai/collections/free-models) (free models, but prompts may be used for training) or [Together.AI](https://www.together.ai/) (generous private free tier).

### Settings

Configure your endpoint, model, and behavior in **WriterAgent > Settings**. The dialog includes **Chat/Text** (endpoint, models, API key, etc.), **Image Settings** (size, aspect ratio, AI Horde options), **Http** (MCP server), **Agent backends**, and other tabs generated from the extension modules.

- **Endpoint URL**: e.g., `http://localhost:11434` for Ollama
- **Additional Instructions**: A shared system prompt for all features with history support
- **API Key**: Required for cloud providers
- **Connection Keep-Alive**: Automatically enabled to reduce latency
- **MCP Server**: On the **Http** tab; when enabled, an HTTP server runs on the configured port (default 8765) for external AI clients. Use **Toggle MCP Server** and **MCP Server Status** from the menu
- **Agent backends**: On the **Agent backends** tab; enable an external backend (Aider or Hermes) so Chat uses that agent instead of the built-in LLM. Paths and arguments are optional per backend
- **OpenRouter Chat Extras**: Advanced provider configuration via JSON editing (Settings → General → Edit config file) for provider routing, model selection, and request metadata

For detailed configuration examples, see [CONFIG_EXAMPLES.md](CONFIG_EXAMPLES.md).

### The Evolution of WriterAgent

A weekly chronicle of building a professional AI suite inside LibreOffice:

- **Week 1**: [Initial fork, sidebar chat, multi-turn tools, and async streaming](https://keithcu.com/wordpress/?p=5060)
- **Week 2 & 3**: [MCP, research sub-agent, voice support, and evaluation dashboard](https://keithcu.com/wordpress/?p=5112)
- **Week 4-6**: [State machines, formal verification, and specialized toolsets](https://keithcu.com/wordpress/?p=5245)
- **Week 6 & 7**: [Async grammar checking and TeX import support](https://keithcu.com/wordpress/?p=5276)

---

## Contributing

[![Ask DeepWiki](https://deepwiki.com/badge.svg)](https://deepwiki.com/KeithCu/writeragent)
[Good First Issues](https://github.com/KeithCu/writeragent/labels/good%20first%20issue)
[Discussions](https://github.com/KeithCu/writeragent/discussions)

> DeepWiki provides excellent analysis of the codebase, including visual dependency graphs

### Local Development

**Update May 11, 2026:** Removed `dspy` from `pyproject.toml` to remove dependencies like `litellm`. Run `uv sync` to update your `.venv`. If you want to do prompt optimization, install manually.

**Prerequisites:** Python 3.11+, [uv](https://docs.astral.sh/uv/), and LibreOffice with `unopkg` on your PATH. Run `make check-setup` to verify.

```bash
# Clone the repository
git clone https://github.com/KeithCu/writeragent.git
cd writeragent

# Build the extension package (.oxt)
make build

# Full dev cycle: build + reinstall + restart LibreOffice + show log
make deploy
or
make deploy writer, calc, draw, or impress

# Run typecheckers (ty, mypy, pyright) then tests
make test

# See all available targets
make help
```

---

## License

WriterAgent is released under the **GNU General Public License v3 (or later)**. See `LICENSE` for the full text. This transition ensures all improvements remain open and reciprocal under GPL v3.

### History & Attribution

WriterAgent was originally released under the **MPL 2.0** license. In 2026, it was transitioned to GPL v3 to ensure stronger protection for user freedoms and better compatibility with modern Python libraries.

| Year | Contribution | Contributor |
|------|--------------|--------------|
| 2024 | Original release | John Balis |
| 2025-2026 | Config, registries, build system | quazardous |
| 2026 | Calc integration features (originally MIT) | LibreCalc AI Assistant |
| 2026 | Modifications and relicensing | KeithCu |

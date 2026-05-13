# WriterAgent

> [!IMPORTANT]
> **Please update to version 0.7.2 or later.** Initial versions of WriterAgent did not include proper version information, meaning LibreOffice will never notify you that WriterAgent is out of date. Starting with version 0.7.2, the extension includes it, and a weekly update check to keep you informed of new releases. This version also includes many bug fixes and new features. Please update now to get the best experience and future notifications.

**Note:** We are excited to announce an official release of the extension! However, the [version on the LibreOffice site](https://extensions.libreoffice.org/en/extensions/show/99526) is **updated less frequently** than this repository. For the **newest builds with the latest features and fixes**, please use the GitHub release from this repo instead.

![WriterAgent logo](https://raw.githubusercontent.com/KeithCu/writeragent/master/extension/assets/logo.png)

A LibreOffice extension (Python + UNO) that adds generative AI editing to Writer, Calc, and Draw.

> [!NOTE]
> **Update May 11, 2026:** Removed `dspy` from `pyproject.toml` to remove dependencies like `litellm`. Run `uv sync` to update your `.venv`. If you want to do prompt optimization, install manually.

![GPLv3+](https://img.shields.io/badge/License-GPL%20v3%2B-blue.svg) ![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-blue.svg)

## 📑 Table of Contents
- [1. Local-First & Flexible](#1-local-first--flexible)
- [2. Powerful Feature Suites](#2-powerful-feature-suites)
- [3. Web Research & Fact-Checking](#3-web-research--fact-checking-local--private)
- [4. High-Fidelity Editing & Formatting](#5-high-fidelity-editing--formatting)
- [5. MCP Server](#6-mcp-server-optional)
- [6. Agent Backends](#7-agent-backends)
- [7. Architecture](#3-architecture)
- [8. Roadmap & Future](#-roadmap--future-vision)
- [9. Credits & Collaboration](#credits--collaboration)

### The Evolution of WriterAgent
A weekly chronicle of building a professional AI suite inside LibreOffice:

- **Week 1:** [Initial fork, sidebar chat, multi-turn tools, and async streaming](https://keithcu.com/wordpress/?p=5060).
- **Week 2 & 3:** [MCP, research sub-agent, voice support, and evaluation dashboard](https://keithcu.com/wordpress/?p=5112).
- **Week 4-6:** [State machines, formal verification, and specialized toolsets](https://keithcu.com/wordpress/?p=5245).
- **Week 6 & 7:** [Async grammar checking and TeX import support](https://keithcu.com/wordpress/?p=5276).

[![Ask DeepWiki](https://deepwiki.com/badge.svg)](https://deepwiki.com/KeithCu/writeragent)

> DeepWiki provides excellent analysis of the codebase, including visual dependency graphs

WriterAgent provides powerful AI-driven capabilities integrated directly into your LibreOffice suite:

### 1. Local-First & Flexible

Unlike proprietary office suites that lock you into a single cloud provider and **send all your data to their servers**, WriterAgent is **local-first**. You can run fast, private models locally (via Ollama, LM Studio, or local servers) ensuring your documents never leave your machine. If you choose to use cloud APIs, you can switch between them in less than 2 seconds, maintaining full control over where your data goes.

## For the GPU-poor

If you don't have a powerful GPU, or an API key for LLMs, we encourage you to sign up for a service provider like [OpenRouter](https://openrouter.ai/collections/free-models) to access their extensive collection of free AI models. As they state on their platform:

> "At OpenRouter, we believe that free models play a crucial role in democratizing access to AI. These models allow hundreds of thousands of users worldwide to experiment, learn, and innovate."

Please note, the prompts to free models are often saved and used for training purposes.

Another option is [Together.AI](https://www.together.ai/), which also has a variety of high-performance and intelligent cost-effective models with a generous, private,  free tier.

### 2. Powerful Feature Suites

##### 🖋️ Writer & Professional Publishing
- **Real-time Grammar Checker**: An experimental, asynchronous proofreader with a **sentence cache** and **Unicode-aware splitting**. Includes **Token-aware Overlap Repair** to fix "LLM slop" and ensure surgical replacements. [Read the Plan](docs/realtime-grammar-checker-plan.md).
- **Math & LaTeX**: **MathML** and **TeX** delimiters are automatically turned into **editable LibreOffice Math formulas** (OLE objects). [Design Docs](docs/libreoffice-html-math-dev-plan.md) & [Extraction Logic](docs/math-extraction-editing-dev-plan.md).
- **Advanced Editing**: Supports rich text, page layout, shapes, charts, bookmarks, fields, footnotes, and track-changes. [Specialized Toolsets](docs/writer-specialized-toolsets.md) & [Writer Tools Deep Dive](docs/writer_tools_analysis.md).
- **Format Preservation**: Uses a "surgical" replacement method that preserves existing bold, italics, highlights, and font sizes.
- **Extend & Edit Selection**: Quick shortcuts (**`Ctrl+Q`** to extend, **`Ctrl+E`** to rewrite) that act directly on your highlighted text.
- **Reference Guides**: [Footnotes](docs/footnotes-api-reference.md), [Bookmarks](docs/bookmarks-api-reference.md), [Page Layout](docs/page-api-reference.md), [Track Changes](docs/writer-tracking-api-reference.md), and [Section Replace Options](docs/section-replace-options.md).

#### 📊 Calc & Data Intelligence
- **=PROMPT() Function**: Run AI prompts directly within spreadsheet cells.
- **Deep Analysis**: Analyze **pivot tables** and detect **complex logical errors** across massive datasets. [Analysis Tools](docs/calc-analysis-tools.md).
- **Rich Text Cells**: Paste **HTML** (bold, links, breaks) into a **single cell** using advanced StarWriter import paths.
- **Batch Range Edits**: Apply formulas and formatting in bulk. [Specialized Toolsets](docs/calc-specialized-toolsets.md).
- **Advanced Features**: [Conditional Formatting](docs/calc-conditional-formatting.md), [Sheet Filtering (AutoFilter)](docs/calc-sheet-filter.md), and [Native Python Support](docs/native_python_in_calc.md). (See also: [Enabling NumPy](docs/enabling_numpy_in_libreoffice.md)).

#### 🌐 Multi-modal & Research
- **Web Research**: Powered by a private, vendored **smolagents** loop. [Web Research Loop](docs/agent-search.md) & [Search Integration](docs/search-engine-integration.md).
- **Audio & Voice**: Integrated cross-platform voice recording. [Audio Architecture](docs/audio-architecture.md).
- **Image Generation**: Generate or edit (Img2Img) images. [Image Generation Guide](docs/image-generation.md).

#### 🧠 The Intelligence Core (LO-DOM)
- **Document Object Model (LO-DOM)**: A recursive model that understands structural relationships. [LO-DOM Semantic Tree](docs/lo-dom-semantic-tree.md).
- **Specialized Toolsets**: A nested API design that prevents context bloat. [Smol vs. Main Chat Tooling](docs/smol-main-chat-tool-architecture.md).
- **Persistent Memory**: [Agent Memory & Skills](docs/agent-memory-and-skills.md) and [Librarian Onboarding](docs/librarian-agentic-onboarding.md).
- **34 Locales**: Automated AI-driven translation and review pipeline. [Localization Pipeline](docs/localization.md).
- **Multilingual Grammar**: An optional feature that uses the LLM to identify and correct the underlying text language when typing in multiple languages, before running the grammar checker.

#### Showcase
Hermes-Agent with Claude Opus 4.6 and the Web Research sub-agent:
![Hermes-Agent / Opus-4.6 Akihabara](Showcase/HermesAkihabara.png)

Opus 4.6 one-shotted this Arch Linux resume:
![Opus 4.6 Resume](Showcase/Opus46Resume.png)
Sonnet 4.6 one-shotted this "pretty spreadsheet"
![Chat Sidebar with Dashboard](Showcase/Sonnet46Spreadsheet.png)
Tex Math, also showing a blue underline from the incomplete sentence.
![Math Expressions](https://github.com/KeithCu/writeragent/blob/master/Showcase/Math.png)


### 3. Web Research & Fact-Checking (Local & Private)

Powered by [Hugging Face smolagents](https://github.com/huggingface/smolagents) (vendored and adapted to have zero dependencies, per [this discussion](https://github.com/huggingface/smolagents/issues/1999)). Now you can ask the AI a question and it will search the web and give you the answer—with all requests running directly from your computer. It uses DuckDuckGo for privacy and executes the entire search-and-browse loop locally, ensuring your research stays private.

It's better than a standard Google search box because it understands natural language and can synthesize information from multiple pages.

- **Ask a question**: "What is the current version of Python and when was it released?"
- **Complex Tasks**: "Write a long and pretty summary of After the Software Wars, according to Wikipedia."
- **Real-time Data**: Ask it to find the current price of a specific item and it can update your document with current data.



### 4. High-Fidelity Editing & Formatting

WriterAgent is "format-aware." Unlike simpler plugins that strip away your hard work, our engine is designed to respect your document's visual integrity.

- **Format Preservation**: When fixing typos or rephrasing, WriterAgent uses a "surgical" replacement method. It preserves your existing bold, italics, highlights, and font sizes—even if the AI sends back plain text.
- **HTML-First Architecture**: For complex elements like tables, nested lists, and colored layouts, we use a robust HTML import layer. This ensures that what the AI "sees" and what it "writes" matches the professional standards of LibreOffice.
- **Legacy Support**: Optimized to work perfectly even on older versions of LibreOffice (pre-26.2) where native Markdown support is unavailable.
- **Tracked Changes Support**: Proper handling of tracked deletions, and streamed rewrite with single-undo.

One of the unique challenges of building an AI assistant for a rich word processor, unlike a plain-text code editor, is the multiple ways of applying formatting. Eventually, we will encourage models to output properly classed HTML that maps to your LibreOffice template. See [LLM_STYLES.md](LLM_STYLES.md) and [Styles & Formatting](docs/llm-styles.md).

### 5. MCP Server (Optional)

When enabled in **WriterAgent > Settings**, an HTTP server runs on localhost and exposes the same Writer/Calc/Draw tools to external AI clients (Cursor, Claude Desktop, etc.).

- **Real-time Sidebar Monitoring**: All MCP activity (requests and tool results) is logged in real-time in the sidebar.
- **Targeting**: Clients target a document via the `**X-Document-URL`** header.
- **Hybrid AI Orchestrator Model**: This exposes the entire toolset to external agents while maintaining the document as the single source of truth.

### 6. Agent Backends

You can plug in **external agent backends** so that Chat with Document uses an external process (e.g. Hermes or others) instead of the built-in LLM.

- **[Hermes ACP Integration](https://github.com/NousResearch/hermes-agent)**: Spawns Hermes locally as a subprocess using the Agent Communication Protocol (ACP) via stdio.
- **HITL (Approve/Reject)**: If a backend requests approval for a tool call, a dialog appears for the user.

### 7. Architecture
WriterAgent is engineered for professional-grade reliability, moving beyond simple script-based plugins. [WriterAgent Architecture Overview](docs/writeragent-architecture.md) & [Sidebar Implementation Guide](docs/chat-sidebar-implementation.md).

- **Finite State Machine (FSM)**: All complex AI interactions are managed by a pure FSM. This architecture breaks down the extension's behavior into small, isolated, and testable units of logic. See [Formal Verification](docs/formal_verification.md).
- **JSON Repair**: Uses a multi-stage parsing pipeline (inspired by **Hermes**) and **json-repair** to handle model syntax errors or Python-style literals. [LLM Hacks & Workarounds](docs/llm-hacks.md).
- **Async Threading**: A custom worker-pool and queue system keep the LibreOffice UI responsive during heavy reasoning. [Streaming & Threading](docs/streaming-and-threading.md) & [Threading Architecture](docs/threading_architecture.md).
- **Static Analysis**: [Type Checking](docs/type-checking.md) with (**`ty`**, **Mypy**, and **Pyright**).
- **Comprehensive Test Suite**: Over 500 tests ensuring stability. [Test Architecture](docs/test_architecture_analysis.md).

![State Machine Architecture](Showcase/full_super_unified_complete.png)

## 8. 🚀 Roadmap & Future Vision
Our primary focus is deep **LibreOffice Fidelity**—systematically closing the gap between the AI's capabilities and the full breadth of the UNO API to ensure the agent can manipulate every professional feature the suite offers.

Our application-specific roadmap is focused on closing the remaining gaps in the LibreOffice API surface:
- **🖋️ Writer**: We are expanding from text and style management into complex document automation, including **Mail Merge** (CSV/DB/Sheets), **Bibliographies**, and **Watermark** support. We are also evolving our **Sections** tools from read-only navigation to a full lifecycle suite (multi-column layouts, conditional visibility, and password protection).
- **📊 Calc**: Beyond cell and sheet manipulation, we are targeting advanced data modeling. This includes **Macros & VBA compatibility**, **Scenarios (what-if analysis)**, and **External Data** integration (SQL/Web queries). We are also working toward interactive controls like **Table Slicers** and comprehensive **Sheet Protection**.
- **🎨 Draw & Impress**: We are moving toward full presentation mastery by adding support for **Slide Animations**, **Layer Management**, and **Slide Show Controls**. High-priority multimedia support, including **Audio/Video insertion** and **3D Shape** manipulation, will round out the creative suite.

Building on this foundation, we are eventually working on **Long-Document and Multi-Document Support**. Handling 100+ page documents is a complex engineering challenge; it will require internal caching, **page-at-a-time navigation** system that allows the agent to move through large files while maintaining awareness of nested elements. For **Multi-Document Support**, we are leveraging the LibreOffice Desktop service to discover and coordinate between all open documents, and eventually expanding the agent's scope to operate on a **directory of files** to synthesize information across Writer reports, Calc sheets, and Draw presentations.
## 9. Credits & Collaboration

WriterAgent stands on the shoulders of giants. We'd like to give credit to:

**[LibreCalc AI Assistant](https://extensions.libreoffice.org/en/extensions/show/99509)**

Their work on AI support for LibreOffice Calc provided the foundation and inspiration for our integration. We sent multiple emails thanking them and asking to collaborate but haven't heard back.

**[LibreOffice MCP Extension](https://github.com/quazardous/mcp-libre)**

Their work on an embedded MCP (Model Context Protocol) server for LibreOffice was an invaluable reference for providing the back-end interface to WriterAgent's tool set. We used their Makefile system, modular discoverable service registry, tool registry.

**[Hermes Agent](https://github.com/NousResearch/hermes-agent)**

Client-side tool call parsers and JSON repair.

**[latex2mathml](https://github.com/roniemartinez/latex2mathml)**

**latex2mathml** converts LaTeX to MathML.

## Recent Progress & Benchmarks (Apr 2026)

**LLM Evaluation Suite & Efficiency Rankings**

We have recently integrated an internal **LLM Evaluation Suite** directly into the LibreOffice UI. This allows users and developers to benchmark models across 10 (so far) real-world tasks in Writer, Calc, and Draw, tracking both accuracy and **Intelligence-per-Dollar (IpD)**. By fetching real-time pricing from OpenRouter, the system calculates the exact cost of every AI turn and ranks backends by **Value (C²/$)**—average correctness squared, divided by average dollars per run (higher is better).

### Intelligence per dollar (higher is better)


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


---

### Key Benchmarking Insights (Apr 2026)

**Quadratic utility** (Value = C² ÷ average USD per run) on hardened, realistic Writer tasks highlights a few patterns that raw “accuracy only” tables can hide:

#### 1. Verbosity vs. average cost (Qwen 35B vs 122B)

**Qwen 3.5-35B-A3B** (rank 9) uses more tokens per run on average (**~5,671**) than **Qwen 3.5-122B-A10B** (rank 12, **~3,959**), but its lower **average cost per run** (**~0.00220** vs **~0.00308**) still yields a higher **Value (C²/$)** (**~446** vs **~318**). Token count alone does not determine dollar efficiency; list pricing and usage patterns interact.

#### 2. C² punishes unreliable “cheap” runs

**OLMo 3.1 32B Instruct** (rank 14) shows **~0.32** average correctness—squaring it crushes value despite a low **~0.00046** average cost. **Nemotron 3 Super 120B (free)** (rank 13) sits at **~0.76** correctness with mediocre value, a reminder that “free” or inexpensive is not enough when quality collapses.

#### 3. Value leader: GPT-OSS 120B

**openai/gpt-oss-120b** (rank 1) pairs **~0.98** average correctness with a very low **~0.00025** average cost per run, for **Value (C²/$) ≈ 3827**. **Google Gemini 3 Flash** (rank 2) remains a strong second (**~0.89** correctness, **Value ≈ 2234**) on this snapshot.

#### 4. Near-ceiling accuracy in the mid-pack

**Qwen 3.5-27B** (rank 11) reaches **~0.993** average correctness—among the highest in the table—while **x-ai/grok-4.1-fast** (rank 10) sits at **~0.95** with similar average cost. Both are useful “accuracy-first” options when the ranking metric is dominated by models with even lower **$/run** at the top.

This benchmarking framework is used to tune system prompts and select the best-performing models for local-first office automation. Details: [scripts/prompt_optimization/README.md](scripts/prompt_optimization/README.md).

**Sophisticated LLM-as-a-Judge Scoring.** We have moved beyond simple keyword matching to a nuanced, multi-dimensional evaluation system. A high-tier "Teacher" model (typically **Claude Sonnet 4.6**) generates gold-standard answers, while a specialized "Judge" model (**Grok 4.1 Fast**) evaluates performance using a weighted rubric.

This framework allows us to differentiate between "Flash" models that prioritize speed and "Frontier" models that possess the "taste" and refinement needed for professional documents.

**Fine-tuning.** An interesting direction is to **fine-tune a model** specifically for this tool set and task distribution: the same correctness could potentially be achieved with fewer reasoning steps and fewer tokens, improving both latency and Value (C²/$). The existing eval and dataset are a natural training signal (correct vs incorrect tool use, minimal vs verbose traces).



## Getting started

### Installation

1. Download the latest `.oxt` file from the [releases page](https://github.com/KeithCu/writeragent/releases).
2. Double-click the downloaded `.oxt` file to install it in LibreOffice, then restart LibreOffice if prompted.

### Backend setup

WriterAgent requires an OpenAI-compatible backend. Recommended options:

- **Ollama**: [ollama.com](https://ollama.com/) (easiest for local usage)
- **text-generation-webui**: [github.com/oobabooga/text-generation-webui](https://github.com/oobabooga/text-generation-webui)
- **OpenRouter / OpenAI**: Cloud-based providers.

### Settings

Configure your endpoint, model, and behavior in **WriterAgent > Settings**. The dialog includes **Chat/Text** (endpoint, models, API key, etc.), **Image Settings** (size, aspect ratio, AI Horde options), **Http** (MCP server), **Agent backends**, and other tabs generated from the extension modules.

- **Endpoint URL**: e.g., `http://localhost:11434` for Ollama.
- **Additional Instructions**: A shared system prompt for all features with history support.
- **API Key**: Required for cloud providers.
- **Connection Keep-Alive**: Automatically enabled to reduce latency.
- **MCP Server**: On the **Http** tab; when enabled, an HTTP server runs on the configured port (default 8765) for external AI clients. Use **Toggle MCP Server** and **MCP Server Status** from the menu.
- **Agent backends**: On the **Agent backends** tab; enable an external backend (Aider or Hermes) so Chat uses that agent instead of the built-in LLM. Paths and arguments are optional per backend.
- **OpenRouter Chat Extras**: Advanced provider configuration via JSON editing (Settings → General → Edit config file) for provider routing, model selection, and request metadata.

For detailed configuration examples, see [CONFIG_EXAMPLES.md](CONFIG_EXAMPLES.md).

## Contributing

### Local Development

**Prerequisites:** Python 3.11+, [uv](https://docs.astral.sh/uv/), and LibreOffice with `unopkg` on your PATH. Run `make check-setup` to verify.

```bash
# Clone the repository
git clone https://github.com/KeithCu/writeragent.git
cd writeragent

# Build the extension package (.oxt)
make build

# Full dev cycle: build + reinstall + restart LibreOffice + show log
make deploy

# Or for fast iteration: symlink the project into LO extensions (no rebuild needed)
make dev-deploy

# Run typecheckers (ty, mypy, pyright) then tests
make test

# See all available targets
make help
```

![Sonnet 4.6 architecture diagram](Showcase/Sonnet46ArchDiagram.jpg)

## License

WriterAgent is released under the **GNU General Public License v3 (or later)**. See `LICENSE` for the full text.

### History & Attribution

WriterAgent was originally released under the **MPL 2.0** license. In 2026, it was transitioned to GPL v3 to ensure stronger protection for user freedoms and better compatibility with modern Python libraries.

Copyright (c) 2024 John Balis
Copyright (c) 2025-2026 quazardous (config, registries, build system)
Copyright (c) 2026 LibreCalc AI Assistant (Calc integration features, originally MIT)
Copyright (c) 2026 KeithCu (modifications and relicensing)

---

## 📚 Documentation Index
The documents below are specialized reports, configuration guides, and future integration plans that are not linked in the main sections above.

- **🛠️ Development & Tooling**
  - [Evaluation & Benchmarking Plan](docs/eval-dev-plan.md)
  - [Agent Architectures Analysis](docs/agent_architectures_analysis.md)
  - [Architecture: LangChain Integration Plan](docs/langchain-plan.md)

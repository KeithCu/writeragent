# WriterAgent

> [!IMPORTANT]
> **Please update to version 0.7.4.** Initial versions of WriterAgent did not include proper version information, meaning LibreOffice will never notify you that WriterAgent is out of date. Starting with version 0.7.2, the extension includes it, and a weekly update check to keep you informed of new releases. This version also includes many bug fixes and new features. Please update now to get the best experience and future notifications. With this version's specialized tools for document editing, it becomes possible to add full-fidelity editing features to LibreOffice without overwhelming models.

**Note:** We are excited to announce an official release of the extension! However, the [version on the LibreOffice site](https://extensions.libreoffice.org/en/extensions/show/99526) is **updated less frequently** than this repository. For the **newest builds with the latest features and fixes**, please use the GitHub release from this repo instead.

![WriterAgent logo](https://raw.githubusercontent.com/KeithCu/writeragent/master/extension/assets/logo.png)

A LibreOffice extension (Python + UNO) that adds generative AI editing to Writer, Calc, and Draw.

**Development story:** Read the writeup of how WriterAgent was built [here](https://keithcu.com/wordpress/?p=5060).

[![Ask DeepWiki](https://deepwiki.com/badge.svg)](https://deepwiki.com/KeithCu/writeragent)

> DeepWiki provides excellent analysis of the codebase, including visual dependency graphs.

## Features

WriterAgent provides powerful AI-driven capabilities integrated directly into your LibreOffice suite:

### 1. Local-First & Flexible
Unlike proprietary office suites that lock you into a single cloud provider and **send all your data to their servers**, WriterAgent is **local-first**. You can run fast, private models locally (via Ollama, LM Studio, or local servers) ensuring your documents never leave your machine. If you choose to use cloud APIs, you can switch between them in less than 2 seconds, maintaining full control over where your data goes.


## For the GPU-poor: OpenRouter Free Models

If you don't have a powerful GPU, or an API key for LLMs, we encourage you to sign up for a service provider like [OpenRouter](https://openrouter.ai/collections/free-models) to access their extensive collection of free AI models. As they state on their platform:

> "At OpenRouter, we believe that free models play a crucial role in democratizing access to AI. These models allow hundreds of thousands of users worldwide to experiment, learn, and innovate."

Please note, the prompts to free models are often saved and used for training purposes.

Another option is [Together.AI](https://www.together.ai/), which also has a variety of high-performance and intelligent cost-effective models with a generous, private,  free tier.


### 2. Chat with Document & Architecture
The main way to interact with your document. While you can ask it anything, **its primary job is to edit your document**, not just answer questions.

#### Features & Performance
*   **Sidebar Panel**: A dedicated deck in the right sidebar for multi-turn chat. It supports tool-calling to read and edit the document directly.
*   **Responsive Streaming**: Unlike simple extensions that freeze when waiting for an AI response, WriterAgent uses a background thread and queue system. This keeps the LibreOffice UI alive and responsive while text and tool calls stream in and are executed.
*   **Interleaved Multi-Step Tools**: The engine natively supports interleaved reasoning tokens, content streaming, and complex multi-turn tool calling. This allows for sophisticated AI behavior that handles multi-step tasks while keeping the user informed in real-time.
*   **High-Throughput (200+ tps)**: Optimized for speed, the system can easily handle 200 tokens per second with zero UI stutter.
*   **Persistent Chat History**: Previous conversations are automatically saved and restored when you reopen a document. History is stored in a local SQLite database in the user config directory.
*   **Isolated Task Contexts**: Each open document in LibreOffice gets its own independent AI sidebar. The AI stays aware of the specific document it's attached to, preventing "cross-talk" when working on multiple projects.
*   **Audio Recording**: Integrated cross-platform voice support directly in the sidebar.
*   **Image Generation**: Generate from chat or edit selected images (Img2Img) using AI Horde or your configured endpoint.
*   **Calc =PROMPT() Function**: Run AI prompts directly within spreadsheet cells.
*   **Librarian onboarding agent**: For new users, a Librarian / Welcome sub-agent chats with the user to learn preferences (name, tone, etc.) and uses the `upsert_memory` tool to store them.
*   **Robust Session Tracking**: Chat history is linked directly to the document using an internal metadata ID, ensuring your conversation follows the document even if renamed or moved.
*   **Multilingual & HiDPI**: Ships with the interface translated into 9 languages (Spanish, French, Portuguese, Russian, German, Japanese, Italian, Polish, and English) and optimized for modern high-resolution displays using device-independent units.

#### Showcase
Hermes-Agent with Claude Opus 4.6 and the Web Research sub-agent:
![Hermes-Agent / Opus-4.6 Akihabara](Showcase/HermesAkihabara.png)

Opus 4.6 one-shotted this Arch Linux resume:
![Opus 4.6 Resume](Showcase/Opus46Resume.png)
Sonnet 4.6 one-shotted this "pretty spreadsheet"
![Chat Sidebar with Dashboard](Showcase/Sonnet46Spreadsheet.png)


### 3. Web Research & Fact-Checking (Local & Private)
Powered by [Hugging Face smolagents](https://github.com/huggingface/smolagents) (vendored and adapted to have zero dependencies, per [this discussion](https://github.com/huggingface/smolagents/issues/1999)). Now you can ask the AI a question and it will search the web and give you the answer—with all requests running directly from your computer. It uses DuckDuckGo for privacy and executes the entire search-and-browse loop locally, ensuring your research stays private.

It's better than a standard Google search box because it understands natural language and can synthesize information from multiple pages.
*   **Ask a question**: "What is the current version of Python and when was it released?"
*   **Complex Tasks**: "Write a long and pretty summary of After the Software Wars, according to Wikipedia."
*   **Real-time Data**: Ask it to find the current price of a specific item and it can update your document with current data.

### 4. Extend & Edit Selection (Writer)
Two Writer shortcuts act on the current selection:

*   **Extend selection** (`Ctrl+Q`): The model continues the selected text. Ideal for drafting emails, stories, or generating lists.
*   **Edit selection** (`Ctrl+E`): Prompt the model to rewrite your selection according to specific instructions (e.g., "make this more formal", "translate to Spanish").

### 5. HTML Richness & Compatibility (Writer)
When you ask the AI to fix a typo or change a name, the result can keep the formatting you already had: highlights, bold, colors, font size, and so on.

*   **HTML-First Document Interaction**: While some models may still attempt to use Markdown, WriterAgent is optimized for HTML. This ensures higher fidelity for complex structures like tables and lists, and ensures the extension works robustly on versions of LibreOffice preceding the 26.2 release.
*   **Native Formatting Persistence**: For structured content (HTML), WriterAgent injects AI-generated text using the import filter, mapping tags to native styles. For plain-text replacements (e.g. typo fixes), we preserve your existing per-character formatting so highlights, bold, and colors stay intact.
*   **Auto-detection**: If the AI returns plain text, we use a format-preserving path that keeps your existing background colors and highlights. If it returns HTML (or Markdown), we use the native import path.
*   **Format-preserving replacement (auto-detected)**: When the AI sends back plain text, WriterAgent automatically preserves every per-character property (colors, fonts, sizes).

### Ongoing Challenge: Styles vs. Custom Formatting
One of the unique challenges of building an AI assistant for a rich word processor, unlike a plain-text code editor, is the multiple ways of applying formatting. Eventually, we will encourage models to output properly classed HTML that maps to your LibreOffice template. See [LLM_STYLES.md](LLM_STYLES.md).

### 6. Specialized Toolsets & Expanded API
LibreOffice Writer exposes an enormous UNO surface (fields, indexes, tables, frames, embedded objects, shapes, charts, track changes, and more). If **every** operation is advertised to the model on every turn, choice gets noisier and providers struggle.

The sidebar exposes a rich set of Writer operations:
*   **Styles**: The AI can discover paragraph and character style names (including localized names).
*   **Comments**: The AI can read, add, and remove inline comments.
*   **Track Changes**: The AI can make edits in tracked-changes mode.
*   **Tables**: The AI can enumerate named tables and read/write full content cells.

Active development explores **progressive disclosure**:
- **Specialized sub-agent (delegation)** — The main chat keeps a small default tool surface; some turns hand off to a nested LLM run with a different tool set (e.g. `tables`, `styles`, `layout`, `fields`, `indexes`).
- **Swapping tools per send or mode** — Attaching only domain tools while keeping full history.

See **[docs/writer-specialized-toolsets.md](docs/writer-specialized-toolsets.md)** for architecture details.

### 7. MCP Server (Optional)
When enabled in **WriterAgent > Settings**, an HTTP server runs on localhost and exposes the same Writer/Calc/Draw tools to external AI clients (Cursor, Claude Desktop, etc.).

*   **Real-time Sidebar Monitoring**: All MCP activity (requests and tool results) is logged in real-time in the sidebar.
*   **Targeting**: Clients target a document via the **`X-Document-URL`** header.
*   **Hybrid AI Orchestrator Model**: This exposes the entire toolset to external agents while maintaining the document as the single source of truth.

### 8. Agent Backends
You can plug in **external agent backends** so that Chat with Document uses an external process (e.g. Hermes or others) instead of the built-in LLM.

*   **[Hermes ACP Integration](https://github.com/NousResearch/hermes-agent)**: Spawns Hermes locally as a subprocess using the Agent Communication Protocol (ACP) via stdio.
*   **HITL (Approve/Reject)**: If a backend requests approval for a tool call, a dialog appears for the user.

## Credits & Collaboration

WriterAgent stands on the shoulders of giants. We'd like to give massive credit to:

**[LibreCalc AI Assistant](https://extensions.libreoffice.org/en/extensions/show/99509)**

Their pioneering work on AI support for LibreOffice provided the foundation and inspiration for our enhanced Calc integration. We've built upon their excellent tools to create more ambitious and performance-oriented spreadsheet features. We sent multiple emails thanking them and asking to work together but haven't heard back.

**[LibreOffice MCP Extension](https://github.com/quazardous/mcp-libre)**

Their work on an embedded MCP (Model Context Protocol) server for LibreOffice was an invaluable reference for expanding WriterAgent's Writer tool set. From their project we adapted production-quality UNO implementations for style inspection, comment management, track-changes control, and table editing — resulting in 12 new Writer tools now available to WriterAgent's embedded AI. We also used their patterns for server lifecycle, health-check probing, and port utilities when we added WriterAgent's built-in MCP HTTP server. We're grateful for the high-quality open work.

**[Hermes Agent](https://github.com/NousResearch/hermes-agent)**

Their client-side tool call parsers (from `environments/tool_call_parsers/`) provide the core logic adapted in our `plugin/contrib/tool_call_parsers/` sub-module, allowing local inference models (Hermes, Mistral, Llama, DeepSeek) to trigger structured tool loops via raw text streams.

## Performance & Batch Optimizations

To handle complex spreadsheet tasks, WriterAgent is optimized for high-throughput "batch" operations:

*   **Batch Tool-Calling**: Instead of making one-by-one changes, tools like `write_formula_range` and `set_cell_style` operate on entire ranges in a single call.
*   **High-Volume Insertion**: The `write_formula_range` tool allows the AI to generate and inject large CSV datasets instantly. This is orders of magnitude faster than inserting data cell-by-cell; we found that providing these batch tools encourages the AI to perform far more ambitious spreadsheet automation and data analysis.
*   **Optimized Ranges**: Formatting and number formats are applied at the range level, minimizing UNO calls and ensuring the UI remains fluid even during heavy document analysis.

## Recent Progress & Benchmarks (Feb 2026)

We have recently integrated an internal **LLM Evaluation Suite** directly into the LibreOffice UI. This allows users and developers to benchmark models across 10 (so far) real-world tasks in Writer, Calc, and Draw, tracking both accuracy and **Intelligence-per-Dollar (IpD)**. By fetching real-time pricing from OpenRouter, the system calculates the exact cost of every AI turn and ranks backends by their value-to-performance ratio.

**Top 10 models by Value (C²/$)** (Writer eval set; (avg correctness)² ÷ total cost; higher = better quality/value ratio):

| Rank | Model | Value (C²/$) | Avg Correctness | Tokens/Run | Cost ($) |
|------|--------|----------|-----------------|------------|----------|
| 1 | openai/gpt-oss-120b | 263.8 | 0.920 | 50,198 | 0.0032 |
| 2 | google/gemini-3-flash-preview | 141.0 | 0.940 | 50,179 | 0.0063 |
| 3 | openai/gpt-4o-mini | 70.5 | 0.790 | 47,540 | 0.0089 |
| 4 | nvidia/nemotron-3-nano-30b-a3b | 60.6 | 0.560 | 50,243 | 0.0052 |
| 5 | x-ai/grok-4.1-fast | 46.5 | 0.980 | 66,929 | 0.0207 |
| 6 | nex-agi/deepseek-v3.1-nex-n1 | 39.4 | 0.915 | 64,222 | 0.0213 |
| 7 | minimax/minimax-m2.1 | 39.2 | 0.983 | 62,394 | 0.0246 |
| 8 | mistralai/devstral-2512 | 27.9 | 0.910 | 57,150 | 0.0297 |
| 9 | z-ai/glm-4.7 | 26.9 | 0.953 | 63,035 | 0.0337 |
| 10 | qwen/qwen3.5-27b | 26.5 | 0.993 | 52,210 | 0.0371 |
| 11 | openai/gpt-5-nano | 26.4 | 0.825 | 99,576 | 0.0258 |
| 12 | allenai/olmo-3.1-32b-instruct | 20.8 | 0.570 | 68,317 | 0.0156 |
| 13 | qwen/qwen3.5-122b-a10b | 14.9 | 0.932 | 62,424 | 0.0583 |
| 14 | qwen/qwen3.5-35b-a3b | 13.1 | 0.980 | 80,773 | 0.0734 |
| 15 | anthropic/claude-haiku-4.5 | 11.3 | 0.993 | 60,730 | 0.0874 |
| 16 | nvidia/nemotron-3-super-120b-a12b:free | 9.3 | 0.770 | 138,870 | 0.0639 |
| 17 | anthropic/claude-sonnet-4.6 | 4.3 | 1.000 | 54,890 | 0.2351 |
---

### Key Benchmarking Insights (Feb 2026)

Our recent transition to **Quadratic Utility Scoring ($Value = C^2/USD$)** and hardened, realistic datasets has revealed several counter-intuitive truths about AI document engineering:

#### 1. The "Verbosity Tax" (Example: Qwen 35B vs 122B)
List prices don't tell the whole story. **Qwen 35B-A3B** (Rank 14) has a list price ~37% lower than the flagship **Qwen 122B-A10B** (Rank 13). However, because the 35B model is far more "chatty" (using 80,773 tokens vs 62,424 for the same tasks), it actually **costs 25% more** to complete the benchmark. This "Verbosity Tax" easily wipes out the perceived savings of smaller models.

#### 2. The "Quality Premium"
By squaring the correctness score ($C^2$), we ensure that "cheap but broken" models like **Nemotron** (#4) no longer dominate the leaderboard. A model that fails ~45% of professional office tasks is accurately penalized as a liability, allowing smarter, more reliable models like **GPT-4o-mini** to leapfrog them in value.

#### 3. The Value "Elite": Gemini 3 Flash
**Google Gemini 3 Flash** (Rank 2) is currently the "best all-rounder" for WriterAgent. It maintains near-perfect accuracy (**0.940**) while remaining cheap enough to yield a value of **141.0**—nearly twice that of the nearest competitor in its tier.

#### 4. The "Budget Sonnet": Qwen 27B
While **Claude Sonnet 4.6** (Rank 16) is our only perfect 1.000 accuracy model, **Qwen 3.5-27B** (Rank 10) achieved an incredible **0.993** accuracy at less than 1/6th the cost. If you need flawless document engineering on a budget, the dense Qwen 27B is currently the "Gold Standard" of high-intensity mid-range models.

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
*   **Ollama**: [ollama.com](https://ollama.com/) (easiest for local usage)
*   **text-generation-webui**: [github.com/oobabooga/text-generation-webui](https://github.com/oobabooga/text-generation-webui)
*   **OpenRouter / OpenAI**: Cloud-based providers.

### Settings
Configure your endpoint, model, and behavior in **WriterAgent > Settings**. The dialog includes **Chat/Text** (endpoint, models, API key, etc.), **Image Settings** (size, aspect ratio, AI Horde options), **Http** (MCP server), **Agent backends**, and other tabs generated from the extension modules.

*   **Endpoint URL**: e.g., `http://localhost:11434` for Ollama.
*   **Additional Instructions**: A shared system prompt for all features with history support.
*   **API Key**: Required for cloud providers.
*   **Connection Keep-Alive**: Automatically enabled to reduce latency.
*   **MCP Server**: On the **Http** tab; when enabled, an HTTP server runs on the configured port (default 8765) for external AI clients. Use **Toggle MCP Server** and **MCP Server Status** from the menu.
*   **Agent backends**: On the **Agent backends** tab; enable an external backend (Aider or Hermes) so Chat uses that agent instead of the built-in LLM. Paths and arguments are optional per backend.

For detailed configuration examples, see [CONFIG_EXAMPLES.md](CONFIG_EXAMPLES.md).

## Contributing

### Local Development

**Prerequisites:** Python 3.11+, [uv](https://docs.astral.sh/uv/), PyYAML (`uv pip install pyyaml`), and LibreOffice with `unopkg` on your PATH. Run `make check-setup` to verify.

Alternatively, use Docker to build with no local dependencies (see `make docker-build`).

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

![Architecture Diagram](Showcase/Sonnet46ArchDiagram.jpg)


## License
WriterAgent is released under the **GNU General Public License v3 (or later)**. See `LICENSE` for the full text.

### History & Attribution
WriterAgent was originally released under the **MPL 2.0** license. In 2026, it was transitioned to GPL v3 to ensure stronger protection for user freedoms and better compatibility with modern Python libraries.

Copyright (c) 2024 John Balis
Copyright (c) 2025-2026 quazardous (config, registries, build system)
Copyright (c) 2026 LibreCalc AI Assistant (Calc integration features, originally MIT)
Copyright (c) 2026 KeithCu (modifications and relicensing)


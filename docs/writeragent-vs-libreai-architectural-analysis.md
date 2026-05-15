# Architect's Guide: WriterAgent vs. LibreAI
## Building the Future of Office Automation

### 1. The Core Philosophy
Two projects, two radically different paths toward the same goal: bringing Generative AI to the 200M+ users of LibreOffice.

*   **WriterAgent (Python + UNO):** An **Agentic Orchestrator**. Designed for complex, multi-turn reasoning, tool use (MCP, smolagents), and deep document manipulation. It prioritizes **development velocity** and **extensibility**.
*   **LibreAI (C++17 + Qt6):** A **Native Writing Assistant**. Designed for performance, responsive UI, and zero-dependency deployments. It prioritizes **runtime efficiency** and **visual polish**.

---

### 2. Development Velocity: The Python Advantage
In the AI space, the rate of change is measured in weeks, not years. C++ is a significant bottleneck in this environment.

**Case Study: Feature Lead Times**
| Feature | WriterAgent (Python) | C++ Equivalent Estimate |
| :--- | :--- | :--- |
| **TeX / MathML Support** | **~1 hour** (using `latex2mathml`) | Days/Weeks (manual parsing or heavy library linking) |
| **Web Research Loop** | **~2 hours** (vendoring `smolagents`) | Months (implementing an async search-and-browse loop) |
| **MCP Server** | **Integrated** (standard HTTP libs) | Complex (writing a protocol-compliant server in C++) |

**The Developer Experience:**
*   **WriterAgent:** "Idea to Feature" happens in hours. The codebase is "hackable" for the 90% of AI devs who already live in the Python ecosystem.
*   **LibreAI:** Every new feature requires managing headers, build systems (CMake), and strict type hierarchies. Contributing to a C++ UNO component is a niche skill; contributing to a Python AI agent is a mainstream skill.

---

### 3. Architectural Comparison

#### **WriterAgent: The Finite State Machine (FSM)**
WriterAgent uses a pure FSM to manage the lifecycle of an AI turn. This ensures that even when an LLM "slops" or hallucinate a tool call, the system can gracefully recover or ask for human-in-the-loop (HITL) approval.
*   **LO-DOM:** A recursive document object model that translates flat UNO objects into a semantic tree the AI can understand.
*   **Async Stream Queue:** A custom worker-pool that keeps the UI thread idle while the "brain" is thinking.

#### **LibreAI: The Signal/Slot Pattern**
LibreAI uses the classic Qt event loop. It is faster at the "edge" (UI interaction) but lacks the internal "reasoning" layer. It sends a prompt and waits for a signal. It is a one-shot engine, whereas WriterAgent is a loop-based agent.

---

### 4. The Path Forward: Best of Both Worlds
The goal is to marry WriterAgent's **intelligence** with LibreAI's **polish**.

#### **Strategy A: The Qt6 UI Layer (Optional)**
While WriterAgent currently uses native UNO Dialogs (preserving the "Office" feel), we are exploring **Qt6 (PyQt6/PySide6)** as an optional UI layer. 
*   **Why:** Qt6 allows for modern VS Code-style themes, better Markdown rendering, and custom widgets that UNO's XDL format cannot support.
*   **The Hybrid Approach:** Keep the Python worker threads and FSM logic, but pipe the results into a sleek Qt6 sidebar.

#### **Strategy B: Feature Porting (The "FOSS Win")**
We should selectively port the high-value logic from LibreAI's C++ codebase into our Python environment:
1.  **Secure Keyring Storage:** Porting their `CredentialStore` logic to Python's `keyring` library. This removes API keys from plaintext config files.
2.  **Batch Section Rewriting:** Porting the heading-based document segmentation logic from `DocumentParser.cpp` to create a "Whole Document" processing mode.
3.  **Advanced Impress Layouts:** Adopting their method for handling `DrawingDocument` shapes to improve slide generation.

---

### 5. Conclusion: Why WriterAgent Wins
C++ is a powerful tool for building an office suite, but it is a "dying" choice for building **AI applications**. The ecosystem is simply moving too fast. WriterAgent's Python architecture allows us to integrate the latest breakthroughs from HuggingFace, OpenAI, and the MCP community in the time it takes a C++ compiler to finish a clean build.

We welcome contributors who want to build the "Brain" of the office suite, not just a better "Find and Replace" tool.

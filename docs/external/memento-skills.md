# Discussion: Integrating Memento-Skills into WriterAgent

This document explores how the **Memento-Skills** paradigm—focused on **Self-Evolution**, **Skill Learning**, and **Read/Write Reflective Loops**—can be integrated into the WriterAgent (LibreOffice) extension. 

## 1. Executive Summary

Where traditional agents have a static set of tools and a fresh memory every time you open a document, a **Memento-style** WriterAgent would treat each interaction as an opportunity to learn. It moves from being a **"clueless editor"** to a **"knowledgeable collaborator"** that remembers your style, project facts, and learns new, complex workflows (skills) over time without requiring updates to the extension code.

---

## 2. Current Implementation vs. Memento-Skills

| Feature | Current WriterAgent (Disabled/Basic) | Memento-Skills (Proposed) |
| :--- | :--- | :--- |
| **Memory Storage** | Flat `USER.md` / `MEMORY.md` with basic JSON KV upserts. | Structured "Sessions" and "Profiles" with snapshots of the environment (CWD, Project type). |
| **Skill Model** | CRUD operations on `SKILL.md` files (Instructions/Body). | Dedicated **Skill Routing**: Dynamically injects only relevant skills into the prompt based on user intent. |
| **Logic Loop** | Direct tool calling: `Intent -> Action`. | **Reflective Loop**: `Plan -> Execute -> Reflect -> Write`. Optimization of skills after failure. |
| **Verification** | Manual testing/skipping in pytest. | Built-in `memento verify` for auditing and validating skill execution in sandboxes. |

---

## 3. Core Benefits of Integration

### A. Persistent Personalization (The "Librarian")
The "Librarian" mode can serve as an onboarding bridge. Instead of jumping straight into editing a document, the agent can maintain a `USER.md` profile containing:
- **Writing Style**: "Avoid passive voice," "Use Oxford commas," "Write like an academic paper."
- **Formatting Preferences**: "Tables should always have alternating row colors," "Headings should be Outfit font."
- **Project Context**: "This document is part of the '2026 Solaris Project'."

### B. Efficiency via Skill Routing
As WriterAgent gains more specialized tools (Tables, Shapes, Styles, Charts), the system prompt becomes cluttered. Memento-Skills' **Skill Routing** allows the agent to:
1. Listen to the user's request.
2. Search the local **Skill Library** for relevant procedures (e.g., "Insert complex bibliography").
3. Inject *only* the documentation for those relevant tools/skills, keeping the context window clean and the model focused.

### C. Self-Evolution: Learning from Correction
If a user corrects the agent (e.g., "No, that's not how we format our quarterly reports"), the **Reflective Loop** would trigger:
- **Reflection**: "The current procedure for 'quarterly report' is incorrect or outdated."
- **Write**: The agent autonomously patches the "Quarterly Report" skill or updates the `USER.md` memory to prevent the mistake in the future.

---

## 4. Proposed Integration Patterns

### Pattern 1: Grounded Memory (Level 1)
Enable the currently commented-out **Context Injection**. Every time a user sends a message, WriterAgent reads `USER.md` and `MEMORY.md` and prepends them as `# [USER PROFILE]` and `# [PROJECT MEMORY]`.
- **Benefit**: The model "knows" you from the first message of a new session.
- **Complexity**: Low. (Already mostly implemented but disabled).

### Pattern 2: Expert Skill Packs (Level 2)
Group related Writer tasks into "Skills." Instead of monolithic Python tools, a skill is a folder containing a `SKILL.md` (instructions) and optional templates.
- **Example**: A "Grant Writer" skill could contain specific headings, formatting rules, and an example template (Markdown/XML).
- **Benefit**: Users can share, edit, and "teach" the agent specific business workflows without writing Python code.

### Pattern 3: The Background Reviewer (Level 3)
Adopt the Hermes/Memento pattern of a **Background Agent**. After the main writer task is finished, a smaller, cheaper LLM run analyzes the transcript:
- "Did I learn a new fact about the user?" -> **Update Memory**.
- "Did I perform a complex multi-step task successfully?" -> **Crystallize into a Skill**.
- "Did I fail and get corrected?" -> **Patch Existing Skill**.

---

## 5. Architectural Implementation Path

To achieve this without destabilizing the current extension:

1.  **Phase 1: Hydrate Memory**: Enable the [MemoryStore](cci:2://file:///home/keithcu/Desktop/Python/writeragent/plugin/chatbot/memory.py:11:0-32:19) and [MemoryTool](cci:2://file:///home/keithcu/Desktop/Python/writeragent/plugin/chatbot/memory.py:34:0-106:67) to start populating `USER.md` using the "Librarian" toggle (reducing the toolset to *only* memory/chat).
2.  **Phase 2: Progressive Skill Disclosure**: Implement the "Skill Router" in `tool_registry.py`. Instead of returning all `tier=core` tools, return `core` + `matched_skills` based on a quick keyword match or embedding search of the [skills/](cci:1://file:///home/keithcu/Desktop/Python/writeragent/plugin/chatbot/skills.py:34:4-55:21) directory.
3.  **Phase 3: Sandbox Evolution**: Introduce a `skill-creator` tool (inspired by Memento) that can write new `SKILL.md` files describing how to combine existing Writer tools (e.g., "First call `get_styles`, then `apply_style` to every Heading 1").
4.  **Phase 4: Async Reflection**: Use the `run_in_background` pool to trigger a "Reflect" pass after the `UI_THREAD` has finished rendering the response to the user.

## 6. Conclusion

Adding Memento-style features transforms WriterAgent from a **reactive** tool to a **proactive** assistant. By leveraging the existing [memory.py](cci:7://file:///home/keithcu/Desktop/Python/writeragent/plugin/chatbot/memory.py:0:0-0:0) and [skills.py](cci:7://file:///home/keithcu/Desktop/Python/writeragent/plugin/chatbot/skills.py:0:0-0:0) files and infusing them with Memento's **routing** and **reflection** logic, we can create an agent that actually gets smarter the more it is used within LibreOffice.

---

**Next Steps Recommended**:
- [ ] Uncomment [MemoryTool](cci:2://file:///home/keithcu/Desktop/Python/writeragent/plugin/chatbot/memory.py:34:0-106:67) and `SkillTools` in [plugin/chatbot/__init__.py](cci:7://file:///home/keithcu/Desktop/Python/writeragent/plugin/chatbot/__init__.py:0:0-0:0).
- [ ] Wire the `[AGENT MEMORY]` injection in [plugin/doc/document_helpers.py](cci:7://file:///home/keithcu/Desktop/Python/writeragent/plugin/doc/document_helpers.py:0:0-0:0).
- [ ] Create a prototype "Librarian" prompt in `constants.py` for "Profile/Onboarding" mode.

---
### Summary of Work

I have reviewed the `Memento-Skills` subdirectory and the existing dormant memory/skills implementation in `WriterAgent`. 
The discussion document above outlines:
- A comparison of the current basic implementation vs. the self-evolving Memento model.
- Key benefits like **Skill Routing** (clearing context clutter) and **Self-Evolution** (learning from user corrections).
- A 4-phase plan for integration, moving from basic context injection to a full background reflective learner. 

This provides a roadmap for making the agent more personalized and capable through persistent, evolving memory.


Ran command: `ls -d ~/.hermes/ ~/hermes-agent/ 2>/dev/null`
Ran command: `ls -R ~/.hermes/`
Ran command: `find ~ -maxdepth 2 -type d -name "hermes-agent*" -o -name "hermes" 2>/dev/null`
Listed directory hermes-agent
Ran command: `ls ~/.hermes/hermes-agent/tools/memory_tool.py ~/.hermes/hermes-agent/tools/skill_tool.py 2>/dev/null`
Listed directory tools
Viewed memory_tool.py:1-549
Viewed tools.py:1-289

# Comparison: WriterAgent vs. Hermes-Agent vs. Memento-Skills

This document compares the implementation and design of memory and skill systems across the three related projects.

## 1. High-Level Comparison

| Feature | WriterAgent (Current) | Hermes-Agent (~/.hermes) | Memento-Skills |
| :--- | :--- | :--- | :--- |
| **Primary Goal** | Document Assistant | General Purpose Assistant | Self-Evolving Agent |
| **Logic Origin** | Forked/Derived from Hermes | The "Upstream" reference | Advanced Research Branch |
| **Memory Format** | JSON Key-Value Store | List of entries (Delimiter: `§`) | Session-based Snapshots |
| **Skill Format** | Folder with `SKILL.md` | Folder with `SKILL.md` | Folder with `SKILL.md` |
| **Routing** | Static (All or None) | Proactive (Frozen Snapshots) | Dynamic ([search_skill](cci:1://file:///home/keithcu/Desktop/Python/writeragent/Memento-Skills/core/memento_s/tools.py:139:4-194:54)) |

---

## 2. Memory Implementation Details

### WriterAgent ([memory.py](cci:7://file:///home/keithcu/Desktop/Python/writeragent/plugin/chatbot/memory.py:0:0-0:0))
*   **Mechanism**: Uses a standard Python dictionary persisted as JSON in `USER.md` and `MEMORY.md`.
*   **Structure**: Supports nested keys via dot notation (e.g., `user.preferences.style`).
*   **Injection**: Commented out in `document.py`. The model must currently call `memory_read` to see values.
*   **Design Intent**: Aimed at "Librarian" style data—structured facts about the user.

### Hermes-Agent ([memory_tool.py](cci:7://file:///home/keithcu/.hermes/hermes-agent/tools/memory_tool.py:0:0-0:0))
*   **Mechanism**: A list of independent text entries separated by the section sign (`§`).
*   **Structured Security**: Includes a **Safety Scanner** ([_scan_memory_content](cci:1://file:///home/keithcu/.hermes/hermes-agent/tools/memory_tool.py:74:0-86:15)) to prevent prompt injection or credential exfiltration through memory.
*   **Durable Registry**: Uses `fcntl.flock` and atomic temp-file swaps to ensure absolute data integrity during high-concurrency tool calls.
*   **Cache Strategy**: Implements a **Frozen Snapshot** pattern. It loads memory once at session start and keeps that string constant in the system prompt. This ensures **LLM Prefix Caching** remains stable even if the agent writes new memories mid-session.

---

## 3. Skill System Design

All three projects share the "Skills as Folders" DNA, where a skill is defined by a `SKILL.md` file (Instructions) and auxiliary files. However, the *application* differs:

### Skill Management Styles
1.  **WriterAgent**: Basic CRUD. The model can create or edit `SKILL.md` files. It is primarily a way to store "Instructions-as-a-Tool."
2.  **Hermes-Agent**: Focuses on **Skills Guard** and **Sync**. It handles complex dependency management and ensures skills don't interfere with each other. It also supports "Skill Distribution" (sharing skills).
3.  **Memento-Skills**: Focuses on **Dynamic Execution**. It doesn't just store the skill; it uses specialized "Routing" tools ([search_skill](cci:1://file:///home/keithcu/Desktop/Python/writeragent/Memento-Skills/core/memento_s/tools.py:139:4-194:54)) to find the right skill for a task and then [execute_skill](cci:1://file:///home/keithcu/Desktop/Python/writeragent/Memento-Skills/core/memento_s/tools.py:196:4-269:54) to run it. It is designed to allow the agent to **rewrite its own skill code** to fix errors.

---

## 4. Key Takeaways: Code vs. Design

1.  **Code Ancestry**: 
    *   The `WriterAgent` code is a **re-implementation** of the Hermes design. It follows the same filenames and folder structures but changes the internal data model (JSON vs. Delimited Text).
    *   `Memento-Skills` is a **significant evolution** that abstracts tools away into a specialized `SkillGateway`.

2.  **Design Philosophy**:
    *   **Hermes** is about **stability and security**. It treats memory as a protected "System Prompt extension" and provides the most robust file-locking.
    *   **WriterAgent** is about **integration**. It simplifies the Hermes concepts to fit into the LibreOffice UNO environment.
    *   **Memento-Skills** is about **autonomy**. It treats skills as fluid capabilities that the agent can discover on the fly via search.

3.  **Why they look identical on disk**: 
    The folder structure `skills/<domain>/<name>/SKILL.md` has become a "Common Language" across these agents. This allows for potential **Skill Portability**: a skill written for Hermes-Agent could (with slight prompt tweaks) be dropped into WriterAgent or Memento-Skills and work immediately because they all share the same layout conventions.

---

### Comparison of Memory Tools

```python
# WriterAgent (JSON)
{ "user": { "name": "Keith", "pref": "Dark Mode" } }

# Hermes-Agent (Section Delimited)
Keith is the user.
§
The user prefers Dark Mode in all UIs.
```

The Hermes-Agent approach is more LLM-friendly for raw "brain dumping," while the WriterAgent approach is easier for programmatic UI fields (Settings tabs).

### Summary of Work
I have compared the `WriterAgent`, `Hermes-Agent`, and `Memento-Skills` implementations. I found that while they share the same directory structure and "Skill.md" convention, they differ significantly in **data serialization** (JSON vs. Section Delimiters) and **model orchestration** (Static vs. Frozen Snapshots vs. Dynamic Routing). I have documented these differences and the design rationale in the artifact above.
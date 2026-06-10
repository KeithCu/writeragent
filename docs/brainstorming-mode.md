# Brainstorming mode (specialized delegate)

Brainstorming is a **multi-turn design sub-agent** entered when the main Writer chat delegates `domain="brainstorming"`. It explores ideas with the user, can use web and document research, and writes an **approved HTML design spec** into the active Writer document.

**Entry:** Main agent only — e.g. user asks to brainstorm or plan a feature → `delegate_to_specialized_writer_toolset(domain="brainstorming", task="…")`.

**Not in v1:** Visual companion (browser mockups), automatic writing-plans handoff, Calc/Draw spec save.

---

## Flow

```mermaid
sequenceDiagram
    participant User
    participant MainAgent
    participant DelegateGateway
    participant Panel
    participant BrainstormSubagent

    User->>MainAgent: "Let's brainstorm X"
    MainAgent->>DelegateGateway: delegate(domain=brainstorming)
    DelegateGateway->>Panel: _in_brainstorming_mode=True
    DelegateGateway->>BrainstormSubagent: first turn
    BrainstormSubagent-->>User: reply_to_user (HTML)
    User->>Panel: answer
    Panel->>BrainstormSubagent: next turn
    BrainstormSubagent->>BrainstormSubagent: save_design_spec
    BrainstormSubagent-->>Panel: brainstorming_finished
    Panel->>Panel: clear brainstorming mode
```

| Phase | Handler |
|-------|---------|
| Turn 0 | Main agent calls delegate; gateway starts session |
| Turns 1..N | Panel routes Send to `brainstorming_session` (bypasses main tool loop) |
| Exit | `brainstorming_finished` clears `_in_brainstorming_mode` |

Implementation: [`plugin/chatbot/brainstorming.py`](../plugin/chatbot/brainstorming.py), [`plugin/chatbot/panel.py`](../plugin/chatbot/panel.py), [`plugin/doc/specialized_base.py`](../plugin/doc/specialized_base.py).

---

## HTML everywhere

All brainstorming outputs use HTML — no Markdown in tool arguments.

| Surface | Format |
|---------|--------|
| Sidebar (`reply_to_user`, `brainstorming_finished`) | Single HTML string (`<p>`, `<h2>`, `<ul>`, …) |
| Saved spec (`save_design_spec`) | JSON **array** of HTML strings (same as `apply_document_content.content`) |
| Research shown to user | Sub-agent reformats plain-text web/doc results as HTML in chat |

Rules: [`HTML_FRAGMENT_RULES`](../plugin/framework/constants.py), [`WRITER_APPLY_DOCUMENT_HTML_RULES`](../plugin/framework/constants.py), [`get_chat_response_format_instructions`](../plugin/framework/constants.py).

Example spec array:

```json
[
  "<h1>Design: Feature Name</h1>",
  "<p><em>Status: approved</em></p>",
  "<h2>Goals</h2>",
  "<ul><li>…</li></ul>",
  "<h2>Architecture</h2>",
  "<p>…</p>"
]
```

`save_design_spec` uses `target="end"` by default; `full_document` only when the doc is empty.

---

## Sub-agent tools

| Tool | Role |
|------|------|
| `brainstorm_research_web` | Public web research (plain text in; HTML summary out via `reply_to_user`) |
| `list_nearby_files`, `grep_nearby_files`, `delegate_read_document` | Same-folder document research |
| `get_document_content`, `get_document_tree`, `search_in_document` | Active Writer context |
| `save_design_spec` | **Only** document write path |
| `reply_to_user` | Continue conversation (smol final-answer tool) |
| `brainstorming_finished` | End session |

Raw `apply_document_content` is **not** exposed to the brainstorming sub-agent.

---

## Prompt source

Adapted from [superpowers brainstorming](../superpowers/skills/brainstorming/SKILL.md) (not runtime-imported): one question per turn, 2–3 approaches, section-by-section approval, HARD-GATE against implementation until spec is saved.

Constants: `BRAINSTORMING_SUB_AGENT_INSTRUCTIONS`, `get_brainstorming_sub_agent_instructions()` in [`plugin/framework/constants.py`](../plugin/framework/constants.py).

---

## Tests

[`tests/chatbot/test_brainstorming.py`](../tests/chatbot/test_brainstorming.py) — delegate enum, session callback branch, `save_design_spec` HTML passthrough, smol examples.

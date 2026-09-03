# Sidebar slash commands (research)

**Status:** not implemented. Candidate list only. WriterAgent has no `/` parser today.

Typed in the **chat sidebar** Ask box (Aider/Cursor style). Not a LibreOffice menu, not LibrePy, not a skills system.

Gold source: [Aider-AI/aider](https://github.com/Aider-AI/aider) `aider/commands.py` and [in-chat commands](https://aider.chat/docs/usage/commands.html). [KeithCu/aider](https://github.com/KeithCu/aider) matches that inventory (same `commands.py`). Steal the *shape* of `/` as in-chat control — do not copy Aider’s system prompt or few-shots.

Aider is a git coding agent. WriterAgent is a document + chat assistant (Writer / Calc / Draw). Candidates below are what that would mean *here*, including **new** product behavior, not aliases for buttons that already exist.

---

## What Aider ships

| Command | Purpose |
|---------|---------|
| `/help` | List commands, or ask a help coder about Aider |
| `/clear` | Wipe chat history |
| `/reset` | Drop chat files **and** wipe history |
| `/model` `/models` | Switch or search the main LLM |
| `/editor-model` `/weak-model` | Switch architect/editor helper models |
| `/chat-mode` `/ask` `/code` `/architect` `/context` `/ok` | Switch edit format / mode; `/ok` means “go ahead and edit” |
| `/settings` | Print current settings |
| `/tokens` | Approximate context-window use |
| `/think-tokens` `/reasoning-effort` | Reasoning budget |
| `/voice` | Record and transcribe into the prompt |
| `/web` | Scrape one URL into the chat |
| `/copy` `/copy-context` `/paste` | Clipboard last reply / repo context / image-or-text |
| `/editor` `/edit` | Open an external editor to write the next prompt |
| `/multiline-mode` | Swap Enter vs Meta+Enter |
| `/report` | Open a GitHub issue |
| `/add` `/drop` `/read-only` `/ls` | Chat file set (what the model may edit) |
| `/save` `/load` | Persist / replay that file set |
| `/map` `/map-refresh` | Repository map |
| `/commit` `/undo` `/diff` `/git` | Git on the repo Aider is editing |
| `/lint` `/run` `/test` | Lint dirty files; run a shell command (`!` alias) |
| `/exit` `/quit` | Leave the CLI |

Aider interrupts a run with **Ctrl-C**, not `/stop`.

**Skip unless a real office analog exists:** `/add` `/drop` `/read-only` `/ls` `/save` `/load` `/map` `/map-refresh` `/commit` `/git` `/lint` `/test` and Aider’s **git** `/undo` `/diff`. Those are repo file-map and VCS ops. WriterAgent does not have a chat file set or a project git repo. Nearby / open LibreOffice documents are the analog — see `/attach` below — not a fake `/add`.

Also skip: `/exit` `/quit` (no process to leave), `/editor-model` `/weak-model` (no dual architect/editor models), `/multiline-mode` (Ask box is already multiline).

---

## Candidates

**Source:** Aider = named after an Aider command. WA-native = no Aider counterpart (or only a loose analogy).

**Priority:** obvious = worth doing if `/` lands. stretch = new product work; do not build `/` just to justify it.

### Chat control

| Command | In WriterAgent | Source | Priority |
|---------|----------------|--------|----------|
| `/help` | Print this command list in the response pane (static; not Aider’s help-coder RAG). | Aider | obvious |
| `/clear` | New chat: wipe the **active** sidebar transcript (Chat / Web / Librarian session, not all three). | Aider | obvious |
| `/stop` | Cancel the in-flight send / tool loop / research run. | WA-native (Aider: Ctrl-C) | obvious |
| `/model [id]` | Switch the chat model for this sidebar; empty = show current + recent LRU. | Aider | obvious |
| `/models [query]` | Search the endpoint catalog (same list Settings already fetches). | Aider | stretch |
| `/mode [name]` | Switch Chat, Image, Web Research, Deep Research, Brainstorming, Writing Plan, PPT-Master, Librarian. Empty = list modes valid for this document type. | Aider `/chat-mode` | obvious |
| `/ask [text]` | This turn (or until `/mode chat`): answer in the sidebar only — no `apply_document_content` / mutating tools. | Aider `/ask` | obvious |
| `/ok` | “Go ahead”: apply the last approved outline / research / draft into the document (Writing Plan, Brainstorming, or last chat draft). | Aider `/ok` | stretch |
| `/tokens` | Show rough context use: system + `[DOCUMENT CONTENT]` + history. | Aider | stretch |
| `/copy` | Copy the last assistant reply (HTML or plain) to the clipboard. | Aider | stretch |
| `/paste` | Append clipboard text (or a clipboard image, if Image mode) into the Ask box. | Aider | stretch |
| `/voice` | Start / stop sidebar mic capture (same path as Record / Stop Rec on Send). | Aider | obvious |
| `/settings` | Open WriterAgent Settings. | Aider (they print; we open the dialog) | obvious |
| `/report [title]` | Open the existing Report bug flow. | Aider | stretch |
| `/export [chat\|doc]` | Save the sidebar transcript, or export the document / selection (Markdown / HTML / plain). | WA-native (Aider `/copy-context` is the closest) | stretch |

### Document and selection

| Command | In WriterAgent | Source | Priority |
|---------|----------------|--------|----------|
| `/apply` | Apply the last assistant draft into the **active** document (`apply_document_content` path). Optional target: selection / cursor / end. | WA-native | obvious |
| `/undo` | Undo the last **agent** edit (grouped LibreOffice undo for `apply_document_content` / streamed rewrite). Not git. | Loose Aider `/undo` | obvious |
| `/diff` | Show what the last agent edit changed (redlines / before–after), without accepting or rejecting. | Loose Aider `/diff` | stretch |
| `/review [off\|record\|wait]` | Set `doc.agent_edit_review_mode` (Writer tracked-change review). Empty = show current. | WA-native | stretch |
| `/edit [instruction]` | Rewrite the current **selection** (existing Edit Selection path). | WA-native (Aider `/edit` opens a prompt editor) | obvious |
| `/extend [instruction]` | Continue writing from the current **selection** (existing Extend Selection path). | WA-native | obvious |
| `/selection` | Pin the current selection as the target for the next `/apply` / `/edit` / chat turn (range + char count in the status line). | WA-native | stretch |
| `/attach [name]` | Pin an **open** LibreOffice document or a **nearby** sibling file as extra read context for this chat (MCP already has `list_open_documents`; Phase 0 `document_research` lists the folder). Empty = list attachable docs. Drop with `/attach -name` or `/clear`. | Loose Aider `/add` — office docs, not a git tree | stretch |

### Research, images, modes

| Command | In WriterAgent | Source | Priority |
|---------|----------------|--------|----------|
| `/web [query]` | Run **Web Research** for this query (or switch to that mode if no query). Not Aider’s single-URL Playwright scrape. | Loose Aider `/web` | obvious |
| `/deep [query]` | Same for **Deep Research**. | WA-native | stretch |
| `/image [prompt]` | Switch to Image mode, or generate/insert with the image model. | WA-native | stretch |
| `/librarian` | Switch to Librarian (global transcript, not per-document). | WA-native | stretch |
| `/brainstorm [topic]` | Start Brainstorming (Writer). | WA-native | stretch |
| `/plan [topic]` | Start Writing Plan (Writer / Calc). | WA-native | stretch |
| `/ppt [topic]` | Start PPT-Master (Draw / Impress). | WA-native | stretch |

### Python, Calc, notebooks

| Command | In WriterAgent | Source | Priority |
|---------|----------------|--------|----------|
| `/py [code\|name]` | Run a named document/user script, or a one-liner, and insert the result (Run Python Script path). | Loose Aider `/run` (they run a shell) | stretch |
| `/reset-py` | Reset Python Session (venv / `=PY()` / `notebook:…` kernel for this document). | WA-native | obvious |
| `/cell` | Calc: open Edit Python in Cell for the active `=PY()` cell. | WA-native | stretch |
| `/notebook [run\|run-all]` | Writer `.ipynb` import: run the current code cell, or all cells. **Run All is not shipped** (notebook Phase 2) — mark as future if `/notebook` is built. | WA-native | stretch |

---

## Do not

- Skills / `SKILL.md` catalogs, user-defined `/` plugins, or a second prompt library.
- New tools invented only to give a command something to call. `/apply` and `/attach` should reuse `apply_document_content` and document-research / open-document listing.
- LibreOffice native menus or a WriterAgent menu item that *is* the feature. `/` lives in the Ask box.
- Copy Aider’s system prompt, help-coder few-shots, or architect/editor split into WriterAgent prompts.
- Port `/add` `/drop` `/commit` `/git` `/map` as coding-agent file ops.

---

## Sources

- Aider `aider/commands.py` (`cmd_*` docstrings; `get_help_md()`).
- WriterAgent sidebar: `extension/Dialogs/ChatPanelDialog.xdl`, `plugin/chatbot/chat_sidebar_mode.py`, `plugin/chatbot/panel.py` (`ClearButtonListener`, `StopButtonListener`, model combobox, Record on Send).
- Related: [sidebar-implementation.md](sidebar-implementation.md), [reviewable-agent-edits.md](../writer/reviewable-agent-edits.md), [multi-document-dev-plan.md](multi-document-dev-plan.md), [jupyter-notebook-import.md](../writer/jupyter-notebook-import.md), [audio-architecture.md](audio-architecture.md).

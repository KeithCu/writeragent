# Sidebar slash commands (research)

**UI status:** The Ask-box `/` completion popup is enabled (`ENABLE_SLASH = True` in `plugin/chatbot/slash_popup.py`). Typing `/` opens a toolkit `listbox` window parented to the Ask field's peer (tall overlay above Ask, over the transcript). `[SLASH-OV]` breadcrumbs emit only when `SLASH_OV_VERBOSE_DEBUG` is true, at DEBUG (same pattern as `RICH_SCROLL_VERBOSE_DEBUG`). `Dropdown=True` + `togglePopup` did not open without a click (headed 575). Further typing filters; Esc dismisses; Up/Down moves the selection; Tab completes the selected name; Enter accepts it without sending a chat turn; a row click accepts that command (not a closed-combo arrow). Ranking uses LRU persisted as `slash_command_lru` in `writeragent.json`. The XDL placeholder is `dlg:menulist` (dialog.dtd has no `listbox`) and stays hidden. Accept on `/help`, `/clear`, `/stop`, and `/undo` runs immediately; other commands insert `/name ` into Ask for the user to finish and Send. Once Ask holds a full command plus trailing space or arguments, the popup stays closed so Enter sends. Popup rows use shortened labels (no usage hints) to avoid horizontal scroll. `/stop` clears Ask after it runs. Answer presets show the typed slash line in the transcript while the expanded instruction goes to the model. **Tier 1 presets also run on Send** when the user sends a full slash line. `/help` prints the static list; `/clear` wipes the active transcript; `/stop` runs the Stop button listener (in-flight send, speech, inline web approval, and hands-free). `/help`, `/clear`, `/undo`, and mocks clear Ask when run immediately. Frame and toolkit key handlers only navigate; the listbox listener is the one that inserts typed characters. Accept and Esc dispose the toolkit window on a later main-thread turn. Ask text changes post hide and show the same way; a failed post is logged and is not run inline. The menu is positioned once, before it is shown. Esc dispatches `TEXT_UPDATED` from the text still in Ask so Send is not left disabled.

**Status:** Shipped on Send (and immediate popup accept for `/help`, `/clear`, `/stop`, and `/undo`): `/help`, `/clear`, `/stop`, `/summarize`, `/proofread`, `/shorten`, `/expand`, `/tone`, `/translate`, `/explain`, `/remember`, `/compact`, `/undo`, and `/formula` (`plugin/chatbot/slash_commands.py`). All Tier 1 verbs in the table below are implemented. The `/` popup and `/help` list are filtered by app (`SlashCommand.apps`); typed commands still run (for example `/formula` outside Calc prints "Only in Calc"). Draw and Impress rewrite presets rewrite the selected text shape in place; with no text shape selected the sidebar shows `Select a text shape.` (or `Select a text shape. Tables are not rewritten.` when only a table is selected). `allow_whole_document` does not rewrite the whole slide. Calc slash edit with no selection overlap on the used range can edit the used range when invoked from the sidebar preset path (`allow_whole_document=True`). Local command output (for example `/help`, `/remember`, and preset sidebar notices) is stored on `session.messages` as assistant rows tagged `_slash_local` for repaint; those rows are omitted from `messages_for_llm`.

**Localized names:** The command id stays English (`help`, `summarize`, …) for dispatch, the `slash_command_lru` key, and logs. Each id is also extracted as a normal gettext msgid (`N_("help")`, …); that locale's `msgstr` can be a localized alias (Japanese `ヘルプ` matches `help` when the UI is Japanese). English ids match in every locale. An alias from another language does not. If the catalog has no usable alias (missing translation, same as English, or unsafe token), matching and the popup label stay on the English id. The popup, `/help`, and a completed Ask draft show the alias when there is one. Argument placeholders in the usage hint (`[target]`, `<description>`, …) are translated; the token in front of them is the same display name.

Typed in the **chat sidebar** Ask box. Not a LibreOffice menu, not LibrePy, not a skills system.

## Rules for picking commands

- **New verbs, not aliases.** A command should do something a user can't already do with one click. The mode dropdown, model combobox, Record, Stop, Clear, the hamburger menu, and the review toolbar already exist.
- **Skip `/model` and `/mode`** (and per-mode launchers such as `/web`, `/deep`, `/image`, `/brainstorm`, `/plan`, `/ppt`, `/librarian`): the dropdowns cover them.
- **Nested pickers are low priority.** A command that opens a second list (pick a document, a language, a tone) is worth less than one that takes a free-text argument.
- **Keep `/help`, `/clear`, `/stop`.**
- **For writers and spreadsheet users first**, not just developers.

## What other apps do

| App | How `/` works | Commands people use most |
|-----|---------------|--------------------------|
| Notion | `/` inserts blocks; Space on an empty line or `/ai` opens Notion AI | `/summarize`, `/action items`, Improve writing, Translate, `/toc`, `/today` |
| Google Docs (Gemini) | `@` menu for chips; "Help me write" / Refine on a selection | Rephrase, Shorten, Elaborate, tone (formal / casual), custom refine |
| Microsoft Word (Copilot) | `/` in the Copilot prompt lists files to reference | Reference a file (up to three), rewrite, summarize |
| ChatGPT | `/` in the composer picks a tool | `/image`, `/search`, `/canvas`, `/reason`, deep research |
| Claude Code | `/` lists built-ins and user commands | `/clear`, `/compact`, `/memory`, `/rewind` (alias `/undo`), `/context`, `/init` |
| GitHub Copilot Chat | `/` verbs act on the selection; `@` and `#` add context | `/explain`, `/fix`, `/tests`, `/doc` |
| Cursor | `@` attaches files, docs, diffs; `/` runs built-in and `.cursor/commands/*.md` commands | `@file` / `@docs`, reset context, side chats |
| Aider | `/` controls the CLI session | `/ask`, `/undo`, `/diff`, `/web`, `/tokens`, `/copy`, `/clear` |
| Slack / Discord | `/` runs app or bot commands in the message box | `/remind`, `/me`, `/giphy` (app), polls (bots) |
| Obsidian | Core "Slash commands" plugin exposes the command palette at `/` with fuzzy match | Any palette command |
| Coda | `/` inserts blocks and opens Coda AI; `=` opens the formula builder | AI write, "describe a formula in plain English" |

**Patterns worth copying:**

1. **Verb presets on the selection** are what document users love most: summarize, shorten, rephrase, change tone, translate, fix grammar, find action items (Notion, Gemini, Copilot). Each is a fixed prompt plus a target, so they are cheap to build.
2. **Explain what I'm looking at** (`/explain` in Copilot Chat) maps well to Calc formulas and dense prose.
3. **Session control** for long chats: `/compact`, `/clear`, and `/undo` or `/rewind` (Claude Code, Aider).
4. **Memory** you can write to directly (`/memory` in Claude Code).
5. **Reference another file** (`/file` in Word Copilot, `@` in Cursor). Useful, but it's a picker, so it costs more.
6. Tool launchers (`/image`, `/search`) are common in ChatGPT. WriterAgent already has them in the mode dropdown.

## Prioritized commands

**Effort:** S = a row in a preset table plus a prompt, reusing an existing path. M = new routing or a small new helper. L = new product work.

Most Tier 1 verbs share one mechanism: a table of `(name, instruction, target)` presets. A **rewrite** preset runs the existing Edit Selection path (`plugin/chatbot/selection.py`), so Writer edits land as reviewable tracked changes (rewrite presets use that sidebar Stop scope). An **answer** preset sends a normal chat turn with the instruction prepended and the selection quoted. One table and one router cover all of them. Each preset should fall back to the whole document (or the used range in Calc) when nothing is selected.

### Tier 1: build these first

| # | Command | What it does in WriterAgent | Why users want it | Inspired by | Effort |
|---|---------|-----------------------------|-------------------|-------------|--------|
| 1 | `/summarize [focus]` | Answer preset: summarize the selection, or the whole document, in the sidebar. In Calc, summarize the selected range ("what does this data say"). | The most-used AI verb in every document app. | Notion `/summarize`, Word Copilot, Gemini | S |
| 2 | `/proofread` | Rewrite preset: fix spelling, grammar, and punctuation only, without restyling. In Writer, changes show as tracked changes the user can accept or reject. | A safe, low-risk first edit. Complements Harper (which catches rules) with an LLM pass on meaning. | Notion "Fix spelling & grammar", Copilot `/fix` | S |
| 3 | `/shorten [target]` and `/expand [what]` | Rewrite presets: tighten the selection (optional target such as "half" or "100 words"), or elaborate it. | Length is the most common thing writers adjust. | Gemini Refine (Shorten / Elaborate), Notion | S |
| 4 | `/tone <description>` | Rewrite preset with free text: "formal", "friendly", "plain English", "for a 10-year-old". | Tone changes are the other top Refine action. A free-text argument avoids a picker. | Gemini tone (Formal / Casual), Notion "Change tone" | S |
| 5 | `/translate <language>` | Rewrite preset: translate the selection in place, keeping formatting. With no selection, answer in the sidebar instead of overwriting the document. | Office users translate constantly; LibreOffice has no built-in translator. | Notion Translate, Word Copilot | S |
| 6 | `/explain` | Answer preset. Calc: explain the active cell's formula or `=PY()` code in plain language, including referenced ranges. Writer: explain the selected passage (jargon, legal, technical). | Inherited spreadsheets full of formulas nobody understands. | Copilot Chat `/explain` | S |
| 7 | `/undo` | Undo the last **agent** edit as one step (compound undo, `plugin/doc/compound_undo.py`). First version: undo only when the agent edit is on top of the undo stack; otherwise say the user has edited since. | Trust: users try more when backing out is one command. Ctrl+Z may need many presses after a streamed edit. | Claude Code `/rewind` (alias `/undo`), Aider `/undo` | M |
| 8 | `/remember <fact>` | Write the fact straight into `USER.md` through the same store as `upsert_memory`, with no LLM round trip, and echo what was saved. | "Always use British spelling", "my company is Acme". Users want to tell it once without hoping the model calls the tool. | Claude Code `/memory`, ChatGPT memory | S |
| 9 | `/formula <description>` | Calc: write a formula for the active cell from a plain-English description, show it in the sidebar, and insert it. | The top spreadsheet AI request. Faster than typing it into chat and copying the result. | Coda AI formula writing, Copilot in Excel | M |
| 10 | `/compact [focus]` | Summarize older chat history now using the existing compaction path (`force_compact` in `tool_loop.py`), optionally keeping a focus. | Long sessions get slow and lose track. Keeps the thread without `/clear`. | Claude Code `/compact` | S |

### Tier 2: worth doing after Tier 1 ships

| Command | What it does | Why | Inspired by | Effort |
|---------|--------------|-----|-------------|--------|
| `/actions` | Answer preset: list action items, owners, and dates from the selection or document (meeting notes). Optional: insert as a checklist. | Meeting notes are a top document type. | Notion "Find action items" | S |
| `/ask <question>` | One read-only turn: mutating tools are withheld, so the model only answers in the sidebar. | Users want to ask about a document without worrying it will be edited. | Aider `/ask` | M (tool filtering by turn) |
| `/chart [description]` | Calc / Writer table: create a chart from the selected range using the existing chart tools. | Charts are hard to set up by hand in LibreOffice. | Copilot in Excel, Gemini in Sheets | M |
| `/clean` | Calc: tidy the selected range (trim, dedupe, fix types and dates) using `clean_and_prepare`, as a reviewable or undoable step. | Messy imported data is the common spreadsheet chore. | Excel / Sheets AI cleanup | M |
| `/attach <document>` | Add another open LibreOffice document (or a nearby file) as read-only context for this chat. Free text matches by title; a picker is optional. | Comparing, merging, or citing across files. | Word Copilot `/file`, Cursor `@file` | L ([multi-document-dev-plan.md](multi-document-dev-plan.md)) |
| `/title` | Suggest a title or headline for the selection or document. | Common small ask. | Notion AI | S |

### Tier 3: low priority

| Command | What it does | Why low | Inspired by | Effort |
|---------|--------------|---------|-------------|--------|
| `/copy` | Copy the last reply to the clipboard as plain text or HTML. | Selecting text in the response pane already works. | Aider `/copy` | S |
| `/tokens` | Show context use: system prompt, `[DOCUMENT CONTENT]`, history, headroom. | Mostly a developer need. | Aider `/tokens`, Claude Code `/context` | S |
| `/fetch <url>` | Pull one web page into the chat as context (not a research run). | Web Research covers most uses. | Aider `/web`, Cursor `@web` | M |
| `/export` | Save the transcript as Markdown or HTML. | Rarely needed. | Aider `/copy-context` | S |
| `/py <code>` | Run a Python one-liner in the document session and show the result. | Run Python Script and `=PY()` exist; power users only. | Aider `/run` | M |

### Considered and skipped

- `/model`, `/models`, `/mode`, and per-mode launchers (`/web`, `/deep`, `/image`, `/brainstorm`, `/plan`, `/ppt`, `/librarian`): dropdowns exist.
- `/edit`, `/extend`, `/settings`, `/report`, `/reset-py`, `/voice`: one-click menu items or buttons already do this.
- `/accept`, `/reject`, `/review`: the review toolbar already has bulk accept and reject, and review mode is a setting.
- Aider repo commands (`/add`, `/drop`, `/ls`, `/map`, `/commit`, `/git`, `/lint`, `/test`) and `/exit`: there is no repo or process to leave.
- `/remind`, `/poll`, `/giphy`: chat-app social features with no document meaning.

## Ask-box UX and arguments

The popup completes a single word (`accept_selected()` on Enter). Commands with arguments need two changes:

- **Tab or Enter on a command that takes arguments** inserts `/name ` and leaves focus in the Ask box instead of running it.
- **Send interception:** on Send, if the first token is a registered command, route through ``plan_slash`` / ``slash_continues_send`` and ``run_slash_command(cmd, host, arg)`` instead of a chat turn. Unknown `/words` go to the model as normal text.

`SlashCommand` would need a `takes_args` flag and a preset kind (`answer` or `rewrite`), so Tier 1 stays a data table, not ten handlers.

## Do not

- Skills / `SKILL.md` catalogs, user-defined `/` plugins, or a second prompt library. (The humanizer `SKILL.md` in `plugin/chatbot/skills.py` is ambient prompt guidance, not a command system.)
- New tools invented only to give a command something to call. Presets reuse Edit Selection and normal chat turns; `/remember` reuses the memory store; `/undo` reuses compound undo; `/attach` reuses document research and open-document listing.
- Commands that only duplicate a dropdown, button, or menu item.
- LibreOffice native menus or a WriterAgent menu item that *is* the feature. `/` lives in the Ask box.
- Copy another product's system prompt or few-shots (Aider's help coder, architect/editor split) into WriterAgent prompts.

## Sources

Other apps (fetched 2026-11; several are third-party guides, so check exact names in the apps):

- Notion: [Using slash commands](https://www.notion.com/help/guides/using-slash-commands), [Everything you can do with Notion AI](https://www.notion.com/help/guides/everything-you-can-do-with-notion-ai), [Notion AI for docs](https://www.notion.com/help/guides/notion-ai-for-docs)
- Google Docs: [Write with Gemini in Google Docs](https://support.google.com/docs/answer/13951448), [9to5Google, Docs Gemini upgrade (2026-03)](https://9to5google.com/2026/03/10/google-docs-gemini-upgrade/), [TechRadar on the `@` menu](https://techradar.com/pro/google-docs-is-getting-an-even-smarter-way-to-help-craft-the-document-of-your-dreams)
- Word Copilot: [DataCamp, Copilot in Word](https://www.datacamp.com/tutorial/copilot-in-word)
- ChatGPT: [OpenAI education hub, basic commands](https://sites.google.com/openai.com/education-hub/home/students/2-features-basic-commands), [BGR on the composer refresh](https://bgr.com/?p=6316982)
- Claude Code: [alexcloudstar cheat sheet](https://alexcloudstar.com/blog/claude-code-commands-cheat-sheet-2026/), [DataCamp, Claude Code slash commands](https://www.datacamp.com/tutorial/claude-code-slash-commands)
- GitHub Copilot Chat: [Microsoft Learn module](https://learn.microsoft.com/en-gb/training/modules/github-copilot-across-environments/3-git-hub-copilot-chat), [GitHub cheat sheet (mirror)](https://huggingface.co/AbdulElahGwaith/github-docs-arabic-enhanced/blob/main/content/copilot/reference/cheat-sheet.md)
- Cursor: [toolsbase command reference](https://toolsbase.dev/en/reference/cursor-commands), [forum: `/summarize` removed](https://forum.cursor.com/t/what-happened-to-the-view-summarized-chat-command/143556)
- Aider: [in-chat commands](https://aider.chat/docs/usage/commands.html), `aider/commands.py` in [Aider-AI/aider](https://github.com/Aider-AI/aider)
- Slack: [Zapier, Slack slash commands](https://zapier.com/blog/slack-slash-commands/)
- Obsidian: [Slash commands core plugin](https://obsidian.md/help/plugins/slash-commands)
- Coda: [The slash command](https://help.superhuman.com/hc/en-us/articles/46210104421005-The-slash-command), [Coda AI](https://help.coda.io/en/articles/7988177-coda-ai)

WriterAgent:

- `plugin/chatbot/slash_commands.py` (registry, `/help` `/clear` `/stop`), `plugin/chatbot/slash_popup.py` (overlay, `ENABLE_SLASH`).
- Sidebar: `extension/Dialogs/ChatPanelDialog.xdl`, `plugin/chatbot/chat_sidebar_mode.py`, `plugin/chatbot/panel.py`, `plugin/chatbot/hamburger_menu.py`.
- Reused paths: `plugin/chatbot/selection.py` (Edit Selection), `plugin/chatbot/memory.py`, `plugin/chatbot/compaction.py`, `plugin/doc/compound_undo.py`, `plugin/writer/inline_review.py`.
- Related: [sidebar-implementation.md](sidebar-implementation.md), [reviewable-agent-edits.md](../writer/reviewable-agent-edits.md), [compaction-dev-plan.md](compaction-dev-plan.md), [multi-document-dev-plan.md](multi-document-dev-plan.md).

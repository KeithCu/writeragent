

### Draft: WriterAgent Testing Scenarios Checklist

**File goal**: `docs/framework-testing-scenarios.md` (or `TESTING_CHECKLIST.md`)

#### 1. Core Tier Sanity (Run these first — everything else depends on this)

- [ ] Basic chat works end-to-end (send → LLM response → apply to document)
- [ ] Document context injection works (`[DOCUMENT CONTENT]` is always present and accurate)
- [ ] With Record Changes on, `[DOCUMENT CONTENT]` excludes tracked deletions (same `get_string_without_tracked_deletions` path as Extend/Edit selection; see `_read_writer_text_slice` in `plugin/doc/text_helpers.py`)
- [ ] Selection / extend / rewrite shortcuts work (Ctrl+Q, Ctrl+E)
- [ ] Undo / tracked changes handling is correct after AI edits
- [ ] Streaming responses render properly without UI lockup
- [ ] Error handling + user-friendly messages for LLM failures, tool failures, network issues
- [ ] History persistence (SQLite or JSON fallback) works across restarts
- [ ] Settings changes apply immediately without restart
- [ ] MCP server (if enabled) can target the correct document via `X-Document-URL`

#### 2. Tool Tier System (The exact area you flagged)

- [ ] Specialized tools only appear when the right document type is active
- [ ] Core tier tools are always available as fallback
- [ ] A specialized tool can successfully call core tier tools internally when needed (e.g., "get current selection" or "apply basic formatting")
- [ ] Tool registry correctly merges `uno_services` + `doc_types` for gateway tools
- [ ] `active_domain` / sub-agent refresh works when switching between Writer ↔ Calc ↔ Draw in the same session
- [ ] LLM is given accurate descriptions of available tools in every domain (recent commit)
- [ ] No "tool not found" errors when a specialized tool legitimately needs a core helper

#### 3. Writer Specialized Scenarios

- [ ] Grammar checker runs asynchronously and applies fixes with format preservation
- [ ] Math/LaTeX → native LibreOffice Math formula conversion works (including inline + display)
- [ ] Outline navigation + heading manipulation (get_document_tree, nav_heading_children)
- [ ] Track changes / tracked deletions are respected during edits
- [ ] Complex formatting preservation (bold, italics, highlights, fonts) after AI rewrite
- [ ] Bookmarks, footnotes, fields, sections, tables, charts, shapes — basic read + modify
- [ ] Long document handling (100+ pages) without context explosion
- [ ] Real-time grammar + manual edit interleaving doesn't corrupt state

#### 4. Calc Specialized Scenarios (High priority — recent changes here)

- [ ] `=PROMPT()` function works in cells (simple + complex prompts)
- [ ] Sheet-level operations (create, delete, rename, move) via specialized tools
- [ ] Range selection + bulk operations (formulas, conditional formatting, filters)
- [ ] Pivot table analysis / manipulation
- [ ] Rich text / HTML paste into single cells
- [ ] Formula parsing edge cases (the one you just fixed — commas/semicolons in prose)
- [ ] Large sheets (thousands of rows) without timeout or memory issues
- [ ] Mixed core + specialized in same session (e.g., "analyze this pivot then rewrite the summary in Writer")

#### 5. Draw / Impress Specialized Scenarios

- [ ] Shape creation, editing, grouping, layering
- [ ] Slide navigation + content manipulation
- [ ] Animation / transition basics (if supported)
- [ ] Export / import fidelity for complex drawings

#### 6. Mixed-Tier & Cross-Domain Workflows (The dangerous ones)

- [ ] User starts in Writer, asks for Calc analysis → tools correctly switch domains
- [ ] Specialized tool fails gracefully and falls back to core tools
- [ ] Multi-turn conversation where specialized tools are used in later turns
- [ ] Librarian mode → document mode handoff
- [ ] Web research sub-agent + specialized document tools in same session

#### 7. Error, Recovery & Edge Cases

- [ ] LLM returns malformed tool calls → json-repair + fallback works
- [ ] Tool execution fails (e.g., locked document, permission issue) → clear error + recovery
- [ ] Very long responses / token limits
- [ ] Multiple rapid sends (stop button behavior)
- [ ] No document open / multiple windows
- [ ] LibreOffice restart while chat is active

#### 8. Non-Functional / Business-Critical

- [ ] Performance: <2s first token on local models, smooth streaming
- [ ] Memory usage stays reasonable during long sessions
- [ ] No segfaults or hard crashes in LibreOffice (especially with dialogs, UNO calls)
- [ ] Localization works (all new strings extracted)
- [ ] Update check + version reporting works

---


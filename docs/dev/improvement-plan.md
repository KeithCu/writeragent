# LocalWriter Improvement Plan

**Based on actual codebase analysis** — a grounded plan that accounts for what exists, what’s missing, and what’s realistically achievable.

---

## Executive Summary

Grok’s plan overstated “Calc integration” (it’s already implemented) and understated the amount of refactoring needed. This plan focuses on: **high-impact polish**, **endpoint + API key support** (any compatible API), **chat mode**, **Calc-specific features**, and **Impress support**, shaped by the real structure of `main.py` and the extension config.

---

## Part 1: Current State (What the Code Actually Does)

### Architecture

| Component | Reality |
|-----------|---------|
| **Entry point** | Single `MainJob` class, `trigger(args)` handles ExtendSelection, EditSelection, settings |
| **API** | Uses `/v1/completions` (text-generation-webui/Ollama style); compatible with any endpoint that supports this |
| **Auth** | **None** — no Authorization header |
| **Config** | `localwriter.json` in LibreOffice UserConfig dir |
| **Apps** | Writer and Calc are active; Impress is not |
| **Error handling** | DONE: Message box via `show_error()`; original text preserved on failure |

### What’s Already Implemented

- **Writer**: Extend/Edit with streaming, system prompts, max tokens
- **Calc**: Same Extend/Edit but per selected cell (cell-by-cell, not range-aware)
- **Settings dialog**: Endpoint, model, max tokens, system prompts
- **Addons.xcu**: `com.sun.star.sheet.SpreadsheetDocument` and text docs — Calc menu is live
- **Accelerators.xcu**: Ctrl+Q / Ctrl+E for Writer and Calc

### Critical Technical Debt

1. **API logic** — DONE: shared helpers (`stream_completion`, `make_api_request`, etc.)
2. **Request timeout** — DONE: config `request_timeout`, `_get_request_timeout()`, all urlopen use timeout
3. ~~**Typo in Edit prompt**~~ — DONE (no longer present)
4. **Manifest** — DONE: no pkg-desc reference in current manifest

---

## Part 2: Prioritized Plan (Grounded in the Codebase)

### Phase 1: Foundation Fixes (1–2 days) — DONE

**Goal:** Stabilize and clean core logic before adding features.

| Task | Effort | Notes |
|------|--------|-------|
| **1.1** Extract a shared `call_completions_api(prompt, max_tokens, stream, system_prompt)` helper | 2–3 hrs | DONE: `stream_completion`, `make_api_request`, `stream_request`; request timeout via `_get_request_timeout()` |
| **1.2** Fix typo `"versio"` → `"version"` in Edit prompt | 5 min | DONE (no longer present) |
| **1.3** Improve error handling — show message box instead of writing errors into selection | 1 hr | DONE: `show_error()` with MessageBox, `_format_error_message()` |
| **1.4** Fix manifest — remove or add `pkg-desc/pkg-description.en` | 15 min | DONE (manifest has no pkg-desc reference) |

---

### Phase 2: Endpoint + API key (1 day)

**Goal:** Enable remote OpenAI-compatible APIs with API key support.

| Task | Effort | Notes |
|------|--------|-------|
| **2.1** Add API key field to `settings_box()` | 1 hr | New edit control; add to `get_config`/`set_config` |
| **2.2** Add `Authorization: Bearer <key>` when key is set | 30 min | Single change in the shared API helper |
| **2.3** Document endpoint in README | 30 min | User sets Endpoint URL/Port in Settings (e.g. local or remote). |
| **2.4** Optional: endpoint preset dropdown in Settings | 1 hr | Pre-fills URL; simplifies setup |

**Endpoint rules:** Code appends `/v1/completions`. User should set base URL only:

- User configures endpoint URL in Settings (local or remote).
- Together: `https://api.together.xyz/v1`
- Local: `http://127.0.0.1:5000` or `http://localhost:11434`

---

### Phase 3: Chat with Document (1–2 weeks)

**Status:** Sidebar chat is implemented with tool-calling, streaming, and reasoning display. See [Chat Sidebar Improvement Plan.md](Chat%20Sidebar%20Improvement%20Plan.md) for current capabilities and recent improvements (system prompt tuning, translation behavior, `reasoning: { effort: 'minimal' }`).

**Goal:** Let users query the full document (summarize, Q&A, etc.).

| Task | Effort | Notes |
|------|--------|-------|
| **3.1** Add `get_full_document_text()` — walk Writer text via UNO `Text` service | 2–3 hrs | Reuse existing `model.Text` access pattern |
| **3.2** Add “Chat with Document” menu item and handler | 1 hr | New trigger arg; Addons.xcu entry |
| **3.3** Chat dialog — input + optional history; context window handling | 4–6 hrs | May need to truncate doc text for large docs |
| **3.4** Prep and send: `doc_context + user_query` to API | 2 hrs | Same completions API, different prompt shape |
| **3.5** Optional: sidebar vs dialog — start with dialog; sidebar later if desired | — | |

**Context handling:** For large docs, send first N chars (e.g. 4000–8000) or a summary. Could add “context length” in settings.

---

### Phase 4: Calc Improvements (1–2 weeks)

**Goal:** Make Calc feel purpose-built instead of “Writer ported to cells.”

| Task | Effort | Notes |
|------|--------|-------|
| **4.1** Range-aware behavior — treat selection as one logical block | 2–3 hrs | Build single prompt from all selected cells; handle response as structured data |
| **4.2** Formula suggestion mode — “suggest formula for this range” | 3–4 hrs | Special prompt for formulas; use `=SUM(A1:A10)` style output |
| **4.3** Data summarization — “summarize this range” → paste into new cell/sheet | 4–6 hrs | New menu item; target cell or new sheet |
| **4.4** Calc-specific system prompts | 1 hr | Add “Calc system prompt” in settings, used only in Calc |

---

### Phase 5: Impress Support (2–3 weeks)

**Goal:** Add Impress; Impress has a different structure than Writer/Calc.

| Task | Effort | Notes |
|------|--------|-------|
| **5.1** Add `com.sun.star.presentation.PresentationDocument` to Addons.xcu Context | 30 min | |
| **5.2** Add Impress accelerators in Accelerators.xcu | 30 min | |
| **5.3** Implement Impress text access — slides use `DrawPage` → shapes → text frames | 4–6 hrs | Get text from current slide or all slides |
| **5.4** Extend Selection — extend selected shape text or speaker notes | 2–3 hrs | Notes: `XPresentationPage.getNotesPage()` |
| **5.5** Edit Selection — same for shape text / notes | 2–3 hrs | |
| **5.6** Slide notes generation — “generate speaker notes from slide text” | 3–4 hrs | Extend mode with slide-specific prompt |
| **5.7** Outline generation — “generate slide outline from doc” | 4–6 hrs | Document-wide; create new slides or outline view |

**Impress UNO basics:**

- `PresentationDocument` → `getDrawPages()` → `DrawPage` → `getByIndex(i)` for each slide
- Text: shape `Text` property or `XText` via `XTextContent`
- Notes: `XPresentationPage.getNotesPage()` → text in notes shape

---

### Phase 6: Code Quality & UX (Ongoing)

| Task | Effort | Notes |
|------|--------|-------|
| **6.1** Split `main.py` — separate modules for API, config, dialogs, per-app logic | 4–6 hrs | E.g. `api.py`, `config.py`, `dialogs.py`, `writer.py`, `calc.py`, `impress.py` |
| **6.2** Add timeout + retry for API calls | 1 hr | In shared API helper |
| **6.3** Loading indicator during API call | 2–3 hrs | Cursor change or status bar message |
| **6.4** Unit tests for API helper and config | 2–3 hrs | Mock `urllib`; test JSON read/write |

---

## Part 3: What Grok Got Wrong

| Grok claim | Reality |
|------------|---------|
| “Calc integration in development” | Calc is already implemented (lines 406–500) |
| “Basic menu integration” for Calc | Addons.xcu already targets Calc |
| “Modify description.xml to target Calc” | Not needed; targeting is via Addons.xcu |
| “Familiarity with UNO for sheet access” | Sheet access is already in place |
| “3–5 patches per phase” | Actual effort depends on existing logic and duplication |

---

## Part 4: Recommended Implementation Order

1. ~~**Phase 1** — Foundation fixes~~ (DONE)
2. **Phase 2** — API key + endpoint (high user value; may overlap with PR #31/#36)
3. **Phase 4.1** — Calc range-aware behavior (improves existing Calc a lot)
4. **Phase 3** — Chat with document (often requested)
5. **Phase 4.2–4.4** — Calc formula/summarization features
6. **Phase 5** — Impress support
7. **Phase 6** — Refactoring and tests (can be done in parallel)

---

## Part 5: Quick Reference — Key Files

| File | Purpose |
|------|---------|
| `main.py` | All logic; ~500 lines |
| `Addons.xcu` | Menu items; Context controls which apps show menu |
| `Accelerators.xcu` | Hotkeys per document type |
| `description.xml` | Extension metadata |
| `META-INF/manifest.xml` | Package manifest (fix pkg-desc) |

---

## Part 6: Open PR Overlap — Don't Duplicate Work!

**As of Feb 2026**, there are **2 open pull requests** that overlap with this plan:

### PR #36 — [Add advanced config, streaming, and settings](https://github.com/balisujohn/localwriter/pull/36) (etiquet, Nov 2025)

| Plan Phase | Overlap |
|------------|---------|
| **Phase 1** (shared API helper, config) | **Yes** — “unified API request/streaming for both chat and completions” |
| **Phase 2** (API key, endpoint) | **Yes** — “Open API compatible”, “OpenWebUI cal style /api/chat/completions” |
| **Phase 6** (config, dynamic settings) | **Yes** — “dynamic settings dialog for all major backend options” |

**Summary:** This PR does a lot of what Phase 1 + Phase 2 cover. If/when it’s merged, you’d mainly build on top of it rather than redoing that work.

### PR #31 — [=PROMPT() function and OpenAI compatible API support](https://github.com/balisujohn/localwriter/pull/31) (Guard1an/MageDoc, Aug 2025)

| Plan Phase | Overlap |
|------------|---------|
| **Phase 2** (API key) | **Yes** — “Added API Key configuration settings” |
| **Phase 4.2** (Formula suggestion) | **Partial** — Adds `=PROMPT()` Calc function (native formula, not menu-based) |
| **Phase 4** (Calc improvements) | Indirect — Temperature, Seed, debug log |

**Summary:** Adds API key support and a Calc `=PROMPT()` function. Different approach than menu-based formula suggestion but addresses similar needs.

### Recommendation

1. **Before implementing Phase 1–2:** Review PR #36 and PR #31. If either looks close to merge, consider waiting or offering to help get it merged.
2. **Phase 3 (Chat), Phase 4.1 (range-aware Calc), Phase 5 (Impress):** No overlap with current PRs — safe to work on.
3. **Phase 4.2 (formula suggestion):** PR #31’s `=PROMPT()` covers formula use; Phase 4.2 can focus on menu-based “suggest formula for this range” if you want both styles.

---

## Appendix: Endpoint Compatibility

- **text-generation-webui** — `/v1/completions`, no auth
- **Ollama** — `/v1/completions`, no auth (model required)
- Configure endpoint URL and API key in Settings; Bearer token where required.
- **Together.ai** — `https://api.together.xyz/v1/completions`, Bearer token

All use the same request shape: `prompt`, `max_tokens`, `temperature`, `stream`, optional `model`.

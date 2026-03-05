# What to Work on Next (LocalWriter)

Recommendations are ordered by impact and dependency, using [AGENTS.md](AGENTS.md) Section 7 and [UI_IMPROVEMENTS.md](UI_IMPROVEMENTS.md) as the main sources.

---

## 1. High-value, contained scope

### Config presets (Settings)

- **What**: "Load from file" or a preset dropdown in Settings so users can switch between `localwriter.json`, `localwriter.openrouter.json`, etc., without manually copying files.
- **Where**: [core/config.py](core/config.py) (read/save paths or preset list), [LocalWriterDialogs/SettingsDialog.xdl](LocalWriterDialogs/SettingsDialog.xdl) (UI).
- **Why next**: Directly improves multi-endpoint workflows (local vs OpenRouter vs custom) with limited code surface.

### EditInputDialog: multiline instructions

- **What**: Make the Edit Selection instruction field multiline so longer prompts are easier to enter (AGENTS.md notes current layout is single-line).
- **Where**: [LocalWriterDialogs/EditInputDialog.xdl](LocalWriterDialogs/EditInputDialog.xdl) (e.g. multiline text control), and any code that reads the control (likely [main.py](main.py)).
- **Why next**: Quick UX win; XDL + wiring only, no new features.

### Endpoint presets (Local / OpenRouter / Together / Custom)

- **What**: Optional preset buttons or dropdown in Settings that set endpoint + API type (and optionally model) in one click.
- **Where**: Settings dialog and [core/config.py](core/config.py).
- **Why next**: Complements config presets; reduces setup friction for common providers.

---

## 2. Format-preserving replacement (deeper work)

These extend the existing behavior in [core/format_support.py](core/format_support.py) (`_replace_text_preserving_format`, `_content_has_markup`).

- **Proportional format mapping**: For large length differences, distribute the original formatting pattern across the new text instead of strict 1:1 character mapping (e.g. map formatting proportionally when replacing a short phrase with a long one).
- **Paragraph-style preservation**: Handle replacements that span paragraph breaks (multiple paragraphs / paragraph-level styles).
- **Edit Selection streaming**: Apply the same format-preserving logic to the Edit Selection streaming path so live edits retain character-level formatting (not only the tool-calling path).

These are the natural next steps after the current search/range/full + auto-detect behavior documented in AGENTS.md Section 3b.

---

## 3. Richer context and safer workflows (medium scope)

### Richer context (metadata)

- **What**: In `get_document_context_for_chat` / `get_calc_context_for_chat` (and Draw equivalent), add optional metadata: word/paragraph count, fonts, tables/images (Writer); formula/chart/error counts, column types (Calc). Optional config (e.g. `context_include_metadata`) to toggle for speed.
- **Where**: [core/document.py](core/document.py), [core/constants.py](core/constants.py) if prompts reference metadata.
- **Why**: Better summaries for the model and user (e.g. "Word count: 1200, Images: 3") with minimal API surface change.

### Safer workflows (propose-first / confirm)

- **What**: Optional "safe edit" mode: tool-calling shows a preview (e.g. diff or short description) and waits for user Accept/Reject before applying. Config flag (e.g. `safe_edit_mode`, default false).
- **Where**: Tool execution loop in [chat_panel.py](chat_panel.py), [LocalWriterDialogs/ChatPanelDialog.xdl](LocalWriterDialogs/ChatPanelDialog.xdl) (Accept/Reject controls), plus a small "proposed change" buffer per turn.
- **Why**: Builds trust and avoids accidental overwrites; can be introduced behind a flag so power users keep current behavior.

---

## 4. Suite completeness and polish

- **Impress support**: Reuse Draw tools with slide-specific behavior (slides, notes, transitions). AGENTS.md and UI_IMPROVEMENTS.md both call this a small delta on top of Draw.
- **Calc range-aware behavior**: Smarter use of selected ranges and dependencies in Calc tools/context (called out in AGENTS.md Section 7).
- **Predictive suggestions**: N-gram or lightweight "ghost text" in chat or document (Phase 4 in UI_IMPROVEMENTS.md); higher effort, optional.

---

## Suggested order (if picking one track)

| Priority | Item                                                         | Rationale                                                          |
| -------- | ------------------------------------------------------------ | ------------------------------------------------------------------ |
| 1        | Config presets                                               | Unblocks power users and pairs well with endpoint presets.         |
| 2        | EditInputDialog multiline                                    | Fast, contained UX improvement.                                    |
| 3        | Endpoint presets                                             | Complements config presets; small UI + config change.              |
| 4        | Format-preserving: proportional + paragraph + Edit Selection | Improves quality of edits across more flows.                       |
| 5        | Richer context (metadata)                                    | Improves model and user awareness without changing tool contracts. |
| 6        | Safe edit mode (propose-first)                               | Larger UX change; best after core flows are stable.                |

If you say which area you care about most (config/UX, format preservation, or safety/context), the next step is a concrete implementation plan for that slice only.

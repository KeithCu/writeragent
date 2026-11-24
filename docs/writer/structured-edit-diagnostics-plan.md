# Structured Edit Diagnostics: `code` + `suggested_call` on Errors and Warnings

**Status:** Phases 0–3 shipped (`suggest_call` in `plugin/framework/errors.py`, `STYLE_IGNORED_PARTIAL_MATCH` in `plugin/writer/html_import_styles.py`, `OLD_CONTENT_NOT_FOUND` / `ANCHOR_START_NOT_FOUND` / `OLD_CONTENT_TOO_LARGE` / `SHAPE_TEXT_NEEDS_DELEGATION` in `plugin/writer/search.py` and `plugin/writer/apply_document_content.py`, and the `suggested_call` rule in `plugin/framework/prompts.py`); Phase 4 (Calc and Draw adoption) remains optional. Incorporates the 2026-11-09 Opus review and tracks row 22 (standardized `code` on errors and warnings) of the editing-API status report (`docs/chat/opuswriteragentprompt.md`, "Seven principles" 1 and 2). Note: "error_code" in row 22 refers to the existing `code` key; no new key is added.
**Scope:** Writer editing tools first (`apply_content`, `apply_style`, `search`, `set_selection`). The mechanism is shared and generic, so Calc and Draw can adopt it later.

## 1. Goal

Every non-success outcome of a Writer edit (a hard error, or a success with dropped or unapplied content) carries three machine-readable fields:

| Field | Meaning |
|-------|---------|
| `code` | Stable, documented, `UPPER_SNAKE` identifier. The model and tests branch on it, never on message wording. |
| `message` | One readable sentence explaining what occurred. Free to reword. |
| `suggested_call` | Optional. A complete tool call (`{"tool": ..., "arguments": {...}}`) that the model can send unchanged to achieve what it most likely intended. |

Why: prompt instructions carry complex API-mechanics rules largely because tool failures are prose that models must parse. A stable error code paired with a ready-to-execute suggested call allows trimming those defensive rules from the prompt, improving tool-use reliability especially for smaller models.

**Also in scope: status-report item 4 (partial-match style skips).** When `data-lo-style` is skipped because a search match covers only part of a paragraph, the result returns a structured `STYLE_IGNORED_PARTIAL_MATCH` warning containing a `suggested_call` for `apply_style(target="search", ...)`. Item 4 is the primary concrete consumer of the shared `Diagnostic` mechanism (`plugin/writer/html_import_styles.py` and `apply_document_content.py`). Details are in §3.5 and Phase 1.

**Non-goals**
- No change to which edits succeed or fail. This is a diagnostics and metadata layer, not an edit behavior change.
- No translation of codes: `code` values are fixed machine identifiers and are never wrapped in `_()`. Warning messages and constants remain plain Python strings; agents must not edit `.po` or `.pot` catalogs (AGENTS.md).
- No new HTTP client, transport, or wire protocol. Payloads remain standard JSON dicts returned by tool execution.
- No speculative argument generation: `suggested_call` is omitted unless every required argument is known and validated.

## 2. Architecture in the tree

- **Error factory:** `make_tool_error(message, code="TOOL_EXECUTION_ERROR", *, suggested_call=None, **details)` in `plugin/framework/errors.py`. `ToolBase._tool_error` in `plugin/framework/tool.py` forwards keyword arguments to it. `format_error_payload` (`plugin/framework/errors.py`) formats exceptions into `{"status": "error", "code", "message", "details"?}`. Extra keyword arguments nest under `details`; `top_level=` places fields beside `code`.
- **Codes in use on the Writer edit path:**
  - Standard codes: `INVALID_PARAM`, `INVALID_REGEX`, `TABLE_INSERT_FAILED`, `MATH_IMPORT_FAILED`, `HYPERLINK_UPDATE_FAILED`, `SHAPE_REPLACE_FAILED`, `OCCURRENCE_OUT_OF_RANGE`, `SEARCH_FAILED`, `ROLLBACK_FAILED`, `TOOL_EXECUTION_ERROR`, `UNDO_UNAVAILABLE` (`plugin/writer/apply_document_content.py`, `styles.py`), `TABLE_IN_ANCHOR_RANGE` (`plugin/writer/apply_document_content.py`), and `STYLE_NOT_FOUND` (`_missing_style_error` in `plugin/writer/styles.py`).
  - Coded `_tool_error(...)` sites (Phase 2) across `plugin/writer/apply_document_content.py`, `plugin/writer/styles.py`, `plugin/writer/selection.py`, and `plugin/writer/search.py` (see §3.3).
  - `TABLE_BLOCK_IN_CELL`: block HTML into a table cell raises `ToolExecutionError(code="TABLE_BLOCK_IN_CELL")` in `replace_single_range_with_content` (`plugin/writer/html_import.py`).
- **Hand-built error dicts bypassing `make_tool_error`:**
  - `build_search_not_found_response` in `plugin/writer/search.py` (also reused in `plugin/writer/styles.py`), search near-miss responses (`closest_match`, `diff_at`), oversized refusals (`oversized: True`), and zero-replacement responses (`replaced_count: 0`) in `plugin/writer/search.py`.
  - Anchor miss dicts built in `apply_document_content._anchor_miss` (`plugin/writer/apply_document_content.py`), returning `{"status": "error", "code", "message", "replaced_count": 0}`.
  - Drawing shape match messages (`search_mod.shape_only_search_message` in `plugin/writer/search.py`, `apply_document_content.py`, `selection.py`).
  - Rollback failure payloads in `plugin/writer/apply_document_content.py` and `plugin/writer/styles.py`.
  - Hand-built dicts in `plugin/writer/styles.py` (`target='search' requires old_content` and search-miss `ValueError`).
  - *Invariant on hand-built dicts:* `is_replaced_zero_result` in `plugin/chatbot/tool_loop_actions.py` checks top-level `result_data.get("replaced_count") == 0`, and unit tests directly inspect top-level `closest_match`, `diff_at`, and `oversized`. These dicts are not routed through `make_tool_error` (which moves extra kwargs under `details` and invokes `_()` on untranslated runtime text); instead, each hand-built dict directly includes a `"code": "..."` field.
- **Warning fields:**
  - Writer unifies `data-lo-style` skips and `data-lo-para` drops into `warnings: list[Diagnostic]` via `html_import_styles.attach_write_report` (`plugin/writer/html_import_styles.py`, using `WriteReport`), called by `apply_document_content.py` and `page.py` (header/footer writes).
  - Other Writer tools write singular `warning` strings for unrelated conditions (`styles.py`, `page.py`, `selection.py`, `search.py`, `get_document_content.py`).
  - Draw, Calc, and vision: `result["warnings"]` is `list[str]` (`plugin/draw/shapes.py`, `plugin/calc/charts.py`, `vision/venv/vision.py`).

## 3. Design

### 3.1 Diagnostic and SuggestedCall shape

Types and builders live in `plugin/framework/errors.py` (which is already included in LibrePy's bundle list in `scripts/librepy_bundle_paths.py`). No separate module file is needed, avoiding bundle drift or import failures in LibrePy:

```python
# plugin/framework/errors.py
class SuggestedCall(TypedDict):
    tool: str
    arguments: dict[str, Any]

class Diagnostic(TypedDict):
    """Structured diagnostic reported on edit failures or partial applications.

    Carries a machine-readable code, a human-readable message, an optional
    suggested_call so the model can send it as its next tool call unchanged,
    and optional flat context fields (declare any new context keys here).
    """

    code: str
    message: str
    suggested_call: NotRequired[SuggestedCall]
    matches_skipped: NotRequired[int]
    matches_total: NotRequired[int]
    link_target: NotRequired[str]
```

Note on naming: `tool` matches tool registration and `ToolRegistry.execute(name, **arguments)`. (While the MCP wire protocol names this key `name`, internal execution and prompt conventions use `tool`).

**Builder:**
```python
# plugin/framework/errors.py
def suggest_call(tool: str, **arguments: Any) -> SuggestedCall:
    return {"tool": tool, "arguments": arguments}
```

**Errors:**
- `make_tool_error(message, code="TOOL_EXECUTION_ERROR", *, suggested_call=None, **details)` accepts `suggested_call: SuggestedCall | None = None`.
- `suggested_call` is attached at the top level of the returned payload dict alongside `code` and `message` when provided.
- `suggested_call` is not stored on `ToolExecutionError`: the re-wrap in `apply_document_content.py` preserves only `str(e)` and `code`, discarding exception attributes.
- Hand-built error dicts include `"code": "..."` directly at the top level, keeping `replaced_count`, `closest_match`, `diff_at`, and `oversized` at the top level.

**Warnings:**
- Each item in `result["warnings"]` is a `Diagnostic` dict.
- Context fields (such as `matches_skipped` and `matches_total`) sit flat on the `Diagnostic` dict rather than in a nested `details` dictionary.

### 3.2 Backward compatibility of `warnings` (Decision: Option A)

| Option | Pros | Cons |
|--------|------|------|
| **A. Writer-only:** `warnings` becomes `list[Diagnostic]`; Draw/Calc keep `list[str]` | Smallest change; isolates Writer editing; no cross-domain breakage. | Same key has two shapes across domains. |
| B. Add `diagnostics` field and keep `warnings` as strings | Zero breaking changes. | Two parallel fields; sends duplicate text to model. |
| C. Convert all domains to `Diagnostic` immediately | Single consistent schema. | Requires touching Calc, Draw, vision, and updating all tests across domains. |

**Decision: Option A.**
- Writer unifies style skips and `data-lo-para` drops into `result["warnings"]: list[Diagnostic]` via `html_import_styles.attach_write_report` (`plugin/writer/html_import_styles.py`), without writing the singular `warning` field (the explanatory sentence is already appended to `message`, avoiding duplicate text).
- Other tools emitting singular `warning` strings (e.g. `selection.py`, `page.py`) remain untouched.
- `attach_write_report` in `plugin/writer/html_import_styles.py` outputs `list[Diagnostic]` with `PARA_PROPERTY_UNSUPPORTED` for dropped `data-lo-para` properties, covering both `apply_document_content.py` and `page.py` header/footer writes.
- Draw, Calc, and vision remain `list[str]` and can adopt `Diagnostic` opportunistically.

*Test assertions updated in Phase 1:*
- `warnings` assertions: `tests/writer/test_apply_document_content_structured_return.py`, `tests/writer/test_page.py`, `tests/writer/test_page_header_html_uno.py`, `tests/writer/test_html_import_uno.py`.
- `warning` assertions: `tests/writer/test_apply_document_content.py`, `tests/writer/test_content_style_model_uno.py`.

### 3.3 Code catalog

The authoritative code catalog (codes, kinds, trigger sites, `suggested_call` rules, and naming invariant) lives in [`docs/framework/exception-policy.md`](../framework/exception-policy.md#structured-edit-diagnostic-codes).

*Heading link resolution:* `resolve_heading_links` (`plugin/writer/html_import_styles.py`) rewrites bare `href="#Heading"` fragments to `href="#Heading|outline"` when the fragment matches an outline heading text (derived from headings written in the HTML plus existing body, table-cell, and text-frame headings; skipped for `full_document`). Matching checks exact text first, then a unique case-insensitive match, then a unique XHTML export anchor id (`a__Heading_Text`). Targets matching an element `id` or `name` in the written HTML, or an existing bookmark in the document, are exempted from rewrite and warning. Bare fragments that match no heading or match several headings emit a `LINK_TARGET_NOT_FOUND` diagnostic warning once per distinct fragment, naming closest or colliding headings when available. When `apply_content` renames a heading (`full_document`, anchor spans, `old_content`, or `all_matches`), `retarget_outline_links` updates exact `#old|outline` links across the document (`links_retargeted`) and emits `LINK_TARGET_STALE` for field-backed links that cannot be rewritten as tracked changes while recording.

*Note on dropped items:* `TABLE_DELETED` is not emitted as a warning or undo suggestion; table deletion is already exposed via structured fields `table_deleted: true` and `pending_review` (`plugin/writer/apply_document_content.py`), and invoking `document_undo` during change recording is unsafe.

### 3.4 Building `suggested_call` safely

Rules for constructing suggestions:
1. **Emit only when all arguments are known:** Never produce incomplete or placeholder arguments. If the model must provide additional parameters, omit `suggested_call` and explain in `message`.
2. **Focus on cross-tool workflows:** In v1, `suggested_call` is reserved for cross-tool corrections the model would not otherwise infer (specifically item 4's `apply_style` after a partial match write, and unambiguous single-match `STYLE_NOT_FOUND`).
3. **Centralized construction per tool:** Construct suggestions in one place per tool from code and parsed arguments (e.g. in `_do_edit` after `_dispatch_edit` in `apply_document_content.py`), rather than plumbing `kwargs` through lower-level helpers.
4. **Filter to schema properties:** Validate arguments against `parameters["properties"]` of the target tool schema. This automatically filters out internal execution flags and MCP's `document_url`.
5. **Bound payload size:** Never echo raw HTML `content` back in a suggestion. Bound string arguments to short targets.
6. **Gate against change recording:** Do not suggest `apply_style` while tracked changes are recording (`record_changes.is_recording_changes(model)`), because `apply_style` is not recorded as a tracked change (`plugin/framework/prompts.py`).
7. **MCP transport independence:** MCP clients manage their own `document_url`; suggestions emit only standard tool arguments without transport-specific fields.
8. **No gettext formatting:** Argument values and code names must never pass through `_()`.

### 3.5 Partial-match style skips (item 4) in detail

**Behavior:**
`data-lo-style` is skipped when replacing text that does not span whole paragraphs (`_range_covers_whole_paragraphs`, `plugin/writer/html_import.py`) or while tracked changes are recording (`record_changes.is_recording_changes`). For each style skip, `result["warnings"]` receives a `Diagnostic` (via `html_import_styles.attach_write_report`):

```json
{
  "code": "STYLE_IGNORED_PARTIAL_MATCH",
  "message": "data-lo-style='Heading 2' was not applied because the match does not cover whole paragraphs; applying it would restyle neighboring text in the host paragraph. Restyle with apply_style(target='search', old_content='...').",
  "suggested_call": {
    "tool": "apply_style",
    "arguments": {
      "target": "search",
      "old_content": "Quarterly results",
      "style": "Heading 2",
      "case_sensitive": true
    }
  },
  "matches_skipped": 1,
  "matches_total": 1
}
```

**Rules for building the `apply_style` suggestion:**
1. **Target new visible text (`params.plain`):** Always target the newly inserted visible text, NOT the original search string. The edit has already executed and replaced the original text; `apply_style` must search for text that currently exists in the document.
2. **Single paragraph check:** `params.plain` must be a single paragraph (no `\n`).
3. **Single distinct style token:** Content must contain exactly one distinct non-empty `data-lo-style` token (`extract_block_lo_styles`).
4. **Resolved style token:** Resolve the style using `resolve_paragraph_style_token` (`plugin/writer/html_import_styles.py`). If it falls back to `"Standard"` for a token that was not `"Standard"`, treat the style as unresolved and omit `suggested_call`.
5. **Exact post-write match count:** After the write, verify the new text matches the expected count in the document: exactly 1 match (or exactly `replaced_count` matches when `all_matches=True`).
6. **Case sensitivity:** Always supply `case_sensitive: true`.
7. **Host paragraph restyling notice:** Paragraph styles apply to the entire paragraph enclosing the match. The warning `message` must clearly state that `apply_style` will restyle the entire host paragraph.
8. **Aggregated counts for `all_matches`:** Emit a single aggregated warning with `matches_skipped` and `matches_total`. The suggestion includes `all_matches: true` (which `apply_style` already supports).
9. **Recording gate:** Check `not record_changes.is_recording_changes(model)`. Because `_lo_style_for_replaced_range` checks partial match before recording, a partial match during recording triggers `STYLE_IGNORED_PARTIAL_MATCH`. Gating on recording status prevents emitting an untracked `apply_style` suggestion during redline recording.
10. **Selection target:** Omit `suggested_call` for `target="selection"`. Following insertion, the view selection is not guaranteed to span the inserted content.
11. **Before/after insertions:** `before` imports at the start of the match's paragraph via `insert_html_at_cursor` (default `apply_styles=True`); `after` uses `insert_html_after_paragraph(apply_styles=True)`, which opens a fresh paragraph. Neither merges, so styles apply and no warning is needed.

**Tests for item 4:**
- `tests/writer/test_apply_document_content.py`: verify `Diagnostic` payload, `code`, flat count fields, and omission of `suggested_call` when recording changes, style is unresolvable, or text contains newlines.
- `tests/writer/test_content_style_model_uno.py` (UNO replay): Execute the `suggested_call` against a live LibreOffice instance. Assert that the host paragraph receives the style and that **neighbouring paragraphs** retain their original styles.
- Schema validation: run the test validator over emitted suggestions to ensure they match `apply_style` schema requirements.

## 4. Implementation phases

Each phase is independently shippable, maintains green tests, and requires no temporary compatibility shims.

### Phase 0: Framework plumbing — Done
- `SuggestedCall`, `Diagnostic`, and `suggest_call` in `plugin/framework/errors.py`.
- Keyword-only `suggested_call: SuggestedCall | None = None` on `make_tool_error` and `format_error_payload` in `errors.py`.
- Schema-validation test helper in `tests/framework/suggested_call_access.py` (`validate_suggested_call`, `assert_valid_suggested_call`), imported by `tests/framework/test_errors.py`.
- Tests: `tests/framework/test_errors.py` (payload shape with and without `suggested_call`, schema validation helper).

### Phase 1: Item 4 — Partial-match style skips as diagnostics (Writer) — Done
- `html_import_styles.attach_write_report` (`plugin/writer/html_import_styles.py`) emits `Diagnostic` dicts in `result["warnings"]` instead of writing singular `warning`.
- `_lo_style_for_replaced_range` in `plugin/writer/html_import.py` returns diagnostic codes (`STYLE_IGNORED_PARTIAL_MATCH`, `STYLE_IGNORED_RECORDING`).
- `suggested_call` for `apply_style` follows §3.5 rules (new text `params.plain`, single paragraph, unique resolved style, exact match count, gated on recording).
- `attach_write_report` emits `Diagnostic` dicts (`PARA_PROPERTY_UNSUPPORTED`) for dropped `data-lo-para` properties across `apply_document_content.py` and `page.py` header/footer writes.
- Tests: `tests/writer/test_apply_document_content.py`, `tests/writer/test_apply_document_content_structured_return.py`, `tests/writer/test_page.py`, `tests/writer/test_html_import.py`, and UNO replay in `tests/writer/test_content_style_model_uno.py`.

### Phase 2: Code all Writer edit errors — Done
- Explicit `code` strings on `_tool_error(...)` sites across `plugin/writer/apply_document_content.py`, `plugin/writer/styles.py`, `plugin/writer/selection.py`, and `plugin/writer/search.py` according to §3.3.
- `ToolExecutionError(code="TABLE_BLOCK_IN_CELL")` in `plugin/writer/html_import.py`.
- `"code": "..."` on hand-built error dicts in `search.py` (near-miss, oversized), `apply_document_content.py` (`_anchor_miss`, rollback), and `styles.py` (search miss), retaining top-level fields (`replaced_count`, `closest_match`, `diff_at`, `oversized`).
- Tests: `tests/writer/test_styles.py`, `tests/writer/test_selection.py`, `tests/writer/test_search.py`, `tests/writer/test_apply_document_content_structured_return.py`.

### Phase 3: Prompt updates and documentation — Done
- Prompt instructions in `plugin/framework/prompts.py` reference `warnings[].suggested_call` for partial-match restyling and include `"When a result includes suggested_call, prefer executing it over re-deriving the call."` in `CONFIRM_EDITS_FROM_STRUCTURED_FIELDS`.
- Existing prompt invariants retained (`|outline`, no `$`, `style_unreviewed`, `clear_direct`, schema empty replacement sentence, table deletion rule).
- Documentation updated: code catalog in `docs/framework/exception-policy.md` and `docs/writer/llm-styles.md`.
- Tests: `tests/framework/test_prompts.py`.

### Phase 4 (Optional): Calc and Draw adoption
- Opportunistically migrate `warnings` in `plugin/calc/charts.py` and `plugin/draw/shapes.py` from `list[str]` to `list[Diagnostic]`.

## 5. Items 16 and 21 interaction

- **Item 21 (Search regex errors):** Shipped as `INVALID_REGEX` validated via LibreOffice's `TextSearch2` (ICU) service before searching. `details.engine` omitted (no consumer). Avoids conflicts with `invalid_regex_tool_message` (`plugin/writer/search.py`).
- **Item 16 (Math object import):** Successfully created Math objects return metadata in their own response field `math_objects`. A failed Math formula import that inserts `[Math import failed]` into the document (`plugin/writer/html_import.py`) represents dropped content and reports a `Diagnostic` warning with code `MATH_IMPORT_FAILED` and `math_objects`.

## 6. Risks and mitigations

| Risk | Mitigation |
|------|------------|
| Suggestion succeeds but performs the wrong edit | Do not suggest replacements for search near-misses (preview text is truncated). For style skips, clearly notify that `apply_style` restyles the host paragraph; assert neighbouring paragraphs retain their styles in UNO replay tests. |
| Inadvertent execution during change recording | Explicitly gate `suggested_call` on `not record_changes.is_recording_changes(model)`. `apply_style` is not tracked. |
| Token bloat and large payloads | Never echo `content` HTML in suggestions. Filter arguments strictly against the target tool's parameter schema. `warnings` remains omitted when empty. |
| LibrePy bundle breakage | Define all types and helpers in existing `plugin/framework/errors.py`, which is already bundled in `scripts/librepy_bundle_paths.py`. |
| Broken external readers of top-level error fields | Preserve existing top-level fields (`replaced_count`, `closest_match`, `diff_at`, `oversized`) on hand-built error dicts. Add `"code"` directly without routing through `make_tool_error`. |
| Translation and catalog drift | Machine codes and suggestion arguments are never translated via `_()`. No changes to `.po` or `.pot` catalogs. |

## 7. Test plan summary

| Module | Test File | Key Assertions |
|--------|-----------|----------------|
| `plugin/framework/errors.py` | `tests/framework/test_errors.py` | Payload format with/without `suggested_call`; schema validation helper for suggestions. |
| `plugin/writer/apply_document_content.py` | `tests/writer/test_apply_document_content.py` | `STYLE_IGNORED_PARTIAL_MATCH` warning emission, suggested call arguments, count fields, recording gate. |
| `plugin/writer/html_import.py` | `tests/writer/test_html_import.py` | `_lo_style_for_replaced_range` returns diagnostic codes; table cell block HTML errors. |
| `plugin/writer/styles.py` | `tests/writer/test_styles.py` | Coded error responses for style lookup, occurrence out of range, clear_direct validation, search misses. |
| `plugin/writer/selection.py` | `tests/writer/test_selection.py` | Coded error responses for parameter conflicts, missing view, not found. |
| `plugin/writer/search.py` | `tests/writer/test_search.py` | Missing search query/pattern errors carry `INVALID_PARAM`; miss dicts carry `OLD_CONTENT_NOT_FOUND` with top-level fields. |
| `plugin/writer/apply_document_content.py` | `tests/writer/test_apply_document_content_structured_return.py` | Coded parameter validation errors; `PARA_PROPERTY_UNSUPPORTED` warnings in `warnings` list. |
| UNO integration | `tests/writer/test_content_style_model_uno.py` | Replay test: execute `suggested_call` and verify host paragraph is restyled while neighbouring paragraphs are preserved. |
| `plugin/framework/prompts.py` | `tests/framework/test_prompts.py` | Prompt retains `suggested_call` rule in `CONFIRM_EDITS_FROM_STRUCTURED_FIELDS`. |

## Open

- `[eng M few]` Migrate Calc and Draw `warnings` from `list[str]` to `list[Diagnostic]` (`plugin/draw/shapes.py`, `plugin/calc/charts.py`, vision). (§ Phase 4 (Optional): Calc and Draw adoption)

# Structured Edit Diagnostics: `code` + `suggested_call` on Errors and Warnings (Plan)

**Status:** Plan, incorporating the 2026-11-09 Opus review. Tracks row 22 (standardized `code` on errors and warnings) of the editing-API status report (`docs/chat/opuswriteragentprompt.md`, "Seven principles" 1 and 2). Note: "error_code" in row 22 refers to the existing `code` key; no new key is added.
**Scope:** Writer editing tools first (`apply_document_content`, `apply_style`, `search_in_document`, `set_selection`). The mechanism is shared and generic, so Calc and Draw can adopt it later.

## 1. Goal

Every non-success outcome of a Writer edit (a hard error, or a success with dropped or unapplied content) must carry three machine-readable fields:

| Field | Meaning |
|-------|---------|
| `code` | Stable, documented, `UPPER_SNAKE` identifier. The model and tests branch on it, never on message wording. |
| `message` | One readable sentence explaining what occurred. Free to reword. |
| `suggested_call` | Optional. A complete tool call (`{"tool": ..., "arguments": {...}}`) that the model can send unchanged to achieve what it most likely intended. |

Why: prompt instructions carry complex API-mechanics rules largely because tool failures are prose that models must parse. A stable error code paired with a ready-to-execute suggested call allows trimming those defensive rules from the prompt, improving tool-use reliability especially for smaller models.

**Also in scope: status-report item 4 (partial-match style skips).** When `data-lo-style` is skipped because a search match covers only part of a paragraph, the result returns a structured `STYLE_IGNORED_PARTIAL_MATCH` warning containing a `suggested_call` for `apply_style(target="search", ...)`. Today this remediation is only described in prose in `message` and `warning`. Item 4 serves as the primary concrete consumer of the shared `Diagnostic` mechanism. Details are in §3.5 and Phase 1.

**Non-goals**
- No change to which edits succeed or fail. This is a diagnostics and metadata layer, not an edit behavior change.
- No translation of codes: `code` values are fixed machine identifiers and are never wrapped in `_()`. Warning messages and constants remain plain Python strings; agents must not edit `.po` or `.pot` catalogs (AGENTS.md).
- No new HTTP client, transport, or wire protocol. Payloads remain standard JSON dicts returned by tool execution.
- No speculative argument generation: `suggested_call` is omitted unless every required argument is known and validated.

## 2. What exists today (verified in the tree)

- **Error factory:** `make_tool_error(message, code="TOOL_EXECUTION_ERROR", **details)` in `plugin/framework/errors.py:404-414`. `ToolBase._tool_error` in `plugin/framework/tool.py:242-251` forwards keyword arguments to it. `format_error_payload` (`plugin/framework/errors.py:255-267`) formats exceptions into `{"status": "error", "code", "message", "details"?}`. Extra keyword arguments are nested under `details`.
- **Codes in use on the Writer edit path:**
  - Standard codes already emitted: `INVALID_PARAM`, `INVALID_REGEX`, `TABLE_INSERT_FAILED`, `HYPERLINK_UPDATE_FAILED`, `SHAPE_REPLACE_FAILED`, `OCCURRENCE_OUT_OF_RANGE`, `SEARCH_FAILED`, `ROLLBACK_FAILED`, `TOOL_EXECUTION_ERROR`.
  - Existing codes in the tree missing from prior catalogs: `UNDO_UNAVAILABLE` (`plugin/writer/apply_document_content.py:644, 722, 1468`) and `TABLE_IN_ANCHOR_RANGE` (`plugin/writer/apply_document_content.py:1579`).
  - Uncoded `_tool_error(...)` calls falling back to `TOOL_EXECUTION_ERROR`:
    - `plugin/writer/apply_document_content.py`: `dry_run` target conflict (line 892), `position` errors (lines 903, 1186, 1188, 1192), content type/item errors (lines 1207, 1211), empty content on insert targets (line 1214), math segments with `position='before'/'after'` (line 1631), and table cell `position='before'/'after'` (line 1639).
    - `plugin/writer/styles.py`: style missing error `_missing_style_error` (lines 518-525), empty style name (line 1312), `clear_direct` validations (lines 1338, 1342, 1347), occurrence out-of-range without code (line 1385), `UndoUnavailable` (line 1403), and mutation failures (lines 1407, 1408, 1426).
    - `plugin/writer/selection.py`: parameter conflicts and validations (lines 75, 77, 87-95), occurrence parse (line 83), missing document view/controller (line 99), empty search text (line 106), cursor build failure (lines 116, 120), occurrence `IndexError` (line 142), shape-only match (line 147), and search text not found (line 148).
    - `plugin/writer/search.py`: pattern required (line 1401) and query required (line 1526).
  - Unhandled exception: `TABLE_BLOCK_IN_CELL` raises raw `RuntimeError` (`plugin/writer/html_import.py:739`) instead of returning a coded tool error.
- **Hand-built error dicts bypassing `make_tool_error`:**
  - `build_search_not_found_response` in `plugin/writer/search.py:1262` (also reused in `plugin/writer/styles.py:1379`).
  - Search near-miss responses (`closest_match`, `diff_at`), oversized refusals (`oversized: True`), and zero-replacement responses (`replaced_count: 0`) in `plugin/writer/search.py:1253, 1303`.
  - Anchor miss dicts built in `apply_document_content._anchor_miss` (`plugin/writer/apply_document_content.py:1530-1556`), returning `{"status": "error", "message", "replaced_count": 0}` with no `code`.
  - Drawing shape match messages (`search_mod.shape_only_search_message`) at `plugin/writer/apply_document_content.py:684, 1545`.
  - Rollback failure payloads at `plugin/writer/apply_document_content.py:1481, 1499`.
  - Hand-built dicts in `plugin/writer/styles.py`: line 1375 (`target='search' requires old_content`) and line 1451 (search-miss `ValueError`, with TODO at line 1449).
  - *Invariant on hand-built dicts:* `is_replaced_zero_result` in `plugin/chatbot/tool_loop_actions.py:218-224` explicitly checks top-level `result_data.get("replaced_count") == 0`, and unit tests directly inspect top-level `closest_match`, `diff_at`, and `oversized`. These dicts must not be routed through `make_tool_error` (which moves extra kwargs under `details` and invokes `_()` on untranslated runtime text). Instead, each hand-built dict directly receives a `"code": "..."` field.
- **Warning fields today:**
  - Writer maintains two distinct warning fields with differing shapes:
    1. `warnings: list[str]`: reports dropped `data-lo-para` properties via `html_import_styles.attach_para_warnings` (`plugin/writer/html_import_styles.py:593-605`). Callers are `apply_document_content.py:1347, 1373, 1403, 1608` and `page.py:1017` (header/footer writes).
    2. `warning: str` (singular): reports `data-lo-style` skips via `_attach_style_notes` (`plugin/writer/apply_document_content.py:324-371`), which formats `_LO_STYLE_PARTIAL_MATCH_NOTE` and `_LO_STYLE_RECORDING_NOTE` into singular `warning` and appends to `message`. Other Writer tools also write singular `warning` strings for unrelated conditions (`styles.py:372, 1276-1278`, `page.py:381, 901, 1413`, `selection.py:128`, `search.py:1484`, `get_document_content.py:166`).
  - Silent style drop on before/after: block content next to a match (`apply_document_content.py:1674, 1683`) passes `apply_styles=False` to `insert_html_at_cursor`, stripping `data-lo-style` without emitting any warning note (`html_import.py:296, 305`).
  - Draw, Calc, and vision: `result["warnings"]` is a `list[str]` (`plugin/draw/shapes.py:675-707`, `plugin/calc/charts.py:762-764`, `vision/venv/vision.py:71`).

## 3. Design

### 3.1 Diagnostic and SuggestedCall shape

Types and builders are added to existing `plugin/framework/errors.py` (which is already included in LibrePy's bundle list in `scripts/librepy_bundle_paths.py:144`). No new module file is introduced, avoiding bundle drift or import failures in LibrePy:

```python
# plugin/framework/errors.py
class SuggestedCall(TypedDict):
    tool: str
    arguments: dict[str, Any]

class Diagnostic(TypedDict, total=False):
    code: str                  # required in practice
    message: str               # required in practice
    suggested_call: SuggestedCall  # optional
    # Optional flat context fields (matches_skipped, matches_total, etc.)
```

Note on naming: `tool` matches tool registration and `ToolRegistry.execute(name, **arguments)`. (While the MCP wire protocol names this key `name`, internal execution and prompt conventions use `tool`).

**Builder:**
```python
# plugin/framework/errors.py
def suggest_call(tool: str, **arguments: Any) -> SuggestedCall:
    return {"tool": tool, "arguments": arguments}
```

**Errors:**
- Extend `make_tool_error(message, code="TOOL_EXECUTION_ERROR", *, suggested_call=None, **details)` to accept `suggested_call: SuggestedCall | None = None`.
- Attach `suggested_call` at the top level of the returned payload dict alongside `code` and `message` when provided.
- Do not store `suggested_call` on `ToolExecutionError`: the re-wrap at `apply_document_content.py:1060, 1076` preserves only `str(e)` and `code`, discarding exception attributes.
- Hand-built error dicts add `"code": "..."` directly at the top level, keeping `replaced_count`, `closest_match`, `diff_at`, and `oversized` at the top level.

**Warnings:**
- Each item in `result["warnings"]` becomes a `Diagnostic` dict.
- Context fields (such as `matches_skipped` and `matches_total`) sit flat on the `Diagnostic` dict rather than in a nested `details` dictionary.

### 3.2 Backward compatibility of `warnings` (Decision: Option A)

| Option | Pros | Cons |
|--------|------|------|
| **A. Writer-only:** `warnings` becomes `list[Diagnostic]`; Draw/Calc keep `list[str]` | Smallest change; isolates Writer editing; no cross-domain breakage. | Same key has two shapes across domains. |
| B. Add `diagnostics` field and keep `warnings` as strings | Zero breaking changes. | Two parallel fields; sends duplicate text to model. |
| C. Convert all domains to `Diagnostic` immediately | Single consistent schema. | Requires touching Calc, Draw, vision, and updating all tests across domains. |

**Decision: Option A.**
- Writer unifies style skips and `data-lo-para` drops into `result["warnings"]: list[Diagnostic]`.
- Style skips in `_attach_style_notes` move from the singular `warning` string to `warnings: list[Diagnostic]`. `_attach_style_notes` stops writing the singular `warning` field, since the explanatory sentence is already appended to `message` (avoiding duplicate text).
- Other tools emitting singular `warning` strings (e.g. `selection.py`, `page.py`) remain untouched.
- `attach_para_warnings` in `plugin/writer/html_import_styles.py` converts to output `list[Diagnostic]` with code `PARA_PROPERTY_UNSUPPORTED`. Because `page.py:1017` calls `attach_para_warnings`, page header/footer writes switch to `Diagnostic` concurrently in Phase 1.
- Draw, Calc, and vision remain `list[str]` for now and can adopt `Diagnostic` opportunistically.

*Affected test assertions updating in Phase 1:*
- `warnings` assertions: `tests/writer/test_apply_document_content_structured_return.py:273, 294, 1032`, `tests/writer/test_page.py:423`, `tests/writer/test_page_header_html_uno.py:534, 548`, `tests/writer/test_html_import_uno.py:540`.
- `warning` assertions: `tests/writer/test_content.py:639, 682`, `tests/writer/test_content_style_model_uno.py:401, 432, 470, 501, 537, 581`.

### 3.3 Code catalog

Code constants remain string literals across call sites, consistent with the codebase invariant of avoiding unnecessary enum classes. The authoritative catalog is documented in `docs/framework/exception-policy.md`. Codes are consolidated into non-overlapping identifiers: do not invent overlapping variants (`PARAM_CONFLICT`, `CONTENT_EMPTY`, `OLD_CONTENT_EMPTY`, `ANCHOR_UNSUPPORTED_OPTION`) for parameter validation errors; use `INVALID_PARAM` unless the model must take a distinctly different action.

| Code | Kind | Trigger site(s) | `suggested_call` |
|------|------|-----------------|------------------|
| `OLD_CONTENT_NOT_FOUND` | error | `build_search_not_found_response` (`search.py:1262`, `styles.py:1379`, `styles.py:1451`) | None in v1 (near-miss `closest_match` is a 16-character preview + ellipsis; replaying would replace the wrong text). |
| `OLD_CONTENT_TOO_LARGE` | error | Oversized old_content refusal (`search.py:1303`) | None (suggesting blank content violates tool schema). |
| `ANCHOR_START_NOT_FOUND` | error | `_anchor_miss` with `miss_kind="start"` (`apply_document_content.py:1550`) | None |
| `ANCHOR_END_NOT_FOUND` | error | `_anchor_miss` with `miss_kind="end"` (`apply_document_content.py:1550`) | None |
| `ANCHOR_TEXT_DIFFERENT` | error | `_anchor_miss` with `miss_kind="different_text"` (`apply_document_content.py:1550`) | None |
| `SHAPE_TEXT_NEEDS_DELEGATION` | error | `shape_only_search_message` in `search.py:1315`, `apply_document_content.py:684, 1545`, `selection.py:147` | None (delegation tool requires free-text task and active inner-agent endpoint). |
| `STYLE_NOT_FOUND` | error | `_missing_style_error` in `styles.py:518-525` | Optional: `apply_style` with corrected `style` only if exactly one match in `similar`. |
| `OCCURRENCE_OUT_OF_RANGE` | error | `apply_document_content.py:895`, `styles.py:1385`, `selection.py:142` | None |
| `TABLE_IN_ANCHOR_RANGE` | error | `apply_document_content.py:1579` | None |
| `TABLE_BLOCK_IN_CELL` | error | Block HTML inserted in table cell (`html_import.py:739`) | None |
| `UNDO_UNAVAILABLE` | error | `apply_document_content.py:644, 722, 1468`, `styles.py:1403` | None |
| `ROLLBACK_FAILED` | error | `apply_document_content.py:1481, 1499`, `styles.py:1423` | None |
| `INVALID_PARAM` | error | Parameter validation failures: missing query/pattern (`search.py:1401, 1526`); invalid style/clear_direct (`styles.py:1312, 1338-1347`); selection parameter conflicts/empty values (`selection.py:75-106`); `apply_document_content` parameter conflicts/empty strings (`892, 903, 1186, 1188, 1192, 1207, 1211, 1214, 1631, 1639`) | None |
| `INVALID_REGEX` | error | `invalid_regex_tool_message` (`search.py:151`) | None (item 21 adds `details.engine: "icu" \| "python"`). |
| `STYLE_IGNORED_PARTIAL_MATCH` | warning | Partial match style skip (`_LO_STYLE_PARTIAL_MATCH_NOTE`, `html_import.py:507`) | `apply_style(target="search", old_content=<params.plain>, style=<style>, case_sensitive=True)` if conditions in §3.5 hold. |
| `STYLE_IGNORED_RECORDING` | warning | Tracked changes recording skip (`_LO_STYLE_RECORDING_NOTE`, `html_import.py:511`) | None (`apply_style` is not tracked). |
| `PARA_PROPERTY_UNSUPPORTED` | warning | Unapplied `data-lo-para` property (`html_import_styles.attach_para_warnings:593`) | None |

*Naming invariant:* A code names the underlying condition, not the tool. Once shipped, a code is never renamed.
*Note on dropped items:* `TABLE_DELETED` is not emitted as a warning or undo suggestion; table deletion is already exposed via structured fields `table_deleted: true` and `pending_review` (`apply_document_content.py:147-165`), and invoking `undo` during change recording is unsafe.

### 3.4 Building `suggested_call` safely

Rules for constructing suggestions:
1. **Emit only when all arguments are known:** Never produce incomplete or placeholder arguments. If the model must provide additional parameters, omit `suggested_call` and explain in `message`.
2. **Focus on cross-tool workflows:** In v1, `suggested_call` is reserved for cross-tool corrections the model would not otherwise infer (specifically item 4's `apply_style` after a partial match write, and unambiguous single-match `STYLE_NOT_FOUND`).
3. **Centralized construction per tool:** Construct suggestions in one place per tool from code and parsed arguments (e.g. in `_do_edit` after `_dispatch_edit` in `apply_document_content.py`), rather than plumbing `kwargs` through lower-level helpers.
4. **Filter to schema properties:** Validate arguments against `parameters["properties"]` of the target tool schema. This automatically filters out internal execution flags and MCP's `document_url`.
5. **Bound payload size:** Never echo raw HTML `content` back in a suggestion. Bound string arguments to short targets.
6. **Gate against change recording:** Do not suggest `apply_style` while tracked changes are recording (`record_changes.is_recording_changes(model)`), because `apply_style` is not recorded as a tracked change (`plugin/framework/prompts.py:391`).
7. **MCP transport independence:** MCP clients manage their own `document_url`; suggestions emit only standard tool arguments without transport-specific fields.
8. **No gettext formatting:** Argument values and code names must never pass through `_()`.

### 3.5 Partial-match style skips (item 4) in detail

**Current behavior:**
`data-lo-style` is skipped when replacing text that does not span whole paragraphs (`_range_covers_whole_paragraphs`, `plugin/writer/html_import.py:499-525`) or while tracked changes are recording (`record_changes.is_recording_changes`). Notes are appended to `message` and singular `warning`. In before/after insertions, `apply_styles=False` silently drops `data-lo-style` without emitting notes (`apply_document_content.py:1674, 1683`, `html_import.py:296, 305`).

**Target behavior:**
For each style skip, `result["warnings"]` receives a `Diagnostic`:

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
4. **Resolved style token:** Resolve the style using `_resolve_paragraph_style_token` (`plugin/writer/html_import_styles.py:547`). If it falls back to `"Standard"` for a token that was not `"Standard"`, treat the style as unresolved and omit `suggested_call`.
5. **Exact post-write match count:** After the write, verify the new text matches the expected count in the document: exactly 1 match (or exactly `replaced_count` matches when `all_matches=True`).
6. **Case sensitivity:** Always supply `case_sensitive: true`.
7. **Host paragraph restyling notice:** Paragraph styles apply to the entire paragraph enclosing the match. The warning `message` must clearly state that `apply_style` will restyle the entire host paragraph.
8. **Aggregated counts for `all_matches`:** Emit a single aggregated warning with `matches_skipped` and `matches_total`. The suggestion includes `all_matches: true` (which `apply_style` already supports).
9. **Recording gate:** Check `not record_changes.is_recording_changes(model)`. Because `_lo_style_for_replaced_range` checks partial match before recording, a partial match during recording triggers `STYLE_IGNORED_PARTIAL_MATCH`. Gating on recording status prevents emitting an untracked `apply_style` suggestion during redline recording.
10. **Selection target:** Omit `suggested_call` for `target="selection"`. Following insertion, the view selection is not guaranteed to span the inserted content.
11. **Before/after insertions:** Before/after inserts currently drop `data-lo-style` silently via `apply_styles=False`. In v1, leave before/after style behavior scoped out; any future warning for before/after must account for boundary snapping (`_paragraph_boundary_cursor`).

**Tests for item 4:**
- `tests/writer/test_content.py`: verify `Diagnostic` payload, `code`, flat count fields, and omission of `suggested_call` when recording changes, style is unresolvable, or text contains newlines.
- `tests/writer/test_content_style_model_uno.py` (UNO replay): Execute the `suggested_call` against a live LibreOffice instance. Assert that the host paragraph receives the style and that **neighbouring paragraphs** retain their original styles.
- Schema validation: run the test validator over emitted suggestions to ensure they match `apply_style` schema requirements.

## 4. Implementation phases

Each phase is independently shippable, maintains green tests, and requires no temporary compatibility shims.

### Phase 0: Framework plumbing
- Add `SuggestedCall`, `Diagnostic`, and `suggest_call` to `plugin/framework/errors.py`.
- Add keyword-only `suggested_call: SuggestedCall | None = None` to `make_tool_error` and `format_error_payload` in `errors.py`.
- Add schema-validation test helper in `tests/framework/test_errors.py` to validate `suggested_call` structures against tool schemas.
- Tests: `tests/framework/test_errors.py` (payload shape with and without `suggested_call`, schema validation helper).

### Phase 1: Item 4 — Partial-match style skips as diagnostics (Writer)
- Migrate `_attach_style_notes` in `plugin/writer/apply_document_content.py` to append `Diagnostic` dicts to `result["warnings"]` and discontinue writing singular `warning`.
- Update `_lo_style_for_replaced_range` in `plugin/writer/html_import.py` to return diagnostic codes (`STYLE_IGNORED_PARTIAL_MATCH`, `STYLE_IGNORED_RECORDING`) rather than note strings, simplifying aggregation.
- Build `suggested_call` for `apply_style` adhering strictly to §3.5 rules (new text `params.plain`, single paragraph, unique resolved style, exact match count, gated on recording).
- Update `attach_para_warnings` in `plugin/writer/html_import_styles.py` to emit `Diagnostic` dicts (`PARA_PROPERTY_UNSUPPORTED`). (This simultaneously updates `page.py:1017` header/footer writes).
- Note: Full-document replacement already routes through `attach_para_warnings` (`apply_document_content.py:1347`), automatically picking up the new diagnostic shape.
- Tests: `tests/writer/test_content.py`, `tests/writer/test_apply_document_content_structured_return.py`, `tests/writer/test_page.py`, `tests/writer/test_html_import.py`, and UNO replay in `tests/writer/test_content_style_model_uno.py`.

### Phase 2: Code all Writer edit errors
- Assign explicit `code` strings to all uncoded `_tool_error(...)` sites across `plugin/writer/apply_document_content.py`, `plugin/writer/styles.py`, `plugin/writer/selection.py`, and `plugin/writer/search.py` according to §3.3.
- Convert raw `RuntimeError` in `plugin/writer/html_import.py:739` to `ToolExecutionError(code="TABLE_BLOCK_IN_CELL")`.
- Add `"code": "..."` to hand-built error dicts in `search.py` (near-miss, oversized), `apply_document_content.py` (`_anchor_miss`, rollback), and `styles.py` (search miss). Retain all existing top-level fields (`replaced_count`, `closest_match`, `diff_at`, `oversized`).
- Tests: `tests/writer/test_styles.py`, `tests/writer/test_selection.py`, `tests/writer/test_search.py`, `tests/writer/test_apply_document_content_structured_return.py`.

### Phase 3: Prompt updates and documentation
- Update prompt instructions with a minimal two-line change:
  1. Update `plugin/framework/prompts.py:429` to reference `warnings[].suggested_call` for partial-match restyling.
  2. Add the prefer-`suggested_call` rule to `CONFIRM_EDITS_FROM_STRUCTURED_FIELDS` in `plugin/framework/prompts.py:380-381`:
     `"When a result includes suggested_call, prefer executing it over re-deriving the call."`
- Keep existing prompt invariants untouched (deferred to separate PROMPT-TRIM work): retain `|outline` (`line 431`), no `$` (`line 425`), `style_unreviewed` (`lines 391-392`), `clear_direct` (`lines 434-435`), schema empty replacement sentence (`apply_document_content.py:487`), and table deletion rule (`apply_document_content.py:473`).
- Mark Item 4 as ✅ in status reports.
- Update documentation: add code catalog to `docs/framework/exception-policy.md`; update `docs/writer/llm-styles.md` and Writer `AGENTS.md`.
- Tests: `tests/framework/test_prompts.py`.

### Phase 4 (Optional): Calc and Draw adoption
- Opportunistically migrate `warnings` in `plugin/calc/charts.py` and `plugin/draw/shapes.py` from `list[str]` to `list[Diagnostic]`.

## 5. Items 16 and 21 interaction

- **Item 21 (Search regex errors):** Kept as `INVALID_REGEX`, optionally carrying `details.engine: "icu" | "python"`. No `suggested_call` is emitted (guessing between `regex=false` and pattern fixes is ambiguous). Avoids conflicts with `invalid_regex_tool_message` (`plugin/writer/search.py:151`).
- **Item 16 (Math object import):** Successfully created Math objects return success metadata in their own response field (not a `Diagnostic`). However, a failed Math formula import that inserts `[Math import failed]` into the document (`plugin/writer/html_import.py:226`) represents dropped content and can report a `Diagnostic` warning (e.g. `MATH_IMPORT_FAILED`) once Phase 0 plumbing lands.

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
| `plugin/writer/apply_document_content.py` | `tests/writer/test_content.py` | `STYLE_IGNORED_PARTIAL_MATCH` warning emission, suggested call arguments, count fields, recording gate. |
| `plugin/writer/html_import.py` | `tests/writer/test_html_import.py` | `_lo_style_for_replaced_range` returns diagnostic codes; table cell block HTML errors. |
| `plugin/writer/styles.py` | `tests/writer/test_styles.py` | Coded error responses for style lookup, occurrence out of range, clear_direct validation, search misses. |
| `plugin/writer/selection.py` | `tests/writer/test_selection.py` | Coded error responses for parameter conflicts, missing view, not found. |
| `plugin/writer/search.py` | `tests/writer/test_search.py` | Missing search query/pattern errors carry `INVALID_PARAM`; miss dicts carry `OLD_CONTENT_NOT_FOUND` with top-level fields. |
| `plugin/writer/apply_document_content.py` | `tests/writer/test_apply_document_content_structured_return.py` | Coded parameter validation errors; `PARA_PROPERTY_UNSUPPORTED` warnings in `warnings` list. |
| UNO integration | `tests/writer/test_content_style_model_uno.py` | Replay test: execute `suggested_call` and verify host paragraph is restyled while neighbouring paragraphs are preserved. |
| `plugin/framework/prompts.py` | `tests/framework/test_prompts.py` | Prompt retains `suggested_call` rule in `CONFIRM_EDITS_FROM_STRUCTURED_FIELDS`. |

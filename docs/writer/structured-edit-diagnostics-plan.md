# Structured Edit Diagnostics: `code` + `suggested_call` on Errors and Warnings (Plan)

**Status:** plan, not implemented. Tracks row 22 of the editing-API status report (`opuswriteragentprompt.md`, "Seven principles" 1 and 2, §4 "What a good error looks like").
**Scope:** Writer editing tools first (`apply_document_content`, `apply_style`, `search_in_document`, `set_selection`). The mechanism is shared and generic, so Calc and Draw can adopt it later.

## 1. Goal

Every non-success outcome of a Writer edit (a hard error, or a success with something dropped) should carry three machine-readable fields:

| Field | Meaning |
|-------|---------|
| `code` | Stable, documented, `UPPER_SNAKE` identifier. The model and tests branch on it, never on message wording. |
| `message` | One readable sentence. Free to reword. |
| `suggested_call` | Optional. A complete tool call (`{"tool": ..., "arguments": {...}}`) that the model can send unchanged to do what it most likely meant. |

Why: the prompt carries API-mechanics rules mainly because failures are prose the model has to interpret. A stable code plus a ready-to-send fix lets us delete those rules from the prompt (status report §6), and it helps small models the most.

**Also in scope: status-report item 4 (partial-match style skips).** When `data-lo-style` is skipped because the match covers only part of a paragraph, the result should carry a structured `STYLE_IGNORED_PARTIAL_MATCH` warning with a `suggested_call` for `apply_style(target="search", ...)`. Today only the prompt and the free-text note name that call, and there is no structured field. This is the first concrete consumer of the shared `Diagnostic` mechanism, not a separate project. Details are in §3.5 and Phase 2. Closing it marks item 4 as ✅ in the status report.

**Non-goals**
- No change to which edits succeed or fail. This is a diagnostics layer, not a behavior change.
- No translation of codes. Messages go through gettext as usual and `code` is never translated. Agents must not edit `.po` or `.pot` files (AGENTS.md).
- No new HTTP client or new transport. Results are the same JSON dicts tools return today.

## 2. What exists today (verified in the tree)

- **Error factory:** `make_tool_error(message, code="TOOL_EXECUTION_ERROR", **details)` in `plugin/framework/errors.py`. `_tool_error` on `ToolBase` delegates to it. `format_error_payload` yields `{"status": "error", "code", "message", "details"?}`. Extra keyword arguments land under `details`.
- **Codes in use on the Writer edit path:** `INVALID_PARAM`, `INVALID_REGEX`, `TABLE_INSERT_FAILED`, `HYPERLINK_UPDATE_FAILED`, `SHAPE_REPLACE_FAILED`, `OCCURRENCE_OUT_OF_RANGE`, `SEARCH_FAILED`, `ROLLBACK_FAILED`, `TOOL_EXECUTION_ERROR`. Dozens of `_tool_error("...")` calls in `plugin/writer/apply_document_content.py` pass **no** code, so they all collapse to the default `TOOL_EXECUTION_ERROR`. Examples are the anchor parameter checks, `content is empty`, `old_content is empty after normalization`, and the `dry_run` and `position` conflicts.
- **Hand-built error dicts bypass the factory:** `build_search_not_found_response` and the oversized, shape-only and anchor-miss responses in `plugin/writer/search.py` return `{"status": "error", "message", ...}` with **no `code`**. The near-miss fields (`closest_match`, `diff_at`), `oversized: True`, and `replaced_count: 0` are ad hoc. Two more hand-built dicts sit in `apply_document_content.py` (`shape_only_search_message` at about lines 684 and 1544, and the `all_matches` rollback payload at about lines 1481 and 1499).
- **Warnings are free-form today:**
  - Writer: `_attach_para_warnings` (`data-lo-para`) and `_attach_style_notes` (`data-lo-style`) append notes. `_attach_style_notes` also folds the note into `message`. The module-level `_LO_STYLE_PARTIAL_MATCH_NOTE` and `_LO_STYLE_RECORDING_NOTE` strings are used as identity keys for counting.
  - Draw and Calc: `result["warnings"]` is a **list of strings** (`plugin/draw/shapes.py`, `plugin/calc/charts.py`), absent when empty. Callers and MCP clients use `.get("warnings", [])`.
- **Existing guidance in prompts** is already phrased as calls, for example "Restyle with apply_style(target='search', old_content=...)". That text is the raw material for `suggested_call`.

## 3. Design

### 3.1 Diagnostic shape

One small typed structure, built in one place:

```python
# plugin/framework/diagnostics.py (new, no UNO imports; safe for LibrePy paths)
class SuggestedCall(TypedDict):
    tool: str
    arguments: dict[str, Any]

class Diagnostic(TypedDict, total=False):
    code: str                 # required in practice
    message: str              # required in practice
    suggested_call: SuggestedCall
    details: dict[str, Any]   # code-specific fields (closest_match, diff_at, ...)

def diagnostic(code, message, *, suggested_call=None, **details) -> Diagnostic: ...
def suggest(tool, **arguments) -> SuggestedCall: ...
```

**Errors:** extend `make_tool_error(message, code, *, suggested_call=None, **details)` to put `suggested_call` at the **top level** of the payload next to `code` and `message`, not under `details`. This keeps the single source of truth, and `format_error_payload` gets an optional `suggested_call` entry. Tools keep calling `self._tool_error(..., code=..., suggested_call=suggest(...))`.

**Warnings:** each entry in `result["warnings"]` becomes a `Diagnostic` dict.

### 3.2 Backward compatibility of `warnings` (decision needed)

Draw and Calc consumers read `warnings` as `list[str]`. Options:

| Option | Pros | Cons |
|--------|------|------|
| A. Writer-only: `warnings` is `list[Diagnostic]`, Draw/Calc keep strings | Smallest change. No cross-tool risk. | Same key, two shapes across domains. |
| B. Add a new `diagnostics` list and keep `warnings` as strings | Zero break. | Two parallel fields. The model sees duplicates. |
| C. Move everything to `Diagnostic` now | One shape. | Touches Draw, Calc, vision, and their tests. |

**Recommendation: A**, then migrate Draw/Calc opportunistically (option C, one tool at a time). Writer `warnings` is new and recent (shipped in #3260), so nothing external depends on its string form yet. Confirm that with a grep of `scripts/` and `tests/` for Writer `warnings` before starting. In all cases the human sentence is also appended to `message` where it is today, so a model that ignores the structured fields still sees the text.

### 3.3 Code catalog

Codes live in one module, `plugin/writer/edit_codes.py` (or a section of `diagnostics.py`), as string constants in a single `class EditCode` namespace. A unit test walks the catalog and asserts that each code has a one-line description and that no two share a value. The first catalog below is the **initial** set. Add codes only when a call site needs one.

| Code | Kind | Trigger (current site) | `suggested_call` |
|------|------|------------------------|------------------|
| `OLD_CONTENT_NOT_FOUND` | error | `build_search_not_found_response` | If near-miss exists, the same call with `old_content=closest_match` |
| `OLD_CONTENT_NEAR_MISS` | detail on the above | near-miss fields | (same as above; `closest_match`, `diff_at` move under `details`, with the old top-level keys kept for now) |
| `OLD_CONTENT_TOO_LARGE` | error | oversized refusal | `apply_document_content(target="full_document", ...)` with the content argument left blank for the model to fill |
| `OLD_CONTENT_EMPTY` | error | "empty after normalization" | none |
| `ANCHOR_START_NOT_FOUND` / `ANCHOR_END_NOT_FOUND` / `ANCHOR_TEXT_DIFFERENT` | error | `anchor_miss_message` (`AnchorMissKind`: `start`, `end`, `different_text`) | none (or the call with the corrected anchor if a near-miss exists) |
| `ANCHOR_UNSUPPORTED_OPTION` | error | the `*_anchor` guards at about lines 1112–1120 | the same call with the option removed |
| `PARAM_CONFLICT` | error | `Pass old_content or start_anchor...not both`, `dry_run` vs `position`, and similar | none |
| `INVALID_PARAM` / `INVALID_REGEX` / `OCCURRENCE_OUT_OF_RANGE` | error | already coded | none |
| `SHAPE_TEXT_NEEDS_DELEGATION` | error | `shape_only_search_message` | the exact `shapes`-domain delegation call from the message |
| `CONTENT_EMPTY` | error | `content is empty; nothing was inserted` | none |
| `TABLE_BLOCK_IN_CELL` | error | block HTML sent into a table cell | the same call with inline content |
| `STYLE_IGNORED_PARTIAL_MATCH` | warning | `_LO_STYLE_PARTIAL_MATCH_NOTE` | `apply_style(target="search", old_content=<match>, style=<style>)` |
| `STYLE_IGNORED_RECORDING` | warning | `_LO_STYLE_RECORDING_NOTE` | none (the fix is to wait until the change is accepted) |
| `PARA_PROPERTY_UNSUPPORTED` | warning | `_attach_para_warnings`, one entry per dropped property | none |
| `TABLE_DELETED` | warning | `_table_deletion_result` | undo, only if the deletion was unintended (cross-checks the prompt's `undo` rule) |
| `ROLLBACK_FAILED` | error | already coded | none |

Naming rule: a code names the **condition**, not the tool. Never reuse a code for a different condition, and never rename a shipped code (tests and prompts reference them).

### 3.4 Building `suggested_call` safely

A suggestion is only useful if it runs. Rules:

1. **Only emit one when every argument is known.** Do not emit placeholder values. If the model must supply content, either omit the suggestion or leave the key out and say so in `message`.
2. **Echo the model's own arguments** (target, occurrence, case mode) and change only what the fix needs. Build suggestions from a copy of the parsed params, not from scratch, so the model's intent is preserved.
3. **Bound size.** Cap string arguments at a few hundred characters (`closest_match` is already short). Never inline whole paragraphs.
4. **Must validate.** A test helper takes a `suggested_call`, resolves `tool` against the registry, and validates `arguments` against that tool's JSON schema. This is the main guard against suggestions rotting when a schema changes.
5. **One suggestion per diagnostic.** If there are several plausible fixes, pick the most likely one and describe the others in `message`.

### 3.5 Partial-match style skips (item 4) in detail

**Current behavior.** `data-lo-style` is honored on `search` and `selection` only when the match covers whole paragraphs. Otherwise it is skipped, because applying it would restyle neighboring text. Skips come from `html_import.py` (`_LO_STYLE_PARTIAL_MATCH_NOTE`, and `_LO_STYLE_RECORDING_NOTE` while tracked changes are recording). `_attach_style_notes` in `apply_document_content.py` turns them into message text, with a "not applied to N of M matches" aggregate when `all_matches` is used. The fix is named only in prose: `apply_style(target='search', old_content=...)`.

**Target behavior.** For each skip, `result["warnings"]` gets a `Diagnostic`:

```json
{
  "code": "STYLE_IGNORED_PARTIAL_MATCH",
  "message": "data-lo-style='Heading 2' was not applied: the match covers part of a paragraph. ...",
  "suggested_call": {
    "tool": "apply_style",
    "arguments": {"target": "search", "old_content": "Quarterly results", "style": "Heading 2"}
  },
  "details": {"style": "Heading 2", "matches_skipped": 1, "matches_total": 1}
}
```

**Rules for building the suggestion**
1. `style` is the style the model asked for in `data-lo-style`. Resolve it to the real paragraph style name the way `apply_style` expects (for example, the HTML class or alias to the display name). If it can't be resolved to a known style, emit the warning **without** a `suggested_call`.
2. `old_content` is the text the model passed as `old_content` on the original call, copied unchanged. Do **not** use the replacement text, because the edit has already happened and `apply_style` must target text that now exists. Use the **new** text when the edit replaced the match, and the original text when the edit used `position='before'/'after'`. Verify this against `apply_style`'s own search semantics. This is the main trap in this item.
3. **Selection target:** there is no `old_content`. Use `apply_style(target="selection", ...)` if that tool supports it, otherwise omit the suggestion and keep the warning.
4. **`all_matches`:** emit one aggregated warning, not one per match. `details.matches_skipped` and `details.matches_total` carry the counts. The suggestion applies `apply_style` with `all_matches=true` if that option exists, and is omitted if not.
5. **Recording mode (`STYLE_IGNORED_RECORDING`):** a different code. No `suggested_call`, because `apply_style` is not tracked and the prompt asks the model to tell the user (`style_unreviewed`). The warning text says to restyle after the change is accepted.
6. Partial-match and recording skips can both apply in one write. Emit both codes, with counts that add up to the skipped total.

**Tests for item 4**
- `tests/writer/test_html_import.py` and `tests/writer/test_content.py`: partial match yields `STYLE_IGNORED_PARTIAL_MATCH` with the expected `suggested_call`. Whole-paragraph match yields no warning. The aggregate case reports the right counts. An unresolvable style yields a warning with no suggestion.
- `tests/writer/test_content_style_model_uno.py`: **replay test.** Apply the suggested call to the live document. Assert the paragraph ends up with the requested style and neighbouring text is unchanged.
- Schema-validation helper (§3.4 rule 4) runs over every suggestion produced by these tests.

## 4. Implementation phases

Each phase is independently shippable, adds tests, and leaves the tree green.

### Phase 0: Plumbing (small, low risk)
- Add `plugin/framework/diagnostics.py` (`Diagnostic`, `SuggestedCall`, `diagnostic`, `suggest`).
- Add the optional `suggested_call` keyword to `make_tool_error` and `format_error_payload`, and carry it through `ToolExecutionError`.
- Add the `EditCode` catalog and the catalog test.
- Add the `suggested_call` validation test helper (§3.4 rule 4).
- Tests: `tests/framework/test_errors.py` (payload shape with and without a suggestion), a new `tests/framework/test_diagnostics.py`.

### Phase 1: Code every Writer edit error (no new behavior)
- Give every code-less `_tool_error(...)` in `apply_document_content.py` a real code from the catalog. Routine mapping, see §3.3.
- Route the hand-built dicts in `search.py` and `apply_document_content.py` through one helper so they gain `code` and keep their existing extra keys (`replaced_count`, `closest_match`, `diff_at`, `oversized`) for compatibility. Prefer extending `make_tool_error` over adding a second factory (AGENTS.md: least code).
- Tests: extend `tests/writer/test_search.py`, `tests/writer/test_apply_document_content_structured_return.py`, and the matching anchor tests. Assert on `code`, never on message text. For each hand-built dict, assert `code` is present.
- Prompt: no change yet.

### Phase 2: Warnings become diagnostics (Writer)
- Replace `_attach_para_warnings` and `_attach_style_notes` internals so they append `Diagnostic` dicts to `result["warnings"]` (see §3.2, option A). Keep appending the readable sentence to `message`.
- Replace the `notes.count(_LO_STYLE_..._NOTE)` identity trick with code counting, so aggregation ("not applied to 2 of 5 matches") works on codes, not on string equality.
- **Item 4:** emit `STYLE_IGNORED_PARTIAL_MATCH` (and `STYLE_IGNORED_RECORDING`) with the `suggested_call` built under the rules in §3.5. This phase is where item 4 is closed.
- Have the full_document summary (#3262) include a `warnings` list from the same channel, so one write has one warning list.
- Tests: `tests/writer/test_content.py`, `tests/writer/test_html_import.py` (per-property para warnings), `tests/writer/test_content_style_model_uno.py` (end to end), plus the schema-validation helper over every emitted suggestion.

### Phase 3: Near-miss and anchor suggestions
- `OLD_CONTENT_NOT_FOUND` with a near-miss gets a `suggested_call` equal to the failed call with `old_content` replaced by the closest match text. Skip it if the diff is a large structural difference (score below the existing near-miss threshold).
- `OLD_CONTENT_TOO_LARGE` suggests `target="full_document"`.
- Anchor errors: same pattern.
- Tests: the Phase 1 tests plus a replay test. Apply the suggested call to the same document and assert it succeeds. This is the real proof the feature works.

### Phase 4: Prompt trim and docs
- Delete prompt text the tool now carries. Candidates (status report §6): the anchor and `old_content` grammar rules and the `data-lo-style` caveats now carried by warnings. Keep one short rule: **"When a result has `suggested_call`, prefer it over re-deriving the call."** Update `tests/framework/test_prompts.py` accordingly.
- Shorten the `data-lo-style` partial-match prose in `prompts.py` (it currently names `apply_style(...)` in text) to point at `suggested_call`.
- Mark status-report item 4 as ✅ and link this plan.
- Update docs: `docs/writer/llm-styles.md`, `docs/writer/html-style-model-plan.md`, the Writer `AGENTS.md`, and `docs/framework/exception-policy.md` (new top-level payload field and code catalog). Add the catalog table to `docs/mcp-protocol.md` if MCP clients see these payloads.

### Phase 5 (optional): other domains
- Calc search and Draw shapes adopt `Diagnostic` warnings one tool at a time, then option C (§3.2) is complete.

## 5. Risks and decisions

| Risk | Mitigation |
|------|-----------|
| Dual shape of `warnings` between Writer and Draw/Calc | Recommendation A in §3.2. Document the rule in the framework docs. |
| `suggested_call` goes stale when a tool schema changes | The schema-validation test (§3.4 rule 4) fails the build. |
| Model loops by applying a bad suggestion repeatedly | A suggestion is built from the failed call, so a repeat would fail the same way. Phase 3 replay tests catch the case where it can't succeed. Consider omitting the suggestion on the second identical failure (needs session state, so **out of scope**). |
| Result size and token cost grow | Cap sizes (§3.4 rule 3). Keep `details` small. `warnings` is absent when empty, as in Calc. |
| Scope creep into behavior fixes | This plan changes diagnostics only. Items such as literal `code`/`pre` handling and `clear_direct` renames are separate rows in the status report. |
| Message wording changes break existing tests | Phase 1 migrates new assertions to `code`. Do it per file in the same commit as the code change. |

**Open decisions for the owner**
1. Confirm option A for `warnings` shape (§3.2).
2. Confirm that `suggested_call` is top-level on errors, not under `details`.
3. Confirm the call format: `{"tool", "arguments"}` (matches how tool calls are sent) vs the `{"tool", "style", "target", ...}` flat form in the status report's §4 sketch. This plan recommends the nested form, which maps directly onto the registry's `execute(name, **arguments)`.

## 6. Test plan summary

| Layer | File(s) | Checks |
|-------|---------|--------|
| Unit | `tests/framework/test_errors.py`, `tests/framework/test_diagnostics.py` | Payload shape. Catalog uniqueness and descriptions. Suggestion validated against tool schemas. |
| Unit | `tests/writer/test_search.py` | Every miss response has a `code`. Near-miss and oversized suggestions. |
| Unit | `tests/writer/test_apply_document_content_structured_return.py`, `tests/writer/test_html_import.py`, `tests/writer/test_content.py` | Coded parameter errors. Warning aggregation by code. |
| UNO | `tests/writer/test_content_style_model_uno.py`, `tests/writer/test_apply_document_content_anchor_uno.py` | Replay: apply `suggested_call` and assert it succeeds. |
| Prompt | `tests/framework/test_prompts.py` | The trimmed prompt keeps the `suggested_call` rule. |

Run targeted pytest and `make typecheck` after each phase (AGENTS.md execution policy). Run `make test-uno` after Phases 2 and 3.

## 7. Estimated size

Phase 0: small (~100 lines plus tests). Phase 1: mechanical but wide (about 40 call sites). Phase 2: medium. Phase 3: small once Phase 1 lands. Phase 4: prompt and doc edits. Most of the risk is in Phase 2, because it changes the warning shape that models already see.


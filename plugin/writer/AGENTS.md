# Writer

Root invariants still apply (tool `uno_services` first, `guard_uno` at
document boundaries, LibrePy-safe helpers in `plugin/doc/text_helpers.py`).

## Entry points

- HTML import / apply-content (callers `import format as format_support`): `format.py` (with `html_import.py`, `html_import_styles.py`, `image_placeholders.py`, `field_restore.py`)
- Charts / shapes (shared **names** with Calc/Draw): `specialized/charts.py`, `specialized/shapes.py`
- Light reads (tracked deletions, heading tree, selection text/range, text slices): `plugin/doc/text_helpers.py`
- Streamed Writer rewrite/append: `plugin/writer/edit_review.py`; compound undo: `plugin/doc/compound_undo.py`

Topic docs: [docs/writer/math-tex.md](../../docs/writer/math-tex.md),
[docs/writer/grammar-checker-plan.md](../../docs/writer/grammar-checker-plan.md),
[docs/writer/specialized-toolsets.md](../../docs/writer/specialized-toolsets.md),
[docs/writer/bibliography-via-indexes.md](../../docs/writer/bibliography-via-indexes.md),
[docs/writer/llm-styles.md](../../docs/writer/llm-styles.md),
[docs/writer/reviewable-agent-edits.md](../../docs/writer/reviewable-agent-edits.md),
[docs/writer/lo-dom-semantic-tree.md](../../docs/writer/lo-dom-semantic-tree.md).

## Sharp edges

- The current Writer text selection is `selected_text_range` in `plugin/doc/text_helpers.py` (skips shape selections and collapsed ranges when a non-collapsed range is present); do not read `getByIndex(0)` for it.
- Writer `charts` / `shapes` share tool **names** with Calc/Draw — the Writer class must declare the **union** of those UNO services or execution rejects the document.
- Extend and Edit selection prompts must read text via `get_string_without_tracked_deletions()` in `text_helpers`, refusing selections that already contain tracked Inserts or Deletes (`TrackedChangesInSelection`). `wait_for_review` completes on an empty intersection of redline comments with registered change tokens, failing closed on unreliable scans while ignoring unregistered orphan tokens. See `tests/writer/test_edit_review.py`, `tests/writer/test_edit_review_uno.py`, and [docs/writer/reviewable-agent-edits.md](../../docs/writer/reviewable-agent-edits.md).
- CJK ruby is portion marks (`RubyText` on the start mark), not a text field. `get_document_content` emits `<ruby>…<rt>…</rt></ruby>` on read. `apply_document_content` strips `<rt>` then sets `RubyText` on the base (StarWriter import concatenates). Search/`getString()` are base-only. See [docs/writer/ruby.md](../../docs/writer/ruby.md).
- Do not import `document_helpers` from LibrePy paths (WriterAgent chat context / `DocumentService`). Light helpers stay in `text_helpers` / `doc_type` / `udprops` and are **not** re-exported from `document_helpers`.
- Search-replace inside outline hyperlinks (`#…|outline`) rewrites the target URL across the full span via `hyperlink_fixup`, preserving bookmark targets and restoring the link character styles and only direct colour / underline after subrange or page-digit replacements. Portion enumerations use `enum_has_more`. See `tests/writer/test_hyperlink_fixup.py`.
- `indexes_refresh_toc_entry`, `indexes_insert_toc_entry`, and `indexes_delete_toc_entry` edit individual TOC rows in-place inside an undo bracket without calling `update()` (which would discard custom formatting), restoring write protection and rolling back on failure. `indexes_list_toc_entries` inspects index paragraphs directly without updating the index or re-exporting document content; it returns entry text and level without `hyperlink_url` by default (pass `include_links=True` to include URLs) to avoid models echoing and corrupting outline links that the delegate rewrites automatically. See `tests/writer/specialized/test_indexes.py`, `tests/writer/specialized/test_indexes_uno.py`, and [docs/writer/specialized-toolsets.md](../../docs/writer/specialized-toolsets.md).
- `BookmarkService.ensure_heading_bookmarks` catches unreadable bookmark maps and leaves locators unchanged rather than inserting or pruning marks; bookmark names starting with `_mcp_` are reserved for internal locators (`bookmark_create`, `bookmark_rename`, and `bookmark_delete` reject them).
- `split_change` returns surgical with zero sub-edits when `old == new`, before `_MAX_DIFF_WORDS`. The cap is the full word count when that count fits. Above the cap, only the words outside the common word prefix and suffix are diffed, and a middle that is still over the cap is one block edit.
- `tracking_manage` skips redlines whose `XText` is not the target's (`uno_same`). Success is `redline_identity_key` dropping by one with every other key's count unchanged, and inline review's post-dispatch safety similarly requires no foreign redline_identity_key count to drop. The reply tells the model to call `tracking_list` again. Bulk resolve on a document with `getRedlines` checks `getCount()`, not the dispatch return value.
- Search tools share the case-insensitivity default `SEARCH_CASE_SENSITIVE_DEFAULT = False` in `plugin/writer/search.py` (`resolve_case_sensitive`). Tool results that execute a search report the mode that ran in `matched_case_sensitive` (omitted and JSON null both run case-insensitively).
- Tools that find the occurrence-th body match go through `occurrence_range_or_error` in `search.py`; the miss payload is `build_search_not_found_response`, which does the shape sweep itself.
- `warnings` in Writer tool results (`apply_document_content`, `page_set_header_footer_text`) is a `list[Diagnostic]`, not a `list[str]`. Each diagnostic dictionary carries `code` and `message`, and optionally `suggested_call` (such as `STYLE_IGNORED_PARTIAL_MATCH` providing an `apply_style` invocation to restyle the host paragraph; `STYLE_IGNORED_RECORDING`, `PARA_PROPERTY_UNSUPPORTED`, `PARA_IGNORED_PARTIAL_MATCH`, and `PARA_IGNORED_RECORDING` do not suggest calls). Do not write singular `warning` for style skips or dropped properties; Calc and Draw still emit `list[str]`.
- LibreOffice 26.2+ changed Writer's default paragraph alignment pool item (`RES_PARATR_ADJUST`) from `LEFT` (0) to `START` (5) (`sw/source/core/bastyp/init.cxx`). Tests asserting that an edit left neighbor paragraph formatting untouched should check `getPropertyState('ParaAdjust') == DEFAULT_VALUE`, not `ParaAdjust == 0`.



# Writer

Root invariants still apply (tool `uno_services` first, `guard_uno` at
document boundaries, LibrePy-safe helpers in `plugin/doc/text_helpers.py`).

## Entry points

- HTML import / apply-content (callers `import format as format_support`): `format.py`
- Charts / shapes (shared **names** with Calc/Draw): `specialized/charts.py`, `specialized/shapes.py`
- Light reads (tracked deletions, heading tree, selection text/range, text slices): `plugin/doc/text_helpers.py`
- Streamed Writer rewrite/append + compound undo: `plugin/writer/edit_review.py`

Topic docs: [docs/writer/math-tex.md](../../docs/writer/math-tex.md),
[docs/writer/grammar-checker-plan.md](../../docs/writer/grammar-checker-plan.md),
[docs/writer/specialized-toolsets.md](../../docs/writer/specialized-toolsets.md),
[docs/writer/bibliography-via-indexes.md](../../docs/writer/bibliography-via-indexes.md),
[docs/writer/llm-styles.md](../../docs/writer/llm-styles.md),
[docs/writer/reviewable-agent-edits.md](../../docs/writer/reviewable-agent-edits.md),
[docs/writer/lo-dom-semantic-tree.md](../../docs/writer/lo-dom-semantic-tree.md).

## Sharp edges

- Writer `charts` / `shapes` share tool **names** with Calc/Draw — the Writer class must declare the **union** of those UNO services or execution rejects the document.
- Extend / Edit selection prompts must use `get_string_without_tracked_deletions()` in `text_helpers`, not a raw text dump that includes tracked deletions.
- Do not import `document_helpers` from LibrePy paths (WriterAgent chat context / `DocumentService`). Light helpers stay in `text_helpers` / `doc_type` / `udprops` and are **not** re-exported from `document_helpers`.
- Search-replace of text inside an outline hyperlink (`#…|outline`) rewrites that `HyperLinkURL` when the matched text occurs once outside the trailing `|outline` (`hyperlink_fixup`). A hit that overlaps that suffix is skipped, so `line` in `#1.The line.|outline` still becomes `#1.The row.|outline`. A repeated title is left unchanged until `hyperlink_url` sets the target. `content` may equal `old_content` in that override, so a title that is already correct can still get a new URL. The write covers the whole link, not only the replaced characters. A match that overlaps two hyperlinks does not receive one URL across both. Bookmark targets (`#__RefHeading___Toc…`) are left alone. `hyperlink_url` applies only when the match overlaps exactly one outline link: it is rejected with `all_matches` and otherwise. Portion walks must use `_enum_has_more` (`hasMoreElements() is True`). `if not enum.hasMoreElements()` never stops on pytest `MagicMock` (the return is another mock). `pull_request` CI checks out the merge with the base branch, so those unit tests hang there while `workflow_dispatch` (branch SHA only) stays green. Assigning `HyperLinkURL` reapplies Internet-link defaults (navy + single underline) on the written range; `restore_outline_hyperlinks` snapshots `CharColor` / `CharUnderline` after the text replace and writes them back so a customized black / no-underline TOC entry does not pick up link chrome.
- `indexes_refresh_toc_entry` rewrites one TOC substring in place and does not call `update()`. Page numbers, other entries, and direct formatting stay (including colour / underline restored after the outline URL write). `indexes_update_all` is still the full rebuild, and that rebuild drops customized TOC formatting.

# Writer

Root invariants still apply (tool `uno_services` first, `guard_uno` at
document boundaries, LibrePy-safe helpers in `plugin/doc/text_helpers.py`).

## Entry points

- HTML import / apply-content: `html_import.py` (with `html_import_styles.py`, `image_placeholders.py`, `field_restore.py`, `html_ruby.py`); HTML export / `get_document_content`: `html_export.py` (with `range_copy.py`, `xhtml_style_postprocess.py`)
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



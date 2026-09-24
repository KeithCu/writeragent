# Writer list numbering (list-only)

This page locks **multi-level list** numbering after `apply_document_content`
HTML import. It is **not** Tools → Chapter Numbering (heading outline numbers).
Do not enable `ChapterNumberingRules` to exercise these paths. Chapter /
heading paint labels live on `writer_tree` as optional `chapter_number` —
see [chapter-numbering.md](chapter-numbering.md).

There is **no** `numbering_inspect` tool and no numbering specialized domain.
Tests and debug code read UNO paragraph properties (`NumberingStyleName`,
`NumberingLevel`, `ListId`, `ListLabelString`, `NumberingRules`) and
`get_document_content` XHTML directly.

## What HTML import does

Nested `<ol>` / `<ul>` via `apply_document_content` become real Writer lists
(StarWriter `HTML (StarWriter)` import):

| HTML | UNO |
|------|-----|
| Outer `<ol><li>` | `NumberingLevel=0`, shared `ListId` / `NumberingStyleName` |
| Nested `<ol><li>` | `NumberingLevel=1` (then `2`), same `ListId` |
| Body `<p>` after the list | no `NumberingStyleName` / `ListId` / `NumberingRules` |
| Second `<ol>` after that body para | **new** `ListId`, `ListLabelString` starts at `1.` again |

`ParaIsNumberingRestart` stays false on that second list. Restart is a new
list instance, not a restart flag on the first list.

A heading next to the list is only a **boundary**. Default Heading 2 has an
empty `NumberingStyleName` unless chapter numbering is turned on — this
project does not turn it on for these tests.

## Visible labels vs search

- `ListLabelString` and XHTML `Numbering_20_Symbols` spans show `1.` / `2.`.
  Nested levels are **not** hierarchical `1.2.3` with the default imported
  list style.
- Bullets export as `Bullet_20_Symbols` (`•`). `ListLabelString` on the
  bullet paragraph is empty; `NumberingType` at that level is `CHAR_SPECIAL`
  (6). Nested numbered children use `ARABIC` (4) and `1.`.
- `search_in_document("1.")` and `search_in_document("1.2")` return **0**.
  LibreOffice `XSearchable` does not see generated list labels. Search the
  item **text** instead.
- Title-only `apply_document_content` search replace of that item text keeps
  `NumberingLevel` / `NumberingStyleName` / `ListId` on the paragraph.

`NumberingStyleName` and `ListId` are generated per import. Assert they are
non-empty and equal (or unequal) across items — never pin a literal id.

## Tests

[`tests/writer/test_numbering_lists_uno.py`](../../tests/writer/test_numbering_lists_uno.py)
builds the fixture in-process (`target='full_document'` HTML). It uses the
same `@with_native_doc` / `skip_windows_leftover_hidden_load` pattern as other
`apply_document_content` Writer UNO suites.

Notebook leftover-list leak checks stay in
[`tests/notebook/test_writer_importer_uno.py`](../../tests/notebook/test_writer_importer_uno.py)
([jupyter-notebook-import.md](jupyter-notebook-import.md)).

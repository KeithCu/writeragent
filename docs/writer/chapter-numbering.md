# Writer chapter numbering (`chapter_number`)

Read-only v1: expose Writer’s **Tools → Chapter Numbering** paint label on
heading nodes. This is heading outline numbering — not HTML `<ol>` / `<ul>`
list markers. List numbering stays in [list-numbering.md](list-numbering.md).

Discussion: [github.com/KeithCu/writeragent/discussions/876](https://github.com/KeithCu/writeragent/discussions/876)

## What `writer_tree` emits

`TreeService.build_heading_tree` and `plugin.doc.text_helpers.build_heading_tree`
read paragraph `ListLabelString` for `OutlineLevel > 0` only:

```text
raw = str(para.getPropertyValue("ListLabelString") or "").rstrip(".")
# set chapter_number only when raw is non-empty
```

- **ON** (Suffix `""` or `"."`): nodes carry `chapter_number` (`"1"`, `"1.1"`,
  `"3.1"`). Trailing `.` is stripped so locators stay `chapter_number:3.1`.
- **OFF**: omit the key entirely. Do not emit `""`.
  `NumberingStyleName` can still be `"Outline"` — that is not an on/off signal.
- Heading `text` stays bare. The label is never written into body text.

Do **not** invent a number from outline depth, sibling order, or literal titles
such as `"DOCUMENT 7"`. LibreOffice already has the authoritative label.

## Locators

| Locator | Meaning |
|---------|---------|
| `heading:1.2` | Sibling-ordinal path: 1st H1 → 2nd child. Unchanged. |
| `chapter_number:3.1` | Exact match on the emitted `chapter_number` field. |

`chapter_number:` flatten-matches the field. If Chapter Numbering is off or no
heading has that label, resolve raises a clear error. It does not fall back to
`heading:` ordinals.

## Tests

Enable/disable Chapter Numbering in `@with_native_doc(..., reuse=False)` tests
with typed
`uno.invoke(..., replaceByIndex, uno.Any("[]com.sun.star.beans.PropertyValue", level))`
— see `tests/writer/chapter_numbering_fixtures.py`. `reuse=False` is required:
touched `ChapterNumberingRules` survive leftover Writer wipe and make Heading 2
`NumberingStyleName='Outline'`, which breaks the list-only suite. Do not fold
those fixtures into `tests/writer/test_numbering_lists_uno.py`.

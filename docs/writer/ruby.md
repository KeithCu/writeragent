# CJK ruby (furigana) read and write

Ruby in Writer is **not** a text field. `fields_list` stays 0; `TextField.Ruby` is unregistered. The model is:

- `TextPortionType="Ruby"` start/end marks
- Start mark carries `RubyText`, `RubyIsAbove`, `RubyAdjust` and has empty `getString()`
- Base is a normal `Text` run
- Paragraph `getString()` is **base only** (`漢字です` when the reading is `かんじ`)

Surgical edit already treats Ruby as offset-unsafe (`edit_review.py`). Leave that path alone.

## Read (`get_document_content`)

| Path | What went wrong | Fix |
|------|-----------------|-----|
| Full | `XHTML Writer File` has no `text:ruby` rule. Children concatenate: `漢字かんじです`. FODT keeps `<text:ruby>`. | Walk source portions; rewrite to `<ruby>漢字<rt>かんじ</rt></ruby>`. Skip the walk when FODT has no `<text:ruby`. |
| Range / selection | Temp-doc copy paints visible Text runs. Ruby marks are empty; `RubyText` on the Text run is `None`. Reading disappeared. | Same rewrite from source spans in the copied window. |

## Write (`apply_document_content`)

StarWriter HTML import concatenates ruby children (`前漢字かんじ後`, no Ruby portions). Before import, `<ruby>…<rt>…</rt></ruby>` is reduced to the **base** only. After import, `RubyText` / `RubyIsAbove` are set on each base run (the same cursor properties used to seed tests).

```html
<p>前<ruby>漢字<rt>かんじ</rt></ruby>後</p>
```

becomes body `前漢字後` with Ruby marks, and a later get emits `<ruby>/<rt>` again.

Format-preserving (plain-text) replace of the base does not drop existing ruby: `setString` is skipped when characters match, so the Ruby portions stay.

## Unchanged / out of scope

- **Search / `getString()`:** reading is not in the paragraph string. `search_in_document` matches `漢字`, not `かんじ`.
- **`fields_insert(Ruby)`:** wrong model — do not add it.
- **Specialized ruby tool:** not added. Write lives in `apply_document_content` / `html_import`.

Code: [`plugin/writer/html_export.py`](../../plugin/writer/html_export.py) (read), [`plugin/writer/html_import.py`](../../plugin/writer/html_import.py) (`extract_and_strip_ruby`, `_apply_ruby_spans`). Tests: [`tests/writer/test_html_export.py`](../../tests/writer/test_html_export.py), [`tests/writer/test_html_export_uno.py`](../../tests/writer/test_html_export_uno.py), [`tests/writer/test_html_import.py`](../../tests/writer/test_html_import.py), [`tests/writer/test_html_import_uno.py`](../../tests/writer/test_html_import_uno.py).

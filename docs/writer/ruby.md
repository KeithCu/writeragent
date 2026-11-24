# CJK ruby (furigana) read and write

Ruby in Writer is **not** a text field. `field_list` stays 0; `TextField.Ruby` is unregistered. The model is:

- `TextPortionType="Ruby"` start/end marks
- Start mark carries `RubyText`, `RubyIsAbove`, `RubyAdjust` and has empty `getString()`
- Base is a normal `Text` run
- Paragraph `getString()` is **base only** (`漢字です` when the reading is `かんじ`)

Surgical edit already treats Ruby as offset-unsafe (`edit_review.py`). Leave that path alone.

## Read (`get_content`)

| Path | What went wrong | Fix |
|------|-----------------|-----|
| Full | `XHTML Writer File` has no `text:ruby` rule. Children concatenate: `漢字かんじです`. FODT keeps `<text:ruby>`. | Walk source portions; rewrite to `<ruby>漢字<rt>かんじ</rt></ruby>`. Skip the walk when FODT has no `<text:ruby`. |
| Range / selection | Temp-doc copy paints visible Text runs. Ruby marks are empty; `RubyText` on the Text run is `None`. Reading disappeared. | Same rewrite from source spans in the copied window. |

## Write (`apply_content`)

StarWriter HTML import concatenates ruby children (`前漢字かんじ後`, no Ruby portions). Before import, `<ruby>…<rt>…</rt></ruby>` is replaced by a `[[WA-RUBY:<nonce>:<i>]]` placeholder before the **base** (`ruby_placeholders`). After import, `field_restore.find_tokens` locates each token (in body, table cells, or frames), deletes it, and sets `RubyText` / `RubyIsAbove` / `RubyAdjust` on the following base characters (`_place_ruby`).

```html
<p>前<ruby>漢字<rt>かんじ</rt></ruby>後</p>
```

becomes body `前漢字後` with Ruby marks, and a later get emits `<ruby>/<rt>` again. An under ruby keeps `<ruby style="ruby-position: under">`; the import writes `RubyIsAbove` from that. When a token is not found or its following text does not match the base, the mismatch is logged and the base stays plain.

Format-preserving (plain-text) replace of the base does not drop existing ruby: `setString` is skipped when characters match, so the Ruby portions stay.

## Unchanged / out of scope

- **Search / `getString()`:** reading is not in the paragraph string. `search` matches `漢字`, not `かんじ`.
- **`field_insert(Ruby)`:** wrong model — do not add it.
- **Specialized ruby tool:** not added. Write lives in `apply_content` / `html_import`.

Code: [`plugin/writer/html_ruby.py`](../../plugin/writer/html_ruby.py). Tests: [`tests/writer/test_html_export.py`](../../tests/writer/test_html_export.py), [`tests/writer/test_html_export_uno.py`](../../tests/writer/test_html_export_uno.py), [`tests/writer/test_html_import.py`](../../tests/writer/test_html_import.py), [`tests/writer/test_html_import_uno.py`](../../tests/writer/test_html_import_uno.py).

# CJK ruby (furigana) on the Writer read path

**Status:** Phase 1 **read** shipped. Phase 2 write (recreating Ruby portions on apply) is out of scope.

Ruby in Writer is **not** a text field. `fields_list` stays 0; `TextField.Ruby` is unregistered. The model is:

- `TextPortionType="Ruby"` start/end marks
- Start mark carries `RubyText`, `RubyIsAbove`, `RubyAdjust` and has empty `getString()`
- Base is a normal `Text` run
- Paragraph `getString()` is **base only** (`漢字です` when the reading is `かんじ`)

Surgical edit already treats Ruby as offset-unsafe (`edit_review.py`). Leave that path alone.

## Why export used to lie

| Path | What went wrong |
|------|-----------------|
| Full `get_document_content` | `XHTML Writer File` has no `text:ruby` rule. ODF children concatenate: `漢字` + `かんじ` → body text `漢字かんじです`. Flat ODF *does* keep `<text:ruby><text:ruby-base>…</text:ruby-base><text:ruby-text>…</text:ruby-text></text:ruby>`. |
| Range / selection | Temp-doc copy paints `_COPIED_CHAR_PROPERTIES` from **visible** portions. Ruby marks are empty, so they are skipped; the Text run has `RubyText=None`. Reading disappeared. |

Both are fixed on **read** by walking source Ruby/Text portions and rewriting the semantic HTML to:

```html
<ruby>漢字<rt>かんじ</rt></ruby>です
```

Full export skips the portion walk when the paired FODT sidecar has no `<text:ruby`. Range/selection always rewrite from the copied window (the temp FODT has no ruby).

## What is unchanged

- **Search / `getString()`:** reading is not in the paragraph string. `search_in_document` matches `漢字`, not `かんじ`.
- **Apply:** importing `<ruby>` still flattens. Do not claim a round-trip. Phase 2 would set `RubyText` on the base run (the same cursor property used to seed tests).
- **`fields_insert(Ruby)`:** wrong model — do not add it.
- **Specialized ruby tool:** not added.

Code: [`plugin/writer/html_export.py`](../../plugin/writer/html_export.py) (`iter_ruby_spans`, `inject_ruby_into_html`). Tests: [`tests/writer/test_html_export.py`](../../tests/writer/test_html_export.py), [`tests/writer/test_html_export_uno.py`](../../tests/writer/test_html_export_uno.py).

# Writer Document Corpus UNO Suite

[`tests/writer/test_corpus_uno.py`](../../tests/writer/test_corpus_uno.py) exercises Writer tools against six generated Word-style (`.docx`) and flat ODF (`.fodt`) documents built by [`tests/writer/corpus_fixture.py`](../../tests/writer/corpus_fixture.py). The `.docx` files are modelled on WordprocessingML rather than saved by Word itself. Six documents run the four checks below; the seventh test reuses the contract builder with `struck_before_heading=True`.

## Documents

| Test | Format | Coverage |
|---|---|---|
| `test_corpus_contract_uno` | `.docx` | Two authors' redlines, numbered justified clauses, merged redlined table, TOC, header logo |
| `test_corpus_row_changes_uno` | `.docx` | Inserted and deleted table rows, format change (`w:rPrChange`), deleted paragraph break |
| `test_corpus_notes_uno` | `.docx` | Footnote, endnote, comment, bookmark cross-reference, content control, insertion |
| `test_corpus_letter_uno` | `.docx` | No tracked changes; letterhead table with logo in the header |
| `test_corpus_sections_uno` | `.fodt` | Protected and hidden text sections |
| `test_corpus_redlined_odf_uno` | `.fodt` | Benchmark contract (small): redlines in a merged table and a three-paragraph deletion |
| `test_corpus_read_keeps_struck_clause_before_heading_uno` | `.docx` | Struck clause with deleted paragraph mark before a heading (currently `SKIP`s on known gap F1) |

## Checks per document

1. **Import shape (`_check_import`)**: verifies expected redlines (authors and types), body tables, document indexes, header images, footnotes, endnotes, comments, bookmarks, and section names/flags are present.
2. **Read tools (`_check_reads`)**: runs every registered Writer read tool declaring `TextDocument` (excluding view-moving/folder reads and known-broken cases) and asserts all succeed and leave `_state` unchanged.
3. **Unchanged writes (`_check_unchanged_writes`)**: with Track Changes off, writes back `apply_content` (`target="search"`), header/footer (`include_images=False`), and unredlined non-empty table cells, asserting `_state` is unchanged.
4. **Recorded edit + Reject All + save/reload (`_check_recorded_edit`)**: enables `RecordChanges`, applies one edit, saves to the document's format and reloads, checks counts match, and verifies `Reject All` matches rejecting on an unedited copy.

## Adding a document or read tool

- **New document**: add a builder and `CorpusDoc` constant in [`tests/writer/corpus_fixture.py`](../../tests/writer/corpus_fixture.py), then add a `@native_test` calling `_check(ctx, YOUR_DOC)` in [`tests/writer/test_corpus_uno.py`](../../tests/writer/test_corpus_uno.py).
- **New read tool**: `_read_calls` pulls from the live tool registry. Tools declaring `TextDocument` with no required parameters run automatically; tools with required parameters fail with `add arguments for read tool <name>` until listed in `_read_calls`'s `arguments` dict.

## Known gaps

- **F1 (live mutation on full `get_content`)**: a full `get_content` (via ODF store; `writer8` and flat ODF stores trigger it while DOCX, HTML, and PDF stores do not) restyles a clause struck with its paragraph mark right before a heading to `Heading 1` and renumbers the next heading (`Fees`, `3.` → `4.`) in the HTML the model reads; Reject All afterwards restores the clause as `Heading 1`. The likely cause (`sw/source/filter/xml/wrtxml.cxx` `SetRedlineFlags` / `SwRangeRedline::MoveFromSection`) was read in source and not debugged. Tested by `test_corpus_read_keeps_struck_clause_before_heading_uno` (currently `SkipTest`).
- **F2 (`shape_get_summary`)**: fails on Writer documents with pictures or frames because `SwXFrame::getPosition` (`sw/source/core/unocore/unoframe.cxx`) throws; skipped in `_read_calls` when pictures or frames exist.
- **F3 (header write-back with images)**: `page_get_header_footer_text(include_images=True)` written back fails with `"Image 'Logo' was lost during header/footer import"` (`plugin/writer/image_placeholders.py`) for `logo_32.png` and a 64×64 PNG, while working for an 8×8 PNG; `_check_unchanged_writes` uses `include_images=False`. Separately, `apply_content` with `<img src="data:...">` returned `ok` with 0 pictures for five images of 268 to 3,172 bytes.
- **F4 (redlined table cells)**: closed — `table_set_cell` reports `old_text` as the visible text, writing back the visible text changes nothing, and a different text is refused with `MATCH_CROSSES_DELETION` while Track Changes is off.
- **F5 (`full_document` write-back)**: writing back full HTML from a numbered `.docx` duplicates heading numbers and drops all redlines, the TOC, and list labels; `_check_unchanged_writes` uses `target="search"`.
- **F6 (empty table cells with pictures)**: closed — `table_set_cell` with `""` on a picture-only cell changes nothing, and new text keeps the picture.
- **F7 (header logo spacing)**: a space between a header logo and text comes back as a no-break space.

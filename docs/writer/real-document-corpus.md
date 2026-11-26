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
3. **Unchanged writes (`_check_unchanged_writes`)**: with Track Changes off, writes back `apply_content` (`target="search"`), refuses `apply_content` (`target="full_document"`, `PENDING_TRACKED_CHANGES`) when redlines exist, writes back header/footer (`include_images=False`) and unredlined non-empty table cells, asserting `_state` is unchanged.
4. **Recorded edit + Reject All + save/reload (`_check_recorded_edit`)**: enables `RecordChanges`, applies one edit, asserts new redlines carry the recording author while earlier ones keep their author and type, saves to the document's format and reloads, checks counts match, and verifies `Reject All` matches rejecting on an unedited copy.

## Adding a document or read tool

- **New document**: add a builder and `CorpusDoc` constant in [`tests/writer/corpus_fixture.py`](../../tests/writer/corpus_fixture.py), then add a `@native_test` calling `_check(ctx, YOUR_DOC)` in [`tests/writer/test_corpus_uno.py`](../../tests/writer/test_corpus_uno.py).
- **New read tool**: `_read_calls` pulls from the live tool registry. Tools declaring `TextDocument` with no required parameters run automatically; tools with required parameters fail with `add arguments for read tool <name>` until listed in `_read_calls`'s `arguments` dict.

## Known gaps

- **F1 (live mutation on full `get_content`)**: a full `get_content` (via ODF store; `writer8` and flat ODF stores trigger it while DOCX, HTML, and PDF stores do not) restyles a clause struck with its paragraph mark right before a heading to `Heading 1` and renumbers the next heading (`Fees`, `3.` → `4.`) in the HTML the model reads; Reject All afterwards restores the clause as `Heading 1`. The likely cause (`sw/source/filter/xml/wrtxml.cxx` `SetRedlineFlags` / `SwRangeRedline::MoveFromSection`) was read in source and not debugged. Tested by `test_corpus_read_keeps_struck_clause_before_heading_uno` (currently `SkipTest`).
- **F2 (`shape_get_summary`)**: fails on Writer documents with pictures or frames because `SwXFrame::getPosition` (`sw/source/core/unocore/unoframe.cxx`) throws; skipped in `_read_calls` when pictures or frames exist.
- **F3 (header write-back with images)**: `page_get_header_footer_text(include_images=True)` written back fails with `"Image 'Logo' was lost during header/footer import"` (`plugin/writer/image_placeholders.py`) for `logo_32.png` and a 64×64 PNG, while working for an 8×8 PNG; `_check_unchanged_writes` uses `include_images=False`. Separately, `apply_content` with `<img src="data:...">` returned `ok` with 0 pictures for five images of 268 to 3,172 bytes.
- **F4 (redlined table cells)**: rewriting a redlined cell with Track Changes off resolves pending changes and produces `old_text` mismatches (for example, `old_text` `100200` against `table_get_cells` `200`); redlined cells are skipped in `_check_unchanged_writes`.
- **F5 (`full_document` write-back)**: writing back full HTML from a numbered `.docx` duplicates heading numbers, the TOC, and list labels; with Track Changes off over pending redlines, `full_document` now refuses with `PENDING_TRACKED_CHANGES` (verified in `_check_unchanged_writes`, which still uses `target="search"` for the unchanged write-back).
- **F6 (empty table cells with pictures)**: `table_set_cell` with `""` clears pictures in the cell; empty cells are skipped in `_check_unchanged_writes`.
- **F7 (header logo spacing)**: a space between a header logo and text comes back as a no-break space.

# Copyright (c) 2025-2026 Keith C. Ivey
# Distributed under the terms of the GNU General Public License v3 or later.
"""Range and flow copy helpers for LibreOffice Writer documents.

Clones paragraphs, tables, fields, and direct formatting into scratch documents
without clipboard transferables or XHTML filter roundtrips.
"""
from __future__ import annotations

import logging
from typing import Any, Iterator

from plugin.doc.text_helpers import FAILED, iter_elements, skip_tracked_deletions
from plugin.framework.errors import best_effort, uno_read
from plugin.framework.uno_context import uno_same
from plugin.writer.math.math_mml_export import (
    OBJECT_REPLACEMENT,
    portion_math_embed,
    read_starmath,
)

log = logging.getLogger("writeragent.writer")


# com.sun.star.text.ControlCharacter.PARAGRAPH_BREAK
PARAGRAPH_BREAK = 0


def record_math_placeholder(records: list[tuple[str, str, bool]], emb: Any, *, display_block: bool) -> str:
    ph = chr(0xE000 + len(records))
    records.append((ph, read_starmath(emb), display_block))
    return ph


def _fallback_insert_para_text(dest_text: Any, cursor: Any, el: Any) -> None:
    try:
        dest_text.insertString(cursor, el.getString() if el else "", False)
    except Exception as exc:
        log.debug("paragraph text fallback failed: %s", exc)


def iter_portions(portions: Any) -> Iterator[tuple[Any, str]]:
    for portion in iter_elements(portions):
        try:
            kind = portion.getPropertyValue("TextPortionType")
        except Exception:
            break
        yield portion, kind


def _portion_field(portion: Any) -> Any:
    try:
        return portion.getPropertyValue("TextField")
    except Exception as exc:
        log.debug("TextField unreadable: %s", exc)
        return None


def copy_text_run(portion: Any, dest_text: Any, cursor: Any, chunk: str, style: Any = None) -> None:
    start = cursor.getStart()
    dest_text.insertString(cursor, chunk, False)
    run = dest_text.createTextCursorByRange(start)
    run.gotoRange(cursor.getEnd(), True)
    _copy_properties(portion, run, _COPIED_CHAR_PROPERTIES, style)
    cursor.gotoRange(run.getEnd(), False)


def _copy_para_properties_to_end(dest_text: Any, cursor: Any, el: Any, style: Any = None) -> None:
    para_cursor = dest_text.createTextCursor()
    para_cursor.gotoEnd(False)
    para_cursor.gotoStartOfParagraph(False)
    para_cursor.gotoEndOfParagraph(True)
    _copy_properties(el, para_cursor, _COPIED_PARA_PROPERTIES, style)
    cursor.gotoRange(para_cursor.getEnd(), False)


def _copy_math_or_text(
    portion: Any,
    parent_el: Any,
    dest_text: Any,
    dest_cursor: Any,
    math_records: list[Any] | None,
) -> None:
    if math_records is not None:
        emb = portion_math_embed(portion)
        if emb is not None:
            raw = parent_el.getString() if hasattr(parent_el, "getString") else ""
            stripped = raw.replace(OBJECT_REPLACEMENT, "").strip()
            ph = record_math_placeholder(math_records, emb, display_block=stripped == "")
            dest_text.insertString(dest_cursor, ph, False)
            return
    chunk = uno_read(lambda: portion.getString(), "")
    if chunk:
        dest_text.insertString(dest_cursor, chunk, False)



# Direct formatting the range copy carries into the temp document. Whitelists rather than "every
# property": a blanket copy drags UNO structs and page/section properties along, which either fail
# to set or change the temp document's layout. Char* is painted per text portion so a bold run
# inside a sentence survives; Para* is set once per paragraph.
# HyperLink* is not a Char* style. The temp copy is setString, which drops the
# source portion's URL, so the XHTML filter would export the TOC line with no
# href. Full-document export does not use this list; it filters the real model.
# RubyText / RubyIsAbove / RubyAdjust are omitted on purpose: they live on the
# empty Ruby *start* mark, not the visible Text run (_visible_portions skips
# empty marks). Painting them from the Text run copies None. Recreating live
# ruby on the temp doc would still glue in XHTML (no text:ruby rule). Range
# export instead rewrites HTML from source spans (inject_ruby_into_html).
_COPIED_CHAR_PROPERTIES = (
    "CharStyleName", "CharFontName", "CharHeight", "CharWeight", "CharPosture",
    "CharUnderline", "CharStrikeout", "CharColor", "CharBackColor", "CharCaseMap",
    "CharEscapement", "CharEscapementHeight",
    "HyperLinkURL", "HyperLinkName", "HyperLinkTarget",
)
# Every UNO property that html_import_styles._para_writers() sets must be listed
# in _COPIED_PARA_PROPERTIES so range, selection, and header reads (as well as
# page-style clone) carry it into the destination. test_every_para_writer_property_is_copied_by_range_reads
# pins this coverage. ParaLineSpacing holds a LineSpacing struct; pyuno compares
# structs by value, so an inherited line spacing equal to the style's value is
# still skipped by _copy_properties.
_COPIED_PARA_PROPERTIES = (
    "ParaLeftMargin", "ParaRightMargin", "ParaTopMargin", "ParaBottomMargin",
    "ParaFirstLineIndent", "ParaAdjust", "ParaBackColor", "ParaLineSpacing",
)



def _copy_properties(src: Any, dst: Any, names: tuple[str, ...], style: Any = None) -> None:
    """Copy *names* from one range to another, but only where they differ from *style*.

    Copying a value equal to the style's turns an inherited value into a hand-set one on the copy,
    and the read would then report it as a direct override — margin-right:0cm and text-align:left
    on every paragraph, drowning the one indent that was actually set. Detection is by VALUE for
    the same reason as apply_paragraph_style_preserving_direct_char: getPropertyState is not
    dependable at the text-portion level.

    Per-property rather than all-or-nothing: the temp document is a plain Writer doc and does not
    necessarily offer every property the source paragraph carries.
    """
    for name in names:
        try:
            value = src.getPropertyValue(name)
        except Exception:  # noqa: S112 - expected missing property in the per-portion _copy_properties loop
            continue
        if style is not None:
            try:
                if value == style.getPropertyValue(name):
                    continue
            except Exception:  # noqa: S110 - style property missing is expected; compare failure still copies the value
                pass
        try:
            dst.setPropertyValue(name, value)
        except Exception:  # noqa: S112 - dest may lack this property; one miss must not skip the rest or flood the portion loop
            continue


def source_style(model: Any, style_name: str, cache: dict[str, Any]) -> Any:
    """The source document's paragraph-style object for *style_name*, or None. Cached per read."""
    if style_name in cache:
        return cache[style_name]
    style = None
    if style_name:
        style = uno_read(lambda: model.getStyleFamilies().getByName("ParagraphStyles").getByName(style_name), None)
    cache[style_name] = style
    return style


def apply_temp_para_style(temp_doc: Any, temp_cursor: Any, style: str) -> bool:
    """Apply *style* on the scratch paragraph when that style exists there.

    Selection and range export copy each paragraph into a blank
    scratch document and set ``ParaStyleName`` with no check. A custom
    or foreign style is not in that document, so ``setPropertyValue``
    raises and the broad handler returns ``""``, discarding the whole
    selection. Skip the missing style (the scratch default stays) and
    keep copying. One style miss must not discard the range.
    Returns True if the style was applied to the scratch paragraph, False otherwise.
    """
    if not style:
        return False
    try:
        families = temp_doc.getStyleFamilies()
        para_styles = families.getByName("ParagraphStyles")
        if not para_styles.hasByName(style):
            log.debug(
                "range export: paragraph style %r is not in the scratch document; keeping the default",
                style,
            )
            return False
        temp_cursor.setPropertyValue("ParaStyleName", style)
        return True
    except Exception:
        log.debug(
            "range export: skipped paragraph style %r on the scratch document",
            style,
            exc_info=True,
        )
        return False


def paint_direct_formatting(
    para: Any,
    portions: list[tuple[Any, str]],
    temp_text: Any,
    trim_start: int,
    trim_end: int,
    style: Any = None,
) -> None:
    """Re-apply the source paragraph's direct formatting to the copy just written.

    setString copies plain text only; this second pass repaints direct Para*/Char* formatting
    over character offsets so block quotes and body text stay distinguishable. Offset painting
    avoids run-by-run insertion bookkeeping, and failures here leave plain text intact instead
    of corrupting the scratch copy.
    """
    def paragraph_start() -> tuple[Any, Any]:
        # The copy always appends, so the paragraph just written is the one at the document end.
        para_cursor = temp_text.createTextCursor()
        para_cursor.gotoEnd(False)
        para_cursor.gotoStartOfParagraph(False)
        return para_cursor, para_cursor.getStart()

    got = best_effort(paragraph_start, "paint_direct_formatting: paragraph start skipped")
    if got is None:
        return
    para_cursor, para_start = got

    def paint_paragraph() -> None:
        para_cursor.gotoEndOfParagraph(True)
        _copy_properties(para, para_cursor, _COPIED_PARA_PROPERTIES, style)

    # Para* refusal is logged and the Char* loop still runs so direct character formatting
    # is not dropped for the entire copied range.
    best_effort(paint_paragraph, "paint_direct_formatting: paragraph properties skipped")

    offset = 0  # position within the visible (tracked-deletions removed) paragraph text
    for portion, chunk in portions:
        chunk_start, chunk_end = offset, offset + len(chunk)
        offset = chunk_end
        lo, hi = max(chunk_start, trim_start), min(chunk_end, trim_end)
        if lo >= hi:
            continue  # portion lies outside the requested range
        try:
            run = temp_text.createTextCursorByRange(para_start)
            run.goRight(lo - trim_start, False)
            run.goRight(hi - lo, True)
            _copy_properties(portion, run, _COPIED_CHAR_PROPERTIES, style)
        except Exception:
            log.debug("paint_direct_formatting: portion skipped", exc_info=True)
            continue



def supports_service(obj: Any, name: str) -> bool:
    return uno_read(lambda: bool(obj.supportsService(name)), False)


def iter_xtext_paragraphs(text_obj: Any) -> Iterator[Any]:
    """Yield paragraphs under *text_obj*, descending into table cells."""
    enum = uno_read(lambda: text_obj.createEnumeration(), FAILED)
    if enum is FAILED:
        return
    for el in iter_elements(enum):
        if supports_service(el, "com.sun.star.text.TextTable"):
            try:
                names = el.getCellNames() or ()
            except Exception as exc:
                log.debug("iter_xtext_paragraphs: getCellNames failed: %s", exc)
                continue
            for name in names:
                try:
                    cell = el.getCellByName(name)
                except Exception as exc:
                    log.debug("iter_xtext_paragraphs: getCellByName failed: %s", exc)
                    continue
                yield from iter_xtext_paragraphs(cell)
            continue
        if hasattr(el, "createEnumeration"):
            yield el


def _goto_doc_end(doc: Any) -> None:
    best_effort(lambda: doc.getCurrentController().getViewCursor().gotoEnd(False), "_goto_doc_end: view cursor unavailable")


def _copy_field_into(dest_doc: Any, dest_text: Any, dest_cursor: Any, src_field: Any) -> bool:
    """Recreate *src_field* in *dest_doc* (used for table-cell fields)."""
    services = uno_read(lambda: [s for s in src_field.getSupportedServiceNames() if str(s).startswith("com.sun.star.text.textfield.")], FAILED)
    if services is FAILED or not services:
        return False
    svc = sorted(services, key=len)[-1]
    new_field = uno_read(lambda: dest_doc.createInstance(svc), FAILED)
    if new_field is FAILED:
        return False
    # Page-style clone copies header/footer fields through this helper
    # (test_copy_page_style_clones_next_page_number_and_roman). The old XHTML
    # path re-injected data-offset; this tuple omitted Offset, so a NEXT field
    # with Offset 1 was cloned as Offset 0. Properties the service does not
    # have still fail the try/except below and are skipped.
    for prop in ("NumberingType", "SubType", "Offset", "IsDate", "IsFixed", "Format"):
        try:
            new_field.setPropertyValue(prop, src_field.getPropertyValue(prop))
        except Exception as e:
            log.debug("_copy_field_into failed to set property %r: %s", prop, e)
    def insert() -> bool:
        dest_text.insertTextContent(dest_cursor, new_field, False)
        return True

    return best_effort(insert, "_copy_field_into failed", False)


def _copy_visible_portions(
    portions: Any,
    dest_doc: Any,
    dest_text: Any,
    cursor: Any,
    parent_el: Any,
    math_records: list[tuple[str, str, bool]] | None,
    deletion_state: list[bool] | None = None,
    style: Any = None,
) -> None:
    # Text a pending tracked change deleted is not copied, same as the other reads.
    for portion, kind in skip_tracked_deletions(iter_portions(portions), deletion_state):
        if kind == "TextField":
            field = _portion_field(portion)
            if field is None:
                continue
            _copy_field_into(dest_doc, dest_text, cursor, field)
        elif kind == "Text":
            chunk = uno_read(lambda: portion.getString(), "")
            if not chunk:
                continue
            copy_text_run(portion, dest_text, cursor, chunk, style)
        else:
            _copy_math_or_text(portion, parent_el, dest_text, cursor, math_records)


def _copy_cell_xtext(
    src_cell: Any,
    dest_doc: Any,
    dest_cell: Any,
    math_records: list[tuple[str, str, bool]] | None = None,
) -> None:
    """Copy one table cell: text + fields. Images in cells are not copied here."""
    # Fresh cell; setString cannot fail here.
    dest_cell.setString("")
    dest_cursor = dest_cell.createTextCursor()
    dest_cursor.gotoStart(False)
    try:
        para_enum = src_cell.createEnumeration()
    except Exception:
        dest_cell.setString(src_cell.getString())
        return
    first_para = True
    deletion_state = [False]
    for para in iter_elements(para_enum):
        if supports_service(para, "com.sun.star.text.TextTable"):
            # Recreate the nested table inside dest_cell (same walk as body _copy_table).
            dest_cursor = _append_table(para, dest_doc, dest_cell, math_records)
            first_para = False
            continue
        if not first_para:
            _begin_next_paragraph(dest_cell, dest_cursor)
        first_para = False
        try:
            portions = para.createEnumeration()
        except Exception:
            _fallback_insert_para_text(dest_cell, dest_cursor, para)
            continue
        _copy_visible_portions(portions, dest_doc, dest_cell, dest_cursor, para, math_records, deletion_state)


def writer_cell_position(name: str) -> tuple[int, int] | None:
    """Parse a Writer cell name the way ``SwXTextTable::GetCellPosition`` does.

    Writer letters are base 52 (A–Z, a–z, then AA…). Spreadsheet ``parse_a1``
    uppercases and treats AA as column 26; do not use it here. This parser is
    for the delete-guard / HTML-copy band only — not to rebuild a read matrix.

    LibreOffice: ``sw/source/core/unocore/unotbl.cxx`` ``GetCellPosition``.
    Row is ``o3tl::toInt32`` on the substring from the first digit — that
    parse stops at the first non-digit, so split-cell ``A1.1.1`` is row 0
    (same band as ``A1``). ``int()`` on the whole tail would raise.

    Lives here because LibrePy ships ``range_copy`` and not
    ``specialized.tables``.
    """
    if not name:
        return None
    n_len = len(name)
    n_row_pos = 0
    while n_row_pos < n_len:
        ch = name[n_row_pos]
        if "0" <= ch <= "9":
            break
        n_row_pos += 1
    if n_row_pos <= 0 or n_row_pos >= n_len:
        return None
    n_col_idx = 0
    for i in range(n_row_pos):
        n_col_idx *= 52
        if i < n_row_pos - 1:
            n_col_idx += 1
        c_char = name[i]
        if "A" <= c_char <= "Z":
            n_col_idx += ord(c_char) - ord("A")
        elif "a" <= c_char <= "z":
            n_col_idx += 26 + ord(c_char) - ord("a")
        else:
            return None
    n_digits_end = n_row_pos
    while n_digits_end < n_len and "0" <= name[n_digits_end] <= "9":
        n_digits_end += 1
    if n_digits_end == n_row_pos:
        return None
    n_row = int(name[n_row_pos:n_digits_end]) - 1
    if n_row < 0 or n_col_idx < 0:
        return None
    return n_col_idx, n_row


def _writer_table_copy_layout(table: Any) -> tuple[int, int, list[str]]:
    """Dest initialize size and named cells so HTML copy does not drop D2.

    Dest rows/cols are the max of ``getRows()``/``getColumns()`` and the Writer
    name coordinates. Split-cell suffixes (``A1.1.1``) map to the parent box
    and must not inflate dest size. The parser is not used to rebuild a
    read-path matrix.
    """
    rows, cols = uno_read(lambda: (int(table.getRows().getCount()), int(table.getColumns().getCount())), (0, 0))
    names: list[str] = uno_read(lambda: list(table.getCellNames() or []), [])
    max_row = max(rows - 1, 0)
    max_col = max(cols - 1, 0)
    for cell_name in names:
        pos = writer_cell_position(cell_name)
        if pos is None:
            continue
        col_idx, row_idx = pos
        if col_idx > max_col:
            max_col = col_idx
        if row_idx > max_row:
            max_row = row_idx
    return max_row + 1, max_col + 1, names


def _copy_table(
    src_table: Any,
    dest_doc: Any,
    dest_text: Any = None,
    math_records: list[tuple[str, str, bool]] | None = None,
) -> None:
    """Recreate *src_table* in *dest_text* (document body if omitted).

    *dest_text* is a cell when copying a nested TextTable; recursion through
    ``_copy_cell_xtext`` then copies inner cells (including further nests).

    Copy by ``getCellNames()`` so a merged banner (``getColumns()`` is the
    first-row box count and can be 1) still copies D2. Dest is sized to the
    max Writer name coordinate so those names exist; merges are not recreated.
    """
    # _writer_table_copy_layout already returns a size when a metric read fails.
    dest_rows, dest_cols, names = _writer_table_copy_layout(src_table)
    if dest_rows < 1 or dest_cols < 1:
        return
    dest_table = dest_doc.createInstance("com.sun.star.text.TextTable")
    dest_table.initialize(dest_rows, dest_cols)
    dest_xtext = dest_text if dest_text is not None else dest_doc.getText()
    dest_xtext.insertTextContent(dest_xtext.getEnd(), dest_table, False)
    if names:
        for cell_name in names:
            try:
                src_cell = src_table.getCellByName(cell_name)
                dest_cell = dest_table.getCellByName(cell_name)
            except Exception as exc:
                log.debug("_copy_table: cell %s unreadable: %s", cell_name, exc)
                continue
            _copy_cell_xtext(src_cell, dest_doc, dest_cell, math_records=math_records)
    else:
        # No name list: last-resort position walk (same first-row bound as before).
        for row in range(dest_rows):
            for col in range(dest_cols):
                try:
                    src_cell = src_table.getCellByPosition(col, row)
                    dest_cell = dest_table.getCellByPosition(col, row)
                except Exception as exc:
                    log.debug("_copy_table: cell (%s,%s) unreadable: %s", col, row, exc)
                    continue
                _copy_cell_xtext(src_cell, dest_doc, dest_cell, math_records=math_records)
    _goto_doc_end(dest_doc)


def _begin_next_paragraph(dest_text: Any, cursor: Any) -> None:
    """Step into the next paragraph when appending flow to *dest_text*.

    The cursor sits at the end of *dest_text*. insertControlCharacter leaves
    the cursor in front of the break, so without gotoNextParagraph the next run
    lands in the previous paragraph. A refused break is logged and the copy goes
    on: the text joins the previous paragraph rather than the whole copy failing.
    """
    try:
        dest_text.insertControlCharacter(cursor, PARAGRAPH_BREAK, False)
        cursor.gotoNextParagraph(False)
    except Exception as exc:
        log.debug("range copy: paragraph break failed: %s", exc)


def _append_table(
    src_table: Any,
    dest_doc: Any,
    dest_text: Any,
    math_records: list[tuple[str, str, bool]] | None = None,
) -> Any:
    """Copy *src_table* into *dest_text* and return a cursor at the new end.

    The old cursor sits before the table once inserted, so appending subsequent
    flow content needs a fresh cursor moved to the end of *dest_text*.
    """
    _copy_table(src_table, dest_doc, dest_text, math_records=math_records)
    cursor = dest_text.createTextCursor()
    cursor.gotoEnd(False)
    return cursor


def _copy_xtext_by_portions(
    src_text: Any,
    dest_doc: Any,
    math_records: list[tuple[str, str, bool]] | None = None,
    src_doc: Any = None,
) -> None:
    """Copy paragraphs/fields without the view transferable.

    Needed when ``select()`` on ``HeaderText`` pastes nothing because the
    view is on the first page (``FirstIsShared=False``).

    With *src_doc*, each copy gets its source paragraph style, and only values that
    differ from that style are copied. Without the style, the export would report
    ``Standard`` for a ``Header`` paragraph, and every inherited margin and font would
    come back as a hand-set ``data-lo-para`` override (same rule as range export, see
    ``_copy_properties``).
    """
    style_cache: dict[str, Any] = {}
    dest_text = dest_doc.getText()
    dest_text.setString("")
    dest_cursor = dest_text.createTextCursor()
    dest_cursor.gotoStart(False)
    try:
        enum = src_text.createEnumeration()
    except Exception:
        dest_text.setString(src_text.getString())
        return
    first_para = True
    deletion_state = [False]
    for el in iter_elements(enum):
        if supports_service(el, "com.sun.star.text.TextTable"):
            dest_cursor = _append_table(el, dest_doc, dest_text, math_records)
            first_para = False
            continue
        if not first_para:
            _begin_next_paragraph(dest_text, dest_cursor)
        first_para = False
        try:
            portions = el.createEnumeration()
        except Exception:
            _fallback_insert_para_text(dest_text, dest_cursor, el)
            continue
        style = None
        if src_doc is not None:
            style_name = uno_read(lambda: el.getPropertyValue("ParaStyleName"), "")
            if apply_temp_para_style(dest_doc, dest_cursor, style_name):
                style = source_style(src_doc, style_name, style_cache)
        _copy_visible_portions(
            portions, dest_doc, dest_text, dest_cursor, el, math_records, deletion_state=deletion_state, style=style
        )
        _copy_para_properties_to_end(dest_text, dest_cursor, el, style)


def copy_flow_into_xtext(doc: Any, src_text: Any, dest_text: Any) -> int:
    """Copy paragraphs, tables, fields, and direct char/para props inside *doc*.

    Used by page-style clone. Does not select the source, does not use the
    clipboard, and does not open a scratch Writer or run XHTML / flat ODF.
    Returns how many fields were inserted.

    The walk copies inside the live document without selecting, clipboard transfer,
    or in-process XHTML/ODF export — that export path could corrupt soffice when
    header/footer content includes titled AT_PARAGRAPH graphics. Fields and direct
    formatting are preserved by the enumeration instead.
    """
    cursor = dest_text.createTextCursor()
    cursor.gotoStart(False)
    cursor.gotoEnd(True)
    cursor.setString("")
    cursor.gotoStart(False)
    fields_copied = 0
    try:
        enum = src_text.createEnumeration()
    except Exception:
        try:
            dest_text.insertString(cursor, src_text.getString(), False)
        except Exception as exc:
            log.debug("copy_flow_into_xtext: plain-text fallback failed: %s", exc)
        return 0

    first_para = True
    for el in iter_elements(enum):
        if supports_service(el, "com.sun.star.text.TextTable"):
            cursor = _append_table(el, doc, dest_text, None)
            first_para = False
            continue
        if not first_para:
            _begin_next_paragraph(dest_text, cursor)
        first_para = False
        try:
            portions = el.createEnumeration()
        except Exception:
            _fallback_insert_para_text(dest_text, cursor, el)
            _copy_para_properties_to_end(dest_text, cursor, el)
            continue
        for portion, kind in iter_portions(portions):
            if kind == "TextField":
                field = _portion_field(portion)
                if field is None:
                    continue
                if _copy_field_into(doc, dest_text, cursor, field):
                    fields_copied += 1
                cursor.gotoEnd(False)
            elif kind == "Text":
                chunk = uno_read(lambda: portion.getString(), "")
                if not chunk:
                    continue
                copy_text_run(portion, dest_text, cursor, chunk)
            else:
                continue
        _copy_para_properties_to_end(dest_text, cursor, el)

    return fields_copied


# TextGraphicObject property list (shared with page._copy_region_graphics).
# The temp Writer does not offer every orient property; the per-property except
# is that miss, not a missing Graphic.
ANCHORED_GRAPHIC_PROPS = (
    "AnchorType", "Graphic", "Width", "Height",
    "HoriOrient", "HoriOrientPosition", "HoriOrientRelation",
    "VertOrient", "VertOrientPosition", "VertOrientRelation",
    "HyperLinkURL", "HyperLinkTarget", "Title", "Description",
)


def prefix_char_count(text_obj: Any, cursor: Any) -> int:
    """``getString()`` length from the start of *text_obj* to *cursor*.

    Captured before import so we can apply ruby only to the inserted suffix.
    A live ``cursor.getStart()`` drifts when ``insertDocumentFromURL`` moves it.
    """
    measure = text_obj.createTextCursor()
    measure.gotoStart(False)
    measure.gotoRange(cursor.getStart(), True)
    return len(measure.getString() or "")


def _copy_anchored_graphics(src_doc: Any, src_text: Any, dest_doc: Any) -> None:
    """Clone TextGraphicObjects anchored in *src_text* onto *dest_doc*.

    The portion walk does not carry Graphic. controller.select + transferable
    into the scratch Writer is the pattern copy_flow_into_xtext removed
    (heap corruption / SIGSEGV on the following XHTML export). Shapes are
    found the way page._scan_region_content finds them: draw page, uno_same
    on the anchor's XText.
    """
    def page_and_count() -> tuple[Any, int]:
        draw_page = src_doc.getDrawPage()
        return draw_page, int(draw_page.getCount())

    got = best_effort(page_and_count, "_copy_anchored_graphics: no draw page")
    if got is None:
        return
    draw_page, count = got
    dest_text = dest_doc.getText()
    for i in range(min(count, 5000)):
        try:
            shape = draw_page.getByIndex(i)
        except Exception:
            break
        try:
            anchor = shape.getAnchor()
            if not uno_same(anchor.getText(), src_text):
                continue
            # `is True`: MagicMock supportsService() is truthy.
            if shape.supportsService("com.sun.star.text.TextGraphicObject") is not True:
                continue
        except Exception as exc:
            log.debug("_copy_anchored_graphics: skip shape: %s", exc)
            continue
        def copy_one() -> None:
            clone = dest_doc.createInstance("com.sun.star.text.TextGraphicObject")
            for prop in ANCHORED_GRAPHIC_PROPS:
                try:
                    clone.setPropertyValue(prop, shape.getPropertyValue(prop))
                except Exception:
                    log.debug("_copy_anchored_graphics: skip %s", prop)
            cursor = dest_text.createTextCursor()
            cursor.gotoStart(False)
            # A bad anchor still inserts the logo at the start. Dropping
            # it here would hide the letterhead.
            offset = uno_read(lambda: prefix_char_count(src_text, anchor), 0)
            if offset:
                cursor.goRight(offset, False)
            dest_text.insertTextContent(cursor, clone, False)

        best_effort(copy_one, "_copy_anchored_graphics: skipped a graphic")


def copy_xtext_into_doc(
    src_doc: Any,
    src_text: Any,
    dest_doc: Any,
    math_records: list[tuple[str, str, bool]] | None = None,
) -> None:
    """Copy *src_text* (body, header, footer, cell) into *dest_doc*'s body.

    Portion copy moves header/footer/body text into the scratch Writer without
    selecting the user's document or running hidden XHTML export (see
    copy_flow_into_xtext). Anchored graphics are cloned after the text walk
    because an early graphic insert would be dropped by subsequent setString steps.
    """
    _copy_xtext_by_portions(src_text, dest_doc, math_records=math_records, src_doc=src_doc)
    _copy_anchored_graphics(src_doc, src_text, dest_doc)


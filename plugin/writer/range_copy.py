# Copyright (c) 2025-2026 Keith C. Ivey
# Distributed under the terms of the GNU General Public License v3 or later.
"""Range and flow copy helpers for LibreOffice Writer documents.

Clones paragraphs, tables, fields, and direct formatting into scratch documents
without clipboard transferables or XHTML filter roundtrips.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import Any, Iterator

from plugin.framework.errors import DocumentDisposedError, best_effort, reraise_if_disposed, uno_read
from plugin.doc.text_helpers import (
    FAILED,
    DeletionState,
    TrackedDeletions,
    get_string_without_tracked_deletions,
    go_right,
    iter_elements,
    visible_portions,
)
from plugin.writer.math.math_mml_export import (
    OBJECT_REPLACEMENT,
    portion_math_embed,
    read_starmath,
)
from .xhtml_style_postprocess import DELETED_MARK

log = logging.getLogger("writeragent.writer")


# com.sun.star.text.ControlCharacter.PARAGRAPH_BREAK
PARAGRAPH_BREAK = 0


def record_math_placeholder(records: list[tuple[str, str, bool]], emb: Any, *, display_block: bool) -> str:
    ph = chr(0xE000 + len(records))
    records.append((ph, read_starmath(emb), display_block))
    return ph


def _portion_field(portion: Any) -> Any:
    try:
        return portion.getPropertyValue("TextField")
    except Exception as exc:
        log.debug("TextField unreadable: %s", exc)
        return None


def copy_text_run(portion: Any, dest_text: Any, cursor: Any, chunk: str) -> None:
    start = cursor.getStart()
    dest_text.insertString(cursor, chunk, False)
    run = dest_text.createTextCursorByRange(start)
    run.gotoRange(cursor.getEnd(), True)
    # One call per range instead of one per property: a table read copies a run per cell.
    wanted = portion.getPropertyValues(_COPIED_CHAR_PROPERTIES)
    current = run.getPropertyValues(_COPIED_CHAR_PROPERTIES)
    for name, value, have in zip(_COPIED_CHAR_PROPERTIES, wanted, current):
        if value != have:
            best_effort(lambda: run.setPropertyValue(name, value), "copy_text_run: refused property")
    cursor.gotoRange(run.getEnd(), False)


def _copy_para_properties_to_end(dest_text: Any, cursor: Any, el: Any) -> None:
    para_cursor = dest_text.createTextCursor()
    para_cursor.gotoEnd(False)
    para_cursor.gotoStartOfParagraph(False)
    para_cursor.gotoEndOfParagraph(True)
    _copy_properties(el, para_cursor, _COPIED_PARA_PROPERTIES)
    cursor.gotoRange(para_cursor.getEnd(), False)


def _copy_math_or_text(
    portion: Any,
    parent_el: Any,
    dest_text: Any,
    dest_cursor: Any,
    math_records: list[Any] | None,
) -> str:
    if math_records is not None:
        emb = portion_math_embed(portion)
        if emb is not None:
            raw = parent_el.getString() if hasattr(parent_el, "getString") else ""
            stripped = raw.replace(OBJECT_REPLACEMENT, "").strip()
            ph = record_math_placeholder(math_records, emb, display_block=stripped == "")
            dest_text.insertString(dest_cursor, ph, False)
            return ph
    chunk = uno_read(lambda: portion.getString(), "")
    if chunk:
        dest_text.insertString(dest_cursor, chunk, False)
    return chunk



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
# UNO names behind the five paragraph-font and colour names that
# xhtml_style_postprocess._FODT_OVERRIDE_ATTRS reports in data-lo-para. Range and
# selection reads paint them on the scratch paragraph before any run exists so
# the export reports them on the paragraph like the full read does:
# sw/source/core/txtnode/thints.cxx SwTextNode::SetAttr stores character
# attributes on the paragraph when the range covers the whole paragraph text and
# there are no CharFormat hints. Other Char* stay per run because the full read
# does not report them on the paragraph.
_PARA_LEVEL_CHAR_PROPERTIES: tuple[str, ...] = (
    "CharFontName", "CharHeight", "CharWeight", "CharPosture", "CharColor",
)
_RUN_ONLY_CHAR_PROPERTIES: tuple[str, ...] = tuple(
    n for n in _COPIED_CHAR_PROPERTIES if n not in _PARA_LEVEL_CHAR_PROPERTIES
)



def _copy_properties(src: Any, dst: Any, names: tuple[str, ...], style: Any = None) -> None:
    """Copy *names* from one range to another, but only where they differ from *style*.

    *style* is what the copy already inherits: the paragraph style, or the source paragraph for
    paragraph-level character properties. Copying a value equal to what is inherited turns an
    inherited value into a hand-set one on the copy, and the read would then report it as a
    direct override — margin-right:0cm and text-align:left on every paragraph, drowning the one
    indent that was actually set. Detection is by VALUE for the same reason as
    apply_paragraph_style_preserving_direct_char: getPropertyState is not dependable at the
    text-portion level.

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


# Holds references to paragraph style proxies cloned into scratch documents.
# These proxies must stay alive until the scratch document is closed; otherwise,
# dropping the proxy during export triggers a race in LibreOffice's style lookup
# (see styles.keep_style_alive). The global deque is bounded (maxlen=256), so placing
# scratch clones there would evict live-document styles.
_scratch_style_holds: dict[int, list[Any]] = {}


def _hold_scratch_style(temp_doc: Any, clone: Any) -> None:
    _scratch_style_holds.setdefault(id(temp_doc), []).append(clone)


def release_scratch_style_holds(temp_doc: Any) -> None:
    """Release any style proxies held for *temp_doc* after it is closed."""
    _scratch_style_holds.pop(id(temp_doc), None)


def _clone_missing_paragraph_style(
    src_doc: Any,
    dest_doc: Any,
    dest_family: Any,
    name: str,
    _chain: tuple[str, ...] = (),
) -> bool:
    """Clone paragraph style *name* and missing ancestors from *src_doc* into *dest_doc*.

    Returns True when *name* already exists in *dest_family* or was successfully cloned and
    inserted. Returns False when the style or an ancestor could not be created, leaving the
    destination style family untouched for *name*.
    """
    try:
        # Boundary: LibrePy ships this module without styles.py, and then no style is cloned.
        from .styles import copy_direct_style_properties
    except ImportError:
        return False

    if not name or name in _chain or len(_chain) >= 16:
        return False

    try:
        if dest_family.hasByName(name):
            return True
    except Exception as exc:
        reraise_if_disposed(exc)
        return False

    if src_doc is None:
        return False

    try:
        src_family = src_doc.getStyleFamilies().getByName("ParagraphStyles")
        if not src_family.hasByName(name):
            return False
        source = src_family.getByName(name)
    except Exception as exc:
        reraise_if_disposed(exc)
        return False

    parent = ""
    try:
        raw_parent = source.getParentStyle()
        if isinstance(raw_parent, str) and raw_parent != name:
            parent = raw_parent
    except Exception as exc:
        reraise_if_disposed(exc)
        parent = ""

    if parent:
        try:
            has_parent = dest_family.hasByName(parent)
        except Exception as exc:
            reraise_if_disposed(exc)
            has_parent = False
        if not has_parent:
            if not _clone_missing_paragraph_style(src_doc, dest_doc, dest_family, parent, _chain + (name,)):
                return False

    try:
        clone = dest_doc.createInstance("com.sun.star.style.ParagraphStyle")
    except Exception as exc:
        reraise_if_disposed(exc)
        return False
    if clone is None:
        return False

    if parent:
        try:
            clone.setParentStyle(parent)
        except Exception as exc:
            reraise_if_disposed(exc)
            return False

    try:
        copy_direct_style_properties(source, clone)
    except Exception as exc:
        reraise_if_disposed(exc)
        return False

    try:
        dest_family.insertByName(name, clone)
    except Exception as exc:
        reraise_if_disposed(exc)
        return False

    _hold_scratch_style(dest_doc, clone)

    # Set FollowStyle only when that name is already in the destination family.
    # A missing follow style must not fail the copy because paragraph export only
    # requires the current style and its inherited ancestors.
    follow = ""
    try:
        raw_follow = source.getPropertyValue("FollowStyle")
        if isinstance(raw_follow, str):
            follow = raw_follow
    except Exception as exc:
        reraise_if_disposed(exc)
        follow = ""

    if follow:
        try:
            if dest_family.hasByName(follow):
                clone.setPropertyValue("FollowStyle", follow)
        except Exception as exc:
            reraise_if_disposed(exc)
            log.debug("failed to set FollowStyle %r on scratch paragraph style %r: %s", follow, name, exc)

    return True


def apply_temp_para_style(
    temp_doc: Any,
    temp_cursor: Any,
    style: str,
    src_doc: Any = None,
) -> bool:
    """Apply *style* on the scratch paragraph, cloning it from *src_doc* when missing.

    A paragraph style missing from the scratch document is copied from the source
    when that document can supply it. If the copy cannot be made, the scratch
    paragraph stays on its factory style so that one missing style does not abort
    the whole read. Returns True if the style was applied to the scratch paragraph,
    False otherwise.
    """
    if not style:
        return False
    try:
        families = temp_doc.getStyleFamilies()
        para_styles = families.getByName("ParagraphStyles")
        if not para_styles.hasByName(style):
            if not _clone_missing_paragraph_style(src_doc, temp_doc, para_styles, style):
                log.debug(
                    "range export: paragraph style %r could not be cloned into the scratch document; keeping the default",
                    style,
                )
                return False
        temp_cursor.setPropertyValue("ParaStyleName", style)
        return True
    except DocumentDisposedError:
        raise
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

    setString copies plain text only; this second pass repaints direct Para* and paragraph-level
    font/colour on the paragraph, then paints each run only where it differs from its paragraph
    (for those five names) or from *style* (for all other Char* properties). Offset painting
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

    def paint_paragraph() -> bool:
        para_cursor.gotoEndOfParagraph(True)
        _copy_properties(
            para, para_cursor, _COPIED_PARA_PROPERTIES + _PARA_LEVEL_CHAR_PROPERTIES, style
        )
        return True

    # Must run before any run hint exists, otherwise SwTextNode::SetAttr sees CharFormat hints
    # and stores paragraph-level Char* as run hints instead of paragraph attributes. Para*
    # refusal is logged and the Char* loop still runs against *style* so direct character
    # formatting is not dropped for the entire copied range.
    char_base = (
        para
        if best_effort(paint_paragraph, "paint_direct_formatting: paragraph properties skipped")
        else style
    )

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
            _copy_properties(portion, run, _RUN_ONLY_CHAR_PROPERTIES, style)
            _copy_properties(portion, run, _PARA_LEVEL_CHAR_PROPERTIES, char_base)
        except Exception:
            log.debug("paint_direct_formatting: portion skipped", exc_info=True)
            continue



def supports_service(obj: Any, name: str) -> bool:
    return uno_read(lambda: bool(obj.supportsService(name)), False)


def _copy_field_into(dest_doc: Any, dest_text: Any, dest_cursor: Any, src_field: Any) -> bool:
    """Recreate *src_field* in *dest_doc* (used for table-cell fields)."""
    services = uno_read(lambda: [s for s in src_field.getSupportedServiceNames() if str(s).startswith("com.sun.star.text.textfield.")], FAILED)
    if services is FAILED or not services:
        return False
    svc = sorted(services, key=len)[-1]
    new_field = uno_read(lambda: dest_doc.createInstance(svc), FAILED)
    if new_field is FAILED:
        return False
    # Page-style clones copy header/footer fields through this helper
    # (test_copy_page_style_clones_next_page_number_and_roman). Offset must be
    # in this tuple: without it a NEXT page-number field with Offset 1 is cloned
    # as Offset 0. Properties the service does not have fail the try/except below
    # and are skipped.
    props: tuple[str, ...] = ("NumberingType", "SubType", "Offset", "IsDate", "IsFixed", "Format")
    if svc == "com.sun.star.text.textfield.Input":
        # Input fields store displayed text in Content and prompt in Hint; other services
        # (such as TableFormula) use Content for formulas that are left alone.
        props += ("Content", "Hint")
    for prop in props:
        try:
            new_field.setPropertyValue(prop, src_field.getPropertyValue(prop))
        except Exception as e:
            log.debug("_copy_field_into failed to set property %r: %s", prop, e)
    def insert() -> bool:
        dest_text.insertTextContent(dest_cursor, new_field, False)
        return True

    return best_effort(insert, "_copy_field_into failed", False)


def clone_graphics(
    host: Any,
    dest_doc: Any,
    dest_text: Any,
    cursor: Any,
    deletions: TrackedDeletions | None = None,
) -> int:
    """Clone the graphics anchored at *host* (a paragraph or a Frame portion) at *cursor*.

    A paragraph lists its AT_PARAGRAPH graphics, a Frame portion its AT_CHARACTER and
    AS_CHARACTER ones (``SwXParagraph`` / ``SwXTextPortion::createContentEnumeration``).
    When *deletions* is given, graphics that Accept All would remove are skipped.
    Returns how many cursor steps the clones take: one per AS_CHARACTER graphic.
    """
    steps = 0
    contents = uno_read(lambda: host.createContentEnumeration("com.sun.star.text.TextContent"), None)
    for shape in iter_elements(contents):
        if not supports_service(shape, "com.sun.star.text.TextGraphicObject"):
            continue
        if deletions is not None and deletions.removes_anchored(shape):
            continue

        def clone_one() -> bool:
            clone = dest_doc.createInstance("com.sun.star.text.TextGraphicObject")
            for prop in ANCHORED_GRAPHIC_PROPS:
                try:
                    clone.setPropertyValue(prop, shape.getPropertyValue(prop))
                except Exception:
                    log.debug("clone_graphics: skip %s", prop)
            dest_text.insertTextContent(cursor, clone, False)
            # Keep the source name when unused in dest_doc so exported placeholders carry the
            # real name; a same-document clone (page-style copy) keeps its automatic name.
            name = shape.getName()
            if not dest_doc.getGraphicObjects().hasByName(name):
                best_effort(lambda: clone.setName(name), "clone_graphics: refused graphic name")
            return shape.getPropertyValue("AnchorType").value == "AS_CHARACTER"

        if best_effort(clone_one, "clone_graphics: skipped a graphic", False):
            steps += 1
    return steps


def clone_window_graphics(
    para: Any,
    portions: list[tuple[Any, str]],
    dest_doc: Any,
    dest_text: Any,
    trim_start: int,
    trim_end: int,
    deletions: TrackedDeletions,
) -> None:
    """Clone graphics anchored in *para* whose offset falls within ``[trim_start, trim_end]``.

    Graphics that Accept All would remove are skipped.
    """
    frames: list[tuple[int, Any]] = []
    offset = 0
    for portion, chunk in portions:
        if not chunk and uno_read(lambda: portion.getPropertyValue("TextPortionType"), "") == "Frame":
            if trim_start <= offset <= trim_end:
                frames.append((offset, portion))
        offset += len(chunk)
    # Clone last-to-first so inserting an AS_CHARACTER graphic does not shift earlier offsets.
    for frame_offset, portion in reversed(frames):
        cursor = dest_text.createTextCursor()
        cursor.gotoEnd(False)
        cursor.gotoStartOfParagraph(False)
        if frame_offset > trim_start:
            go_right(cursor, frame_offset - trim_start, False)
        clone_graphics(portion, dest_doc, dest_text, cursor, deletions)
    if trim_start == 0:
        cursor = dest_text.createTextCursor()
        cursor.gotoEnd(False)
        cursor.gotoStartOfParagraph(False)
        clone_graphics(para, dest_doc, dest_text, cursor, deletions)


def iter_nested_texts(doc: Any, text_obj: Any, state: DeletionState) -> Iterator[Any]:
    """Yield *text_obj* and recursively every cell XText inside its live tables.

    *state* is the tracked-deletion state at the start of *text_obj*; struck tables
    (``table_is_struck``) are skipped. Walks down from *text_obj* because a header/footer
    table's ``getAnchor().getText()`` is None.
    """
    yield text_obj
    enum = uno_read(lambda: text_obj.createEnumeration(), None)
    for el in iter_elements(enum):
        if not supports_service(el, "com.sun.star.text.TextTable"):
            for _unused in visible_portions(el, state=state):
                pass  # advances state past this paragraph's markers
            continue
        if table_is_struck(doc, el, state):
            continue
        cell_names: tuple[str, ...] = uno_read(lambda: el.getCellNames(), ())
        for cell_name in cell_names:
            cell = uno_read(lambda: el.getCellByName(cell_name), None)
            if cell is not None:
                yield from iter_nested_texts(doc, cell, DeletionState())


def _copy_visible_portions(
    para: Any,
    dest_doc: Any,
    dest_text: Any,
    cursor: Any,
    math_records: list[tuple[str, str, bool]] | None,
    *,
    in_cell: bool,
    state: DeletionState,
    deletions: TrackedDeletions,
) -> list[tuple[Any, str]]:
    """Copy visible portions of *para* at *cursor*.

    Tracked deletions are left out. Returns ``(portion, text)`` pairs where
    ``len(text)`` is the portion's cursor steps in the destination. Cells paint
    each run as they go.
    """
    copied: list[tuple[Any, str]] = []
    for portion, chunk in visible_portions(para, abort_on_portion_error=True, state=state, deletions=deletions):
        kind = portion.getPropertyValue("TextPortionType")
        if kind == "TextField":
            field = _portion_field(portion)
            if field is None:
                continue
            # A field is one cursor step in the destination text regardless of its rendered text.
            if _copy_field_into(dest_doc, dest_text, cursor, field):
                copied.append((portion, OBJECT_REPLACEMENT))
        elif kind == "Text":
            if in_cell:
                copy_text_run(portion, dest_text, cursor, chunk)
            else:
                dest_text.insertString(cursor, chunk, False)
            copied.append((portion, chunk))
        else:
            text = _copy_math_or_text(portion, para, dest_text, cursor, math_records)
            if not text and kind == "Frame":
                text = OBJECT_REPLACEMENT * clone_graphics(portion, dest_doc, dest_text, cursor, deletions)
            if text:
                copied.append((portion, text))
    return copied


def _copy_cell_xtext(
    src_doc: Any,
    src_cell: Any,
    dest_doc: Any,
    dest_cell: Any,
    deletions: TrackedDeletions,
    math_records: list[tuple[str, str, bool]] | None = None,
    *,
    mark_struck: bool = False,
) -> None:
    """Copy one table cell: text, fields, and graphics.

    Only the HTML export scratch copy sets *mark_struck*: page-style clones use
    this function to write into live documents, where the marker would stay as text.
    """
    # Fresh cell; setString cannot fail here.
    dest_cell.setString("")
    dest_cursor = dest_cell.createTextCursor()
    dest_cursor.gotoStart(False)
    try:
        para_enum = src_cell.createEnumeration()
    except Exception:
        dest_cell.setString(src_cell.getString())
        return
    state = DeletionState()
    fresh = True
    for para in iter_elements(para_enum):
        is_table = supports_service(para, "com.sun.star.text.TextTable")
        if is_table and table_is_struck(src_doc, para, state):
            continue
        if not fresh:
            begin_next_paragraph(dest_cell, dest_cursor)
        fresh = is_table
        if fresh:
            # Recreate the nested table inside dest_cell (same walk as body copy_table).
            copy_table(src_doc, para, dest_doc, dest_cell, dest_cursor, deletions, math_records, mark_struck=mark_struck)
            continue
        opened = state.in_delete
        _copy_visible_portions(
            para, dest_doc, dest_cell, dest_cursor, math_records, in_cell=True, state=state, deletions=deletions
        )
        dest_cursor.gotoEnd(False)
        if mark_struck and state.strikes_paragraph(para, opened):
            dest_cell.insertString(dest_cursor, DELETED_MARK, False)
        clone_graphics(para, dest_doc, dest_cell, dest_cursor, deletions)


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


def copy_table(
    src_doc: Any,
    src_table: Any,
    dest_doc: Any,
    dest_text: Any,
    dest_cursor: Any,
    deletions: TrackedDeletions,
    math_records: list[tuple[str, str, bool]] | None,
    *,
    mark_struck: bool = False,
) -> None:
    """Recreate *src_table* before *dest_cursor* in *dest_text*.

    *dest_cursor* must sit in an empty paragraph: Writer puts the table before
    it, and *dest_cursor* stays in that paragraph so the next paragraph fills it.
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
    dest_text.insertTextContent(dest_cursor, dest_table, False)
    # Table tools take the name the HTML carries; a clone inside the same document keeps its automatic name.
    name = src_table.getName()
    if not dest_doc.getTextTables().hasByName(name):
        best_effort(lambda: dest_table.setName(name), "copy_table: refused table name")
    if names:
        for cell_name in names:
            try:
                src_cell = src_table.getCellByName(cell_name)
                dest_cell = dest_table.getCellByName(cell_name)
            except Exception as exc:
                log.debug("copy_table: cell %s unreadable: %s", cell_name, exc)
                continue
            _copy_cell_xtext(
                src_doc, src_cell, dest_doc, dest_cell, deletions, math_records=math_records, mark_struck=mark_struck
            )
    else:
        # No name list: last-resort position walk (same first-row bound as before).
        for row in range(dest_rows):
            for col in range(dest_cols):
                try:
                    src_cell = src_table.getCellByPosition(col, row)
                    dest_cell = dest_table.getCellByPosition(col, row)
                except Exception as exc:
                    log.debug("copy_table: cell (%s,%s) unreadable: %s", col, row, exc)
                    continue
                _copy_cell_xtext(
                    src_doc, src_cell, dest_doc, dest_cell, deletions, math_records=math_records, mark_struck=mark_struck
                )


def begin_next_paragraph(dest_text: Any, cursor: Any) -> None:
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


@dataclass
class _Block:
    """One table or paragraph that Accept All leaves.

    A paragraph lists the source paragraphs a deletion joins, each with the deletion
    state at its start; a table has no *parts*. *element* gives the paragraph formatting.
    """

    element: Any
    parts: list[tuple[Any, DeletionState]]
    live: bool = True


def _accepted_blocks(doc: Any, text: Any, deletions: TrackedDeletions) -> list[_Block]:
    """Return the tables and joined/live paragraphs Accept All leaves in *text*."""
    state = deletions.state_at(text)
    blocks: list[_Block] = []
    struck = False
    for el in iter_elements(text.createEnumeration()):
        if supports_service(el, "com.sun.star.text.TextTable"):
            if not table_is_struck(doc, el, state):
                blocks.append(_Block(el, []))
            continue
        start = replace(state)
        state.saw_delete = False
        # Nothing is struck without deletions, so portions are only walked when some are pending.
        # List comprehension runs visible_portions to the end so state sees every redline marker.
        live = not deletions or any([True for _u in visible_portions(el, state=state, deletions=deletions)])
        if not (start.in_delete and blocks and blocks[-1].parts):
            blocks.append(_Block(el, [(el, start)], live))
            struck = start.in_delete or state.saw_delete
            continue
        # A paragraph that starts inside a deletion is joined to the one before it on Accept.
        block = blocks[-1]
        block.parts.append((el, start))
        struck = True
        if not block.live:
            # A wholly struck start keeps the later paragraph's formatting (sw_GetJoinFlags, docedt.cxx).
            block.element, block.live = el, live
    # Every recorded rewrite leaves a struck last paragraph. A copy of it would come back as live
    # text, so only a logo Accept keeps is carried, on the paragraph before it.
    if struck and len(blocks) > 1 and blocks[-1].parts and not blocks[-1].live and blocks[-2].parts:
        blocks[-2].parts.extend(blocks.pop().parts)
    return blocks


def _copy_xtext_by_portions(
    src_doc: Any,
    src_text: Any,
    dest_doc: Any,
    math_records: list[tuple[str, str, bool]] | None = None,
) -> None:
    """Copy paragraphs/fields without the view transferable, leaving out tracked deletions.

    Needed when ``select()`` on ``HeaderText`` pastes nothing because the
    view is on the first page (``FirstIsShared=False``).

    Each copy gets its source paragraph style, and only values that differ from
    that style are copied. The style object used for the direct-formatting
    comparison is the source style, after the scratch document has that style.
    Without the style, the export would report ``Standard`` for a ``Header`` paragraph,
    and every inherited margin and font would come back as a hand-set ``data-lo-para``
    override (same rule as range export, see ``_copy_properties``).

    Paragraphs joined across a deletion are merged as Accept All does, blank
    struck paragraphs stay as blanks unless they are the trailing struck
    paragraph left by a recorded rewrite, and any surviving logo anchored to a
    dropped last paragraph moves to the preceding paragraph.
    """
    style_cache: dict[str, Any] = {}
    dest_text = dest_doc.getText()
    dest_text.setString("")
    dest_cursor = dest_text.createTextCursor()
    dest_cursor.gotoStart(False)
    deletions = TrackedDeletions(src_doc)
    fresh = True
    for block in _accepted_blocks(src_doc, src_text, deletions):
        if not fresh:
            begin_next_paragraph(dest_text, dest_cursor)
        el = block.element
        fresh = not block.parts
        if fresh:
            copy_table(src_doc, el, dest_doc, dest_text, dest_cursor, deletions, math_records)
            continue
        style = None
        style_name = uno_read(lambda: el.getPropertyValue("ParaStyleName"), "")
        if apply_temp_para_style(dest_doc, dest_cursor, style_name, src_doc):
            style = source_style(src_doc, style_name, style_cache)
        # Insert unformatted, then paint: a run painted while it is the paragraph's only text
        # lands on the paragraph (SwTextNode::SetAttr), and text inserted after a painted run
        # takes its formatting (SwTextNode::Update).
        copied: list[tuple[Any, str]] = []
        for para, state in block.parts:
            copied += _copy_visible_portions(
                para, dest_doc, dest_text, dest_cursor, math_records, in_cell=False, state=state, deletions=deletions
            )
            dest_cursor.gotoEnd(False)
        paint_direct_formatting(el, copied, dest_text, 0, sum(len(c) for _p, c in copied), style)
        dest_cursor.gotoEnd(False)
        for para, _state in block.parts:
            clone_graphics(para, dest_doc, dest_text, dest_cursor, deletions)


def copy_flow_into_xtext(doc: Any, src_text: Any, dest_text: Any) -> int:
    """Copy paragraphs, tables, fields, graphics, and direct char/para props inside *doc*.

    Used by page-style clone. Does not select the source, does not use the
    clipboard, and does not open a scratch Writer or run XHTML / flat ODF.
    Tracked deletions (including wholly struck paragraphs and graphics that Accept All
    removes) are left out. Returns how many fields were inserted.

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
    deletions = TrackedDeletions(doc)
    fresh = True
    for block in _accepted_blocks(doc, src_text, deletions):
        if not fresh:
            begin_next_paragraph(dest_text, cursor)
        el = block.element
        fresh = not block.parts
        if fresh:
            # copy_table also clones cell graphics.
            copy_table(doc, el, doc, dest_text, cursor, deletions, None)
            continue
        for para, state in block.parts:
            for portion, chunk in visible_portions(para, abort_on_portion_error=True, state=state, deletions=deletions):
                kind = portion.getPropertyValue("TextPortionType")
                if kind == "TextField":
                    field = _portion_field(portion)
                    if field is None:
                        continue
                    if _copy_field_into(doc, dest_text, cursor, field):
                        fields_copied += 1
                    cursor.gotoEnd(False)
                elif kind == "Frame":
                    clone_graphics(portion, doc, dest_text, cursor, deletions)
                    cursor.gotoEnd(False)
                elif kind == "Text":
                    if not chunk:
                        continue
                    copy_text_run(portion, dest_text, cursor, chunk)
        _copy_para_properties_to_end(dest_text, cursor, el)
        for para, _state in block.parts:
            clone_graphics(para, doc, dest_text, cursor, deletions)

    return fields_copied


def cells_all_tracked_deleted(cells: list[Any], doc: Any) -> bool:
    """True when *cells* hold something struck and no cell has visible text left."""
    deletions = TrackedDeletions(doc)
    struck = False
    for cell in cells:
        state = deletions.state_at(cell)
        if uno_read(lambda: get_string_without_tracked_deletions(cell, state).strip("\n"), True):
            return False
        # A struck image has no raw text, only its Delete marker. Nested cell text is raw text here.
        if state.saw_delete or uno_read(cell.getString, ""):
            struck = True
    return struck


def table_is_all_deleted(doc: Any, table: Any) -> bool:
    """True when every cell of *table* has lost all visible text (with something struck in it)."""
    names: list[str] = uno_read(lambda: list(table.getCellNames() or ()), list[str]())
    if not names:
        return False
    return cells_all_tracked_deleted([table.getCellByName(n) for n in names], doc)


def table_is_struck(doc: Any, table: Any, state: DeletionState) -> bool:
    """True when accepting the pending tracked changes removes *table*.

    Either the deletion open at the table (*state*) spans it, or every row has
    ``HasTextChangesOnly`` false and ``table_is_all_deleted`` holds
    (``SwTableLine::UpdateTextChangesOnly``, sw/source/core/table/swtable.cxx). The text
    check is on the whole table rather than row by row, which is enough for Delete Table
    because ``SwDoc::SetRowNotTracked`` puts a struck dummy character in each empty row.
    A table with only its cell texts struck keeps ``HasTextChangesOnly`` true and survives Accept.
    """
    if state.in_delete:
        return True
    rows = table.getRows()
    if any(rows.getByIndex(i).getPropertyValue("HasTextChangesOnly") is not False for i in range(rows.getCount())):
        return False
    return table_is_all_deleted(doc, table)


GRAPHIC_LAYOUT_PROPS = (
    "HoriOrient", "HoriOrientPosition", "HoriOrientRelation",
    "VertOrient", "VertOrientPosition", "VertOrientRelation",
    "TextWrap", "SurroundContour", "SurroundAnchorOnly",
    "LeftMargin", "RightMargin", "TopMargin", "BottomMargin",
    "Title", "Description", "GraphicCrop", "Opaque",
)

# TextGraphicObject property list (shared with image_placeholders).
# The temp Writer does not offer every orient property; the per-property except
# is that miss, not a missing Graphic.
ANCHORED_GRAPHIC_PROPS = (
    "AnchorType", "Graphic", "Width", "Height",
    "HyperLinkURL", "HyperLinkName", "HyperLinkTarget",
) + GRAPHIC_LAYOUT_PROPS


def copy_xtext_into_doc(
    src_doc: Any,
    src_text: Any,
    dest_doc: Any,
    math_records: list[tuple[str, str, bool]] | None = None,
) -> None:
    """Copy *src_text* (body, header, footer, cell) into *dest_doc*'s body.

    Does not select the live document or use a transferable, because that pattern
    corrupts the heap on the XHTML export that follows.
    """
    _copy_xtext_by_portions(src_doc, src_text, dest_doc, math_records=math_records)


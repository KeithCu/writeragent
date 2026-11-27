# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""HTML/StarWriter import, replace, and markup routing for Writer documents.

Header/footer apply uses ``replace_xtext_with_html`` (same StarWriter
insert as the body path, pointed at a region ``XText``). It applies
data-lo-style and data-lo-para like a ``full_document`` write.

CJK ruby: StarWriter concatenates ``<rt>`` into the body. ``ruby_placeholders``
swaps each ``<ruby>`` for a ``[[WA-RUBY:…]]`` token before its base;
``_place_ruby`` deletes the token and sets ``RubyText`` on the base run.
Not a text field — do not use ``TextField.Ruby``.
"""

from __future__ import annotations

import contextlib
import logging
import re
import uuid
from typing import Any, Iterator, cast

import uno
from com.sun.star.text.ControlCharacter import PARAGRAPH_BREAK  # type: ignore

from plugin.doc.compound_undo import UndoStepOrRollback, UndoUnavailable, WriterCompoundUndo, commit_or_rollback
from plugin.doc.temp_files import with_temp_buffer
from plugin.doc.text_helpers import (
    CLEARABLE_PARA_PROPERTIES,
    clear_direct_numbering,
    go_right,
    iter_elements,
    iter_elements_limited,
    language_at_start,
    live_insert_range,
    normalize_linebreaks as _normalize,
    redline_bounds,
    reset_direct_char_formatting,
    reset_properties_to_default,
    set_paragraph_background,
    uniform_font,
)
from plugin.framework.constants import HTML_WRITER_FILTER
from plugin.framework.errors import ToolExecutionError, best_effort, reraise_if_disposed, uno_read
from plugin.framework.html_stripper import escape_prose_angle_brackets, html_to_search_text, text_has_real_html_tag
from plugin.framework.i18n import _
from plugin.framework.uno_context import create_property_value, supports_service, uno_any
from . import field_restore, html_import_styles, html_ruby, hyperlink_fixup, image_placeholders, record_changes
from .html_export import strip_html_boilerplate
from .math.html_math_segment import (
    MathSegment,
    html_fragment_contains_mixed_math,
    html_fragment_contains_tex_math,
    math_placeholders,
)
from .math.math_mml_convert import (
    PARA_CENTER,
    convert_latex_to_starmath,
    convert_mathml_to_starmath,
    insert_writer_math_formula,
)
from .table_nesting import raise_if_range_hosts_nested_table

log = logging.getLogger("writeragent.writer")


def _wrap_html_fragment(html_content: str, extra_css: str | None = None) -> str:
    """Wrap an HTML fragment in a full document structure for LO's filter."""
    if not html_content:
        return html_content
    low = html_content.lower()
    has_html = "<html" in low and "</html>" in low
    has_body = "<body" in low and "</body>" in low
    if has_html and has_body:
        return html_content
    head = '<meta charset="UTF-8">'
    if extra_css:
        head = '<meta charset="UTF-8">\n<style>%s</style>' % extra_css
    return '<!DOCTYPE html>\n<html>\n<head>\n%s\n</head>\n<body>\n%s\n</body>\n</html>' % (head, html_content)


def ensure_html_linebreaks(content: str) -> str:
    """Convert newlines to ``<br>``/``<p>`` when content is plain text
    and the active format is HTML, so LO's filter preserves them.

    Do not ``html.unescape`` here. Entry points normalize complete character
    references while keeping ``< > & " '`` escaped, and the StarWriter filter
    decodes entities itself so escaped markup stays literal text.
    """
    if not content:
        return content

    content = escape_prose_angle_brackets(_normalize(content))
    # Vision/Docling export full documents; nesting another wrapper breaks StarWriter import.
    if re.search(r"<!DOCTYPE\s+html|<html[\s>]", content, re.IGNORECASE):
        content = strip_html_boilerplate(content)
    # A block tag or <br> means the content has its own paragraphs; wrapping it in <p> is invalid.
    if html_import_styles.LINEBREAK_MARKUP_RE.search(content):
        return _wrap_html_fragment(content)

    paras = html_import_styles.plain_paragraphs(content)
    out = []
    for p in paras:
        p_html = p.replace("\n", "<br>\n")
        out.append("<p>%s</p>" % p_html)
    return _wrap_html_fragment("\n".join(out))


def html_to_plain_text(html_string: str) -> str:
    """Convert HTML to plain search text matching LibreOffice paragraph structure.

    Paragraphs, headings, list items, and table elements are separated by newlines;
    line breaks (<br>) emit a newline. Intra-block whitespace collapses to a single space.
    """
    if not html_string:
        return ""
    # Plain text without markup or entities needs no parsing.
    if "<" not in html_string and "&" not in html_string:
        return html_string.strip()

    stripped = html_ruby.strip_ruby(html_string)
    return html_to_search_text(stripped).strip()


def _parked_cursor(cursor: Any) -> Any:
    """A cursor parked at *cursor*'s end, in the same ``XText``.

    Callers must not move *cursor* to the end of the body instead: a mid-document
    insert would orphan later operations at the body end, and a cursor in a frame,
    cell, or header raises an empty RuntimeException on a range in another text.
    HTML import, ``insertString``, and ``insertTextContent`` all leave a cursor
    parked at the insert point AFTER what they insert (while *cursor* itself
    stays BEFORE it), so returning to the parked cursor continues right after
    the imported content in any text.
    """
    return cursor.getText().createTextCursorByRange(cursor.getEnd())


_LINK_TAG_RE = re.compile(r"<(/?)a(?=[\s/>])([^>]*)>", re.IGNORECASE)
_LINK_ATTR_RE = re.compile(r'\s+([\w:-]+)(\s*=\s*(?:"[^"]*"|\'[^\']*\'|[^\s>]+))?')


def _strip_link_anchor_attrs(open_tag: str) -> str:
    return _LINK_ATTR_RE.sub(
        lambda attr: "" if attr.group(1).lower() in ("id", "name") else attr.group(0),
        open_tag,
    )


def _split_nested_links(html: str) -> str:
    """End a link before each link nested in it and reopen it after.

    SwHTMLParser::NewAnchor (sw/source/filter/html/htmlgrin.cxx) ends the open link at the next
    <a>, so the text after a linked picture inside a text link would lose its link.
    """
    cuts: list[tuple[int, str]] = []
    stack: list[tuple[re.Match[str], list[tuple[int, str]]]] = []
    for m in _LINK_TAG_RE.finditer(html):
        if re.search(r'(?:^|[\s"\'])/$', m.group(2)):
            continue
        if not m.group(1):
            stack.append((m, []))
            continue
        if not stack:
            continue
        tag, inner = stack.pop()
        cuts.extend(inner)
        if stack and re.search(r"\shref\s*=", stack[-1][0].group(2), re.IGNORECASE):
            # NewAnchor -> HasStyleOptions / InsertAttrs (htmlgrin.cxx, htmlctxt.cxx) calls
            # InsertBookmark for id/name, so strip them on the reopened tag to avoid duplicate bookmarks.
            reopen = _strip_link_anchor_attrs(stack[-1][0].group(0))
            stack[-1][1].extend([(tag.start(), "</a>"), (m.end(), reopen)])
    if not cuts:
        return html
    cuts.sort(key=lambda item: item[0])
    out: list[str] = []
    pos = 0
    for idx, piece in cuts:
        out.append(html[pos:idx])
        out.append(piece)
        pos = idx
    out.append(html[pos:])
    return "".join(out)


def insert_html_fragment_at_cursor(
    cursor: Any,
    html_fragment: str,
    *,
    extra_css: str | None = None,
    wrap: bool = True,
    advance: bool = False,
) -> None:
    """Import a fragment via the StarWriter HTML filter at *cursor*.

    When *wrap* is True, wraps bare fragments in a full HTML document.
    *extra_css* is injected into ``<head>`` (e.g. sidebar list margins).
    When *advance* is True, moves *cursor* past the imported content.
    """
    html_fragment = _split_nested_links(escape_prose_angle_brackets(html_fragment))
    prepared = _wrap_html_fragment(html_fragment, extra_css=extra_css) if wrap else html_fragment
    tail = _parked_cursor(cursor) if advance else None
    with with_temp_buffer(prepared) as (_path, file_url):
        filter_name = HTML_WRITER_FILTER
        filter_props = (create_property_value("FilterName", filter_name),)
        try:
            cursor.insertDocumentFromURL(file_url, filter_props)
        except Exception as e:
            reraise_if_disposed(e)
            # Log insertDocumentFromURL failures with filter and URL so mid-script
            # apply paths surface the underlying UNO error.
            log.error(
                "insertDocumentFromURL failed: type=%s str=%r repr=%r filter=%r url=%r",
                type(e).__name__,
                str(e),
                repr(e),
                filter_name,
                file_url,
                exc_info=e,
            )
            # Raise inside the with block so contextlib._GeneratorContextManager.__exit__
            # does not assign __traceback__ on a PyUNO exception (which PyUNO rejects).
            # InsertFile (sw/source/core/unocore/unocrsrhelper.cxx) rejects content inside
            # content controls and input fields with "cannot insert file inside ...".
            if str(e).startswith("cannot insert file inside"):
                raise ToolExecutionError(
                    "LibreOffice cannot insert formatted content here (%s). Use plain text without markup "
                    "inside a content control or input field, or edit outside it." % e,
                    code="MARKUP_IN_CONTROL",
                ) from e
            raise ToolExecutionError(
                "LibreOffice could not import the content (%s)." % (str(e) or type(e).__name__),
                code="HTML_IMPORT_FAILED",
            ) from e
    if tail is not None:
        cursor.gotoRange(tail.getStart(), False)


def _math_attempt_entry(
    seg: MathSegment, snippet: str, *, ok: bool, error: str | None = None
) -> dict[str, Any]:
    """Build a math conversion attempt record for structured tool reporting.

    Kind is reported as "mathml" for XML segments and "tex" for TeX runs.
    Errors are attached only when the conversion failed and yielded a non-empty message.
    """
    kind = "mathml" if seg.kind == "math" else "tex"
    entry: dict[str, Any] = {
        "kind": kind,
        "display_block": seg.display_block,
        "snippet": snippet,
        "ok": ok,
    }
    if not ok and error:
        entry["error"] = error
    return entry


_MATH_TOKEN_RE = re.compile(r"\[\[WA-MATH:([a-zA-Z0-9]+):(\d+)\]\]")


def _place_formulas(
    model: Any,
    ctx: Any,
    text_obj: Any,
    formulas: list[MathSegment],
    nonce: str,
) -> list[dict[str, Any]]:
    """Replace ``[[WA-MATH:<nonce>:<i>]]`` placeholders in *text_obj* with Math formulas.

    Must run after style/ruby application and after ``restore_style`` (a style
    write resets a centred display-math paragraph).
    """
    if not formulas:
        return []
    prefix = f"[[WA-MATH:{nonce}:"
    hits = field_restore.find_tokens(text_obj, prefix, _MATH_TOKEN_RE)
    by_idx = {int(m.group(2)): cur for cur, m in hits}
    # Read before any split, because splitting for one display formula can leave
    # the next one alone in what was a heading.
    alone = {idx for idx, cur in by_idx.items() if _range_covers_whole_paragraphs(cur)}
    attempts: list[dict[str, Any]] = []
    for idx, seg in enumerate(formulas):
        cursor = by_idx.get(idx)
        if cursor is None:
            snippet = seg.text.replace("\n", " ")[:120]
            attempts.append(
                _math_attempt_entry(seg, snippet, ok=False, error=html_import_styles.MATH_POSITION_LOST)
            )
            continue
        if seg.kind == "tex":
            tex_text = html_import_styles.decode_complete_character_references(seg.text)
            snippet = tex_text.replace("\n", " ")[:120]
            res = convert_latex_to_starmath(ctx, tex_text, display_block=seg.display_block)
        else:
            snippet = seg.text.replace("\n", " ")[:120]
            res = convert_mathml_to_starmath(ctx, seg.text)
        cursor.setString("")
        if not (res.ok and res.starmath):
            fallback = "[Math import failed] " + snippet
            cursor.getText().insertString(cursor, fallback, False)
            log.debug("math import failed: %s snippet=%r", res.error_message, snippet)
            attempts.append(_math_attempt_entry(seg, snippet, ok=False, error=res.error_message))
            continue
        if seg.display_block:
            text = cursor.getText()
            if not cursor.isEndOfParagraph():
                text.insertControlCharacter(cursor, PARAGRAPH_BREAK, False)
                cursor.gotoPreviousParagraph(False)
                cursor.gotoEndOfParagraph(False)
            if not cursor.isStartOfParagraph():
                text.insertControlCharacter(cursor, PARAGRAPH_BREAK, False)
            # A split keeps the heading style on a formula at the heading's start (and everywhere in a
            # style that follows itself), which would add an empty-looking entry to the outline. A formula
            # that was the whole heading keeps the style its author chose.
            if idx not in alone and cursor.getPropertyValue("OutlineLevel") > 0:
                cursor.setPropertyValue("ParaStyleName", "Text body")
            cursor.setPropertyValue("ParaAdjust", PARA_CENTER)
        insert_writer_math_formula(model, cursor, res.starmath, display_block=False)
        attempts.append(_math_attempt_entry(seg, snippet, ok=True))
    return attempts


def _insert_start_anchor(text_obj: Any, cursor: Any) -> Any:
    """Cursor one character before the insert point, or ``None`` at the start of the text."""
    anchor = text_obj.createTextCursorByRange(cursor.getStart())
    return anchor if anchor.goLeft(1, False) else None


def _insert_start_from_anchor(text_obj: Any, anchor: Any, cursor: Any) -> Any:
    """Start of the imported content, from the anchor taken before the import."""
    try:
        if anchor is None:
            return text_obj.getStart()
        start = text_obj.createTextCursorByRange(anchor.getStart())
        start.goRight(1, False)
        return start
    except Exception as exc:
        reraise_if_disposed(exc)
        return cursor


def _place_ruby(text_obj: Any, spans: list[html_ruby.ImportRubySpan], nonce: str) -> None:
    """Paint ``RubyText`` on the base text after each ``[[WA-RUBY:<nonce>:<i>]]`` token."""
    if not spans:
        return
    prefix = html_ruby.ruby_token_prefix(nonce)
    hits = field_restore.find_tokens(text_obj, prefix, html_ruby.RUBY_TOKEN_RE)
    for cursor, m in hits:
        idx = int(m.group(2))
        if idx >= len(spans):
            continue
        span = spans[idx]
        cursor.setString("")
        if not go_right(cursor, len(span.base), True) or cursor.getString() != span.base:
            log.warning("_place_ruby: base %r did not match at token %d", span.base, idx)
            continue
        cursor.setPropertyValue("RubyText", span.reading)
        cursor.setPropertyValue("RubyIsAbove", span.is_above)
        # 0 = com.sun.star.text.RubyAdjust.LEFT
        cursor.setPropertyValue("RubyAdjust", 0)
    if len(hits) < len(spans):
        log.warning("_place_ruby: found %d of %d ruby tokens", len(hits), len(spans))


def _apply_ruby_and_restore_fields(
    model: Any, text_obj: Any, spans: list[html_ruby.ImportRubySpan], nonce: str
) -> None:
    _place_ruby(text_obj, spans, nonce)
    field_restore.restore_field_placeholders(model, text_obj, nonce=nonce)


def _table_count(model: Any) -> int | None:
    """Table count from model.getTextTables(). Disposal propagates."""
    # Invariant: uno_read raises DocumentDisposedError, so table verification never reports success on a closed doc.
    tables = uno_read(lambda: model.getTextTables(), None)
    if tables is None:
        return None
    return uno_read(lambda: tables.getCount(), None)


def raise_if_no_table_added(added: int | None) -> None:
    """Raise TABLE_INSERT_FAILED when an import that carried a <table> added none."""
    if added is not None and added <= 0:
        raise ToolExecutionError("Table insert produced no table in document.", code="TABLE_INSERT_FAILED")


def _sections_at(cursor: Any) -> list[Any]:
    """Return the TextSection chain at *cursor* (a cursor, paragraph or table), innermost first."""
    out: list[Any] = []
    section = uno_read(lambda: cursor.getPropertyValue("TextSection"), None)
    while section is not None:
        out.append(section)
        cur_sec: Any = section
        section = uno_read(lambda: cur_sec.getParentSection(), None)
    return out


def _in_index(element: Any) -> bool:
    """True when *element* (a paragraph or table) is inside an index such as a TOC."""
    # DocumentIndex climbs to the enclosing index (SwXTextSection, unosect.cxx
    # WID_SECT_DOCUMENT_INDEX), so the innermost section answers for the whole chain.
    sections = _sections_at(element)
    return bool(sections) and sections[0].getPropertyValue("DocumentIndex") is not None


def _in_header_footer(text: Any) -> bool:
    """Return True when *text* lives inside a Writer header or footer.

    Walks ``ParentText`` up through table cells and text frames
    (``SwXCell::getPropertyValue`` in ``sw/source/core/unocore/unotbl.cxx`` and
    ``SwXFrame::getPropertyValue`` in ``sw/source/core/unocore/unoframe.cxx``).
    A page-anchored frame has ``ParentText == None`` and counts as body.
    """
    while text is not None and text.getImplementationName() in ("SwXCell", "SwXTextFrame"):
        text = text.getPropertyValue("ParentText")
    return text is not None and text.getImplementationName() == "SwXHeadFootText"


def _iter_body_sections(model: Any) -> Iterator[tuple[str, Any]]:
    """Yield ``(name, section)`` for each text section outside headers and footers.

    A section in a table cell or text frame belongs to the text that holds the
    table or frame anchor (``ParentText`` via ``SwXCell::getPropertyValue`` in
    ``sw/source/core/unocore/unotbl.cxx`` and ``SwXFrame::getPropertyValue``
    in ``sw/source/core/unocore/unoframe.cxx``). A frame anchored to a page
    number has no parent text, so its sections count as body sections. Checking
    the implementation name rather than ``TextSection`` handles table-only body
    sections whose anchor text is ``SwXBodyText``.
    """
    sections = model.getTextSections()
    for name in sections.getElementNames():
        section = sections.getByName(name)
        if not _in_header_footer(section.getAnchor().getText()):
            yield name, section


def raise_if_hidden_section(model: Any) -> None:
    """Refuse full_document when the document has any hidden or conditional body section.

    A full read returns hidden section text, and importing that HTML back
    creates a visible section instead. Also ``SwXTextCursor::gotoStart``
    (``sw/source/core/unocore/unoobj.cxx``) skips a leading hidden section,
    leaving it in place while overwriting what follows. Also covers a section
    whose condition currently shows it (``IsVisible=False``), because the
    rewrite would drop the condition and LibreOffice aborts when such a
    section starts the body and is deleted (reproduces in pure UNO: paragraph before
    the section, select all, ``setString("")``).
    """
    hidden_names = [
        name for name, sec in _iter_body_sections(model)
        if not sec.getPropertyValue("IsVisible")
    ]
    if hidden_names:
        names = ", ".join(repr(n) for n in hidden_names)
        raise ToolExecutionError(
            f"Document has hidden or conditional sections ({names}) that target='full_document' would delete, reveal or skip over; "
            "nothing was changed. Edit the text outside them with target='search' (old_content, or anchors that do not span a hidden section). "
            "Only if the user wants them replaced, make them unconditionally visible first with section_set_visibility (visible=true).",
            code="HIDDEN_SECTION",
        )


def raise_if_protected_sections(model: Any) -> None:
    """Refuse full_document when it would delete a protected body section.

    Index sections (such as TOCs) are protected by default; refusing them would
    block ``full_document`` on every document with a TOC. A read-then-write
    brings the section back unprotected, and Writer refuses the same edit by
    hand (``SwPaM::HasReadonlySel``, ``sw/source/core/crsr/pam.cxx``).
    """
    names = [
        name for name, section in _iter_body_sections(model)
        if section.getPropertyValue("IsProtected") and section.getPropertyValue("DocumentIndex") is None
    ]
    if names:
        raise ToolExecutionError(
            "Document has protected section(s) %s and target='full_document' would replace them without their protection; "
            "nothing was changed. Edit the text outside them with target='search' (old_content, or anchors that do not span a protected section). "
            "Only if the user wants the protected text replaced, unprotect it first with section_set_protection."
            % ", ".join(repr(n) for n in names),
            code="PROTECTED_SECTION",
        )


def raise_if_pending_changes_untracked(model: Any) -> None:
    """Refuse full_document when Track Changes is off over pending body Insert/Delete redlines.

    Without recording, ``replace_full_document`` calls ``setString("")``, which removes every
    redline in the body (``DocumentContentOperationsManager::DeleteRangeImplImpl``), whoever
    made it. While recording, ``PreAppendDeleteRedline`` keeps another author's deletion and
    ``PreAppendForeignRedline`` stacks the new deletion on another author's insertion, so nothing
    is lost. Header and footer redlines are untouched by ``full_document`` and do not count.
    Checks the live ``RecordChanges`` state, so record/wait mode (enabled by
    ``EditReviewSession.__enter__``) passes.
    """
    if record_changes.is_recording_changes(model):
        return
    redlines = model.getRedlines()
    authors: list[str] = []
    seen: set[str] = set()
    for rl in iter_elements_limited(redlines.createEnumeration(), redlines.getCount()):
        if rl.getPropertyValue("RedlineType") not in ("Insert", "Delete"):
            continue
        text, _start, _end = redline_bounds(rl)
        if _in_header_footer(text):
            continue
        author = str(rl.getPropertyValue("RedlineAuthor") or "Unknown Author")
        if author not in seen:
            seen.add(author)
            authors.append(author)
    if authors:
        names = ", ".join(repr(a) for a in authors)
        raise ToolExecutionError(
            f"Document has pending tracked insertions or deletions (by {names}) and Track Changes is off, "
            "so target='full_document' would remove them without review; nothing was changed. "
            "Edit the text around them with target='search' (old_content, or start_anchor/end_anchor that do not span a tracked change). "
            "To replace the whole document, ask the user to turn on Track Changes or the record/wait review mode first, or to resolve those changes.",
            code="PENDING_TRACKED_CHANGES",
        )


def _insert_mixed_or_plain_html(
    model: Any,
    ctx: Any,
    cursor: Any,
    unescaped_content: str,
    *,
    apply_styles: bool = True,
) -> html_import_styles.WriteReport:
    """HTML import (optional MathML + TeX layer).

    data-lo-style and data-lo-para paragraph styling is applied via UNO after import when *apply_styles*
    is True (``full_document``, ``beginning``/``end`` after absorb prep, before/after block inserts,
    and whole-paragraph ``selection``/``search`` replacements). Partial matches pass False so
    pre-existing content is not restyled. We still strip attributes for a clean StarWriter import
    either way. Returns the WriteReport for this import.
    """
    nonce = uuid.uuid4().hex[:8]
    unescaped_content = field_restore.rewrite_exported_field_spans(unescaped_content, nonce=nonce)
    unescaped_content, formulas = math_placeholders(unescaped_content, nonce)
    # Strip data-lo-style and data-lo-para so the StarWriter import sees clean markup (it drops unknown
    # attributes anyway); we re-apply formatting via UNO afterwards only when apply_styles is True.
    clean, block_styles, block_paras, skipped_paras = html_import_styles.extract_block_lo_styles(unescaped_content)
    # Expand literal \n and \t before StarWriter import
    clean = html_import_styles.expand_escapes(clean)
    # Replace <ruby> with a placeholder + base so StarWriter does not concatenate reading; paint RubyText after.
    clean, ruby_spans = html_ruby.ruby_placeholders(clean, nonce)
    has_table = "<table" in clean.lower()
    tables_before = _table_count(model) if has_table else None
    styled = bool(apply_styles and (any(block_styles) or any(block_paras)))
    text_obj = cursor.getText()
    # Parked at the insert point, this ends up after the imported content (see _parked_cursor).
    imported_end = _parked_cursor(cursor) if styled else None
    # Tip: the live cursor (and any cursor parked at the insert point) ends up after the imported
    # content, so anchor one character to the left and step back right after the import.
    start_anchor = _insert_start_anchor(text_obj, cursor)

    single = ensure_html_linebreaks(clean)
    insert_html_fragment_at_cursor(cursor, single, wrap=False, advance=not styled)

    tables_after = _table_count(model) if tables_before is not None else None
    tables_added = (
        tables_after - tables_before
        if tables_before is not None and tables_after is not None
        else None
    )

    # Measure added_content before disposing the empty paragraph before a leading table:
    # afterwards a UNO cursor steps or skips over the table (SwUnoCursor::IsSelOvr,
    # SwXTextCursor::gotoStart) onto tail, making added_content read False.
    first = _insert_start_from_anchor(text_obj, start_anchor, cursor)
    tail = imported_end if imported_end is not None else cursor
    added_content = first is cursor or text_obj.compareRegionStarts(first, tail.getStart()) != 0

    if has_table and added_content:
        if imported_end is not None:
            # Move cursor off the leading empty paragraph before disposing it.
            cursor.gotoRange(imported_end.getStart(), False)
        with record_changes.record_changes(model, False):
            _drop_empty_paragraph_before_table(text_obj, first)

    if styled:
        # Per-paragraph failures are already logged inside apply_block_lo_styles.
        start = _insert_start_from_anchor(text_obj, start_anchor, cursor)
        failures = html_import_styles.apply_block_lo_styles(
            model, text_obj, start, block_styles, end=imported_end, paras=block_paras,
        )
        # Parked cursor lives in this range's XText (which may be a cell, frame,
        # header, or footer, not just the body text) and lands right after the
        # imported content.
        if imported_end is not None:
            cursor.gotoRange(imported_end.getStart(), False)
    else:
        # This target does not apply paragraph formatting, so every declaration is unapplied.
        failures = html_import_styles.declared_para_names(block_paras)
    # data-lo-para on inline tags and on blocks inside tables is stripped, never applied.
    failures.extend(html_import_styles.declared_para_names(skipped_paras))
    _apply_ruby_and_restore_fields(model, text_obj, ruby_spans, nonce)
    # Run after styles and ruby so a style write cannot reset a centred display-math paragraph.
    math_entries = _place_formulas(model, ctx, text_obj, formulas, nonce)
    return html_import_styles.WriteReport(
        unapplied_para=failures,
        style_tokens=list(dict.fromkeys(s for s in block_styles if s)),
        math=math_entries,
        added_content=added_content,
        tables_added=tables_added,
    )


def insert_html_at_cursor(
    model: Any,
    ctx: Any,
    cursor: Any,
    unescaped_content: str,
    *,
    apply_styles: bool = True,
) -> html_import_styles.WriteReport:
    """Insert HTML or plain text at *cursor* (public API for tools)."""
    with image_placeholders.keep_placeholder_images(model, unescaped_content) as content:
        return _insert_mixed_or_plain_html(model, ctx, cursor, content, apply_styles=apply_styles)


# LibreOffice SwXTextCursor::getString() returns "\n" on POSIX and "\r\n"
# on Windows (SAL_NEWLINE_STRING) for a single paragraph break.
_PARAGRAPH_BREAKS = ("\n", "\r\n")


def _delete_preceding_break(cursor: Any) -> bool:
    """Delete a paragraph break immediately before *cursor*; return True if one was deleted."""
    cursor.collapseToStart()
    if not cursor.goLeft(1, True):
        return False
    if cursor.getString() in _PARAGRAPH_BREAKS:
        cursor.setString("")
        return True
    cursor.collapseToEnd()
    return False


def _starts_new_page(cursor: Any) -> bool:
    """True when *cursor*'s paragraph carries a direct page or column break or a direct page style."""
    para = next(iter_elements(cursor.createEnumeration()), None)
    target = para if para is not None else cursor
    return (
        (
            target.getPropertyState("BreakType").value == "DIRECT_VALUE"
            and target.getPropertyValue("BreakType").value != "NONE"
        )
        or (
            target.getPropertyState("PageDescName").value == "DIRECT_VALUE"
            and bool(target.getPropertyValue("PageDescName"))
        )
    )


def _drop_leftover_paragraph(leftover: Any, *, merge_forward_after_table: bool) -> None:
    """Drop the empty paragraph *leftover* left at the import point.

    ``SwHTMLParser::CallParser`` (``sw/source/filter/html/swhtml.cxx``) puts every
    imported block before the paragraph at the insert point, so that paragraph is
    left over empty. Deleting the preceding break joins it onto the last block,
    which keeps that block's style.

    Right after a table, ``goLeft(1)`` spans the table (``SwUnoCursor::IsSelOvr`` in
    ``sw/source/core/unocore/unocrsr.cxx``), so no break is deleted. With
    *merge_forward_after_table*, the empty paragraph is merged into the paragraph
    after it unless *leftover* holds a bookmark (joining backward keeps bookmarks,
    whereas merging forward deletes the leftover paragraph and its bookmarks) or the
    following paragraph carries a direct page/column break or direct page style
    (which merging forward would drop). When merged, the following paragraph keeps
    its own style (``sw_GetJoinFlags`` in ``sw/source/core/doc/docedt.cxx``: a
    deletion starting at offset 0 keeps the end node). In a review-mode
    full-document replace, a recorded whole-paragraph replace
    (``_replace_whole_paragraphs``), or a recorded header/footer rewrite
    (``replace_xtext_with_html``), the paragraph after it is the first
    tracked-deleted paragraph; Accept empties it, so the result matches the
    untracked path. At the end of the text the paragraph stays because a Writer text
    cannot end with a table.

    Trailing content outside a block (``<table>...</table>tail``) takes over the
    paragraph at the insert point and leaves *leftover* at its end. That paragraph
    is not empty, so merging it forward would join the next paragraph onto it.
    """
    at_paragraph_start = leftover.isStartOfParagraph()
    leftover.gotoEndOfParagraph(True)
    if leftover.getString() != "":
        return
    # A trailing inline picture or formula stays in this paragraph and has no text.
    if any(
        paragraph_has_non_text_portion(para, ignore_markers=True, ignore_bookmarks=True)
        for para in iter_elements(leftover.createEnumeration())
    ):
        return
    # Join the leftover onto the last imported block by deleting that block's break.
    # While recording, the break belongs to our own tracked insertion, so deleting it
    # removes it rather than adding a Delete redline.
    if _delete_preceding_break(leftover) or not merge_forward_after_table or not at_paragraph_start:
        return
    # Joining backward keeps bookmarks, whereas merging forward deletes the
    # leftover paragraph and its bookmarks.
    if any(
        paragraph_has_non_text_portion(para, ignore_markers=True)
        for para in iter_elements(leftover.createEnumeration())
    ):
        return
    if leftover.goRight(1, True):
        following = leftover.getText().createTextCursorByRange(leftover.getEnd())
        if leftover.getString() in _PARAGRAPH_BREAKS and not _starts_new_page(following):
            leftover.setString("")
        else:
            leftover.collapseToStart()


def insert_html_after_paragraph(
    model: Any,
    ctx: Any,
    text_obj: Any,
    cursor: Any,
    content: str,
    *,
    apply_styles: bool = False,
) -> html_import_styles.WriteReport:
    """Import block *content* as new paragraphs immediately after *cursor*'s paragraph.

    In LibreOffice ``SwHTMLParser::CallParser`` (``sw/source/filter/html/swhtml.cxx``),
    importing into an existing paragraph calls ``SplitNode`` twice at the insert point
    and then ``pTextNode->JoinNext()`` on the first imported paragraph, which merges
    the first block onto the text before *cursor*. Importing at the start of an empty
    paragraph avoids that merge, so this moves *cursor* to the end of its paragraph,
    inserts a paragraph break to open an empty paragraph, imports at its start, and
    drops the trailing leftover paragraph (merging forward when *content* contains a
    table). Any empty paragraph StarWriter leaves before a leading table is removed by
    ``_insert_mixed_or_plain_html``.

    When *apply_styles* is True, block ``data-lo-style`` and ``data-lo-para`` are safe
    here because the import goes into a fresh empty paragraph and never merges.
    """
    cursor.gotoEndOfParagraph(False)
    text_obj.insertControlCharacter(cursor, PARAGRAPH_BREAK, False)
    # The import puts every block (each with its own break) BEFORE that paragraph,
    # which is then left over empty. This cursor stays at its start through the
    # import (checked live, also with a table next and at the end of the text).
    leftover = text_obj.createTextCursorByRange(cursor.getStart())
    report = insert_html_at_cursor(model, ctx, cursor, content, apply_styles=apply_styles)
    _drop_leftover_paragraph(leftover, merge_forward_after_table="<table" in content.lower())
    return report


def _drop_empty_paragraph_before_table(text_obj: Any, import_start: Any) -> None:
    """Delete the empty paragraph the StarWriter import leaves before a leading table.

    ``SwHTMLParser::CallParser`` (``sw/source/filter/html/swhtml.cxx``) opens with
    two ``SplitNode`` calls and ``JoinNext``/``JoinPrev`` the opening block onto
    the imported content; it cannot join across a ``SwTableNode``, so content
    that starts with a table leaves the split node empty directly before the
    table. A UNO text cursor cannot step into the table
    (``SwUnoCursor::IsSelOvr`` in ``sw/source/core/unocore/unocrsr.cxx``), so
    ``goRight(1, True)`` over the empty paragraph selects across the whole table
    and its enumeration lists the table as its own element:
    ``[empty paragraph, table]``. Dispose the empty paragraph only when it holds
    no non-text portion (image, field, bookmark). ``first_para.dispose()`` is
    best-effort: ``DelFullPara`` (``sw/source/core/doc/DocumentContentOperationsManager.cxx``)
    may refuse (e.g. on fieldmark overlap or a lone paragraph in a section) and
    ``SwXParagraph::dispose`` ignores the return value, leaving the paragraph.
    """
    para_cursor = text_obj.createTextCursorByRange(import_start.getStart())
    para_cursor.gotoStartOfParagraph(False)
    para_cursor.gotoEndOfParagraph(True)
    if para_cursor.getString() != "":
        return
    if not para_cursor.goRight(1, True):
        return
    enum = iter_elements(para_cursor.createEnumeration())
    first_para = next(enum, None)
    next_elem = next(enum, None)
    if first_para is None or next_elem is None:
        return
    if not supports_service(next_elem, "com.sun.star.text.TextTable"):
        return
    # Redline and SoftPageBreak portions carry no content. A bookmark does count here:
    # dispose() would delete it.
    if paragraph_has_non_text_portion(first_para, ignore_markers=True):
        return
    first_para.dispose()


def non_text_portion_type(
    para: Any,
    *,
    ignore_markers: bool = False,
    ignore_bookmarks: bool = False,
) -> str | None:
    """First non-text ``TextPortionType`` in *para*, ``"unreadable"`` on read failure, or ``None``.

    ``getString()`` is empty for images, fields, frames, and comments, so a paragraph
    holding only one of those would look empty and the import would merge its first block
    into it. When *ignore_markers* is True,
    ``Redline`` and ``SoftPageBreak`` markers are ignored; *ignore_bookmarks* also
    ignores ``Bookmark`` portions (``_drop_leftover_paragraph`` joins the paragraph,
    which keeps its bookmarks, whereas ``_drop_empty_paragraph_before_table`` disposes
    the paragraph and must keep user bookmarks). ``ReferenceMark`` and
    ``DocumentIndexMark`` always count as content.
    """
    ignored: tuple[str, ...] = ("Text",)
    if ignore_markers:
        ignored += ("Redline", "SoftPageBreak")
    if ignore_bookmarks:
        ignored += ("Bookmark",)
    try:
        portions = para.createEnumeration()
        for portion in iter_elements(portions):
            kind = portion.getPropertyValue("TextPortionType")
            if kind not in ignored:
                return str(kind)
    except Exception as exc:
        # Fail safe: an unreadable portion may be a frame, field, or image.
        reraise_if_disposed(exc)
        log.debug("Failed to inspect paragraph portions: %s", exc)
        return "unreadable"
    return None


def paragraph_has_non_text_portion(
    para: Any,
    *,
    ignore_markers: bool = False,
    ignore_bookmarks: bool = False,
) -> bool:
    """True when *para* holds an image, field, frame, or other non-text portion."""
    return non_text_portion_type(
        para,
        ignore_markers=ignore_markers,
        ignore_bookmarks=ignore_bookmarks,
    ) is not None


def _cursor_paragraph_has_content(cursor: Any) -> bool:
    """True if the paragraph containing *cursor* has any text (spaces included) or a non-text portion."""
    def has_content() -> bool:
        text_obj = cursor.getText()
        para = text_obj.createTextCursorByRange(cursor.getStart())
        para.gotoStartOfParagraph(False)
        para.gotoEndOfParagraph(True)
        # A cursor enumerates paragraphs; the portions are one level down.
        return para.getString() != "" or any(
            paragraph_has_non_text_portion(p, ignore_markers=True)
            for p in iter_elements(para.createEnumeration())
        )

    # Fail safe: treat as non-empty so end-path prep still runs.
    return uno_read(has_content, True)


def _open_absorb_paragraph_at_end(
    text: Any, cursor: Any, *, recording: bool
) -> None:
    """Place *cursor* in a paragraph safe for styled HTML import (``apply_styles=True``) at document end.

    StarWriter HTML import behavior (verified with headed/native UNO probes):

    - **Paragraph start** (``target=beginning`` after ``gotoStart``): the first block does **not**
      merge into existing text, so the beginning path needs no prep.
    - **Paragraph end** (``target=end``) when the absorb paragraph has content: the first
      imported ``<p>`` *merges* into that paragraph, so applying ``data-lo-style`` would restyle
      neighbor text. Insert ``PARAGRAPH_BREAK`` first; LibreOffice leaves the cursor in the new
      empty trailing paragraph, which the import then absorbs. The opened paragraph inherits the
      previous paragraph's direct font (weight, size, colour) on purpose, so appended text
      continues it. While recording a break is always opened, because absorbing an empty last
      paragraph would leave it in the import's style after Reject.
    - **Empty absorb paragraph** (blank doc / already-empty trailing, when not recording): merge
      is harmless — no prep. ``caret`` and ``beginning`` in an empty paragraph are handled by
      the caller in both modes.

    ``selection`` / ``search`` are not handled here: whole-paragraph block
    selections go through ``_replace_whole_paragraphs``, other non-collapsed
    selections are cleared by ``setString`` before import, a collapsed caret goes
    through ``_insert_at_caret``, and search replaces handle their own boundaries.
    """
    if not recording and not _cursor_paragraph_has_content(cursor):
        return
    # A failed break must not continue. The import would merge into the
    # neighbor paragraph and data-lo-style would restyle that text.
    text.insertControlCharacter(cursor, PARAGRAPH_BREAK, False)


def _prep_inline_html(html: str) -> tuple[str, list[html_ruby.ImportRubySpan], list[MathSegment], str]:
    nonce = uuid.uuid4().hex[:8]
    inline_html = field_restore.rewrite_exported_field_spans(html, nonce=nonce)
    inline_html, formulas = math_placeholders(inline_html, nonce)
    inline_html = html_import_styles.expand_escapes(inline_html)
    inline_html, ruby_spans = html_ruby.ruby_placeholders(inline_html, nonce)
    return inline_html, ruby_spans, formulas, nonce


def _edge_inline_ws(run: str) -> str:
    return "" if ("\n" in run or "\r" in run) else run


def _insert_raw_inline_html(model: Any, text: Any, cursor: Any, inline_html: str) -> None:
    """Import *inline_html* raw at *cursor*, keeping horizontal edge whitespace and leaving *cursor* at the end."""
    core = inline_html.strip()
    if not core:
        edge = _edge_inline_ws(inline_html)
        if edge:
            text.insertString(cursor, edge, False)
        return
    # The HTML import drops whitespace at the edges of a fragment, so "<b>muito</b> " would lose
    # its trailing space and fuse with the next word. Put horizontal edge whitespace in as text;
    # runs containing CR/LF are dropped as the HTML filter did, avoiding line breaks or splits.
    lead = _edge_inline_ws(inline_html[: len(inline_html) - len(inline_html.lstrip())])
    trail = _edge_inline_ws(inline_html[len(inline_html.rstrip()):])
    if lead:
        text.insertString(cursor, lead, False)
    insert_html_fragment_at_cursor(cursor, core, wrap=False, advance=True)
    if trail:
        anchor = _insert_start_anchor(text, cursor)
        text.insertString(cursor, trail, False)
        trail_cur = (
            text.createTextCursor()
            if anchor is None
            else text.createTextCursorByRange(anchor.getStart())
        )
        if anchor is None:
            trail_cur.gotoStart(False)
        else:
            trail_cur.goRight(1, False)
        if trail_cur.goRight(len(trail), True) and trail_cur.getString() == trail:
            # Clear direct char formatting inherited from the end of *core* (e.g. "<b>bold</b> ")
            # without creating a Format redline when RecordChanges is on.
            with record_changes.record_changes(model, False):
                reset_direct_char_formatting(trail_cur)
            cursor.gotoRange(trail_cur.getEnd(), False)


def insert_inline_at_cursor(model: Any, ctx: Any, cursor: Any, content: str) -> list[dict[str, Any]]:
    """Insert inline *content* at a collapsed *cursor* without splitting its paragraph.

    ``insert_html_at_cursor`` routes plain text through ``ensure_html_linebreaks``, which
    wraps it in ``<p>``: imported mid-paragraph, that ``<p>`` splits the host paragraph in
    two, so inserting "bem " before a word would leave "... bem" / "inteiro." as two paragraphs.
    Plain text goes in with ``insertString``; inline markup is imported RAW (no ``<p>``),
    the same way ``replace_single_range_with_content`` handles inline replacements.
    """
    with image_placeholders.keep_placeholder_images(model, content) as swapped:
        # insertString writes literal text without an HTML parser, so decode complete
        # character references here while leaving incomplete ones like "&sect 2o" untouched.
        if not content_needs_html_import(swapped):
            plain = html_import_styles.decode_complete_character_references(swapped)
            cursor.getText().insertString(cursor, plain, False)
            return []
        inline_html, ruby_spans, formulas, nonce = _prep_inline_html(swapped)
        text = cursor.getText()
        _insert_raw_inline_html(model, text, cursor, inline_html)
        _apply_ruby_and_restore_fields(model, text, ruby_spans, nonce)
        return _place_formulas(model, ctx, text, formulas, nonce)


def _range_covers_whole_paragraphs(text_range: Any) -> bool:
    """Return True if *text_range* starts at paragraph start and ends at paragraph end."""
    text = text_range.getText()
    start = text.createTextCursorByRange(text_range.getStart())
    end = text.createTextCursorByRange(text_range.getEnd())
    return start.isStartOfParagraph() and end.isEndOfParagraph()


def _within_one_paragraph(rng: Any) -> bool:
    """Return True when *rng* starts and ends in the same paragraph (a line break does not end one)."""
    text = rng.getText()
    start = text.createTextCursorByRange(rng.getStart())
    end = text.createTextCursorByRange(rng.getEnd())
    start.gotoStartOfParagraph(False)
    end.gotoStartOfParagraph(False)
    return text.compareRegionStarts(start, end) == 0


def _without_trailing_break(rng: Any) -> Any:
    """Trim a lone trailing ``\\n`` from a non-collapsed paragraph selection.

    Shift+Down or a drag past the line end leaves the selection ending at the
    next paragraph's start, whereas ``SwWrtShell::SelPara``
    (``sw/source/uibase/wrtsh/select.cxx``) stops at the paragraph end without
    the break. Without trimming that break, the range counts as partial and
    skips block styles.

    Directly after a table the crossed text is the whole table, and at the
    start of the text ``goLeft`` returns False. Neither is a lone ``"\\n"``, so
    *rng* is returned unchanged. The caller passes only a non-collapsed range.
    """
    text = rng.getText()
    start = text.createTextCursorByRange(rng.getStart())
    end = text.createTextCursorByRange(rng.getEnd())
    if not (start.isStartOfParagraph() and end.isStartOfParagraph()):
        return rng
    if not end.goLeft(1, True) or end.getString() not in _PARAGRAPH_BREAKS:
        return rng
    start.gotoRange(end.getStart(), True)
    return start


def _import_and_drop_leftover(
    model: Any,
    ctx: Any,
    cursor: Any,
    content: str,
    *,
    apply_styles: bool = True,
    merge_forward_after_table: bool = True,
) -> html_import_styles.WriteReport:
    """Import *content* into an emptied range and drop the leftover empty paragraph.

    ``SwHTMLParser::CallParser`` places every imported block before the emptied
    paragraph, leaving a blank paragraph behind unless dropped.
    """
    text_obj = cursor.getText()
    leftover = text_obj.createTextCursorByRange(cursor.getStart())
    report = _insert_mixed_or_plain_html(model, ctx, cursor, content, apply_styles=apply_styles)
    # The import added nothing (<p></p>): the emptied paragraph is what the content asked for.
    if report.added_content:
        # Untracked: the leftover break is part of the fresh absorb paragraph, and a tracked
        # delete of it joins paragraphs on Accept. When merge_forward_after_table is True and
        # content has a table, the helper merges the leftover forward into the first deleted
        # paragraph, which Accept then empties: the same end state as without recording.
        with record_changes.record_changes(model, False):
            _drop_leftover_paragraph(
                leftover,
                merge_forward_after_table=merge_forward_after_table and "<table" in content.lower(),
            )
    return report


def _direct_list_tag(cursor: Any) -> str | None:
    """``"ol"`` or ``"ul"`` when *cursor*'s paragraph is in a list set directly on it, else None."""
    para = next(iter_elements(cursor.createEnumeration()), None)
    if (
        para is None
        or not para.getPropertyValue("NumberingIsNumber")
        or para.getPropertyState("NumberingStyleName").value != "DIRECT_VALUE"
    ):
        return None
    return "ol" if para.getPropertyValue("ListLabelString") else "ul"


_SHARED_ITEM_GROUPS = (
    frozenset({"ParaTopMargin", "ParaBottomMargin", "ParaContextMargin"}),
    frozenset({"LeftBorder", "RightBorder", "TopBorder", "BottomBorder"}),
)


def _restore_host_para_props(
    model: Any,
    text_obj: Any,
    anchor: Any,
    cursor: Any,
    host_style: str,
    kept_para: dict[str, Any],
    content: str,
) -> None:
    """Restore direct paragraph properties cleared by a same-style ``data-lo-style`` write.

    Snapshots property states once before writing because LibreOffice shares items across
    related properties (``RES_UL_SPACE`` for top/bottom/context margins and ``RES_BOX``
    for all four borders). Explicit ``data-lo-para`` properties and direct properties
    already set by inline CSS (for properties not sharing an item with an explicit name)
    are skipped so caller-requested formatting wins.
    """
    if not kept_para:
        return
    start = _insert_start_from_anchor(text_obj, anchor, cursor)
    first_para = next(iter_elements(start.createEnumeration()), None)
    if first_para is None or first_para.getPropertyValue("ParaStyleName") != host_style:
        return
    _clean, _styles, block_paras, _skipped = html_import_styles.extract_block_lo_styles(content)
    explicit_uno = (
        {uno_prop for uno_prop, _unused in html_import_styles.parse_data_lo_para(block_paras[0])[0].values()}
        if block_paras and block_paras[0]
        else set()
    )
    shared_with_explicit = {
        prop for group in _SHARED_ITEM_GROUPS if group & explicit_uno for prop in group
    }
    states = {name: first_para.getPropertyState(name).value for name in kept_para}
    recording = record_changes.is_recording_changes(model)
    with record_changes.record_changes(model, False) if recording else contextlib.nullcontext():
        for name, value in kept_para.items():
            if name in explicit_uno:
                continue
            if name == "ParaBackColor":
                if first_para.getPropertyValue("FillStyle").value != "NONE":
                    continue
                set_paragraph_background(first_para, value)
                continue
            if name not in shared_with_explicit and states[name] == "DIRECT_VALUE":
                continue
            if name == "ParaTabStops":
                uno.invoke(first_para, "setPropertyValue", cast("Any", (name, uno_any("[]com.sun.star.style.TabStop", value))))
            else:
                first_para.setPropertyValue(name, value)


def _direct_para_props(cursor: Any) -> dict[str, Any]:
    """Direct paragraph properties on *cursor*'s paragraph that a style write would reset."""
    para = next(iter_elements(cursor.createEnumeration()), None)
    if para is None:
        return {}
    kept = {
        name: para.getPropertyValue(name)
        for name in _ABSORB_PARA_PROPERTIES
        if para.getPropertyState(name).value == "DIRECT_VALUE"
    }
    # Solid fill lives in XATTR_FILL* (see set_paragraph_background); store it once under ParaBackColor.
    if para.getPropertyValue("FillStyle").value == "SOLID":
        kept["ParaBackColor"] = para.getPropertyValue("FillColor")
    return kept


def _replace_whole_paragraphs(
    model: Any,
    ctx: Any,
    rng: Any,
    content: str,
    *,
    apply_styles: bool,
) -> html_import_styles.WriteReport:
    """Replace whole-paragraph *rng* with block *content*, recorded or not.

    ``SwHTMLParser::CallParser`` (``sw/source/filter/html/swhtml.cxx``) places
    every imported block before the target paragraph. While recording, we extend
    the tracked deletion across the following paragraph break so its mark is
    deleted with the text: Accept leaves nothing and keeps the following
    paragraph's style (``sw_GetJoinFlags`` in ``sw/source/core/doc/docedt.cxx``
    when the follower is non-empty, or ``lcl_AcceptRedline`` calling
    ``lcl_CopyStyle`` in ``sw/source/core/doc/DocumentRedlineManager.cxx`` when
    empty), while Reject keeps both styles because ``AppendRedline`` skips its
    tdf#119571 restyle when the deletion starts at offset 0. We then open a
    fresh paragraph before the struck text and reset the copied direct character
    formatting on it. Because a Word clause's numbering, indents, justification,
    and font are direct formatting, a reset would drop them; ``keep_layout``
    preserves them (and any uniform font family/size) when the first imported
    block is plain ``<p>``/text for that same paragraph, while headings, lists,
    and differently styled blocks reset layout so they do not inherit the old
    clause's formatting. If the first block explicitly names the host's own
    ``data-lo-style``, direct paragraph properties saved before the import are
    restored afterward for any property not set by ``data-lo-para``. The leftover
    synthetic paragraph is dropped untracked because that break is our own new
    insertion. Where no following break exists (document end, before a table,
    section edge), the old paragraph stays empty after Accept; the preceding
    break is not used because ``DocumentRedlineManager::AppendRedline``
    (``sw/source/core/doc/DocumentRedlineManager.cxx``) would restyle the
    struck paragraph.
    """
    text_obj = rng.getText()
    cursor = text_obj.createTextCursorByRange(rng.getStart())
    doomed = rng
    recording = record_changes.is_recording_changes(model)
    host_style = cursor.getPropertyValue("ParaStyleName")
    keep_layout = html_import_styles.first_block_keeps_layout(
        content, host_style, _direct_list_tag(cursor)
    )
    kept_char = language_at_start(rng)
    if keep_layout:
        kept_char |= uniform_font(text_obj.createTextCursorByRange(rng))
    if recording:
        following = text_obj.createTextCursorByRange(rng.getEnd())
        if following.goRight(1, True) and following.getString() in _PARAGRAPH_BREAKS:
            doomed = text_obj.createTextCursorByRange(rng.getStart())
            doomed.gotoRange(following.getEnd(), True)
    else:
        # Before the delete: on the emptied paragraph this reset cannot be undone, and
        # Undo would bring the old text back without its font, size and language.
        reset_direct_char_formatting(text_obj.createTextCursorByRange(rng), kept_char)
    with _deletion_author():
        doomed.setString("")
    if recording:
        text_obj.insertControlCharacter(cursor, PARAGRAPH_BREAK, False)
        cursor.goLeft(1, False)
        # Untracked: the fresh paragraph copies the struck paragraph's formatting, and a
        # recorded reset would add a Format redline. The struck paragraph keeps its own for Reject.
        with record_changes.record_changes(model, False):
            if not keep_layout:
                _drop_absorb_paragraph_props(cursor)
            reset_direct_char_formatting(cursor, kept_char)
    elif not keep_layout:
        _drop_absorb_paragraph_props(cursor)
    kept_para = _direct_para_props(cursor) if keep_layout and apply_styles else {}
    anchor = _insert_start_anchor(text_obj, cursor)
    report = _import_and_drop_leftover(model, ctx, cursor, content, apply_styles=apply_styles)
    _restore_host_para_props(model, text_obj, anchor, cursor, host_style, kept_para, content)
    return report


def _lo_style_for_replaced_range(
    content: str, text_range: Any
) -> tuple[bool, html_import_styles.StyleSkipCode | None]:
    """Determine whether to apply block data-lo-style or data-lo-para on a replaced range.

    Both attributes require the replaced range to cover whole paragraphs so neighbor text is
    not restyled. Returns ``"STYLE_IGNORED_PARTIAL_MATCH"`` when that check fails so callers
    can report specific warnings for data-lo-style and data-lo-para.
    """
    if not content or ("data-lo-style" not in content and "data-lo-para" not in content):
        return False, None
    _clean, styles, paras, _skipped = html_import_styles.extract_block_lo_styles(content)
    has_style = any(styles)
    if not has_style and not any(paras):
        return False, None
    if not _range_covers_whole_paragraphs(text_range):
        return False, "STYLE_IGNORED_PARTIAL_MATCH"
    return True, None


def _insert_inline(model: Any, ctx: Any, cursor: Any, content: str) -> html_import_styles.WriteReport:
    """Insert inline *content* at *cursor* without splitting its host paragraph.

    Inline inserts never apply ``data-lo-para`` properties, so any requested names
    are reported as unapplied on the returned ``WriteReport``.
    """
    math_entries = insert_inline_at_cursor(model, ctx, cursor, content)
    return html_import_styles.WriteReport(
        unapplied_para=html_import_styles.unapplied_para_names_for_content(content),
        math=math_entries,
    )


def _insert_at_caret(
    model: Any,
    ctx: Any,
    cursor: Any,
    content: str,
    apply_styles: bool,
) -> html_import_styles.WriteReport:
    """Insert *content* at a collapsed *cursor* without splitting its paragraph.

    - Empty paragraph (when not recording): only non-block content reaches this branch,
      where importing at the start of an empty paragraph neither merges nor splits.
    - Inline content (no block markup and no real newline): insert inline via
      ``insert_inline_at_cursor`` so the host paragraph is not split by ``<p>`` wrapping.
    - Block content at paragraph start: ``_insert_mixed_or_plain_html`` already places new
      blocks before the paragraph without merging.
    - Block content mid-paragraph or at paragraph end: move to after the paragraph via
      ``insert_html_after_paragraph``, marking ``snapped_to_paragraph`` when the caret
      was mid-paragraph.
    """
    if cursor.isStartOfParagraph() and cursor.isEndOfParagraph():
        return _insert_mixed_or_plain_html(model, ctx, cursor, content, apply_styles=apply_styles)
    if not content_is_block(content):
        return _insert_inline(model, ctx, cursor, content)
    if cursor.isStartOfParagraph():
        return _insert_mixed_or_plain_html(model, ctx, cursor, content, apply_styles=apply_styles)
    snapped = not cursor.isEndOfParagraph()
    report = insert_html_after_paragraph(
        model,
        ctx,
        cursor.getText(),
        cursor,
        content,
    )
    report.snapped_to_paragraph = snapped
    return report


def _should_insert_into_empty_paragraph(
    cursor: Any, content: str, *, recording: bool
) -> bool:
    """Return True when *cursor* sits in an empty paragraph that should open or absorb a line."""
    return (
        cursor.isStartOfParagraph()
        and cursor.isEndOfParagraph()
        and (recording or content_is_block(content))
    )


def _insert_into_empty_paragraph(
    model: Any,
    ctx: Any,
    cursor: Any,
    content: str,
    *,
    apply_styles: bool,
    recording: bool,
) -> html_import_styles.WriteReport:
    """Import *content* into an empty paragraph, opening a fresh paragraph when recording."""
    # Recording: the import would reuse the empty paragraph's node, and Reject's join keeps that
    # node with the imported style (sw_GetJoinFlags, docedt.cxx), so the content goes
    # into a fresh paragraph after it. After a trailing table the leftover stays: merged
    # into a non-empty follower, Reject would join the empty paragraph into that follower
    # and lose it.
    if recording:
        cursor.getText().insertControlCharacter(cursor, PARAGRAPH_BREAK, False)
    # Not recording: the content takes the empty line; the leftover would otherwise
    # stay behind in the follow style of the paragraph
    # (SwTextNode::MakeNewTextNode, ndtxt.cxx).
    return _import_and_drop_leftover(
        model,
        ctx,
        cursor,
        content,
        apply_styles=apply_styles,
        merge_forward_after_table=not recording,
    )


def _resolve_selection_range(model: Any, selection: Any | None) -> tuple[Any, Any, Any]:
    """Return ``(rng, rng_text, cursor_at_start)`` for *selection* or the live view cursor."""
    # Explicit selection (such as a pinned Send-time cursor) skips the live view read.
    if selection is not None:
        rng = selection
    else:
        controller = uno_read(lambda: model.getCurrentController(), None)
        rng = live_insert_range(controller)[0]
    # None: the view has no caret (Print Preview). SwXTextViewCursor::getText
    # (sw/source/uibase/uno/unotxvw.cxx) raises when an object is selected.
    rng_text = uno_read(lambda: rng.getText(), None) if rng is not None else None
    if rng is None or rng_text is None:
        raise ToolExecutionError(
            "The selection is not text: a floating image, shape, frame or object, several objects, "
            "or a block of cells is selected, or the view has no caret (Print Preview). Nothing was "
            "changed. Select text with set_selection, use target='search' with old_content "
            "(position='after' inserts next to it), or use table_set_cell for cells.",
            code="SELECTION_NOT_TEXT",
        )
    cursor = rng_text.createTextCursorByRange(rng.getStart())
    # SwXCell::createXTextCursorByRangeImpl (sw/source/core/unocore/unotbl.cxx)
    # returns null when an endpoint sits outside the cell's text; a null cursor
    # cannot be inserted at.
    if cursor is None or rng_text.createTextCursorByRange(rng.getEnd()) is None:
        raise ToolExecutionError(
            "The selection starts in a table and ends outside it; nothing was "
            "changed. Use target='full_document' if the whole document is "
            "selected, otherwise target='search' with old_content.",
            code="SELECTION_SPANS_TABLE",
        )
    return rng, rng_text, cursor


def _block_range_replace_flags(
    rng: Any, content: str
) -> tuple[bool, bool, html_import_styles.StyleSkipCode | None]:
    """Return ``(apply_styles, whole_paragraphs, note)`` for replacing *rng* with block *content*.

    Read these before *rng* is cleared. When ``whole_paragraphs`` is False the caller empties
    *rng* itself, through the range and deletion author it uses for its own deletions.
    """
    apply_styles, note = _lo_style_for_replaced_range(content, rng)
    return apply_styles, _range_covers_whole_paragraphs(rng), note


def _import_block_range_replace(
    model: Any,
    ctx: Any,
    rng: Any,
    cursor: Any,
    content: str,
    *,
    apply_styles: bool,
    whole_paragraphs: bool,
    note: html_import_styles.StyleSkipCode | None,
) -> html_import_styles.WriteReport:
    """Import block/mixed *content* into *rng* after ``_block_range_replace_flags`` and the caller's clear."""
    if whole_paragraphs:
        report = _replace_whole_paragraphs(model, ctx, rng, content, apply_styles=apply_styles)
    else:
        report = _insert_mixed_or_plain_html(model, ctx, cursor, content, apply_styles=apply_styles)
    if note:
        report.style_skips.append(note)
    return report


def _insert_at_end(
    model: Any,
    ctx: Any,
    content: str,
    *,
    recording: bool,
) -> html_import_styles.WriteReport:
    """Insert *content* at the end of the document body."""
    text = model.getText()
    cursor = text.createTextCursor()
    cursor.gotoEnd(False)
    # Honor data-lo-style after absorb prep (see _open_absorb_paragraph_at_end);
    # _import_and_drop_leftover defaults to apply_styles=True.
    _open_absorb_paragraph_at_end(text, cursor, recording=recording)
    return _import_and_drop_leftover(model, ctx, cursor, content)


def _insert_at_beginning(
    model: Any,
    ctx: Any,
    content: str,
    *,
    recording: bool,
) -> html_import_styles.WriteReport:
    """Insert *content* at the beginning of the document body."""
    text = model.getText()
    cursor = text.createTextCursor()
    cursor.gotoStart(False)
    if _should_insert_into_empty_paragraph(cursor, content, recording=recording):
        return _insert_into_empty_paragraph(
            model, ctx, cursor, content, apply_styles=True, recording=recording
        )
    return _insert_mixed_or_plain_html(model, ctx, cursor, content, apply_styles=True)


def _insert_at_selection(
    model: Any,
    ctx: Any,
    content: str,
    selection: Any | None,
    *,
    recording: bool,
) -> html_import_styles.WriteReport:
    """Insert or replace *content* at the resolved text selection or caret."""
    rng, rng_text, cursor = _resolve_selection_range(model, selection)
    # A collapsed range is the user's caret.
    caret = rng_text.compareRegionStarts(rng.getStart(), rng.getEnd()) == 0
    raise_if_range_hosts_nested_table(rng)
    if caret:
        styled, note = _lo_style_for_replaced_range(content, rng)
        rng.setString("")
        if _should_insert_into_empty_paragraph(cursor, content, recording=recording):
            report = _insert_into_empty_paragraph(
                model, ctx, cursor, content, apply_styles=styled, recording=recording
            )
        else:
            report = _insert_at_caret(model, ctx, cursor, content, styled)
        if note:
            report.style_skips.append(note)
        return report

    # Trim a lone trailing break; otherwise a recorded Shift+Down selection
    # deletes the paragraph break and Accept joins the next paragraph.
    rng = _without_trailing_break(rng)
    # Inline content (plain text or inline tags, no newline)
    # replaces the selection in place, like typing over it. Otherwise
    # ensure_html_linebreaks wraps it in <p>, which splits the host
    # paragraph on import (SwHTMLParser::CallParser in
    # sw/source/filter/html/swhtml.cxx).
    if not content_is_block(content):
        styled, note = _lo_style_for_replaced_range(content, rng)
        if recording and _within_one_paragraph(rng):
            # Insert after the struck text so plain text inherits its character
            # formatting. Multi-paragraph ranges keep inserting at the start so
            # Accept keeps the first paragraph's style rather than the last's
            # (sw_GetJoinFlags in sw/source/core/doc/docedt.cxx).
            cursor = rng.getText().createTextCursorByRange(rng.getEnd())
        rng.setString("")
        report = _insert_inline(model, ctx, cursor, content)
        if note:
            report.style_skips.append(note)
        return report

    # Whole-paragraph block selections go through _replace_whole_paragraphs.
    apply_styles, whole_paragraphs, note = _block_range_replace_flags(rng, content)
    if not whole_paragraphs:
        rng.setString("")
    return _import_block_range_replace(
        model,
        ctx,
        rng,
        cursor,
        content,
        apply_styles=apply_styles,
        whole_paragraphs=whole_paragraphs,
        note=note,
    )


def insert_content_at_position(
    model: Any,
    ctx: Any,
    content: str,
    position: str,
    *,
    selection: Any | None = None,
) -> html_import_styles.WriteReport:
    """Insert formatted content at *position* (``'beginning'``, ``'end'``, or ``'selection'``).

    The whole insert runs as one undo step and is rolled back on failure, unless an outer
    undo group is open, which then owns the rollback.
    When ``position == 'selection'`` and the insert point is a collapsed caret (or a single
    selected as-character object, which counts as a caret just after it), inline
    content is inserted at the caret without splitting its paragraph, and block content
    is placed as new paragraphs after the caret's paragraph (or before it when at
    paragraph start), setting ``snapped_to_paragraph`` on the returned report if the
    caret was mid-paragraph. When recording, an insert at a caret in an empty paragraph
    or at ``'beginning'`` when the first paragraph is empty opens a fresh paragraph after
    the empty one so Reject restores its style and direct formatting; when not recording,
    block content at an empty paragraph replaces that paragraph instead of leaving an
    empty trailing line. When the selection is non-collapsed and *content* is inline
    (no block markup, no real newline), it replaces the selection in place, keeping the
    host paragraph and its style with Track Changes on or off. Pass *selection* to act
    on an explicit text range (such as a pinned Send-time selection) instead of reading
    the live view.

    Returns the WriteReport for this insert. ``style_skips`` gets the skip code only
    for ``'selection'`` when a ``data-lo-style`` attribute could not be applied due to a
    partial match.
    """
    with (
        UndoStepOrRollback(model, _("WriterAgent: Insert result")),
        image_placeholders.keep_placeholder_images(model, content) as swapped,
    ):
        prepared = html_import_styles.normalize_character_references(swapped)
        recording = record_changes.is_recording_changes(model)
        if position == "end":
            return _insert_at_end(model, ctx, prepared, recording=recording)
        if position == "beginning":
            return _insert_at_beginning(model, ctx, prepared, recording=recording)
        if position == "selection":
            return _insert_at_selection(model, ctx, prepared, selection, recording=recording)
        raise ToolExecutionError("Unknown position: %s" % position)


def _deletion_author() -> Any:
    """WriterAgent split-author coloring; no-op when ``review_authors`` is omitted (LibrePy)."""
    try:
        from .review_authors import deletion_author

        return deletion_author()
    except ImportError:
        return contextlib.nullcontext()


_ABSORB_PARA_PROPERTIES = CLEARABLE_PARA_PROPERTIES + (
    "ParaLineSpacing",
    "ParaContextMargin",
    "ParaTabStops",
    "LeftBorder",
    "RightBorder",
    "TopBorder",
    "BottomBorder",
)


def _drop_absorb_paragraph_props(cursor: Any) -> None:
    """Drop direct paragraph formatting and direct numbering from the absorb paragraph before HTML import.

    In LibreOffice's HTML parser (`SwHTMLParser::CallParser` and `SetTextCollAttrs`
    in `sw/source/filter/html/swhtml.cxx`), `GetParaAttr` captures direct paragraph
    attributes from `RES_PARATR_BEGIN` through `RES_PARATR_CONNECT_BORDER` (excluding
    `RES_PARATR_NUMRULE`), plus `RES_LR_SPACE` and `RES_UL_SPACE` (`sw/inc/hintids.hxx`).
    Because this range includes line spacing (`RES_PARATR_LINESPACING`) and tab stops
    (`RES_PARATR_TABSTOP`), those must be reset to default before `SetTextCollAttrs`
    stamps them onto every imported paragraph. Direct borders (`RES_BOX`) and
    `NumberingStyleName` (`RES_PARATR_NUMRULE`) sit outside that stamped range and
    otherwise remain on the reused first paragraph. Clearing direct paragraph
    properties ensures imported paragraphs take values from their own style; direct
    formatting from `data-lo-para` is applied later. Callers reset character
    formatting separately because untracked whole-paragraph replaces must do that
    before deleting the old text so Undo restores it. `CLEARABLE_PARA_PROPERTIES`
    remains the clear-direct subset for Ctrl+M / `clear_direct`.
    """
    reset_properties_to_default(cursor, _ABSORB_PARA_PROPERTIES)
    clear_direct_numbering(cursor)


def replace_full_document(
    model: Any,
    ctx: Any,
    content: str,
    *,
    kept_images: tuple[str, list[Any]] | None = None,
) -> html_import_styles.WriteReport:
    """Clear the document and insert *content*.

    Leading tables outside sections are out of reach of the body cursor
    (``SwXTextCursor::gotoStart`` steps past them), so ``_edit_full_document``
    deletes them first with ``delete_leading_tables``; tables inside a leading
    section go with the section.
    """
    with image_placeholders.keep_placeholder_images(model, content, pre_swapped=kept_images) as swapped:
        content = html_import_styles.normalize_character_references(swapped)

        text = model.getText()
        cursor = text.createTextCursor()
        recording = record_changes.is_recording_changes(model)
        cursor.gotoStart(False)
        # Read before the delete: without recording, setString("") leaves one paragraph with the
        # last old paragraph's character formatting. An index's own text is usually in the
        # document default language, so the first paragraph outside any index decides; a body
        # that is only an index has none, and its start is used.
        language = language_at_start(
            next(
                (
                    el for el in iter_elements(text.createEnumeration())
                    if not supports_service(el, "com.sun.star.text.TextTable") and not _in_index(el)
                ),
                cursor,
            )
        )
        # Leading tables outside sections are already deleted or struck (delete_leading_tables).
        # A table inside a section counts as the start: gotoStart would step past that section.
        first = next(
            (
                el for el in iter_elements(text.createEnumeration())
                if not supports_service(el, "com.sun.star.text.TextTable") or _sections_at(el)
            ),
            None,
        )
        leading = _sections_at(first)
        cursor.gotoEnd(False)
        trailing = _sections_at(cursor) if recording else []
        # The deletion has to start and end outside any section: a section is removed only when it is
        # enclosed whole, and SwRangeRedline::HasValidRange (sw/source/core/doc/docredln.cxx) rejects a
        # recorded deletion whose ends lie in different sections, so nothing or only part is struck.
        # Only a recorded deletion needs the trailing paragraph because an untracked delete joins
        # across section boundaries (SwContentNode::CanJoinNext, node.cxx). Untracked, because
        # LibreOffice cannot track a paragraph next to a section boundary; Reject keeps these
        # paragraphs. Created inline: a held SwXParagraph breaks later enumeration
        # (SwXParagraph::attachToText).
        with record_changes.record_changes(model, False):
            if leading and first is not None:
                page_style = first.getPropertyValue("PageDescName")
                page_number = first.getPropertyValue("PageNumberOffset")
                text.insertTextContentBefore(model.createInstance("com.sun.star.text.Paragraph"), leading[-1])
                if page_style:
                    cursor.gotoStart(False)
                    cursor.setPropertyValue("PageDescName", page_style)
                    cursor.setPropertyValue("PageNumberOffset", page_number)
            if trailing:
                text.insertTextContentAfter(model.createInstance("com.sun.star.text.Paragraph"), trailing[-1])
        cursor.gotoStart(False)
        one_paragraph = not text.createTextCursorByRange(cursor).gotoNextParagraph(False)
        cursor.gotoEnd(True)
        # A reset on an emptied paragraph cannot be undone exactly (SwUndoResetAttr::UndoImpl,
        # sw/source/core/undo/unattr.cxx, calls DontExpandFormat, which turns paragraph attributes
        # into text attributes via FormatToTextAttr), so Undo would bring a lone paragraph back
        # without its font, size and language. Undo restores several paragraphs with their own
        # formatting, and resetting a long document first doubles the delete time.
        reset_first = one_paragraph and not recording
        if reset_first:
            reset_direct_char_formatting(cursor, language)
        with _deletion_author():  # author the deletion distinctly (split by-author coloring)
            cursor.setString("")
        cursor.gotoStart(False)
        if recording:
            # In review mode the deleted text stays in place, and an import at
            # its start takes the character formatting of the first old
            # paragraph as direct formatting -- a document opening with an 18pt
            # bold heading comes back 18pt bold throughout (reproduced live;
            # the data-lo-style pass then keeps it as a "hand-set" override).
            # Import into a fresh empty paragraph opened before the deleted
            # text, then drop that paragraph if it is left over empty
            # (`_drop_leftover_paragraph`).
            text.insertControlCharacter(cursor, PARAGRAPH_BREAK, False)
            cursor.goLeft(1, False)
        # Untracked: the fresh paragraph is new, so a recorded reset would add a FORMAT redline; the
        # struck text below keeps its direct props for Reject. Character formatting left on the
        # paragraph would become direct formatting on every imported paragraph.
        with record_changes.record_changes(model, False):
            _drop_absorb_paragraph_props(cursor)
            if not reset_first:
                reset_direct_char_formatting(cursor, language)
        return _import_and_drop_leftover(model, ctx, cursor, content)


def replace_single_range_with_content(
    model: Any,
    text_range: Any,
    content: str,
    ctx: Any,
) -> html_import_styles.WriteReport:
    """Replace the given text range with rendered *content* (HTML path).

    Returns the WriteReport for this replacement.
    """
    with image_placeholders.keep_placeholder_images(model, content) as swapped:
        prepared = html_import_styles.normalize_character_references(swapped)
        text_obj = text_range.getText()

        # Detect a table-cell target up front (the cursor's TextTable property is set inside a cell).
        # Block/rich HTML import into a cell's nested XText can raise an empty RuntimeException; this
        # flag turns that opaque failure into a clear, actionable message below.
        in_table_cell = uno_read(lambda: text_obj.createTextCursorByRange(text_range.getStart()).getPropertyValue("TextTable") is not None, False)

        # Preserve the target paragraph style for INLINE replacements. The StarWriter
        # HTML import resets the paragraph to a default body style, silently demoting
        # headings (e.g. "Heading 3" -> "Text body"). Inline content (including inline
        # math unless content_is_block is True — one token import would wrap it in <p>
        # and split the host paragraph) is inserted without jumping to the document end
        # so we can reapply the original paragraph style across the inserted range.
        inline_preserve = not (
            content_has_block_markup(prepared)
            or (html_fragment_contains_mixed_math(prepared) and content_is_block(prepared))
        )
        saved_style = None
        if inline_preserve:
            saved_style = uno_read(lambda: text_obj.createTextCursorByRange(text_range.getStart()).getPropertyValue("ParaStyleName"), None)

        cursor = text_obj.createTextCursorByRange(text_range)

        # setString on a host cell wipes nested TextTables — same refuse as table_set_cell.
        raise_if_range_hosts_nested_table(text_range)

        if saved_style is not None:
            # Imported at call time: styles.py is WriterAgent-only (not in scripts/librepy_bundle_paths.py) and imports this module back through edit_review.
            from .styles import apply_paragraph_style_preserving_direct_char
            with _deletion_author():  # author the deletion distinctly (split by-author coloring)
                cursor.setString("")
            anchor = text_obj.createTextCursorByRange(cursor.getStart())
            # Insert the inline fragment RAW (do not route through ensure_html_linebreaks:
            # it does not recognise <span> as HTML and would wrap it in <p>, creating an
            # extra body paragraph). The import uses advance=True, so anchor (parked at
            # the insert point) can end up after it too; [anchor, cursor] still lies in
            # the host paragraph, and apply_paragraph_style_preserving_direct_char expands
            # to whole paragraphs.
            inline_html, ruby_spans, formulas, nonce = _prep_inline_html(prepared)
            _insert_raw_inline_html(model, text_obj, cursor, inline_html)
            _apply_ruby_and_restore_fields(model, text_obj, ruby_spans, nonce)
            # Re-apply the saved paragraph style (the HTML import can demote Heading -> body).
            # Skip it while Track Changes is recording: setString("") above leaves the old text in
            # place as a tracked DELETE, and re-applying a paragraph style across [anchor, cursor]
            # spans that struck text, converting its DELETE redline into a FORMAT redline -- so
            # accepting the change would keep BOTH the old and new text. The inline import does not
            # demote the style here, so skipping the restore keeps a clean Delete + Insert pair.
            if not record_changes.is_recording_changes(model):
                def restore_style() -> None:
                    restore = text_obj.createTextCursorByRange(anchor.getStart())
                    restore.gotoRange(cursor.getEnd(), True)
                    apply_paragraph_style_preserving_direct_char(model, restore, saved_style)

                best_effort(restore_style, "replace_single_range_with_content: could not restore ParaStyleName")
            # Run after restore_style so a style write cannot reset a centred display-math paragraph.
            math_entries = _place_formulas(model, ctx, text_obj, formulas, nonce)
            # Inline content has no blocks, so data-lo-para exists only on stripped inline tags.
            _clean, _styles, _paras, skipped = html_import_styles.extract_block_lo_styles(prepared)
            return html_import_styles.WriteReport(
                unapplied_para=html_import_styles.declared_para_names(skipped),
                math=math_entries,
            )

        # A whole-paragraph range has no leftover text in host paragraphs, so data-lo-style
        # and data-lo-para apply whether Track Changes is recording or not (the import sits in
        # its own paragraphs). A partial range leaves neighbor text that would be restyled by
        # the block style.
        if inline_preserve:
            apply_styles, whole_paragraphs, note = False, False, None
        else:
            apply_styles, whole_paragraphs, note = _block_range_replace_flags(text_range, prepared)
        if not whole_paragraphs:
            with _deletion_author():  # author the deletion distinctly (split by-author coloring)
                cursor.setString("")
        try:
            return _import_block_range_replace(
                model,
                ctx,
                text_range,
                cursor,
                prepared,
                apply_styles=apply_styles,
                whole_paragraphs=whole_paragraphs,
                note=note,
            )
        except Exception as e:
            reraise_if_disposed(e)
            if isinstance(e, ToolExecutionError) and e.code == "MARKUP_IN_CONTROL":
                raise
            # Block/rich HTML into a table cell can raise an empty RuntimeException from the StarWriter
            # HTML import (nested-XText cursor mapping). Surface a clear, actionable message instead of
            # the opaque error; the atomic wrapper rolls back the partial edit either way.
            if in_table_cell and content_has_block_markup(prepared):
                raise ToolExecutionError(
                    "Rich/block HTML can't be inserted inside a table cell yet (the LibreOffice HTML "
                    "import mishandles nested cell text). Use plain text or inline tags only inside "
                    "table cells, or place block/rich content outside the table.",
                    code="TABLE_BLOCK_IN_CELL",
                ) from e
            raise


def _clear_region(model: Any, text_obj: Any) -> tuple[Any, Any]:
    """Clear *text_obj* and return ``(import_cursor, leftover_cursor)``.

    When *text_obj* starts or ends with a table, a text cursor cannot span it, so
    ``SwXText::setString`` (``sw/source/core/unocore/unotext.cxx``) is used. It
    appends empty helper paragraphs at the very start and very end of the text
    whenever it holds any table or section, so a leading helper exists even when
    only the last element is a table. While recording, that leading helper is
    merged forward (never ``dispose()``d, because a later Undo aborts LibreOffice)
    and an empty fresh paragraph is opened for the import so direct properties and
    heading styles from the old paragraph do not stamp the imported blocks.
    """
    elements = list(iter_elements(text_obj.createEnumeration()))
    tables = [supports_service(el, "com.sun.star.text.TextTable") for el in elements]
    table_edge = bool(tables and (tables[0] or tables[-1]))
    recording = record_changes.is_recording_changes(model)
    if not recording:
        # Explicit table removal keeps the remaining paragraph's style, whereas
        # SwXText::setString alone resets it to Standard.
        for el, is_table in zip(elements, tables):
            if is_table:
                text_obj.removeTextContent(el)
    if not recording or table_edge:
        text_obj.setString("")
    else:
        # Cursor clear avoids SwXText::setString's helper paragraphs when text_obj both
        # starts and ends with a paragraph, so Reject All restores the structure without extras.
        doomed = text_obj.createTextCursor()
        doomed.gotoStart(False)
        doomed.gotoEnd(True)
        doomed.setString("")
    # Create after clearing: createTextCursor() on a table-only region raises "no text available".
    cursor = text_obj.createTextCursor()
    cursor.gotoStart(False)
    if not recording:
        _leave_heading_style(model, cursor)
        return cursor, None
    if table_edge:
        with record_changes.record_changes(model, False):
            _merge_empty_paragraph_forward(cursor)
        cursor.gotoStart(False)
    text_obj.insertControlCharacter(cursor, PARAGRAPH_BREAK, False)
    cursor.goLeft(1, False)
    with record_changes.record_changes(model, False):
        _drop_absorb_paragraph_props(cursor)
        reset_direct_char_formatting(cursor)
        _leave_heading_style(model, cursor)
    return cursor, text_obj.createTextCursorByRange(cursor.getStart())


def _merge_empty_paragraph_forward(cursor: Any) -> None:
    """Merge an empty paragraph at *cursor* into the following paragraph.

    Joining two empty paragraphs keeps the first one's style (``sw_GetJoinFlags``
    in ``sw/source/core/doc/docedt.cxx``), so copy the next paragraph's style first.
    """
    para = cursor.getText().createTextCursorByRange(cursor)
    para.gotoEndOfParagraph(True)
    if para.getString():
        return
    if cursor.goRight(1, True) and cursor.getString() in _PARAGRAPH_BREAKS:
        following = cursor.getText().createTextCursorByRange(cursor.getEnd())
        para.setPropertyValue("ParaStyleName", following.getPropertyValue("ParaStyleName"))
        cursor.setString("")


def _leave_heading_style(model: Any, cursor: Any) -> None:
    """Switch off any style whose follow style differs (such as headings or Title).

    ``SwHTMLParser::CallParser`` / ``SetTextCollAttrs`` (``sw/source/filter/html/swhtml.cxx``)
    stamps the insertion paragraph's effective attributes onto every imported block.
    """
    name = cursor.getPropertyValue("ParaStyleName")
    style = model.getStyleFamilies().getByName("ParagraphStyles").getByName(name)
    if style.getPropertyValue("FollowStyle") != name:
        cursor.setPropertyValue("ParaStyleName", "Standard")


def replace_xtext_with_html(
    text_obj: Any, html: str, *, model: Any
) -> html_import_styles.WriteReport:
    """Clear *text_obj* and import *html* via the shared StarWriter path.

    Field spans from ``document_to_content`` / ``xtext_to_content`` are
    restored as live fields after import. *model* is the owning document
    (needed to create fields, enter the undo context, and find placeholders).

    data-lo-style and data-lo-para are stripped before the import and applied
    afterwards, the same way as a ``full_document`` write. Stripping also matters
    for the import itself: a tag with a long data-lo-para runs past the tag-length
    limit in ``escape_prose_angle_brackets`` and would land as literal text.
    Returns the WriteReport for this import.

    The clear and import are protected by a document undo context: any
    failure rolls back to the previous state, preserving graphics and fields.
    """
    nonce = uuid.uuid4().hex[:8]
    html, region_images = image_placeholders.capture_region_images(model, text_obj, html)
    html = html_import_styles.normalize_character_references(html)
    html = html_import_styles.expand_prose_escapes(html)
    html, ruby_spans = html_ruby.ruby_placeholders(html, nonce)
    rewritten = field_restore.rewrite_exported_field_spans(html, nonce=nonce)
    clean, block_styles, block_paras, skipped_paras = html_import_styles.extract_block_lo_styles(rewritten)
    prepared = ensure_html_linebreaks(clean)
    # data-lo-para on inline tags and on blocks inside tables is stripped, never applied.
    report = html_import_styles.WriteReport(
        unapplied_para=html_import_styles.declared_para_names(skipped_paras),
        style_tokens=list(dict.fromkeys(s for s in block_styles if s)),
    )
    # Imported at call time: edit_review imports this module at its top, so a module-level
    # import is a cycle. LibrePy does not ship edit_review, and only page.py's header/footer
    # write reaches this function.
    from .edit_review import next_agent_edit_undo_title

    title = next_agent_edit_undo_title()

    def mutate() -> None:
        cursor, leftover = _clear_region(model, text_obj)
        has_table = "<table" in prepared.lower()
        tables_before = _table_count(model) if has_table else None
        insert_html_fragment_at_cursor(
            cursor, prepared, wrap=False,
        )
        if tables_before is not None:
            tables_after = _table_count(model)
            raise_if_no_table_added(
                tables_after - tables_before if tables_after is not None else None
            )
        if has_table:
            with record_changes.record_changes(model, False):
                _drop_empty_paragraph_before_table(text_obj, text_obj.getStart())
        if any(block_styles) or any(block_paras):
            # The whole region was replaced, so the walk starts at the text start and
            # needs no end bound: it stops after the last imported block.
            report.unapplied_para.extend(html_import_styles.apply_block_lo_styles(
                model, text_obj, text_obj.getStart(), block_styles, paras=block_paras,
            ))
        _apply_ruby_and_restore_fields(model, text_obj, ruby_spans, nonce)
        if leftover is not None:
            # merge_forward_after_table=True is right even without a table: empty content
            # merges the fresh paragraph into the struck text.
            with record_changes.record_changes(model, False):
                _drop_leftover_paragraph(leftover, merge_forward_after_table=True)

        # Issue #1424: HTML import can leave a trailing empty paragraph.
        # If there is more than one paragraph and the last one is entirely empty, remove it.
        try:
            enum = text_obj.createEnumeration()
            prev: Any = None
            last: Any = None
            count = 0
            for elem in iter_elements(enum):
                prev = last
                last = elem
                count += 1
            # A Writer text must not end with a table; keep the trailing paragraph when prev is one.
            if count > 1 and last is not None and not supports_service(prev, "com.sun.star.text.TextTable"):
                if not last.getString():
                    if not paragraph_has_non_text_portion(last):
                        # To actually remove the trailing break, select from the end of the previous paragraph
                        # to the end of the document, and set String to empty.
                        c = text_obj.createTextCursorByRange(prev)
                        c.gotoEndOfParagraph(False)
                        c.gotoRange(last.getEnd(), True)
                        c.setString("")
        except Exception as e:
            reraise_if_disposed(e)
            log.debug("Failed to inspect trailing paragraph: %s", e)
        # Restore last, after trailing-paragraph cleanup: the inline copy's Frame portion
        # is what keeps a logo-only last paragraph from being removed above.
        image_placeholders.restore_region_images(model, region_images)

    try:
        outcome = commit_or_rollback(model, title, mutate)
    except UndoUnavailable:
        raise ToolExecutionError("Could not enter undo context for header/footer import.", code="UNDO_UNAVAILABLE")

    if (failed := outcome.rollback_failure) is not None:
        raise ToolExecutionError(
            f"the header/footer was written and could not be rolled back: {failed.get('message')}",
            code="ROLLBACK_FAILED",
        )
    outcome.raise_mutate_error()
    return report


def content_has_markup(content: str | None) -> bool:
    """Return ``True`` if *content* appears to contain HTML or math markup."""
    if not content:
        return False

    if text_has_real_html_tag(content):
        return True
    return "$$" in content or r"\(" in content or r"\[" in content or "<math" in content.lower()


def content_needs_html_import(content: str | None) -> bool:
    """Return ``True`` if *content* needs the StarWriter HTML import path.

    Unlike :func:`content_has_markup`, this includes complete guarded single-dollar
    TeX math ($...$), which must take the import path to become an embedded Math
    object while plain-text guards (such as TOC entries) still accept it.
    """
    return content_has_markup(content) or (content is not None and html_fragment_contains_tex_math(content))


def plain_text_if_markup(text: str) -> str:
    """Return plain search text if text contains markup, else text unchanged."""
    if content_has_markup(text):
        return html_to_plain_text(text)
    return text


def content_has_block_markup(content: str | None) -> bool:
    """Return ``True`` if *content* contains block-level HTML (paragraph-defining)."""
    if not content:
        return False
    return bool(html_import_styles.BLOCK_MARKUP_RE.search(content))


def content_is_block(content: str | None) -> bool:
    """Return ``True`` if *content* should be placed as separate paragraphs.

    Content is block-level when it contains block HTML tags or a real newline in
    prose (newlines inside TeX math do not count, while literal ``\\n`` in math
    prose does because it becomes a paragraph). Only real newlines make plain
    content without math a block.
    """
    if not content:
        return False
    prose, formulas = math_placeholders(content, "")
    return content_has_block_markup(content) or "\n" in (
        html_import_styles.expand_escapes(prose) if formulas else content
    )


def _text_runs(target_range: Any, *, in_redlines: bool) -> list[Any]:
    """Non-empty text portions in *target_range*, or [] when it holds anything but text.

    A break between paragraphs is in no run, so a range over several paragraphs adds up to
    less than its string. When *in_redlines* is True, ``Redline`` boundary portions are
    skipped so recorded replaces across another author's change still keep formatting.
    When *in_redlines* is False (untracked), a range edge may carry a leading/trailing
    marker, but text inside a redline or a mid-range redline returns ``[]`` so untracked
    replaces do not alter or split tracked-change markup without recording.
    """
    runs: list[Any] = []
    inside = False
    try:
        for para in iter_elements(target_range.createEnumeration()):
            for portion in iter_elements(para.createEnumeration()):
                kind = portion.getPropertyValue("TextPortionType")
                if kind == "Redline":
                    inside = portion.getPropertyValue("IsStart")
                    if runs and not inside and not in_redlines:
                        return []
                    continue
                if kind not in ("Text", "SoftPageBreak"):
                    return []
                if portion.getString():
                    if inside and not in_redlines:
                        return []
                    runs.append(portion)
    except Exception as exc:
        reraise_if_disposed(exc)
        return []
    return runs


_ADVANCE_FAILED_MSG = "Could not replace that text in place (the selection did not advance)."


def _advance(cur: Any, n: int, expand: bool = False) -> None:
    if not go_right(cur, n, expand):
        raise ToolExecutionError(_ADVANCE_FAILED_MSG)


def _replace_run_text(text: Any, main_cursor: Any, old: str, new: str, *, inside_run: bool) -> None:
    """Replace *old* with *new* inside one run of equal formatting.

    *inside_run* means *main_cursor* is past that run's first character. New text goes in
    before the old text is deleted, at a point with run characters on both sides, so it
    takes all the run's formatting (``SwTextNode::InsertText`` in
    ``sw/source/core/txtnode/ndtxt.cxx`` extends attributes that end at the insert point
    unless DontExpand, and attributes that start there only at paragraph start). A lone
    break is replaced in one step, because the point after it may be the next paragraph.
    """
    doomed = text.createTextCursorByRange(main_cursor)
    _advance(doomed, len(old), True)  # fails before anything is written
    if not new or old == "\n":
        doomed.setString(new)
        main_cursor.gotoRange(doomed.getEnd(), False)
        return
    added = text.createTextCursorByRange(main_cursor)
    kept_first = 0 if inside_run else 1
    if kept_first:
        _advance(added, 1)
    text.insertString(added, new, True)  # absorb: added now spans the new text
    tail = text.createTextCursorByRange(added.getEnd())
    if len(old) > kept_first:
        _advance(tail, len(old) - kept_first, True)
        tail.setString("")
    if kept_first:
        head = text.createTextCursorByRange(main_cursor)
        head.gotoRange(added.getStart(), True)
        head.setString("")
    main_cursor.gotoRange(tail.getEnd(), False)


def _step_replace_characters(text: Any, main_cursor: Any, old_text: str, new_text: str) -> None:
    for old, new in zip(old_text, new_text):
        if new == old:
            _advance(main_cursor, 1)
        else:
            _replace_run_text(text, main_cursor, old, new, inside_run=False)


def _replace_recorded(
    model: Any, text: Any, target_range: Any, old_text: str, new_text: str, author_deletion: bool
) -> None:
    # Track Changes: run-by-run redlines scramble the edit, so insert the new text once after
    # the longest run (leaving the deletion in two parts when that run is not last).
    cursor = text.createTextCursorByRange(target_range)
    runs = _text_runs(target_range, in_redlines=True)
    lengths = [len(run.getString()) for run in runs]
    if new_text and runs and sum(lengths) == len(old_text):
        # Text inserted at a run's end takes that run's formatting (SwTextNode::InsertText, ndtxt.cxx).
        # The longest run stands for a mixed range.
        longest = lengths.index(max(lengths))
        split = sum(lengths[: longest + 1])
        # "" when the range is only partly linked or spans several links: the new text gets no link.
        url = cursor.getPropertyValue("HyperLinkURL") or ""
        props = hyperlink_fixup.read_link_props(runs[longest])
        added = text.createTextCursorByRange(target_range.getStart())
        _advance(added, split)
        text.insertString(added, new_text, True)  # insert first: a failure never leaves a bare deletion
        try:
            # A link does not extend over text inserted at its end, and does extend inside it.
            if (added.getPropertyValue("HyperLinkURL") or "") != url:
                with record_changes.record_changes(model, False):
                    hyperlink_fixup.write_link(added, url, props)
            tail = text.createTextCursorByRange(added.getEnd())
            if len(old_text) > split:
                _advance(tail, len(old_text) - split, True)
            head = text.createTextCursorByRange(target_range.getStart())  # target_range may have grown
            head.gotoRange(added.getStart(), True)
            with _deletion_author() if author_deletion else contextlib.nullcontext():
                if len(old_text) > split:
                    tail.setString("")
                head.setString("")
        except Exception:
            best_effort(lambda: added.setString(""), "replace_preserving_format: could not take back the insertion")
            raise
        return
    if not author_deletion:
        # Single atomic path. Taken when split-author coloring is OFF, OR when the caller has NOT
        # opened an undo context around this edit -- in which case a delete-then-insert could NOT be
        # rolled back if the insert failed. Use the SINGLE atomic setString (one UNO action: it
        # records the whole tracked replace -- Delete+Insert, or Delete-only when new_text is "" --
        # or changes nothing; never a partial deletion). Whether an undo manager merely EXISTS is
        # irrelevant -- only an actually-open rollback context makes the two-step safe.
        # Trade-off on this path: the deletion and insertion share one author (one color).
        cursor.setString(new_text)
        return
    # split_author AND in_undo_context: the caller GUARANTEES this runs inside an open undo context
    # that will roll back a failed delete+insert, so use the two-step that preserves split-author
    # deletion coloring. The restore on insert-failure is a best-effort extra net (the caller's
    # context rollback also cleans up).
    original = cursor.getString()
    with _deletion_author():  # author the deletion distinctly (split by-author coloring)
        cursor.setString("")
    if new_text:
        try:
            text.insertString(cursor, new_text, False)
        except Exception:
            try:
                text.insertString(cursor, original, False)
            except Exception:
                log.warning(
                    "replace_preserving_format: insert failed AND restore failed; range may be left partial",
                    exc_info=True,
                )
            raise


def _replace_untracked(text: Any, target_range: Any, old_text: str, new_text: str) -> None:
    old_len = len(old_text)
    new_len = len(new_text)

    if old_len == 0:
        cursor = text.createTextCursorByRange(target_range.getStart())
        text.insertString(cursor, new_text, False)
        return

    # Do not pump processEvents inside this edit. A queued tool or a keystroke
    # would run on the main thread, inside the caller's undo context.
    main_cursor = text.createTextCursorByRange(target_range.getStart())

    lengths = [len(run.getString()) for run in _text_runs(target_range, in_redlines=False)]
    if sum(lengths) == old_len:
        start = 0
        for i, plen in enumerate(lengths):
            stop = start + plen
            old = old_text[start:stop]
            # The last run takes all remaining new text, so a longer replacement keeps its formatting.
            new = new_text[start:stop] if i + 1 < len(lengths) else new_text[start:]
            start = stop
            if old == new:
                _advance(main_cursor, plen)
                continue
            limit = min(plen, len(new))
            p = 0
            while p < limit and old[p] == new[p]:
                p += 1
            s = 0
            while s < limit - p and old[plen - 1 - s] == new[len(new) - 1 - s]:
                s += 1
            if p + s == plen:  # pure insertion: rewrite one neighbour so the insert point is inside the run
                if p:
                    p -= 1
                else:
                    s -= 1
            if p:
                _advance(main_cursor, p)
            _replace_run_text(text, main_cursor, old[p : plen - s], new[p : len(new) - s], inside_run=p > 0)
            if s:
                _advance(main_cursor, s)
        return

    # Fields and markers are crossed per portion, and paragraph breaks are in no run, so a
    # range whose run lengths do not sum to old_len is stepped character by character.
    overlap = min(old_len, new_len)
    _step_replace_characters(text, main_cursor, old_text[:overlap], new_text[:overlap])

    # Handle length changes.
    if new_len > old_len:
        # Extra chars inherit formatting from the predecessor.
        text.insertString(main_cursor, new_text[old_len:], False)
    elif old_len > new_len:
        # Delete remaining original characters. go_right chunks and reports a short range,
        # so a stop must fail loudly instead of silently under-deleting.
        del_cursor = text.createTextCursorByRange(main_cursor)
        _advance(del_cursor, old_len - new_len, True)
        del_cursor.setString("")


def replace_preserving_format(
    model: Any,
    target_range: Any,
    new_text: str,
    in_undo_context: bool = False,
    split_author: bool = True,
) -> bool:
    """Replace text in *target_range* with *new_text* run by run, preserving formatting.

    Untracked new text takes the run's formatting (bold, italic, font, color, link, etc.).
    Recorded single-paragraph plain replaces insert after the longest run so the new text
    inherits its formatting, and carry the common link only when the whole range shares one URL.

    Cursors are created on ``target_range.getText()`` (the cell, frame, or body
    ``XText`` that owns the range), not ``model.getText()``. The range must lie
    entirely within that text object.

    Refuses a host cell that contains a nested TextTable (setString / whole-range
    replace would delete it — use table_set_cell).

    When recording tracked changes, single-paragraph plain-text ranges insert the new text
    after the longest run first, then delete the old text in one or two parts (regardless of
    ``in_undo_context``). For deletions, fields, and multi-paragraph ranges, *split_author*
    selects the rendering: ``True`` (default) authors the deletion and insertion separately so
    LibreOffice's by-author coloring shows removed vs new text in two distinct colors;
    ``False`` records the whole replace as a single atomic op authored once (one color).
    The split-author two-step is only safe inside an open undo context (``in_undo_context``)
    that can roll back a half-applied edit, so it is used only when BOTH ``split_author`` and
    ``in_undo_context`` hold; otherwise the atomic single-op path keeps those edits
    all-or-nothing.

    Returns False when *new_text* already equals the range text, so nothing was written.
    """
    # Use the range's OWN text object, not the document body. When target_range
    # lives inside a table cell, model.getText() (the body) is the wrong XText and
    # createTextCursorByRange() raises "End of content node doesn't have the proper
    # start node". target_range.getText() resolves to the cell (or body) correctly,
    # matching the markup path which already uses found.getText().
    raise_if_range_hosts_nested_table(target_range)
    text = target_range.getText()
    old_text = _normalize(target_range.getString())
    new_text = _normalize(new_text)

    if new_text == old_text:
        return False

    # One replace is several UNO actions; group them so one Ctrl+Z reverts it.
    with WriterCompoundUndo(model, _("WriterAgent: Replace text")):
        if record_changes.is_recording_changes(model):
            _replace_recorded(model, text, target_range, old_text, new_text, split_author and in_undo_context)
        else:
            _replace_untracked(text, target_range, old_text, new_text)
    return True


def run_writer_mutation_with_optional_review(doc: Any, ctx: Any, apply_fn: Any) -> Any:
    """Run a Writer document mutation, optionally wrapped in EditReviewSession.

    For modules LibrePy also ships (scripting, vision). LibrePy omits ``plugin.writer.edit_review``,
    so there the mutation applies directly; WriterAgent-only modules call ``edit_review.record_write``.
    """
    try:
        # Lazy: LibrePy ships without plugin.writer.edit_review.
        from plugin.writer.edit_review import record_write
    except ImportError:
        return apply_fn()
    return record_write(doc, ctx, apply_fn)[0]

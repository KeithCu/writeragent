# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""HTML/StarWriter import, replace, and markup routing for Writer documents.

Public entries are re-exported from ``plugin.writer.format``.
Header/footer apply uses ``replace_xtext_with_html`` (same StarWriter
insert as the body path, pointed at a region ``XText``).

CJK ruby: StarWriter concatenates ``<rt>`` into the body. ``extract_and_strip_ruby``
drops the reading before import; ``_apply_ruby_spans`` sets ``RubyText`` on each
base run. Not a text field — do not use ``TextField.Ruby``.
"""

from __future__ import annotations

import html as html_mod
import logging
import re
import uuid
from typing import Any


from plugin.framework.errors import best_effort
from plugin.doc.text_helpers import go_right, normalize_linebreaks as _normalize
from plugin.framework.errors import ToolExecutionError, reraise_if_disposed, uno_read
from . import field_restore, html_import_styles, html_ruby, image_placeholders
from .math.html_math_segment import html_fragment_contains_mixed_math, segment_html_with_mixed_math
from .math.math_mml_convert import convert_latex_to_starmath, convert_mathml_to_starmath, insert_writer_math_formula

log = logging.getLogger("writeragent.writer")
# uno_read default where None is a real UNO result.
_FAILED: Any = object()


_BLOCK_MARKUP_RE = re.compile(
    r"<(?:p|div|h[1-6]|ul|ol|li|table|thead|tbody|tfoot|tr|td|th|blockquote|pre|hr|section|article)\b",
    re.IGNORECASE,
)


def _wrap_html_fragment(html_content: str, extra_css: str | None = None) -> str:
    """Wrap an HTML fragment in a full document structure for LO's filter."""
    if not html_content or not isinstance(html_content, str):
        return html_content
    has_html = "<html" in html_content.lower() and "</html>" in html_content.lower()
    has_body = "<body" in html_content.lower() and "</body>" in html_content.lower()
    if has_html and has_body:
        return html_content
    head = '<meta charset="UTF-8">'
    if extra_css:
        head = '<meta charset="UTF-8">\n<style>%s</style>' % extra_css
    return '<!DOCTYPE html>\n<html>\n<head>\n%s\n</head>\n<body>\n%s\n</body>\n</html>' % (head, html_content)



def _ensure_html_linebreaks(content: str) -> str:
    """Convert newlines to ``<br>``/``<p>`` when content is plain text
    and the active format is HTML, so LO's filter preserves them.

    Do not ``html.unescape`` here. Insert and replace already unescape once,
    and the StarWriter filter unescapes entities itself. A second unescape
    turned ``&amp;lt;p&amp;gt;`` / ``&amp;lt;b&amp;gt;`` inside real markup
    into live tags.
    """
    if not isinstance(content, str) or not content:
        return content
    from plugin.framework.html_stripper import escape_prose_angle_brackets

    content = escape_prose_angle_brackets(_normalize(content))
    # Vision/Docling export full documents; nesting another wrapper breaks StarWriter import.
    if re.search(r"<!DOCTYPE\s+html|<html[\s>]", content, re.IGNORECASE):
        from . import format as format_mod
        content = format_mod._strip_html_boilerplate(content)
    # Why-comment: a table or <h4> has none of the old markers, so it was
    # wrapped in <p> and its newlines became <br>. Do not treat every tag as HTML.
    if re.search(
        r"</?(?:p|br|div|h[1-6]|ul|ol|li|table|thead|tbody|tfoot|tr|td|th|blockquote|pre|hr)\b",
        content,
        re.IGNORECASE,
    ):
        return _wrap_html_fragment(content)

    content = re.sub(r"\n{3,}", "\n\n", content)
    paras = content.split("\n\n")
    out = []
    for p in paras:
        if not p.strip():
            continue
        p_html = p.replace("\n", "<br>\n")
        out.append("<p>%s</p>" % p_html)
    return _wrap_html_fragment("\n".join(out))



def html_to_plain_text(html_string: str, ctx: Any = None, config_svc: Any = None) -> str:
    """Convert HTML to plain search text matching LibreOffice paragraph structure.

    Paragraphs, headings, list items, and table elements are separated by newlines;
    line breaks (<br>) emit a newline. Intra-block whitespace collapses to a single space.
    """
    if not html_string or not isinstance(html_string, str):
        return (html_string or "").strip()
    # Plain text without markup or entities needs no parsing.
    if "<" not in html_string and "&" not in html_string:
        return html_string.strip()
    from plugin.framework.html_stripper import html_to_search_text

    stripped, _unused_ruby = html_ruby.extract_and_strip_ruby(html_string)
    return html_to_search_text(stripped).strip()



def _parked_cursor(cursor: Any) -> Any:
    """A cursor parked at *cursor*'s end, in the same ``XText``.

    Multi-segment inserts must not move the cursor to the END OF THE
    BODY after each segment. That only works when inserting at the
    end. A search replace whose content has math leaves "[Math import
    failed] ..." and the rest of the content orphaned at the end of
    the document (relatos #46/#47/#48), and any cursor in a frame,
    cell or header raises an empty RuntimeException (#52): it cannot
    go to a range in another text. The HTML import, ``insertString``
    and ``insertTextContent`` all leave a cursor parked at the insert
    point AFTER what they insert (checked live; the import leaves
    *cursor* itself BEFORE it), so going back to the parked cursor
    continues right after the content, in any text.
    """
    return cursor.getText().createTextCursorByRange(cursor.getEnd())



def _cursor_goto_document_end(model: Any, cursor: Any) -> None:
    """Move *cursor* to the end of the document body (``model.getText()``)."""
    end_c = model.getText().createTextCursor()
    end_c.gotoEnd(False)
    cursor.gotoRange(end_c.getStart(), False)



def insert_html_fragment_at_cursor(
    cursor: Any,
    html_fragment: str,
    *,
    extra_css: str | None = None,
    wrap: bool = True,
    config_svc: Any = None,
    model: Any = None,
) -> None:
    """Import a fragment via the StarWriter HTML filter at *cursor*.

    When *wrap* is True, wraps bare fragments in a full HTML document.
    *extra_css* is injected into ``<head>`` (e.g. sidebar list margins).
    When *model* is provided, moves *cursor* past the imported content
    (needed for multi-segment Writer inserts).
    """
    from plugin.framework.html_stripper import escape_prose_angle_brackets

    html_fragment = escape_prose_angle_brackets(html_fragment)
    prepared = _wrap_html_fragment(html_fragment, extra_css=extra_css) if wrap else html_fragment
    tail = _parked_cursor(cursor) if model is not None else None
    from . import format as format_mod
    with format_mod._with_temp_buffer(prepared) as (_path, file_url):
        filter_name = format_mod.HTML_FILTER
        filter_props = (format_mod.create_property_value("FilterName", filter_name),)
        try:
            cursor.insertDocumentFromURL(file_url, filter_props)
        except Exception as e:
            # Mid-script apply_document_content / insert_content paths used to bubble with no log.
            log.error(
                "insertDocumentFromURL failed: type=%s str=%r repr=%r filter=%r url=%r",
                type(e).__name__,
                str(e),
                repr(e),
                filter_name,
                file_url,
                exc_info=e,
            )
            raise
    if tail is not None:
        cursor.gotoRange(tail.getStart(), False)



def _insert_starwriter_html_at_cursor(model: Any, cursor: Any, prepared_html: str, config_svc: Any = None) -> None:
    """Import one HTML fragment through the StarWriter HTML filter at *cursor*."""
    insert_html_fragment_at_cursor(
        cursor, prepared_html, wrap=False, config_svc=config_svc, model=model
    )



def _insert_mixed_html_and_math_at_cursor(
    model: Any, ctx: Any, cursor: Any, unescaped: str, config_svc: Any = None
) -> None:
    """Insert alternating HTML (via filter) and math (MathML or TeX) as formula objects."""
    _segs = segment_html_with_mixed_math(unescaped)
    if log.isEnabledFor(logging.DEBUG) and html_fragment_contains_mixed_math(unescaped):
        _math_i = 0
        for _si, _s in enumerate(_segs):
            if _s.kind == "html":
                log.debug("mixed_html_math: segment[%d] html nl=%d len=%d", _si, _s.text.count("\n"), len(_s.text))
            else:
                _math_i += 1
                log.debug("mixed_html_math: segment[%d] %s#%d display_block=%s src_nl=%d src_len=%d", _si, _s.kind, _math_i, _s.display_block, _s.text.count("\n"), len(_s.text))
    tail = _parked_cursor(cursor)
    for seg in _segs:
        if seg.kind == "html":
            chunk = seg.text
            if not chunk:
                continue
            if not chunk.strip():
                cursor.getText().insertString(cursor, chunk, False)
                cursor.gotoRange(tail.getStart(), False)
                continue

            # Expand literal \n and \t for plain HTML without math
            chunk = chunk.replace("\\n", "\n").replace("\\t", "\t")
            chunk, seg_ruby = html_ruby.extract_and_strip_ruby(chunk)

            sub = _ensure_html_linebreaks(chunk)
            seg_ruby_skip = html_ruby._ruby_skip(cursor.getText(), cursor, seg_ruby)
            _insert_starwriter_html_at_cursor(model, cursor, sub, config_svc=config_svc)
            if seg_ruby:
                html_ruby._apply_ruby_spans(cursor.getText(), seg_ruby, skip_chars=seg_ruby_skip)
            continue
        if seg.kind == "tex":
            res = convert_latex_to_starmath(ctx, seg.text, display_block=seg.display_block)
        else:
            res = convert_mathml_to_starmath(ctx, seg.text)
        if res.ok and res.starmath and log.isEnabledFor(logging.DEBUG):
            log.debug("mixed_html_math: StarMath from converter nl=%d len=%d repr=%r", res.starmath.count("\n"), len(res.starmath), res.starmath[:500])
        if res.ok and res.starmath:
            insert_writer_math_formula(model, cursor, res.starmath, display_block=seg.display_block)
        else:
            snippet = (seg.text or "").replace("\n", " ")[:120]
            fallback = "[Math import failed] " + snippet
            cursor.getText().insertString(cursor, fallback, False)
            log.debug("math import failed: %s snippet=%r", res.error_message, snippet)
        cursor.gotoRange(tail.getStart(), False)



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


def _paragraph_before(text_obj: Any, cursor: Any) -> Any:
    """Cursor at the start of the paragraph before *cursor*'s, or None in the first one."""
    cur = text_obj.createTextCursorByRange(cursor.getStart())
    cur.gotoStartOfParagraph(False)
    return cur if cur.gotoPreviousParagraph(False) else None


def _paragraph_after(text_obj: Any, before_para: Any) -> Any:
    """Start of the paragraph after *before_para* (the text start when it is None)."""
    if before_para is None:
        return text_obj.createTextCursorByRange(text_obj.getStart())
    cur = text_obj.createTextCursorByRange(before_para.getStart())
    cur.gotoNextParagraph(False)
    return cur


def _insert_mixed_or_plain_html(
    model: Any,
    ctx: Any,
    cursor: Any,
    unescaped_content: str,
    config_svc: Any = None,
    apply_styles: bool = True,
) -> None:
    """HTML import (optional MathML + TeX layer).

    data-lo-style paragraph styling is applied via UNO after the import when *apply_styles* is True
    (``full_document``, and ``beginning``/``end`` after absorb prep in ``insert_content_at_position``).
    For ``selection``/``search`` it stays False: those paths split or merge into neighbor text, so
    applying the named style would restyle pre-existing content. We still strip data-lo-style for a
    clean StarWriter import either way.
    """
    nonce = uuid.uuid4().hex[:8]
    unescaped_content = field_restore.rewrite_exported_field_spans(unescaped_content, nonce=nonce)
    # Strip data-lo-style so the StarWriter import sees clean markup (it drops unknown attributes
    # anyway); we re-apply the named styles via UNO afterwards only when apply_styles is True.
    clean, block_styles = html_import_styles._extract_block_lo_styles(unescaped_content)
    # Expand literal \n and \t before measuring ruby offsets and StarWriter import
    clean = clean.replace("\\n", "\n").replace("\\t", "\t")
    has_math = html_fragment_contains_mixed_math(clean)
    if not has_math:
        # Strip <rt> so StarWriter does not concatenate reading into the body; paint RubyText after.
        clean, ruby_spans = html_ruby.extract_and_strip_ruby(clean)
    else:
        ruby_spans = []
    styled = apply_styles and any(block_styles)
    text_obj = cursor.getText()
    ruby_skip = html_ruby._ruby_skip(text_obj, cursor, ruby_spans)
    # Parked at the insert point, this ends up after the imported content (see _parked_cursor).
    imported_end = _parked_cursor(cursor) if styled else None
    # The math path moves *cursor* past each segment, so keep the paragraph before the insert.
    before_para = _paragraph_before(text_obj, cursor) if styled and has_math else None
    # Tip: the live cursor (and any cursor parked at the insert point) ends up after the imported
    # content, so anchor one character to the left and step back right after the import.
    start_anchor = _insert_start_anchor(text_obj, cursor) if styled and not has_math else None

    if has_math:
        _insert_mixed_html_and_math_at_cursor(model, ctx, cursor, clean, config_svc=config_svc)
    else:
        single = _ensure_html_linebreaks(clean)
        if not styled:
            _insert_starwriter_html_at_cursor(model, cursor, single, config_svc=config_svc)
        else:
            # model=None: keep the cursor at the end of the inserted content.
            insert_html_fragment_at_cursor(cursor, single, wrap=False, config_svc=config_svc, model=None)

    if styled:
        # Per-paragraph failures are already logged inside _apply_block_lo_styles.
        # This wrapper swallowed compareRegionStarts, so a closed document
        # still looked like a finished insert.
        if has_math:
            start = _paragraph_after(text_obj, before_para)
        else:
            start = _insert_start_from_anchor(text_obj, start_anchor, cursor)
        html_import_styles._apply_block_lo_styles(model, text_obj, start, block_styles, end=imported_end)
        _cursor_goto_document_end(model, cursor)
    if not has_math:
        html_ruby._apply_ruby_spans(text_obj, ruby_spans, skip_chars=ruby_skip)
    if model is not None:
        field_restore._restore_field_placeholders(model, text_obj, nonce=nonce)



def insert_html_at_cursor(
    model: Any,
    ctx: Any,
    cursor: Any,
    unescaped_content: str,
    config_svc: Any = None,
    apply_styles: bool = True,
) -> None:
    """Insert HTML or plain text at *cursor* (public API for tools)."""
    with image_placeholders._keep_placeholder_images(model, unescaped_content) as content:
        _insert_mixed_or_plain_html(model, ctx, cursor, content, config_svc=config_svc, apply_styles=apply_styles)




def _uno_service_true(obj: Any, service: str) -> bool:
    """``supportsService`` is true only for sal_Bool True or integer 1."""
    val = uno_read(lambda: obj.supportsService(service), _FAILED)
    if val is _FAILED:
        return False
    return val is True or (type(val) is int and val == 1)


def _selection_is_draw_shape(obj: Any) -> bool:
    """True when the controller selection is a Draw/Writer shape, not a text range.

    After ``create_shape`` / ``shape.upsert``, Writer selects the new shape so the
    user can see handles. That selection is not an ``XTextCursor`` host:
    ``insertDocumentFromURL`` then raises ``AttributeError: insertDocumentFromURL``
    (opaque dialog: Failed to insert result: insertDocumentFromURL).
    """
    if obj is None:
        return False
    try:
        from plugin.doc.visual_helpers import is_graphic_object

        if is_graphic_object(obj):
            return True
    except Exception:
        log.debug("_selection_is_draw_shape: graphic probe failed", exc_info=True)
    if _uno_service_true(obj, "com.sun.star.drawing.Shape"):
        return True
    if _uno_service_true(obj, "com.sun.star.drawing.CustomShape"):
        return True
    return False



def _cursor_paragraph_has_visible_text(cursor: Any) -> bool:
    """True if the paragraph containing *cursor* has non-whitespace text."""
    def visible() -> bool:
        text_obj = cursor.getText()
        para = text_obj.createTextCursorByRange(cursor.getStart())
        para.gotoStartOfParagraph(False)
        para.gotoEndOfParagraph(True)
        return bool((para.getString() or "").strip())

    # Fail safe: treat as non-empty so end-path prep still runs.
    return uno_read(visible, True)


def _ensure_empty_absorb_for_styled_insert(text: Any, cursor: Any, position: str) -> Any:
    """Place *cursor* in a paragraph safe for styled HTML import (``apply_styles=True``).

    StarWriter HTML import behavior (verified with headed/native UNO probes):

    - **Paragraph start** (``target=beginning`` after ``gotoStart``): imported ``<p>`` blocks are
      inserted as *new* paragraphs *before* the current one — the first block does **not** merge
      into existing text — so no prep is required; styling by paragraph index leaves neighbors alone.
    - **Paragraph end** (``target=end``) when the absorb paragraph has visible text: the first
      imported ``<p>`` *merges* into that paragraph, so applying ``data-lo-style`` would restyle
      neighbor text. Insert ``PARAGRAPH_BREAK`` first; LibreOffice leaves the cursor in the new
      empty trailing paragraph, which the import then absorbs (no leftover empty absorb para).
      A trailing empty paragraph after import is the same StarWriter quirk as ``full_document``.
    - **Empty absorb paragraph** (blank doc / already-empty trailing): merge is harmless — no prep.

    ``selection`` / ``search`` are not handled here (still ``apply_styles=False``).
    """
    if position != "end":
        return cursor
    if not _cursor_paragraph_has_visible_text(cursor):
        return cursor
    # 0 == com.sun.star.text.ControlCharacter.PARAGRAPH_BREAK
    # A failed break must not continue. The import would merge into the
    # neighbor paragraph and data-lo-style would restyle that text.
    text.insertControlCharacter(cursor, 0, False)
    return cursor


def insert_inline_at_cursor(model: Any, ctx: Any, cursor: Any, content: str, config_svc: Any = None) -> None:
    """Insert inline *content* at a collapsed *cursor* (data-less images kept, see
    ``image_placeholders._keep_placeholder_images``)."""
    with image_placeholders._keep_placeholder_images(model, content) as swapped:
        _insert_inline_at_cursor(model, ctx, cursor, swapped, config_svc)


def _insert_inline_at_cursor(model: Any, ctx: Any, cursor: Any, content: str, config_svc: Any = None) -> None:
    """Insert inline *content* at a collapsed *cursor* without splitting its paragraph.

    ``insert_html_at_cursor`` routes plain text through ``_ensure_html_linebreaks``, which
    wraps it in ``<p>``: imported mid-paragraph, that ``<p>`` splits the host paragraph in
    two, so inserting "bem " before a word left "... bem" / "inteiro." as two paragraphs.
    Plain text goes in with ``insertString``; inline markup is imported RAW (no ``<p>``),
    the same way ``replace_single_range_with_content`` handles inline replacements.
    """
    # No html.unescape here: apply_document_content.py already decoded complete character references on the
    # plain path (_ENTITY_RE), and a full unescape turns "&sect 2o" into "§ 2o"; the HTML import
    # decodes entities in markup itself.
    if not content_has_markup(content):
        cursor.getText().insertString(cursor, content, False)
        return
    nonce = uuid.uuid4().hex[:8]
    inline_html = field_restore.rewrite_exported_field_spans(content, nonce=nonce)
    inline_html = inline_html.replace("\\n", "\n").replace("\\t", "\t")
    # Same <rt> handling as the other import paths: StarWriter would glue the reading onto the base.
    inline_html, ruby_spans = html_ruby.extract_and_strip_ruby(inline_html)
    # The HTML import drops whitespace at the edges of a fragment, so "<b>muito</b> " lost
    # its trailing space and fused with the next word. Put edge whitespace in as text.
    core = inline_html.strip()
    lead = inline_html[: len(inline_html) - len(inline_html.lstrip())]
    trail = inline_html[len(inline_html.rstrip()):]
    text = cursor.getText()
    ruby_skip = html_ruby._ruby_skip(text, cursor, ruby_spans)
    if lead:
        text.insertString(cursor, lead, False)
    if trail:
        # The fragment import does not leave the cursor after the imported text, so the
        # trailing whitespace goes in first and the fragment is imported just before it.
        text.insertString(cursor, trail, False)
        cursor.goLeft(len(trail), False)
    if core:
        insert_html_fragment_at_cursor(cursor, core, wrap=False, config_svc=config_svc, model=None)
    html_ruby._apply_ruby_spans(text, ruby_spans, skip_chars=ruby_skip)
    if model is not None:
        field_restore._restore_field_placeholders(model, text, nonce=nonce)


# Named (``&amp;``), decimal (``&#36;``) and hex (``&#x24;``) character references with trailing semicolon.
_COMPLETE_ENTITY_RE = re.compile(r"&(?:#[0-9]+|#[xX][0-9a-fA-F]+|[A-Za-z][A-Za-z0-9]{1,31});")


def _decode_complete_character_references(text: str) -> str:
    """Decode only HTML character references ending with a semicolon, ignoring incomplete references like '&sect 2o'."""
    if "&" not in text or not _COMPLETE_ENTITY_RE.search(text):
        return text
    return _COMPLETE_ENTITY_RE.sub(lambda m: html_mod.unescape(m.group(0)), text)


def insert_content_at_position(model: Any, ctx: Any, content: str, position: str, config_svc: Any = None) -> None:
    """Insert formatted content at *position* (``'beginning'``,
    ``'end'``, or ``'selection'``) using ``insertDocumentFromURL``.
    """
    with image_placeholders._keep_placeholder_images(model, content) as swapped:
        _insert_content_at_position(model, ctx, swapped, position, config_svc)


def _insert_content_at_position(model: Any, ctx: Any, content: str, position: str, config_svc: Any = None) -> None:
    content = _decode_complete_character_references(content)

    text = model.getText()
    cursor = text.createTextCursor()

    if position == "beginning":
        cursor.gotoStart(False)
    elif position == "end":
        cursor.gotoEnd(False)
    elif position == "selection":
        # Prefer a real text selection (table/frame-aware). Draw shape selections (post
        # shape.upsert) and empty selections fall back to the view text cursor, then
        # document end — RPS Universal Sample must not require the user to select text.
        def _doc_end_cursor() -> Any:
            c = text.createTextCursor()
            c.gotoEnd(False)
            return c

        controller = uno_read(lambda: model.getCurrentController(), None)

        from plugin.doc.text_helpers import selected_text_range

        rng = selected_text_range(controller)

        if rng is not None and _selection_is_draw_shape(rng):
            log.debug(
                "insert_content_at_position: selection is Draw/Writer shape; "
                "falling back to view/document text cursor"
            )
            rng = None

        if rng is None and controller is not None:
            vc = uno_read(lambda: controller.getViewCursor(), None)
            if vc is not None and not _selection_is_draw_shape(vc):
                rng = vc

        if rng is None:
            cursor = _doc_end_cursor()
        else:
            try:
                cursor = rng.getText().createTextCursorByRange(rng.getStart())
                if not hasattr(cursor, "insertDocumentFromURL"):
                    raise AttributeError("insertDocumentFromURL")
                # Clear only real text selections (never shapes).
                if hasattr(rng, "setString") and not _selection_is_draw_shape(rng):
                    from plugin.writer.table_nesting import raise_if_range_hosts_nested_table

                    raise_if_range_hosts_nested_table(rng)
                    rng.setString("")
            except AttributeError as e:
                # No text cursor (a shape that was not classified earlier).
                # ToolExecutionError (nested table) and a closed document must
                # not become an insert at the document end.
                log.debug(
                    "insert_content_at_position: text selection unusable (%s); using document end",
                    e,
                )
                cursor = _doc_end_cursor()
    else:
        raise ToolExecutionError("Unknown position: %s" % position)

    # beginning/end: honor data-lo-style after absorb prep (see _ensure_empty_absorb_for_styled_insert).
    # selection/search: still apply_styles=False — replace splits/merges into neighbor text (phase 3).
    if position in ("beginning", "end"):
        cursor = _ensure_empty_absorb_for_styled_insert(text, cursor, position)
        _insert_mixed_or_plain_html(model, ctx, cursor, content, config_svc=config_svc, apply_styles=True)
    else:
        _insert_mixed_or_plain_html(model, ctx, cursor, content, config_svc=config_svc, apply_styles=False)



def replace_full_document(model: Any, ctx: Any, content: str, config_svc: Any = None) -> None:
    """Clear the document and insert *content*."""
    with image_placeholders._keep_placeholder_images(model, content) as swapped:
        _replace_full_document(model, ctx, swapped, config_svc)


def _replace_full_document(model: Any, ctx: Any, content: str, config_svc: Any = None) -> None:
    content = _decode_complete_character_references(content)

    text = model.getText()
    cursor = text.createTextCursor()
    cursor.gotoStart(False)
    cursor.gotoEnd(True)
    from . import format as format_mod
    with format_mod._deletion_author():  # author the deletion distinctly (split by-author coloring)
        cursor.setString("")
    cursor.gotoStart(False)
    if not image_placeholders._is_recording_changes(model):
        _insert_mixed_or_plain_html(model, ctx, cursor, content, config_svc=config_svc)
        return
    # In review mode the deleted text stays in place, and an import at
    # its start takes the character formatting of the first old
    # paragraph as direct formatting -- a document opening with an 18pt
    # bold heading comes back 18pt bold throughout (reproduced live;
    # the data-lo-style pass then keeps it as a "hand-set" override).
    # Import into a fresh empty paragraph opened before the deleted
    # text, then drop that paragraph if it is left over empty (the
    # import puts its blocks before it) -- a tracked insertion swallows
    # the break.
    text.insertControlCharacter(cursor, 0, False)  # 0 == ControlCharacter.PARAGRAPH_BREAK
    cursor.goLeft(1, False)
    leftover = text.createTextCursorByRange(cursor.getStart())
    _insert_mixed_or_plain_html(model, ctx, cursor, content, config_svc=config_svc)
    leftover.gotoEndOfParagraph(True)
    if leftover.getString() == "":
        leftover.collapseToStart()
        leftover.goLeft(1, True)
        # Tip: a tracked delete of this break joins paragraphs ('initiativeproject'); delete it untracked.
        was_rec = bool(model.getPropertyValue("RecordChanges"))
        if was_rec:
            model.setPropertyValue("RecordChanges", False)
        try:
            leftover.setString("")
        finally:
            if was_rec:
                model.setPropertyValue("RecordChanges", True)




def replace_single_range_with_content(
    model: Any, text_range: Any, content: str, ctx: Any, config_svc: Any = None
) -> None:
    """Replace the given text range with rendered *content* (HTML path)."""
    with image_placeholders._keep_placeholder_images(model, content) as swapped:
        _replace_single_range_with_content(model, text_range, swapped, ctx, config_svc)


def _replace_single_range_with_content(
    model: Any, text_range: Any, content: str, ctx: Any, config_svc: Any = None
) -> None:
    prepared = _decode_complete_character_references(content)
    text_obj = text_range.getText()

    # Detect a table-cell target up front (the cursor's TextTable property is set inside a cell).
    # Block/rich HTML import into a cell's nested XText can raise an empty RuntimeException (see the
    # FOLLOW-UP above); we use this to turn that opaque failure into a clear, actionable message.
    in_table_cell = uno_read(lambda: text_obj.createTextCursorByRange(text_range.getStart()).getPropertyValue("TextTable") is not None, False)

    # Preserve the target paragraph style for INLINE replacements. The StarWriter
    # HTML import resets the paragraph to a default body style, silently demoting
    # headings (e.g. "Heading 3" -> "Text body"). For inline-only content (no
    # block-level tags, no math), insert without jumping the cursor to the document
    # end so we can reapply the original paragraph style across the inserted range.
    inline_preserve = not _content_has_block_markup(prepared) and not html_fragment_contains_mixed_math(prepared)
    saved_style = None
    if inline_preserve:
        saved_style = uno_read(lambda: text_obj.createTextCursorByRange(text_range.getStart()).getPropertyValue("ParaStyleName"), None)

    cursor = text_obj.createTextCursorByRange(text_range)
    from plugin.writer.table_nesting import raise_if_range_hosts_nested_table

    # setString on a host cell wipes nested TextTables — same refuse as table_set_cell.
    raise_if_range_hosts_nested_table(text_range)
    from . import format as format_mod
    with format_mod._deletion_author():  # author the deletion distinctly (split by-author coloring)
        cursor.setString("")

    if saved_style is not None:
        anchor = text_obj.createTextCursorByRange(cursor.getStart())
        # Insert the inline fragment RAW (do not route through _ensure_html_linebreaks:
        # it does not recognise <span> as HTML and would wrap it in <p>, creating an
        # extra body paragraph). model=None leaves the cursor at the end of the
        # INSERTED content (not the document end), so [anchor, cursor] bounds it.
        nonce = uuid.uuid4().hex[:8]
        inline_html = field_restore.rewrite_exported_field_spans(prepared, nonce=nonce)
        inline_html = inline_html.replace("\\n", "\n").replace("\\t", "\t")
        inline_html, ruby_spans = html_ruby.extract_and_strip_ruby(inline_html)
        ruby_skip = html_ruby._ruby_skip(text_obj, anchor, ruby_spans)
        insert_html_fragment_at_cursor(cursor, inline_html, wrap=False, config_svc=config_svc, model=None)
        html_ruby._apply_ruby_spans(text_obj, ruby_spans, skip_chars=ruby_skip)
        if model is not None:
            field_restore._restore_field_placeholders(model, text_obj, nonce=nonce)
        # Re-apply the saved paragraph style (the HTML import can demote Heading -> body).
        # Skip it while Track Changes is recording: setString("") above leaves the old text in
        # place as a tracked DELETE, and re-applying a paragraph style across [anchor, cursor]
        # spans that struck text, converting its DELETE redline into a FORMAT redline -- so
        # accepting the change would keep BOTH the old and new text. The inline import does not
        # demote the style here, so skipping the restore keeps a clean Delete + Insert pair.
        if not image_placeholders._is_recording_changes(model):
            def restore_style() -> None:
                restore = text_obj.createTextCursorByRange(anchor.getStart())
                restore.gotoRange(cursor.getEnd(), True)
                format_mod.apply_paragraph_style_preserving_direct_char(model, restore, saved_style)

            best_effort(restore_style, "replace_single_range_with_content: could not restore ParaStyleName")
    else:
        # apply_styles=False: a search/replace splits the matched paragraph, so applying a
        # data-lo-style here would restyle the surrounding text. Styled writes use full_document.
        try:
            _insert_mixed_or_plain_html(model, ctx, cursor, prepared, config_svc=config_svc, apply_styles=False)
        except Exception as e:
            reraise_if_disposed(e)
            # Block/rich HTML into a table cell can raise an empty RuntimeException from the StarWriter
            # HTML import (nested-XText cursor mapping). Surface a clear, actionable message instead of
            # the opaque error; the atomic wrapper rolls back the partial edit either way.
            if in_table_cell and _content_has_block_markup(prepared):
                raise RuntimeError(
                    "Rich/block HTML can't be inserted inside a table cell yet (the LibreOffice HTML "
                    "import mishandles nested cell text). Use plain text or inline tags only inside "
                    "table cells, or place block/rich content outside the table."
                ) from e
            raise




def _restore_xtext_string(text_obj: Any, original: str) -> None:
    """Write *original* back into *text_obj* after a failed import.

    ``replace_xtext_with_html`` clears the header or footer before
    StarWriter import. A failed ``insertDocumentFromURL`` leaves the
    region empty unless the previous characters are put back. Direct
    formatting and live fields are not reconstructed; the region is
    not left blank.
    """
    try:
        cur = text_obj.createTextCursor()
        cur.gotoStart(False)
        cur.gotoEnd(True)
        cur.setString(original)
    except Exception:
        log.exception("replace_xtext_with_html: could not restore text after a failed import")


def _rollback_undo_action(mgr: Any, title: str | None, log_message: str) -> bool:
    try:
        titles = mgr.getAllUndoActionTitles()
        if titles and titles[0] == title:
            mgr.undo()
            return True
    except Exception:
        log.exception(log_message)
    return False


def replace_xtext_with_html(text_obj: Any, html: str, config_svc: Any = None, model: Any = None) -> None:
    """Clear *text_obj* and import *html* via the shared StarWriter path.

    Field spans from ``document_to_content`` / ``xtext_to_content`` are
    restored as live fields after import. *model* is the owning document
    (needed to create fields, enter the undo context, and find placeholders).

    When *model* is provided, the clear and import are protected by a document
    undo context: any failure rolls back to the previous state, preserving
    graphics and fields. When *model* is None, a failed import falls back to
    plain-text restore, which cannot rebuild fields or graphics.
    """
    if text_obj is None:
        raise ToolExecutionError("No text object to import into.")
    nonce = uuid.uuid4().hex[:8]
    expanded = html if isinstance(html, str) else ("" if html is None else str(html))
    expanded = expanded.replace("\\n", "\n").replace("\\t", "\t")
    expanded, ruby_spans = html_ruby.extract_and_strip_ruby(expanded)
    rewritten = field_restore.rewrite_exported_field_spans(expanded, nonce=nonce)
    prepared = _ensure_html_linebreaks(rewritten)
    cursor = text_obj.createTextCursor()
    cursor.gotoStart(False)
    cursor.gotoEnd(True)

    mgr = None
    title = None
    if model is not None:
        from .edit_review import next_agent_edit_undo_title

        title = next_agent_edit_undo_title()
        try:
            candidate = model.getUndoManager()
            # `is True`: MagicMock isLocked() is truthy and must not look locked.
            if candidate is not None and candidate.isLocked() is not True:
                candidate.enterUndoContext(title)
                mgr = candidate
        except Exception as exc:
            reraise_if_disposed(exc)
            mgr = None
        if mgr is None:
            raise ToolExecutionError("Could not enter undo context for header/footer import.")

    original = None
    if mgr is None:
        original = uno_read(lambda: cursor.getString(), None)

    cleared = False
    try:
        cursor.setString("")
        cleared = True
        cursor.gotoStart(False)
        insert_html_fragment_at_cursor(
            cursor, prepared, wrap=False, config_svc=config_svc, model=None,
        )
        html_ruby._apply_ruby_spans(text_obj, ruby_spans)
        if model is not None:
            field_restore._restore_field_placeholders(model, text_obj, nonce=nonce)

        # Issue #1424: HTML import can leave a trailing empty paragraph.
        # If there is more than one paragraph and the last one is entirely empty, remove it.
        try:
            enum = text_obj.createEnumeration()
            paras = []
            while enum.hasMoreElements() is True:
                try:
                    paras.append(enum.nextElement())
                except Exception as e:
                    reraise_if_disposed(e)
                    log.debug("Failed to enumerate paragraph: %s", e)
                    break
            if len(paras) > 1:
                last_para = paras[-1]
                if not last_para.getString():
                    # Check for fields/portions before deleting
                    has_content = False
                    try:
                        portions = last_para.createEnumeration()
                        while portions.hasMoreElements() is True:
                            portion = portions.nextElement()
                            if portion.getString() or portion.getPropertyValue("TextPortionType") != "Text":
                                has_content = True
                                break
                    except Exception as e:
                        # A frame, field, or image paragraph has empty getString(); delete only when the walk finishes empty.
                        reraise_if_disposed(e)
                        log.debug("Failed to inspect trailing paragraph portions: %s", e)
                        has_content = True
                    if not has_content:
                        try:
                            # To actually remove the trailing break, select from the end of the previous paragraph
                            # to the end of the document, and set String to empty.
                            cursor = text_obj.createTextCursorByRange(paras[-2])
                            cursor.gotoEndOfParagraph(False)
                            cursor.gotoRange(last_para.getEnd(), True)
                            cursor.setString("")
                        except Exception as e:
                            reraise_if_disposed(e)
                            log.debug("Failed to remove trailing paragraph break via cursor: %s", e)
                            try:
                                if hasattr(last_para, "dispose"):
                                    last_para.dispose()
                            except Exception as e2:
                                reraise_if_disposed(e2)
                                log.debug("Failed to dispose trailing paragraph: %s", e2)
        except Exception as e:
            reraise_if_disposed(e)
            log.debug("Failed to inspect trailing paragraph: %s", e)
    except Exception as exc:
        if mgr is not None:
            left = False
            undone = False
            try:
                mgr.leaveUndoContext()
                left = True
            except Exception:
                log.exception("replace_xtext_with_html: leaveUndoContext failed")
            if left:
                undone = _rollback_undo_action(mgr, title, "replace_xtext_with_html: undo failed")
            if not undone:
                raise ToolExecutionError(
                    f"the header/footer was written and could not be rolled back: {exc}"
                ) from exc
            raise
        if cleared and original is not None:
            _restore_xtext_string(text_obj, original)
        raise

    if mgr is not None:
        try:
            mgr.leaveUndoContext()
        except Exception:
            log.exception("replace_xtext_with_html: leaveUndoContext failed")


def content_has_markup(content: str | None) -> bool:
    """Return ``True`` if *content* appears to contain HTML or math markup."""
    if not content or not isinstance(content, str):
        return False
    from plugin.framework.html_stripper import text_has_real_html_tag

    if text_has_real_html_tag(content):
        return True
    return "$$" in content or r"\(" in content or r"\[" in content or "<math" in content.lower()


def plain_text_if_markup(text: str, uno_ctx: Any, config_svc: Any) -> str:
    """Return plain search text if text contains markup, else text unchanged."""
    if content_has_markup(text):
        return html_to_plain_text(text, uno_ctx, config_svc)
    return text



def _content_has_block_markup(content: str | None) -> bool:
    """Return ``True`` if *content* contains block-level HTML (paragraph-defining)."""
    if not content or not isinstance(content, str):
        return False
    return bool(_BLOCK_MARKUP_RE.search(content))



def _collect_range_portions(target_range: Any) -> tuple[bool, list[tuple[str, int]] | None]:
    """Return (has_special, portions_list_or_None)."""
    try:
        if not hasattr(target_range, "createEnumeration"):
            return False, None
        enum = target_range.createEnumeration()
        if enum is None:
            return False, None
        portions: list[tuple[str, int]] = []
        while enum.hasMoreElements() is True:
            elem = enum.nextElement()
            if hasattr(elem, "createEnumeration"):
                penum = elem.createEnumeration()
                while penum.hasMoreElements() is True:
                    portion = penum.nextElement()
                    kind = getattr(portion, "TextPortionType", None)
                    if kind is None and hasattr(portion, "getPropertyValue"):
                        kind = portion.getPropertyValue("TextPortionType")
                    if kind and kind not in ("Text", "SoftPageBreak"):
                        return True, None
                    text_str = portion.getString() if hasattr(portion, "getString") else ""
                    portions.append((kind or "Text", len(text_str)))
            else:
                kind = getattr(elem, "TextPortionType", None)
                if kind is None and hasattr(elem, "getPropertyValue"):
                    kind = elem.getPropertyValue("TextPortionType")
                if kind and kind not in ("Text", "SoftPageBreak"):
                    return True, None
                text_str = elem.getString() if hasattr(elem, "getString") else ""
                portions.append((kind or "Text", len(text_str)))
        return False, portions
    except Exception as exc:
        reraise_if_disposed(exc)
        return False, None


def _step_replace_characters(
    text: Any, main_cursor: Any, old_text: str, new_text: str, start: int, stop: int
) -> None:
    for i in range(start, stop):
        sel = text.createTextCursorByRange(main_cursor)
        if not sel.goRight(1, True):
            raise ToolExecutionError(
                "Could not replace that text in place (the selection did not advance)."
            )
        if new_text[i] != old_text[i]:
            sel.setString(new_text[i])
        main_cursor.gotoRange(sel.getEnd(), False)


def replace_preserving_format(
    model: Any,
    target_range: Any,
    new_text: str,
    ctx: Any = None,
    in_undo_context: bool = False,
    split_author: bool = True,
) -> None:
    """Replace text in *target_range* with *new_text* character by
    character, preserving per-character formatting (bold, italic,
    font, color, etc.).

    Cursors are created on ``target_range.getText()`` (the cell, frame, or body
    ``XText`` that owns the range), not ``model.getText()``. The range must lie
    entirely within that text object.

    Refuses a host cell that contains a nested TextTable (setString / whole-range
    replace would delete it — use table_set_cell).

    When recording tracked changes, *split_author* selects the rendering:
    ``True`` (default) authors the deletion and insertion separately so
    LibreOffice's by-author coloring shows removed vs new text in two distinct
    colors; ``False`` records the whole replace as a single atomic op authored
    once (one color). The split-author two-step is only safe inside an open undo
    context (``in_undo_context``) that can roll back a half-applied edit, so it is
    used only when BOTH ``split_author`` and ``in_undo_context`` hold; otherwise
    the atomic single-op path keeps the edit all-or-nothing.
    """
    # Use the range's OWN text object, not the document body. When target_range
    # lives inside a table cell, model.getText() (the body) is the wrong XText and
    # createTextCursorByRange() raises "End of content node doesn't have the proper
    # start node". target_range.getText() resolves to the cell (or body) correctly,
    # matching the markup path which already uses found.getText().
    from plugin.writer.table_nesting import raise_if_range_hosts_nested_table

    raise_if_range_hosts_nested_table(target_range)
    text = target_range.getText()
    old_text = _normalize(target_range.getString())
    new_text = _normalize(new_text)

    if new_text == old_text:
        return

    # Track Changes: the char-by-char diff below records a separate redline for EACH changed
    # character, which renders as a scrambled, un-reviewable mess (old and new text interleaved).
    # When recording, replace the whole range in one shot so the edit is a single tracked
    # Delete + Insert the user can accept (-> new text) or reject (-> old text) cleanly.
    if image_placeholders._is_recording_changes(model):
        cursor = text.createTextCursorByRange(target_range)
        if not (split_author and in_undo_context):
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
        from . import format as format_mod
        with format_mod._deletion_author():  # author the deletion distinctly (split by-author coloring)
            cursor.setString("")
        if new_text:
            try:
                text.insertString(cursor, new_text, False)
            except Exception:
                try:
                    text.insertString(cursor, original, False)
                except Exception:
                    log.warning("replace_preserving_format: insert failed AND restore failed; "
                                "range may be left partial", exc_info=True)
                raise
        return

    old_len = len(old_text)
    new_len = len(new_text)

    if old_len == 0 and new_len == 0:
        return
    if old_len == 0:
        cursor = text.createTextCursorByRange(target_range.getStart())
        text.insertString(cursor, new_text, False)
        return

    overlap = min(old_len, new_len)

    # Do not pump processEvents inside this edit. A queued tool or a keystroke
    # would run on the main thread, inside the caller's undo context.
    main_cursor = text.createTextCursorByRange(target_range.getStart())

    has_special, portions = _collect_range_portions(target_range)

    if has_special:
        # Non-text portions (fields, ruby) advance by portion instead of char, so step 1-by-1.
        _step_replace_characters(text, main_cursor, old_text, new_text, 0, overlap)
    elif portions is not None and sum(plen for _, plen in portions) == old_len:
        # Coalesce changes inside each Text portion to avoid per-character setString calls.
        offset = 0
        for _kind, plen in portions:
            if plen == 0:
                continue
            portion_start = offset
            portion_end = offset + plen
            offset = portion_end

            if portion_start >= overlap:
                break

            slice_end = min(portion_end, overlap)
            old_slice = old_text[portion_start:slice_end]
            new_slice = new_text[portion_start:slice_end]
            slice_len = len(old_slice)

            if old_slice == new_slice:
                if not go_right(main_cursor, slice_len, False):
                    raise ToolExecutionError(
                        "Could not replace that text in place (the selection did not advance)."
                    )
                continue

            p = 0
            while p < slice_len and old_slice[p] == new_slice[p]:
                p += 1
            if p > 0:
                if not go_right(main_cursor, p, False):
                    raise ToolExecutionError(
                        "Could not replace that text in place (the selection did not advance)."
                    )

            s = 0
            max_s = slice_len - p
            while s < max_s and old_slice[slice_len - 1 - s] == new_slice[slice_len - 1 - s]:
                s += 1

            diff_len = slice_len - p - s
            if diff_len > 0:
                sel = text.createTextCursorByRange(main_cursor)
                if not go_right(sel, diff_len, True):
                    raise ToolExecutionError(
                        "Could not replace that text in place (the selection did not advance)."
                    )
                sel.setString(new_slice[p : slice_len - s])
                main_cursor.gotoRange(sel.getEnd(), False)

            if s > 0:
                if not go_right(main_cursor, s, False):
                    raise ToolExecutionError(
                        "Could not replace that text in place (the selection did not advance)."
                    )

        if offset < overlap:
            _step_replace_characters(text, main_cursor, old_text, new_text, offset, overlap)
    else:
        i = 0
        while i < overlap:
            if new_text[i] == old_text[i]:
                run_start = i
                while i < overlap and new_text[i] == old_text[i]:
                    i += 1
                k = i - run_start
                if not go_right(main_cursor, k, False):
                    raise ToolExecutionError(
                        "Could not replace that text in place (the selection did not advance)."
                    )
            else:
                sel = text.createTextCursorByRange(main_cursor)
                if not sel.goRight(1, True):
                    raise ToolExecutionError(
                        "Could not replace that text in place (the selection did not advance)."
                    )
                sel.setString(new_text[i])
                main_cursor.gotoRange(sel.getEnd(), False)
                i += 1

    # Handle length changes.
    if new_len > old_len:
        # Extra chars inherit formatting from the predecessor.
        text.insertString(main_cursor, new_text[old_len:], False)
    elif old_len > new_len:
        # Delete remaining original characters.
        # Ensure we don't go out of bounds of the original target_range.
        remaining_to_del = old_len - new_len
        del_cursor = text.createTextCursorByRange(main_cursor)
        # Use chunks for deletion just in case it's large.
        while remaining_to_del > 0:
            n = min(remaining_to_del, 8192)
            del_cursor.goRight(n, True)
            remaining_to_del -= n
        del_cursor.setString("")




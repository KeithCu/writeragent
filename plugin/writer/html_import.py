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
from html.parser import HTMLParser
from typing import Any

from plugin.doc.text_helpers import normalize_linebreaks as _normalize
from plugin.framework.errors import ToolExecutionError
from plugin.framework.uno_context import get_desktop, uno_same
from . import xhtml_style_postprocess as xhtml_post
from . import format as format_mod
from .math.html_math_segment import html_fragment_contains_mixed_math, segment_html_with_mixed_math
from .math.math_mml_convert import convert_latex_to_starmath, convert_mathml_to_starmath, insert_writer_math_formula

log = logging.getLogger("writeragent.writer")

_MARKUP_PATTERNS = [
    # Markdown
    "**",
    "__",
    "``",
    "# ",
    "## ",
    "### ",
    "| ",
    "|---",
    "- [ ]",
    # HTML
    "<b>",
    "<i>",
    "<p>",
    "<h1",
    "<h2",
    "<h3",
    "<table",
    "<tr",
    "<td",
    "<ul>",
    "<ol>",
    "<li>",
    "<div",
    "<span",
    "<br",
    "<img",
    "<strong",
    "<em>",
    "</",
    "<html",
    "<body",
    "<!DOCTYPE",
    "<math",
    "<ruby",
    "<rt",
    # TeX (so plain ``\\( … \\)`` / ``$$`` is not misclassified as format-preserving)
    "$$",
    "\\(",
    "\\[",
]


# StarWriter HTML import has no ruby rule: <ruby>漢字<rt>かんじ</rt></ruby> becomes
# concatenated body text. Strip <rt> before import, then set RubyText on the base.
_RUBY_BLOCK_RE = re.compile(r"<ruby\b([^>]*)>(.*?)</ruby>", re.IGNORECASE | re.DOTALL)
_RT_RE = re.compile(r"<rt\b[^>]*>(.*?)</rt>", re.IGNORECASE | re.DOTALL)
_RP_RE = re.compile(r"<rp\b[^>]*>.*?</rp>", re.IGNORECASE | re.DOTALL)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_GO_RIGHT_CHUNK = 8192


def _visible_html_text(fragment: str):
    """Tag-stripped, entity-unescaped text of an HTML fragment."""
    if not fragment:
        return ""
    return html_mod.unescape(_HTML_TAG_RE.sub("", fragment))


def extract_and_strip_ruby(html: str):
    """Replace ``<ruby>`` with its base and return ``(clean_html, spans)``.

    Each span is ``(base, reading, is_above)`` in document order. StarWriter
    concatenates ruby children, so the import must see base only; ``RubyText``
    is painted afterwards via ``_apply_ruby_spans``.
    """
    if not html or not isinstance(html, str) or "<ruby" not in html.lower():
        return html, []
    spans = []

    def _repl(match: re.Match[str]):
        attrs = match.group(1) or ""
        inner = match.group(2) or ""
        rt_m = _RT_RE.search(inner)
        reading = _visible_html_text(rt_m.group(1) if rt_m else "").strip()
        base_html = _RP_RE.sub("", inner)
        base_html = _RT_RE.sub("", base_html)
        base = _visible_html_text(base_html)
        is_above = "under" not in attrs.lower()
        if base and reading:
            spans.append((base, reading, is_above))
        return base_html

    return _RUBY_BLOCK_RE.sub(_repl, html), spans


def _go_right(cursor: Any, n: int, expand: bool):
    """Move or extend *cursor* right by *n* characters (UNO caps the count)."""
    while n > 0:
        step = n if n < _GO_RIGHT_CHUNK else _GO_RIGHT_CHUNK
        if not cursor.goRight(step, expand):
            return False
        n -= step
    return True


def _set_ruby_on_range(cursor: Any, reading: str, is_above: bool = True) -> None:
    """Create live ruby on the selected base run (same as the UNO seed probe)."""
    cursor.setPropertyValue("RubyText", reading)
    try:
        cursor.setPropertyValue("RubyIsAbove", bool(is_above))
    except Exception:
        pass
    try:
        cursor.setPropertyValue("RubyAdjust", 0)
    except Exception:
        pass


def _prefix_char_count(text_obj: Any, cursor: Any):
    """``getString()`` length from the start of *text_obj* to *cursor*.

    Captured before import so we can apply ruby only to the inserted suffix.
    A live ``cursor.getStart()`` drifts when ``insertDocumentFromURL`` moves it.
    """
    try:
        prefix = text_obj.createTextCursor()
        prefix.gotoStart(False)
        prefix.gotoRange(cursor.getStart(), True)
        return len(prefix.getString() or "")
    except Exception:
        return 0


def _apply_ruby_spans(text_obj: Any, spans: list[Any], skip_chars: int = 0) -> None:
    """Set ``RubyText`` on each *spans* base, ignoring the first *skip_chars*.

    After a StarWriter import the document has base characters only. Offsets
    come from ``getString()`` then ``goRight`` — they agree on plain imported
    body text (no fields/ruby yet).
    """
    if not spans or text_obj is None:
        return
    try:
        origin = text_obj.getStart()
        hay = text_obj.createTextCursorByRange(origin)
        hay.gotoEnd(True)
        haystack = hay.getString() or ""
    except Exception:
        log.debug("_apply_ruby_spans: could not read imported text", exc_info=True)
        return
    pos = skip_chars if skip_chars > 0 else 0
    for item in spans:
        base, reading, is_above = item[0], item[1], item[2] if len(item) > 2 else True
        if not base or not reading:
            continue
        idx = haystack.find(base, pos)
        if idx < 0:
            log.debug("_apply_ruby_spans: base %r not found in imported text", base)
            continue
        try:
            cur = text_obj.createTextCursorByRange(origin)
            if idx and not _go_right(cur, idx, False):
                continue
            if not _go_right(cur, len(base), True):
                continue
            if cur.getString() != base:
                log.debug(
                    "_apply_ruby_spans: offset mismatch want %r got %r",
                    base, cur.getString(),
                )
                pos = idx + len(base)
                continue
            _set_ruby_on_range(cur, reading, is_above)
        except Exception:
            log.debug("_apply_ruby_spans: failed for %r", base, exc_info=True)
        pos = idx + len(base)


_BLOCK_MARKUP_PATTERNS = [
    "<p>", "<p ", "<h1", "<h2", "<h3", "<h4", "<h5", "<h6",
    "<div", "<ul", "<ol", "<li", "<table", "<tr", "<td", "<th",
    "<blockquote", "<pre", "<hr", "<section", "<article",
]


def _strip_data_lo_style(start_tag: str):
    """Remove any data-lo-style attribute from a single start-tag string."""
    start_tag = re.sub(r'\s+data-lo-style="[^"]*"', "", start_tag)
    return re.sub(r"\s+data-lo-style='[^']*'", "", start_tag)



class _BlockLoStyleExtractor(HTMLParser):
    """Collect data-lo-style per TOP-LEVEL block (document order) and strip the attribute
    from the HTML, so the StarWriter import sees clean markup and we apply the named styles
    ourselves afterwards. Content inside <table> is left to the import (avoids order desync)."""

    def __init__(self):
        super().__init__(convert_charrefs=False)
        self._table_depth = 0
        self.styles = []
        self._out = []

    def _emit(self, raw: str, attrs: list[tuple[str, str | None]], is_block: bool) -> None:
        if is_block and self._table_depth == 0:
            val = None
            for k, v in attrs:
                if k == "data-lo-style":
                    val = v
            self.styles.append(val)
            self._out.append(_strip_data_lo_style(raw))
        else:
            # Non-top-level / non-block: leave verbatim. In particular, a table-cell block's
            # data-lo-style is left for the import to ignore (v1 doesn't apply cell styles),
            # rather than silently stripped without being applied.
            self._out.append(raw)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]):
        raw = self.get_starttag_text() or ("<%s>" % tag)
        if tag.lower() == "table":
            self._table_depth += 1
            self._out.append(raw)
            return
        # BLOCK_TAGS excludes <div> (transparent container), so a wrapper does not consume a
        # positional style slot — keeps read and write symmetric on <div>.
        self._emit(raw, attrs, tag.lower() in xhtml_post.BLOCK_TAGS)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]):
        raw = self.get_starttag_text() or ("<%s/>" % tag)
        self._emit(raw, attrs, tag.lower() in xhtml_post.BLOCK_TAGS)

    def handle_endtag(self, tag: str):
        if tag.lower() == "table" and self._table_depth > 0:
            self._table_depth -= 1
        self._out.append("</%s>" % tag)

    def handle_data(self, data: str):
        self._out.append(data)

    def handle_entityref(self, name: str):
        self._out.append("&%s;" % name)

    def handle_charref(self, name: str):
        self._out.append("&#%s;" % name)

    def handle_comment(self, data: str):
        self._out.append("<!--%s-->" % data)

    def result(self):
        return "".join(self._out), self.styles



def _extract_block_lo_styles(html: str):
    """Return ``(clean_html, [data_lo_style_or_None per top-level block])``.

    Short-circuits (returns the html unchanged, no styles) when there is no data-lo-style,
    so existing callers are completely unaffected."""
    if not html or "data-lo-style" not in html:
        return html, []
    ex = _BlockLoStyleExtractor()
    ex.feed(html)
    ex.close()
    return ex.result()



def _count_preceding_paras(text_obj: Any, target: Any):
    """Number of paragraphs in *text_obj* whose start is before *target* (insertion point).

    Computed BEFORE the import so we know where the inserted block paragraphs begin (a saved
    cursor would drift to the end of the inserted content)."""
    idx = 0
    try:
        e = text_obj.createEnumeration()
    except Exception:
        return 0
    guard = 0
    while e.hasMoreElements() and guard < 200000:
        guard += 1
        try:
            el = e.nextElement()
        except Exception:
            break
        if not (hasattr(el, "supportsService") and el.supportsService("com.sun.star.text.Paragraph")):
            continue
        try:
            if text_obj.compareRegionStarts(el.getStart(), target) == 1:
                idx += 1
            else:
                break
        except Exception:
            break
    return idx



def _resolve_paragraph_style_token(model: Any, fam: Any, token: str):
    """Resolve an agent-facing compact ``data-lo-style`` token to a real UNO ParaStyleName.

    A candidate is any paragraph style whose name equals the token (covers space-free names
    and an agent that passed the exact spaced form) OR whose space-free form equals the token
    (``Heading1`` -> ``Heading 1``). Exactly one candidate -> use it. **More than one ->
    ambiguous** (e.g. a literal ``Heading1`` coexisting with built-in ``Heading 1``); fail safe
    to ``Standard`` instead of silently picking one. No candidate -> case-insensitive resolve,
    then ``Standard``. The ambiguity gate runs BEFORE any exact-name shortcut so a colliding
    token can never silently land on the wrong style (per docs/writer/html-style-model-plan.md)."""
    if fam is not None:
        candidates = []
        try:
            for name in fam.getElementNames():
                if name == token or xhtml_post.compact_lo_style_name(name) == token:
                    if name not in candidates:
                        candidates.append(name)
        except Exception:
            candidates = []
        if len(candidates) == 1:
            return candidates[0]
        if len(candidates) > 1:
            log.debug("data-lo-style: token %r is ambiguous across %r; falling back to Standard",
                      token, candidates)
            return "Standard"
    resolved = format_mod._resolve_style_name(model, token)
    if fam is None or fam.hasByName(resolved):
        return resolved
    return "Standard"



def _apply_block_lo_styles(model: Any, text_obj: Any, start_idx: int, styles: list[str | None]) -> None:
    """Apply each block's data-lo-style to the inserted paragraphs (positionally), starting at
    paragraph index *start_idx*. Reuses apply_paragraph_style_preserving_direct_char so the
    named style is applied first and the import's inline char overrides survive on top.
    Compact tokens (``Heading1``) are resolved to UNO names; unknown styles fall back to
    'Standard' (per the style-model plan)."""
    try:
        fam = model.getStyleFamilies().getByName("ParagraphStyles")
    except Exception:
        fam = None
    # Collect the target paragraphs (from start_idx, at most len(styles)) before mutating.
    paras = []
    try:
        e = text_obj.createEnumeration()
    except Exception:
        return
    i = 0
    guard = 0
    while e.hasMoreElements() and guard < 200000 and len(paras) < len(styles):
        guard += 1
        try:
            el = e.nextElement()
        except Exception:
            break
        if not (hasattr(el, "supportsService") and el.supportsService("com.sun.star.text.Paragraph")):
            continue
        if i >= start_idx:
            paras.append(el)
        i += 1
    for para_el, style in zip(paras, styles):
        if not style:
            continue
        resolved = _resolve_paragraph_style_token(model, fam, style)
        try:
            cur = text_obj.createTextCursorByRange(para_el.getStart())
            cur.gotoEndOfParagraph(True)
            format_mod.apply_paragraph_style_preserving_direct_char(model, cur, resolved)
        except Exception:
            log.debug("data-lo-style: failed to apply %r", style, exc_info=True)



def _wrap_html_fragment(html_content: str, extra_css: str | None = None):
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



def _ensure_html_linebreaks(content: str):
    """Convert newlines to ``<br>``/``<p>`` when content is plain text
    and the active format is HTML, so LO's filter preserves them.
    """
    if not isinstance(content, str) or not content:
        return content
    content = _normalize(content)
    unescaped = html_mod.unescape(content)
    # Vision/Docling export full documents; nesting another wrapper breaks StarWriter import.
    if re.search(r"<!DOCTYPE\s+html|<html[\s>]", unescaped, re.IGNORECASE):
        unescaped = format_mod._strip_html_boilerplate(unescaped)
    html_tags = ["<p>", "<br>", "<h1", "<h2", "<h3", "</ul>", "</li>", "</div>"]
    has_html = any(tag in unescaped.lower() for tag in html_tags)
    if has_html:
        return _wrap_html_fragment(unescaped)

    content = re.sub(r"\n{3,}", "\n\n", content)
    paras = content.split("\n\n")
    out = []
    for p in paras:
        if not p.strip():
            continue
        p_html = p.replace("\n", "<br>\n")
        out.append("<p>%s</p>" % p_html)
    return _wrap_html_fragment("\n".join(out))



def html_to_plain_text(html_string: str, ctx: Any, config_svc: Any = None):
    """Convert HTML to plain text by loading it into LibreOffice and reading
    the text out. Use this instead of regex stripping so entities, nested
    tags, and whitespace are handled correctly.
    """
    if not html_string or not isinstance(html_string, str):
        return (html_string or "").strip()
    # Base only: StarWriter would concatenate <rt> into the search string.
    stripped, _unused_ruby = extract_and_strip_ruby(html_string)
    prepared = _wrap_html_fragment(stripped.strip())
    temp_doc = None
    try:
        desktop = get_desktop(ctx)
        load_props = (format_mod.create_property_value("Hidden", True),)
        temp_doc = desktop.loadComponentFromURL("private:factory/swriter", "_default", 0, load_props)
        if not temp_doc or not hasattr(temp_doc, "getText"):
            return html_string.strip()
        with format_mod._with_temp_buffer(prepared, config_svc) as (_path, file_url):
            filter_name, _unused = format_mod._get_format_props(config_svc)
            filter_props = (format_mod.create_property_value("FilterName", filter_name),)
            text = temp_doc.getText()
            cursor = text.createTextCursor()
            cursor.gotoStart(False)
            cursor.insertDocumentFromURL(file_url, filter_props)
            cursor.gotoStart(False)
            cursor.gotoEnd(True)
            return cursor.getString().strip()
    except Exception:
        log.exception("html_to_plain_text failed")
        return html_string.strip()
    finally:
        if temp_doc is not None:
            try:
                temp_doc.close(True)
            except Exception:
                pass



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
    When *model* is provided, moves *cursor* to document end after import
    (needed for multi-segment Writer inserts).
    """
    prepared = _wrap_html_fragment(html_fragment, extra_css=extra_css) if wrap else html_fragment
    with format_mod._with_temp_buffer(prepared, config_svc) as (_path, file_url):
        filter_name, _unused = format_mod._get_format_props(config_svc)
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
    if model is not None:
        _cursor_goto_document_end(model, cursor)



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
    for seg in _segs:
        if seg.kind == "html":
            chunk = seg.text
            if not chunk:
                continue
            if not chunk.strip():
                model.getText().insertString(cursor, chunk, False)
                _cursor_goto_document_end(model, cursor)
                continue

            # Expand literal \n and \t for plain HTML without math
            chunk = chunk.replace("\\n", "\n").replace("\\t", "\t")

            sub = _ensure_html_linebreaks(chunk)
            _insert_starwriter_html_at_cursor(model, cursor, sub, config_svc=config_svc)
            continue
        if seg.kind == "tex":
            res = convert_latex_to_starmath(ctx, seg.text, display_block=seg.display_block)
        else:
            res = convert_mathml_to_starmath(ctx, seg.text)
        if res.ok and res.starmath and log.isEnabledFor(logging.DEBUG):
            log.debug("mixed_html_math: StarMath from converter nl=%d len=%d repr=%r", res.starmath.count("\n"), len(res.starmath), res.starmath[:500])
        if res.ok and res.starmath:
            insert_writer_math_formula(model, cursor, res.starmath, display_block=seg.display_block)
            _cursor_goto_document_end(model, cursor)
        else:
            snippet = (seg.text or "").replace("\n", " ")[:120]
            fallback = "[Math import failed] " + snippet
            model.getText().insertString(cursor, fallback, False)
            _cursor_goto_document_end(model, cursor)
            log.debug("math import failed: %s snippet=%r", res.error_message, snippet)



def _insert_mixed_or_plain_html(
    model: Any,
    ctx: Any,
    cursor: Any,
    unescaped_content: str,
    config_svc: Any = None,
    apply_styles: bool = True,
) -> None:
    """HTML import (optional MathML + TeX layer).

    data-lo-style paragraph styling is applied via UNO after the import only when *apply_styles*
    is True (target=full_document). For insert/replace targets it is False: the StarWriter import
    merges the first inserted block into the cursor's EXISTING paragraph, so applying the named
    style there would restyle the pre-existing text (corruption). On those paths we still strip
    data-lo-style (clean import) but do not apply it — styled writes go through full_document, or
    use apply_style to (re)style existing text. (Targeted styled inserts are a future follow-up.)
    """
    # Strip data-lo-style so the StarWriter import sees clean markup (it drops unknown attributes
    # anyway); we re-apply the named styles via UNO afterwards only when apply_styles is True.
    clean, block_styles = _extract_block_lo_styles(unescaped_content)
    # Strip <rt> so StarWriter does not concatenate reading into the body; paint RubyText after.
    clean, ruby_spans = extract_and_strip_ruby(clean)
    styled = apply_styles and any(block_styles)
    text_obj = cursor.getText()
    ruby_skip = _prefix_char_count(text_obj, cursor)
    # Index of the paragraph where the inserted block content begins (computed pre-import).
    # The first imported block MERGES into the paragraph that contains the cursor when the
    # cursor is not at a paragraph boundary (target=end/search/selection). So count paragraphs
    # strictly before the cursor's *paragraph* (not the cursor position) — otherwise the applied
    # styles shift by one. (For full_document the cursor is already at the paragraph start.)
    start_idx = 0
    if styled:
        ref = cursor.getStart()
        try:
            para_cur = text_obj.createTextCursorByRange(cursor.getStart())
            para_cur.gotoStartOfParagraph(False)
            ref = para_cur.getStart()
        except Exception:
            pass
        start_idx = _count_preceding_paras(text_obj, ref)

    if html_fragment_contains_mixed_math(clean):
        _insert_mixed_html_and_math_at_cursor(model, ctx, cursor, clean, config_svc=config_svc)
    else:
        # Expand literal \n and \t for plain HTML without math
        expanded = clean.replace("\\n", "\n").replace("\\t", "\t")
        single = _ensure_html_linebreaks(expanded)
        if not styled:
            _insert_starwriter_html_at_cursor(model, cursor, single, config_svc=config_svc)
            _apply_ruby_spans(text_obj, ruby_spans, skip_chars=ruby_skip)
            return
        # model=None: keep the cursor at the end of the inserted content.
        insert_html_fragment_at_cursor(cursor, single, wrap=False, config_svc=config_svc, model=None)

    if styled:
        try:
            _apply_block_lo_styles(model, text_obj, start_idx, block_styles)
        except Exception:
            log.debug("data-lo-style application failed", exc_info=True)
        _cursor_goto_document_end(model, cursor)
    _apply_ruby_spans(text_obj, ruby_spans, skip_chars=ruby_skip)



def insert_html_at_cursor(
    model: Any,
    ctx: Any,
    cursor: Any,
    unescaped_content: str,
    config_svc: Any = None,
    apply_styles: bool = True,
) -> None:
    """Insert HTML or plain text at *cursor* (public API for tools)."""
    _insert_mixed_or_plain_html(model, ctx, cursor, unescaped_content, config_svc=config_svc, apply_styles=apply_styles)




def _uno_service_true(obj: Any, service: str) -> bool:
    """``supportsService`` may return True/1; ignore MagicMock without a side_effect."""
    try:
        val = obj.supportsService(service)
    except Exception:
        return False
    if val is True or val == 1:
        return True
    if val is False or val == 0 or val is None:
        return False
    # unittest.mock.MagicMock is truthy but not a real UNO answer
    if type(val).__module__.startswith("unittest.mock"):
        return False
    return bool(val)


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
        pass
    if _uno_service_true(obj, "com.sun.star.drawing.Shape"):
        return True
    if _uno_service_true(obj, "com.sun.star.drawing.CustomShape"):
        return True
    return False


def insert_content_at_position(model: Any, ctx: Any, content: str, position: str, config_svc: Any = None) -> None:
    """Insert formatted content at *position* (``'beginning'``,
    ``'end'``, or ``'selection'``) using ``insertDocumentFromURL``.
    """
    content = html_mod.unescape(content)

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
        def _doc_end_cursor():
            c = text.createTextCursor()
            c.gotoEnd(False)
            return c

        controller = None
        try:
            controller = model.getCurrentController()
        except Exception:
            controller = None

        rng = None
        try:
            sel = controller.getSelection() if controller else None
            if sel and hasattr(sel, "getCount"):
                try:
                    if int(sel.getCount()) > 0:
                        rng = sel.getByIndex(0)
                except Exception:
                    rng = None
        except Exception:
            rng = None

        if rng is not None and _selection_is_draw_shape(rng):
            log.debug(
                "insert_content_at_position: selection is Draw/Writer shape; "
                "falling back to view/document text cursor"
            )
            rng = None

        if rng is None and controller is not None:
            try:
                vc = controller.getViewCursor()
                if vc is not None and not _selection_is_draw_shape(vc):
                    rng = vc
            except Exception:
                rng = None

        if rng is None:
            cursor = _doc_end_cursor()
        else:
            try:
                cursor = rng.getText().createTextCursorByRange(rng.getStart())
                if not hasattr(cursor, "insertDocumentFromURL"):
                    raise AttributeError("insertDocumentFromURL")
                # Clear only real text selections (never shapes).
                if hasattr(rng, "setString") and not _selection_is_draw_shape(rng):
                    from plugin.writer.specialized.tables import raise_if_range_hosts_nested_table

                    raise_if_range_hosts_nested_table(rng)
                    rng.setString("")
            except Exception as e:
                log.debug(
                    "insert_content_at_position: text selection unusable (%s); using document end",
                    e,
                )
                cursor = _doc_end_cursor()
    else:
        raise ToolExecutionError("Unknown position: %s" % position)

    # apply_styles=False: inserting next to existing text merges the first block into it, so the
    # named style would restyle that text. Styled paragraph writes go through full_document.
    _insert_mixed_or_plain_html(model, ctx, cursor, content, config_svc=config_svc, apply_styles=False)



def replace_full_document(model: Any, ctx: Any, content: str, config_svc: Any = None) -> None:
    """Clear the document and insert *content*."""
    content = html_mod.unescape(content)

    text = model.getText()
    cursor = text.createTextCursor()
    cursor.gotoStart(False)
    cursor.gotoEnd(True)
    with format_mod._deletion_author():  # author the deletion distinctly (split by-author coloring)
        cursor.setString("")
    cursor.gotoStart(False)
    _insert_mixed_or_plain_html(model, ctx, cursor, content, config_svc=config_svc)



def _is_recording_changes(model: Any):
    """True if *model* is currently recording Track Changes (redlines).

    Agent edits made while recording must land as a clean tracked Delete + Insert so the
    user can accept (-> new text) or reject (-> old text) each one. The format-preserving
    replace paths (the char-by-char diff in ``replace_preserving_format`` and the
    paragraph-style restore below) corrupt that into a per-character mess or a FORMAT
    redline that keeps the old text on Accept -- so those steps are skipped while recording.
    """
    try:
        return bool(model.getPropertyValue("RecordChanges"))
    except Exception:
        return False



def replace_single_range_with_content(
    model: Any, text_range: Any, content: str, ctx: Any, config_svc: Any = None
) -> None:
    """Replace the given text range with rendered *content* (HTML path).

    FOLLOW-UP: cursor uses ``text_range.getText()`` but HTML import still calls
    ``_cursor_goto_document_end`` (body) in places — markup search-replace inside
    table cells / nested ``XText`` can raise the same RuntimeException as the
    plain-text bug fixed in ``replace_preserving_format``.
    """
    prepared = html_mod.unescape(content)
    text_obj = text_range.getText()

    # Detect a table-cell target up front (the cursor's TextTable property is set inside a cell).
    # Block/rich HTML import into a cell's nested XText can raise an empty RuntimeException (see the
    # FOLLOW-UP above); we use this to turn that opaque failure into a clear, actionable message.
    in_table_cell = False
    try:
        in_table_cell = text_obj.createTextCursorByRange(text_range.getStart()).getPropertyValue("TextTable") is not None
    except Exception:
        in_table_cell = False

    # Preserve the target paragraph style for INLINE replacements. The StarWriter
    # HTML import resets the paragraph to a default body style, silently demoting
    # headings (e.g. "Heading 3" -> "Text body"). For inline-only content (no
    # block-level tags, no math), insert without jumping the cursor to the document
    # end so we can reapply the original paragraph style across the inserted range.
    inline_preserve = not _content_has_block_markup(prepared) and not html_fragment_contains_mixed_math(prepared)
    saved_style = None
    if inline_preserve:
        try:
            saved_style = text_obj.createTextCursorByRange(
                text_range.getStart()).getPropertyValue("ParaStyleName")
        except Exception:
            saved_style = None

    cursor = text_obj.createTextCursorByRange(text_range)
    from plugin.writer.specialized.tables import raise_if_range_hosts_nested_table

    # setString on a host cell wipes nested TextTables — same refuse as table_set_cell.
    raise_if_range_hosts_nested_table(text_range)
    with format_mod._deletion_author():  # author the deletion distinctly (split by-author coloring)
        cursor.setString("")

    if saved_style is not None:
        anchor = text_obj.createTextCursorByRange(cursor.getStart())
        # Insert the inline fragment RAW (do not route through _ensure_html_linebreaks:
        # it does not recognise <span> as HTML and would wrap it in <p>, creating an
        # extra body paragraph). model=None leaves the cursor at the end of the
        # INSERTED content (not the document end), so [anchor, cursor] bounds it.
        inline_html = prepared.replace("\\n", "\n").replace("\\t", "\t")
        inline_html, ruby_spans = extract_and_strip_ruby(inline_html)
        insert_html_fragment_at_cursor(cursor, inline_html, wrap=False, config_svc=config_svc, model=None)
        _apply_ruby_spans(text_obj, ruby_spans, skip_chars=_prefix_char_count(text_obj, anchor))
        # Re-apply the saved paragraph style (the HTML import can demote Heading -> body).
        # Skip it while Track Changes is recording: setString("") above leaves the old text in
        # place as a tracked DELETE, and re-applying a paragraph style across [anchor, cursor]
        # spans that struck text, converting its DELETE redline into a FORMAT redline -- so
        # accepting the change would keep BOTH the old and new text. The inline import does not
        # demote the style here, so skipping the restore keeps a clean Delete + Insert pair.
        if not _is_recording_changes(model):
            try:
                restore = text_obj.createTextCursorByRange(anchor.getStart())
                restore.gotoRange(cursor.getEnd(), True)
                format_mod.apply_paragraph_style_preserving_direct_char(model, restore, saved_style)
            except Exception:
                log.debug("replace_single_range_with_content: could not restore ParaStyleName", exc_info=True)
    else:
        # apply_styles=False: a search/replace splits the matched paragraph, so applying a
        # data-lo-style here would restyle the surrounding text. Styled writes use full_document.
        try:
            _insert_mixed_or_plain_html(model, ctx, cursor, prepared, config_svc=config_svc, apply_styles=False)
        except Exception as e:
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



# XHTML export of Writer fields (body and copied header XText) is a titled
# span, e.g. ``<span title="page-number"/>``. StarWriter HTML import drops
# those spans (probed: no TextField after insert). Swap in a token the
# filter keeps, import, then replace the token with a real field.
#
# LO's XHTML filter (filter/source/xslt/odf2xhtml/export/xhtml/body.xsl)
# sets ``title`` to ``local-name()`` of the ODF field element. Full list
# LO can emit (kept here so widening the map is a one-place edit):
#   author-initials, author-name, chapter, character-count, creation-date,
#   creation-time, creator, date, description, editing-cycles,
#   editing-duration, file-name, image-count, initial-creator, keywords,
#   modification-date, modification-time, object-count, page-continuation,
#   page-count, page-number, paragraph-count, print-date, print-time,
#   printed-by, sender-city, sender-company, sender-country, sender-email,
#   sender-fax, sender-firstname, sender-initials, sender-lastname,
#   sender-phone-private, sender-phone-work, sender-position,
#   sender-postal-code, sender-state-or-province, sender-street,
#   sender-title, sheet-name, subject, table-count, time, title,
#   user-defined, word-count
#
# Restore only a letterhead-useful subset for now (page/date plus chapter,
# author, file name, doc title/subject). Extending ``_EXPORTED_FIELD_TITLES``
# / ``_FIELD_TITLE_TO_SERVICE`` is intentional and easy when a real bug
# arrives. GetReference / cross-ref is a separate loss class (different
# markup, not this XSLT ``title=`` path).
_FIELD_PLACEHOLDER_FMT = "[[WA-FIELD:%s]]"
_EXPORTED_FIELD_TITLES = (
    "page-number",
    "page-count",
    "time",
    "date",
    "chapter",
    "author-name",
    "author-initials",
    "file-name",
    "title",
    "subject",
)
# Built from the tuple so the span regex and restore loop stay in sync.
_FIELD_SPAN_RE = re.compile(
    r'<span\b(?=[^>]*\btitle\s*=\s*["\'](%s)["\'])'
    r'(?:[^>]*/>|[^>]*>.*?</span>)' % "|".join(_EXPORTED_FIELD_TITLES),
    re.IGNORECASE | re.DOTALL,
)
_FIELD_TITLE_TO_SERVICE = {
    "page-number": "com.sun.star.text.textfield.PageNumber",
    "page-count": "com.sun.star.text.textfield.PageCount",
    "time": "com.sun.star.text.textfield.DateTime",
    "date": "com.sun.star.text.textfield.DateTime",
    "chapter": "com.sun.star.text.textfield.Chapter",
    "author-name": "com.sun.star.text.textfield.Author",
    "author-initials": "com.sun.star.text.textfield.Author",
    "file-name": "com.sun.star.text.textfield.FileName",
    "title": "com.sun.star.text.textfield.docinfo.Title",
    "subject": "com.sun.star.text.textfield.docinfo.Subject",
}


def rewrite_exported_field_spans(html: str):
    """Replace XHTML field spans with placeholders the HTML import will keep."""
    if not html or "title=" not in html:
        return html

    def _repl(match: re.Match[str]):
        return _FIELD_PLACEHOLDER_FMT % match.group(1).lower()

    return _FIELD_SPAN_RE.sub(_repl, html)


def _insert_restored_field(model: Any, text_range: Any, title: str):
    service = _FIELD_TITLE_TO_SERVICE.get(title)
    if not service:
        return False
    try:
        field = model.createInstance(service)
    except Exception:
        return False
    if title == "page-number":
        try:
            from com.sun.star.text.PageNumberType import CURRENT

            field.setPropertyValue("PageNumberType", CURRENT)
        except Exception:
            pass
        try:
            field.setPropertyValue("NumberingType", 4)  # Arabic
        except Exception:
            pass
    elif title == "date":
        try:
            field.setPropertyValue("IsDate", True)
        except Exception:
            pass
    elif title == "time":
        try:
            field.setPropertyValue("IsDate", False)
        except Exception:
            pass
    elif title == "author-name":
        # Same Author service as initials; FullName selects the display form.
        try:
            field.setPropertyValue("FullName", True)
        except Exception:
            pass
    elif title == "author-initials":
        try:
            field.setPropertyValue("FullName", False)
        except Exception:
            pass
    try:
        text = text_range.getText()
        cursor = text.createTextCursorByRange(text_range)
        cursor.setString("")
        text.insertTextContent(cursor, field, False)
        return True
    except Exception:
        log.debug("_insert_restored_field failed title=%s", title, exc_info=True)
        return False


def _restore_field_placeholders(model: Any, text_obj: Any = None):
    """Turn ``[[WA-FIELD:…]]`` tokens back into UNO fields.

    Uses document ``findFirst`` (same reach as body search: headers included).
    When *text_obj* is set, only matches in that ``XText`` are replaced.
    """
    if model is None or not hasattr(model, "createSearchDescriptor"):
        return 0
    restored = 0
    for title in _EXPORTED_FIELD_TITLES:
        needle = _FIELD_PLACEHOLDER_FMT % title
        try:
            sd = model.createSearchDescriptor()
            sd.SearchString = needle
            sd.SearchRegularExpression = False
            found = model.findFirst(sd)
        except Exception:
            continue
        while found is not None:
            in_region = True
            if text_obj is not None:
                try:
                    in_region = uno_same(found.getText(), text_obj)
                except Exception:
                    in_region = True
            nxt = None
            try:
                nxt = model.findNext(found.getEnd(), sd)
            except Exception:
                nxt = None
            if in_region and _insert_restored_field(model, found, title):
                restored += 1
            found = nxt
    return restored


def replace_xtext_with_html(text_obj: Any, html: str, config_svc: Any = None, model: Any = None) -> None:
    """Clear *text_obj* and import *html* via the shared StarWriter path.

    Field spans from ``document_to_content`` / ``xtext_to_content`` are
    restored as live fields after import. *model* is the owning document
    (needed to create fields and to find placeholders). Do not pass it
    through to ``insert_html_fragment_at_cursor`` — that helper would
    then jump the cursor to the *body* end.
    """
    if text_obj is None:
        raise ToolExecutionError("No text object to import into.")
    expanded = html if isinstance(html, str) else ("" if html is None else str(html))
    expanded = expanded.replace("\\n", "\n").replace("\\t", "\t")
    expanded, ruby_spans = extract_and_strip_ruby(expanded)
    rewritten = rewrite_exported_field_spans(expanded)
    prepared = _ensure_html_linebreaks(rewritten)
    cursor = text_obj.createTextCursor()
    cursor.gotoStart(False)
    cursor.gotoEnd(True)
    cursor.setString("")
    cursor.gotoStart(False)
    insert_html_fragment_at_cursor(
        cursor, prepared, wrap=False, config_svc=config_svc, model=None,
    )
    _apply_ruby_spans(text_obj, ruby_spans)
    if model is not None:
        _restore_field_placeholders(model, text_obj)


def content_has_markup(content: str):
    """Return ``True`` if *content* appears to contain Markdown or HTML."""
    if not content or not isinstance(content, str):
        return False
    lower = content.lower()
    return any(p.lower() in lower for p in _MARKUP_PATTERNS)



def _content_has_block_markup(content: str):
    """Return ``True`` if *content* contains block-level HTML (paragraph-defining)."""
    if not content or not isinstance(content, str):
        return False
    lower = content.lower()
    return any(p in lower for p in _BLOCK_MARKUP_PATTERNS)



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
    from plugin.writer.specialized.tables import raise_if_range_hosts_nested_table

    raise_if_range_hosts_nested_table(target_range)
    text = target_range.getText()
    old_text = _normalize(target_range.getString())
    new_text = _normalize(new_text)

    # Track Changes: the char-by-char diff below records a separate redline for EACH changed
    # character, which renders as a scrambled, un-reviewable mess (old and new text interleaved).
    # When recording, replace the whole range in one shot so the edit is a single tracked
    # Delete + Insert the user can accept (-> new text) or reject (-> old text) cleanly.
    if _is_recording_changes(model):
        if new_text == old_text:
            return  # no-op: don't record a spurious tracked Delete+Insert (keeps the change count honest)
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

    # Optional toolkit for UI responsiveness.
    toolkit = None
    if ctx:
        try:
            toolkit = ctx.getServiceManager().createInstanceWithContext("com.sun.star.awt.Toolkit", ctx)
        except Exception:
            pass

    # Process overlapping characters one by one.
    # setString on a selected character preserves the range's formatting.
    main_cursor = text.createTextCursorByRange(target_range.getStart())

    for i in range(overlap):
        if i > 0 and i % 500 == 0 and toolkit:
            try:
                toolkit.processEvents()
            except Exception:
                toolkit = None

        # Create a selection for exactly one character to check/replace.
        sel = text.createTextCursorByRange(main_cursor)
        if not sel.goRight(1, True):
            break

        if new_text[i] != old_text[i]:
            sel.setString(new_text[i])

        # Explicitly move main_cursor to the end of the character just processed.
        # This is more robust than goRight(1) because setString() can affect
        # the cursor's logical position in some environments.
        main_cursor.gotoRange(sel.getEnd(), False)

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




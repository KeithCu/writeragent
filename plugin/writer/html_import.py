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

import contextlib
import html as html_mod
import logging
import re
import uuid
from html.parser import HTMLParser
from typing import TYPE_CHECKING, Any, ClassVar, NamedTuple

if TYPE_CHECKING:
    from collections.abc import Iterator

from plugin.framework.errors import best_effort
from plugin.doc.text_helpers import normalize_linebreaks as _normalize
from plugin.framework.errors import ToolExecutionError, reraise_if_disposed, uno_read
from plugin.framework.uno_context import uno_same
from . import xhtml_style_postprocess as xhtml_post
from .math.html_math_segment import html_fragment_contains_mixed_math, segment_html_with_mixed_math
from .math.math_mml_convert import convert_latex_to_starmath, convert_mathml_to_starmath, insert_writer_math_formula

log = logging.getLogger("writeragent.writer")
# uno_read default where None is a real UNO result.
_FAILED: Any = object()


# StarWriter HTML import has no ruby rule: <ruby>漢字<rt>かんじ</rt></ruby> becomes
# concatenated body text. Strip <rt> before import, then set RubyText on the base.
_RUBY_BLOCK_RE = re.compile(r"<ruby\b([^>]*)>(.*?)</ruby>", re.IGNORECASE | re.DOTALL)
_RT_RE = re.compile(r"<rt\b[^>]*>(.*?)</rt>", re.IGNORECASE | re.DOTALL)
_RP_RE = re.compile(r"<rp\b[^>]*>.*?</rp>", re.IGNORECASE | re.DOTALL)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_GO_RIGHT_CHUNK = 8192


def _visible_html_text(fragment: str) -> str:
    """Tag-stripped, entity-unescaped text of an HTML fragment."""
    if not fragment:
        return ""
    return html_mod.unescape(_HTML_TAG_RE.sub("", fragment))


def extract_and_strip_ruby(html: str) -> tuple[str, list[tuple[str, str, bool, int]]]:
    """Replace ``<ruby>`` with its base and return ``(clean_html, spans)``.

    Each span is ``(base, reading, is_above, visible_offset)`` in document order.
    ``visible_offset`` is the base's index in the tag-stripped, entity-decoded
    text of *html* (readings omitted). StarWriter concatenates ruby children,
    so the import must see base only; ``RubyText`` is painted afterwards via
    ``_apply_ruby_spans``, which starts at this offset instead of the first
    copy of the base.
    """
    if not html or not isinstance(html, str) or "<ruby" not in html.lower():
        return html, []
    spans: list[tuple[str, str, bool, int]] = []
    out: list[str] = []
    visible_len = 0
    pos = 0
    for match in _RUBY_BLOCK_RE.finditer(html):
        before = html[pos:match.start()]
        out.append(before)
        # Offset in the text StarWriter will keep, not in the HTML source.
        # An earlier identical base (plain 漢字 before <ruby>漢字</ruby>) must
        # not receive this reading.
        visible_len += len(_visible_html_text(before))
        attrs = match.group(1) or ""
        inner = match.group(2) or ""
        rt_m = _RT_RE.search(inner)
        reading = _visible_html_text(rt_m.group(1) if rt_m else "").strip()
        base_html = _RP_RE.sub("", inner)
        base_html = _RT_RE.sub("", base_html)
        base = _visible_html_text(base_html)
        is_above = "under" not in attrs.lower()
        if base and reading:
            spans.append((base, reading, is_above, visible_len))
        out.append(base_html)
        visible_len += len(base)
        pos = match.end()
    out.append(html[pos:])
    return "".join(out), spans


def _go_right(cursor: Any, n: int, expand: bool) -> bool:
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
    # RubyIsAbove and RubyAdjust are on the same cursor that just took RubyText.
    cursor.setPropertyValue("RubyIsAbove", bool(is_above))
    cursor.setPropertyValue("RubyAdjust", 0)


def _prefix_char_count(text_obj: Any, cursor: Any) -> int:
    """``getString()`` length from the start of *text_obj* to *cursor*.

    Captured before import so we can apply ruby only to the inserted suffix.
    A live ``cursor.getStart()`` drifts when ``insertDocumentFromURL`` moves it.
    """
    # Returning 0 painted ruby at the start of the text and bound the reading
    # to an earlier copy of the same base. A closed document must raise.
    prefix = text_obj.createTextCursor()
    prefix.gotoStart(False)
    prefix.gotoRange(cursor.getStart(), True)
    return len(prefix.getString() or "")


def _ruby_skip(text_obj: Any, cursor: Any, spans: list[Any]) -> int:
    """Character count before *cursor* when *spans* is non-empty, else 0.

    Building the prefix cursor and calling ``getString()`` reads the entire
    text before the insert point, which is wasted when there is no ruby.
    """
    if not spans or text_obj is None or cursor is None:
        return 0
    return _prefix_char_count(text_obj, cursor)


def _apply_ruby_spans(text_obj: Any, spans: list[Any], skip_chars: int = 0) -> None:
    """Set ``RubyText`` on each *spans* base, ignoring the first *skip_chars*.

    After a StarWriter import the document has base characters only. Offsets
    come from ``getString()`` then ``goRight`` — they agree on plain imported
    body text (no fields/ruby yet).
    """
    if not spans or text_obj is None:
        return
    def read_imported() -> tuple[Any, str]:
        origin = text_obj.getStart()
        hay = text_obj.createTextCursorByRange(origin)
        hay.gotoEnd(True)
        return origin, hay.getString() or ""

    got = best_effort(read_imported, "_apply_ruby_spans: could not read imported text")
    if got is None:
        return
    origin, haystack = got
    # skip_chars is the imported suffix. Offsets from extract_and_strip_ruby
    # are relative to that suffix, not to earlier document text.
    anchor = skip_chars if skip_chars > 0 else 0
    pos = anchor

    cur: Any = None
    cur_pos = 0

    for item in spans:
        base, reading, is_above = item[0], item[1], item[2] if len(item) > 2 else True
        if not base or not reading:
            continue

        # The span's fourth item is the base's visible-text offset, so a repeated
        # base (漢字 … <ruby>漢字</ruby>) gets the reading on the right copy.
        offset = item[3] if len(item) > 3 and isinstance(item[3], int) and item[3] >= 0 else None
        target_offset = (anchor + offset) if offset is not None else None

        idx = -1
        if target_offset is not None and target_offset >= pos and target_offset + len(base) <= len(haystack) and haystack[target_offset : target_offset + len(base)] == base:
            idx = target_offset
        else:
            if target_offset is not None:
                log.debug("_apply_ruby_spans: base %r not at offset %d, falling back to find", base, target_offset)
            idx = haystack.find(base, pos)

        if idx < 0:
            log.debug("_apply_ruby_spans: base %r not found in imported text", base)
            continue

        try:
            if cur is None:
                cur = text_obj.createTextCursorByRange(origin)
                cur_pos = 0

            delta = idx - cur_pos
            if delta < 0:
                cur = text_obj.createTextCursorByRange(origin)
                cur_pos = 0
                delta = idx

            if delta > 0 and not _go_right(cur, delta, False):
                cur = None
                continue
            cur_pos = idx

            sel = text_obj.createTextCursorByRange(cur)
            if not _go_right(sel, len(base), True):
                cur = None
                continue

            if sel.getString() != base:
                resync = 0
                while sel.getString() != base and resync < 50 and _go_right(cur, 1, False):
                    cur_pos += 1
                    resync += 1
                    sel = text_obj.createTextCursorByRange(cur)
                    if not _go_right(sel, len(base), True):
                        break

            if sel.getString() != base:
                log.debug(
                    "_apply_ruby_spans: offset mismatch want %r got %r",
                    base, sel.getString(),
                )
                _go_right(cur, len(base), False)
                cur_pos = idx + len(base)
                pos = cur_pos
                continue

            _set_ruby_on_range(sel, reading, is_above)
            _go_right(cur, len(base), False)
            cur_pos = cur_pos + len(base)
            pos = idx + len(base)
        except Exception:
            log.warning("_apply_ruby_spans: failed for %r", base, exc_info=True)
            cur = None
            pos = idx + len(base)


_BLOCK_MARKUP_PATTERNS = [
    "<p>", "<p ", "<h1", "<h2", "<h3", "<h4", "<h5", "<h6",
    "<div", "<ul", "<ol", "<li", "<table", "<tr", "<td", "<th",
    "<blockquote", "<pre", "<hr", "<section", "<article",
]


def _strip_data_lo_style(start_tag: str) -> str:
    """Remove any data-lo-style attribute from a single start-tag string."""
    start_tag = re.sub(r'\s+data-lo-style="[^"]*"', "", start_tag)
    return re.sub(r"\s+data-lo-style='[^']*'", "", start_tag)



class _BlockLoStyleExtractor(HTMLParser):
    """Collect data-lo-style per TOP-LEVEL block (document order) and strip the attribute
    from the HTML, so the StarWriter import sees clean markup and we apply the named styles
    ourselves afterwards. Content inside <table> is left to the import (avoids order desync)."""

    _table_depth: int

    # Paragraph-level blocks cannot contain blocks: a new one closes the open one (<p>a<p>b).
    _PARA_LEVEL: ClassVar[frozenset[str]] = frozenset({"p", "pre", "h1", "h2", "h3", "h4", "h5", "h6"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self._table_depth = 0
        self.styles: list[str | None] = []
        self._out: list[str] = []
        # Open block-level elements: [tag, first child block already shared this slot?], or
        # [tag, None] for a transparent <div> (no slot of its own).
        self._open: list[list[Any]] = []

    @staticmethod
    def _style_of(attrs: list[tuple[str, str | None]]) -> str | None:
        val = None
        for k, v in attrs:
            if k == "data-lo-style":
                val = v
        return val

    def _is_block(self, tag: str, attrs: list[tuple[str, str | None]]) -> bool:
        # A <div> is a transparent container -- except the one the read path gives a
        # data-lo-style: LibreOffice exports a paragraph holding a frame (an image) as
        # <div class="paragraph-X">, and that div is one Writer paragraph on import too.
        return tag in xhtml_post.BLOCK_TAGS or (tag == "div" and self._style_of(attrs) is not None)

    def _slot(self, val: str | None) -> None:
        # <blockquote><p> and <li><p> take two slots but make ONE Writer
        # paragraph, so every later style lands one paragraph late ("2. DO
        # DIREITO" loses its heading, a list item becomes a quote). A
        # block's first child block is that same paragraph -- it shares
        # the slot, and its own style wins when it has one.
        parent = self._open[-1] if self._open else None
        if parent is not None and parent[1] is False and self.styles:
            parent[1] = True
            if val is not None:
                self.styles[-1] = val
        else:
            self.styles.append(val)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        raw = self.get_starttag_text() or ("<%s>" % tag)
        t = tag.lower()
        if t == "table":
            self._table_depth += 1
            self._out.append(raw)
            return
        if self._table_depth or (t not in xhtml_post.BLOCK_TAGS and t != "div"):
            # Inline tags, and blocks inside a table (their data-lo-style is left for the import to
            # ignore -- v1 doesn't apply cell styles), stay verbatim.
            self._out.append(raw)
            return
        if not self._is_block(t, attrs):
            self._open.append([t, None])  # transparent div
            self._out.append(raw)
            return
        while self._open and self._open[-1][0] in self._PARA_LEVEL:
            self._open.pop()
        self._slot(self._style_of(attrs))
        self._open.append([t, False])
        self._out.append(_strip_data_lo_style(raw))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        raw = self.get_starttag_text() or ("<%s/>" % tag)
        t = tag.lower()
        if self._table_depth == 0 and self._is_block(t, attrs):
            self._slot(self._style_of(attrs))
            self._out.append(_strip_data_lo_style(raw))
        else:
            self._out.append(raw)

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t == "table" and self._table_depth > 0:
            self._table_depth -= 1
        elif self._table_depth == 0 and (t in xhtml_post.BLOCK_TAGS or t == "div"):
            # Pop up to and including the matching open element (tolerates an omitted </p>).
            for idx in range(len(self._open) - 1, -1, -1):
                if self._open[idx][0] == t:
                    del self._open[idx:]
                    break
        self._out.append("</%s>" % tag)

    def handle_data(self, data: str) -> None:
        self._out.append(data)

    def handle_entityref(self, name: str) -> None:
        self._out.append("&%s;" % name)

    def handle_charref(self, name: str) -> None:
        self._out.append("&#%s;" % name)

    def handle_comment(self, data: str) -> None:
        self._out.append("<!--%s-->" % data)

    def result(self) -> tuple[str, list[str | None]]:
        return "".join(self._out), self.styles



def _extract_block_lo_styles(html: str) -> tuple[str, list[str | None]]:
    """Return ``(clean_html, [data_lo_style_or_None per top-level block])``.

    Short-circuits (returns the html unchanged, no styles) when there is no data-lo-style,
    so existing callers are completely unaffected."""
    if not html or "data-lo-style" not in html:
        return html, []
    ex = _BlockLoStyleExtractor()
    ex.feed(html)
    ex.close()
    return ex.result()



def _resolve_paragraph_style_token(
    model: Any,
    fam: Any,
    token: str,
    element_names: list[str] | tuple[str, ...] | None = None,
) -> str:
    """Resolve an agent-facing compact ``data-lo-style`` token to a real UNO ParaStyleName.

    A candidate is any paragraph style whose name equals the token (covers space-free names
    and an agent that passed the exact spaced form) OR whose space-free form equals the token
    (``Heading1`` -> ``Heading 1``). Exactly one candidate -> use it. **More than one ->
    ambiguous** (e.g. a literal ``Heading1`` coexisting with built-in ``Heading 1``); fail safe
    to ``Standard`` instead of silently picking one. No candidate -> case-insensitive resolve,
    then ``Standard``. The ambiguity gate runs BEFORE any exact-name shortcut so a colliding
    token can never silently land on the wrong style (per docs/writer/html-style-model-plan.md)."""
    from . import format as format_mod
    names = element_names
    if names is None and fam is not None:
        names = uno_read(lambda: list(fam.getElementNames()), [])
    if names:
        # dict.fromkeys keeps first-seen order and drops duplicates.
        candidates: list[str] = list(
            dict.fromkeys(
                n for n in names
                if n == token or xhtml_post.compact_lo_style_name(n) == token
            )
        )
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



def _apply_block_lo_styles(model: Any, text_obj: Any, start: Any, styles: list[str | None], end: Any = None) -> None:
    """Apply each block's data-lo-style to the inserted paragraphs (positionally).

    Forward cursor walk from the insertion point avoids scanning the whole document.
    Compact tokens (``Heading1``) are resolved to UNO names; unknown styles fall back to
    'Standard' (per the style-model plan)."""
    from . import format as format_mod

    fam = uno_read(lambda: model.getStyleFamilies().getByName("ParagraphStyles"), None)
    element_names: list[str] = uno_read(lambda: list(fam.getElementNames()), []) if fam is not None else []
    token_cache: dict[str, str] = {}

    try:
        start_range = start.getStart() if hasattr(start, "getStart") else start
        walk_cur = text_obj.createTextCursorByRange(start_range)
        walk_cur.gotoStartOfParagraph(False)
    except Exception as exc:
        reraise_if_disposed(exc)
        return

    style_cache: dict[Any, Any] = {}
    for style in styles:
        # Stop at the first paragraph that does not start before the parked end cursor.
        try:
            if end is not None and text_obj.compareRegionStarts(walk_cur.getStart(), end) != 1:
                break
        except Exception:
            break

        if style:
            resolved = token_cache.get(style)
            if resolved is None:
                resolved = _resolve_paragraph_style_token(model, fam, style, element_names=element_names)
                token_cache[style] = resolved
            try:
                cur = text_obj.createTextCursorByRange(walk_cur.getStart())
                cur.gotoEndOfParagraph(True)
                format_mod.apply_paragraph_style_preserving_direct_char(
                    model, cur, resolved, style_cache=style_cache,
                )
            except Exception as exc:
                reraise_if_disposed(exc)
                log.debug("data-lo-style: failed to apply %r", style, exc_info=True)

        try:
            if not walk_cur.gotoNextParagraph(False):
                break
        except Exception:
            break



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
    content = _normalize(content)
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

    stripped, _unused_ruby = extract_and_strip_ruby(html_string)
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



# --- images that come back without their data ------------------------------------------------
# get_document_content leaves image data out (include_images=false), so a picture comes back as
# its frame's wrapper -- <div ... id="Name"> for paragraph/character/page anchors, <span ...
# id="Name"> inside a line -- around <img src="">, and a floating frame is followed by a
# clear:both <div>. Imported as is, the picture was lost: the edit deleted the original with the
# old text and the import made nothing (a signature vanished on "Accept all"), and the clear:both
# <div> became an extra empty paragraph that shifted the next data-lo-style by one.
# A picture inside a heading comes back with a <p> between the wrapper and the <img>
# (<span id="Name"><p><img src=""/></p></span>); without that optional <p> the pattern missed
# it and the whole edit was refused as "an image without its data".
_IMAGE_PLACEHOLDER_RE = re.compile(
    r'<(div|span)\b[^>]*\bid="([^"]+)"[^>]*>\s*(?:<p\b[^>]*>\s*)?<img\b[^>]*\bsrc=""[^>]*>\s*(?:</p>\s*)?</\1>'
    r'(?:\s*<div style="clear:both;[^"]*">(?:\s|&nbsp;|\xa0)*</div>)?',
    re.IGNORECASE)
_EMPTY_IMG_RE = re.compile(r'<img\b[^>]*\bsrc=""', re.IGNORECASE)

# Frame properties copied onto the restored picture (read with try: not all exist on every frame).
_KEPT_IMAGE_PROPS = (
    "HoriOrient", "HoriOrientPosition", "HoriOrientRelation", "VertOrient", "VertOrientPosition",
    "VertOrientRelation", "TextWrap", "SurroundContour", "LeftMargin", "RightMargin", "TopMargin",
    "BottomMargin", "Title", "Description", "GraphicCrop", "Opaque",
)


class _KeptImage(NamedTuple):
    marker: str
    name: str
    original: Any
    graphic: Any
    anchor: Any
    size: Any
    props: dict[str, Any]


def _swap_image_placeholders(model: Any, content: str) -> tuple[str, list[_KeptImage]]:
    """Swap each data-less image wrapper that names a picture of *model* for a text marker.

    Runs BEFORE the edit deletes anything: the picture is read now, while it still exists (with
    review off, deleting its paragraph disposes of it). Raises ToolExecutionError -- nothing
    changed yet -- when an image has no data and names no picture here, instead of losing it.
    """
    if not content or 'src=""' not in content:
        return content, []
    graphics = uno_read(lambda: model.getGraphicObjects(), None)
    kept: list[_KeptImage] = []
    missing: list[str] = []
    nonce = uuid.uuid4().hex[:8]

    def _swap(match: re.Match[str]) -> str:
        name = html_mod.unescape(match.group(2))
        obj = uno_read(lambda: graphics.getByName(name) if graphics is not None and graphics.hasByName(name) else None, None)
        if obj is None:
            missing.append(name)
            return match.group(0)
        props: dict[str, Any] = {}
        for prop in _KEPT_IMAGE_PROPS:
            try:
                props[prop] = obj.getPropertyValue(prop)
            except Exception:
                pass
        marker = "wa%simg%d" % (nonce, len(kept))
        kept.append(_KeptImage(marker, name, obj, obj.getPropertyValue("Graphic"),
                               obj.getPropertyValue("AnchorType"), obj.getSize(), props))
        return marker

    swapped = _IMAGE_PLACEHOLDER_RE.sub(_swap, content)
    if missing or _EMPTY_IMG_RE.search(swapped):
        named = (" It names %s, which this document does not have." % ", ".join(repr(n) for n in missing)
                 if missing else "")
        raise ToolExecutionError(
            "The content has an image without its data (src=\"\"), so it would be lost.%s Keep the "
            "image's wrapper exactly as get_document_content returned it (<div ... id=\"Name\"> or "
            "<span ... id=\"Name\">) so the tool puts that picture back, leave the image out to "
            "delete it, or read with include_images=true to copy it from another document." % named)
    return swapped, kept


def _deleted_by_the_edit(model: Any, original: Any) -> bool:
    """True when the edit removed *original*: disposed (review off), or its anchor -- for a
    paragraph anchor, the paragraph's break -- inside a pending tracked deletion (review on).
    Anything else is still in place and must not be duplicated, e.g. a search replace of the
    text of the paragraph a picture is anchored to."""
    try:
        anchor = original.getAnchor()
    except Exception:
        return True
    if not _is_recording_changes(model):
        return False
    point = anchor
    if getattr(original.getPropertyValue("AnchorType"), "value", "") == "AT_PARAGRAPH":
        # A paragraph-anchored picture goes only when its paragraph goes: when the break that
        # ends it is deleted. A search replace deletes the paragraph's text, not the paragraph.
        try:
            point = anchor.getText().createTextCursorByRange(anchor.getStart())
            point.gotoEndOfParagraph(False)
            if not point.goRight(1, True):
                return False  # the last paragraph has no break: it stays
        except Exception:
            return False
    try:
        redlines = model.getRedlines()
        total = int(redlines.getCount())
        enum = redlines.createEnumeration()
    except Exception:
        return False
    for _unused in range(total):
        if enum.hasMoreElements() is not True:
            break
        redline = enum.nextElement()
        try:
            if redline.getPropertyValue("RedlineType") != "Delete":
                continue
            start = redline.getPropertyValue("RedlineStart")
            end = redline.getPropertyValue("RedlineEnd")
            text = start.getText()
            # compareRegionStarts(a, b): 1 when a starts before b, 0 when equal.
            if text.compareRegionStarts(start, point) >= 0 and text.compareRegionEnds(point, end) >= 0:
                return True
        except Exception:
            continue  # another text object, or not comparable
    return False


def _restore_image_placeholders(model: Any, kept: list[_KeptImage]) -> None:
    """Put a copy of each kept picture where its marker landed, and remove the marker."""
    for image in kept:
        search = model.createSearchDescriptor()
        search.SearchString = image.marker
        search.SearchCaseSensitive = True
        found = model.findFirst(search)
        if found is None and hasattr(model, "getTextTables"):
            try:
                tables = model.getTextTables()
                for tbl_name in tables.getElementNames():
                    tbl = tables.getByName(tbl_name)
                    for cell_name in tbl.getCellNames():
                        cell = tbl.getCellByName(cell_name)
                        if hasattr(cell, "createSearchDescriptor"):
                            cell_found = cell.findFirst(search)
                            if cell_found is not None:
                                found = cell_found
                                break
                    if found is not None:
                        break
            except Exception:
                pass
        if found is None:
            if getattr(image.anchor, "value", "") != "AT_PAGE" and _deleted_by_the_edit(model, image.original):
                raise ToolExecutionError(f"Kept image {image.name!r} marker not found after import")
            log.warning("kept image %r: marker not found after the import", image.name)
            continue
        text = found.getText()
        if getattr(image.anchor, "value", "") == "AT_PAGE" or not _deleted_by_the_edit(model, image.original):
            # The original is still where it was (page-anchored, or outside what the edit
            # replaced): drop the marker instead of adding a duplicate.
            found.setString("")
            continue
        picture = model.createInstance("com.sun.star.text.TextGraphicObject")
        picture.Graphic = image.graphic
        picture.AnchorType = image.anchor
        picture.setSize(image.size)
        # Known LibreOffice limit (checked live, recorded or not): with review on, a picture in a
        # cell of a table this edit inserts keeps "Reject all" from removing that table. Accept is
        # right; Reject leaves the new table behind. Without the picture it would be lost instead.
        text.insertTextContent(found, picture, True)  # True: the marker text is replaced
        for prop, value in image.props.items():
            try:
                picture.setPropertyValue(prop, value)
            except Exception:
                log.debug("kept image %r: could not restore %s", image.name, prop, exc_info=True)
        with contextlib.suppress(Exception):
            picture.setName(image.name)  # taken while the original is a pending deletion


@contextlib.contextmanager
def _keep_placeholder_images(model: Any, content: str) -> Iterator[str]:
    """Yield *content* with data-less images swapped for markers; put the pictures back after."""
    swapped, kept = _swap_image_placeholders(model, content)
    yield swapped
    if kept:
        _restore_image_placeholders(model, kept)


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
    prepared = _wrap_html_fragment(html_fragment, extra_css=extra_css) if wrap else html_fragment
    tail = _parked_cursor(cursor) if model is not None else None
    from . import format as format_mod
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

            chunk, seg_ruby = extract_and_strip_ruby(chunk)

            # Expand literal \n and \t for plain HTML without math
            chunk = chunk.replace("\\n", "\n").replace("\\t", "\t")

            sub = _ensure_html_linebreaks(chunk)
            seg_ruby_skip = _ruby_skip(cursor.getText(), cursor, seg_ruby)
            _insert_starwriter_html_at_cursor(model, cursor, sub, config_svc=config_svc)
            if seg_ruby:
                _apply_ruby_spans(cursor.getText(), seg_ruby, skip_chars=seg_ruby_skip)
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
    # Strip data-lo-style so the StarWriter import sees clean markup (it drops unknown attributes
    # anyway); we re-apply the named styles via UNO afterwards only when apply_styles is True.
    clean, block_styles = _extract_block_lo_styles(unescaped_content)
    has_math = html_fragment_contains_mixed_math(clean)
    if not has_math:
        # Strip <rt> so StarWriter does not concatenate reading into the body; paint RubyText after.
        clean, ruby_spans = extract_and_strip_ruby(clean)
    else:
        ruby_spans = []
    styled = apply_styles and any(block_styles)
    text_obj = cursor.getText()
    ruby_skip = _ruby_skip(text_obj, cursor, ruby_spans)
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
        # Per-paragraph failures are already logged inside _apply_block_lo_styles.
        # This wrapper swallowed compareRegionStarts, so a closed document
        # still looked like a finished insert.
        if has_math:
            start = _paragraph_after(text_obj, before_para)
        else:
            start = _insert_start_from_anchor(text_obj, start_anchor, cursor)
        _apply_block_lo_styles(model, text_obj, start, block_styles, end=imported_end)
        _cursor_goto_document_end(model, cursor)
    if not has_math:
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
    with _keep_placeholder_images(model, unescaped_content) as content:
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
        pass
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
    ``_keep_placeholder_images``)."""
    with _keep_placeholder_images(model, content) as swapped:
        _insert_inline_at_cursor(model, ctx, cursor, swapped, config_svc)


def _insert_inline_at_cursor(model: Any, ctx: Any, cursor: Any, content: str, config_svc: Any = None) -> None:
    """Insert inline *content* at a collapsed *cursor* without splitting its paragraph.

    ``insert_html_at_cursor`` routes plain text through ``_ensure_html_linebreaks``, which
    wraps it in ``<p>``: imported mid-paragraph, that ``<p>`` splits the host paragraph in
    two, so inserting "bem " before a word left "... bem" / "inteiro." as two paragraphs.
    Plain text goes in with ``insertString``; inline markup is imported RAW (no ``<p>``),
    the same way ``replace_single_range_with_content`` handles inline replacements.
    """
    # No html.unescape here: content.py already decoded complete character references on the
    # plain path (_ENTITY_RE), and a full unescape turns "&sect 2o" into "§ 2o"; the HTML import
    # decodes entities in markup itself.
    if not content_has_markup(content):
        cursor.getText().insertString(cursor, content, False)
        return
    inline_html = content.replace("\\n", "\n").replace("\\t", "\t")
    # Same <rt> handling as the other import paths: StarWriter would glue the reading onto the base.
    inline_html, ruby_spans = extract_and_strip_ruby(inline_html)
    # The HTML import drops whitespace at the edges of a fragment, so "<b>muito</b> " lost
    # its trailing space and fused with the next word. Put edge whitespace in as text.
    core = inline_html.strip()
    lead = inline_html[: len(inline_html) - len(inline_html.lstrip())]
    trail = inline_html[len(inline_html.rstrip()):]
    text = cursor.getText()
    ruby_skip = _ruby_skip(text, cursor, ruby_spans)
    if lead:
        text.insertString(cursor, lead, False)
    if trail:
        # The fragment import does not leave the cursor after the imported text, so the
        # trailing whitespace goes in first and the fragment is imported just before it.
        text.insertString(cursor, trail, False)
        cursor.goLeft(len(trail), False)
    if core:
        insert_html_fragment_at_cursor(cursor, core, wrap=False, config_svc=config_svc, model=None)
    _apply_ruby_spans(text, ruby_spans, skip_chars=ruby_skip)


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
    with _keep_placeholder_images(model, content) as swapped:
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
                    from plugin.writer.specialized.tables import raise_if_range_hosts_nested_table

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
    with _keep_placeholder_images(model, content) as swapped:
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
    if not _is_recording_changes(model):
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
        leftover.setString("")



def _is_recording_changes(model: Any) -> bool:
    """True if *model* is currently recording Track Changes (redlines).

    Agent edits made while recording must land as a clean tracked Delete + Insert so the
    user can accept (-> new text) or reject (-> old text) each one. The format-preserving
    replace paths (the char-by-char diff in ``replace_preserving_format`` and the
    paragraph-style restore below) corrupt that into a per-character mess or a FORMAT
    redline that keeps the old text on Accept -- so those steps are skipped while recording.
    """
    # RecordChanges does not fail on a live document. Returning False hid a
    # closed document and took the review-off path.
    return bool(model.getPropertyValue("RecordChanges"))



def replace_single_range_with_content(
    model: Any, text_range: Any, content: str, ctx: Any, config_svc: Any = None
) -> None:
    """Replace the given text range with rendered *content* (HTML path)."""
    with _keep_placeholder_images(model, content) as swapped:
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
    from plugin.writer.specialized.tables import raise_if_range_hosts_nested_table

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
        inline_html = prepared.replace("\\n", "\n").replace("\\t", "\t")
        inline_html, ruby_spans = extract_and_strip_ruby(inline_html)
        ruby_skip = _ruby_skip(text_obj, anchor, ruby_spans)
        insert_html_fragment_at_cursor(cursor, inline_html, wrap=False, config_svc=config_svc, model=None)
        _apply_ruby_spans(text_obj, ruby_spans, skip_chars=ruby_skip)
        # Re-apply the saved paragraph style (the HTML import can demote Heading -> body).
        # Skip it while Track Changes is recording: setString("") above leaves the old text in
        # place as a tracked DELETE, and re-applying a paragraph style across [anchor, cursor]
        # spans that struck text, converting its DELETE redline into a FORMAT redline -- so
        # accepting the change would keep BOTH the old and new text. The inline import does not
        # demote the style here, so skipping the restore keeps a clean Delete + Insert pair.
        if not _is_recording_changes(model):
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
_FIELD_TOKEN_RE = re.compile(
    r"\[\[WA-FIELD:([a-z-]+)(?:\|([^\]]+))?\]\]",
    re.IGNORECASE,
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

_PAGE_NUMBER_SUBTYPE_VALS: dict[str, str] = {
    "PREV": "PREV",
    "CURRENT": "CURRENT",
    "NEXT": "NEXT",
    "0": "PREV",
    "1": "CURRENT",
    "2": "NEXT",
}


def rewrite_exported_field_spans(html: str) -> str:
    """Replace XHTML field spans with placeholders the HTML import will keep."""
    if not html or "title=" not in html:
        return html
    from . import format as format_mod

    def _repl(match: re.Match[str]) -> str:
        title = match.group(1).lower()
        span_html = match.group(0)
        props = []
        if title == "page-number":
            subtype_m = re.search(r'data-subtype=["\']([^"\']+)["\']', span_html)
            offset_m = re.search(r'data-offset=["\'](-?\d+)["\']', span_html)
            num_m = re.search(r'data-numbering-type=["\']([^"\']+)["\']', span_html)
            if subtype_m:
                st_raw = subtype_m.group(1).strip().upper()
                st_name = _PAGE_NUMBER_SUBTYPE_VALS.get(st_raw)
                if st_name is None:
                    log.warning("Unknown PageNumber SubType attribute %r, defaulting to CURRENT", subtype_m.group(1))
                    st_name = "CURRENT"
                if st_name != "CURRENT":
                    props.append(f"subtype={st_name}")
            if offset_m:
                # The group is (-?\d+), so int() cannot fail.
                off_val = int(offset_m.group(1))
                if off_val != 0:
                    props.append(f"offset={off_val}")
            if num_m:
                nt_raw = num_m.group(1).strip().upper()
                if nt_raw in format_mod.PAGE_NUMBER_NUMBERING_TYPE_VALS:
                    nt_name = nt_raw
                elif nt_raw.isdigit() and int(nt_raw) in format_mod.PAGE_NUMBER_NUMBERING_TYPE_NAMES:
                    nt_name = format_mod.PAGE_NUMBER_NUMBERING_TYPE_NAMES[int(nt_raw)]
                else:
                    log.warning("Unknown PageNumber NumberingType attribute %r, defaulting to ARABIC", num_m.group(1))
                    nt_name = "ARABIC"
                if nt_name != "ARABIC":
                    props.append(f"numbering_type={nt_name}")

        prop_str = ("|" + "|".join(props)) if props else ""
        return f"[[WA-FIELD:{title}{prop_str}]]"

    return _FIELD_SPAN_RE.sub(_repl, html)


def _insert_restored_field(model: Any, text_range: Any, title: str, props: dict[str, str] | None = None) -> bool:
    service = _FIELD_TITLE_TO_SERVICE.get(title)
    if not service:
        raise ToolExecutionError("Could not restore the %s field." % title)
    try:
        field = model.createInstance(service)
    except Exception as e:
        reraise_if_disposed(e)
        log.debug("_insert_restored_field createInstance failed for %s: %s", service, e)
        raise ToolExecutionError("Could not restore the %s field." % title) from e
    props = props or {}
    if title == "page-number":
        from . import format as format_mod
        try:
            subtype_str = props.get("subtype", "CURRENT").strip().upper()
            st_name = _PAGE_NUMBER_SUBTYPE_VALS.get(subtype_str)
            if st_name is None:
                log.warning("Unknown PageNumber SubType: %r, defaulting to CURRENT", props.get("subtype"))
                st_name = "CURRENT"
            import uno
            # SubType accepts only this enum. An int is refused, and uno is
            # always importable inside LibreOffice.
            field.setPropertyValue(
                "SubType", uno.Enum("com.sun.star.text.PageNumberType", st_name),
            )
        except Exception as e:
            log.debug("_insert_restored_field failed to set SubType: %s", e)
        try:
            offset_val = int(props.get("offset", "0"))
            field.setPropertyValue("Offset", offset_val)
        except Exception as e:
            log.debug("_insert_restored_field failed to set Offset: %s", e)
        try:
            num_type_str = props.get("numbering_type", "ARABIC").strip().upper()
            if num_type_str in format_mod.PAGE_NUMBER_NUMBERING_TYPE_VALS:
                nt_val = format_mod.PAGE_NUMBER_NUMBERING_TYPE_VALS[num_type_str]
            elif num_type_str.isdigit() and int(num_type_str) in format_mod.PAGE_NUMBER_NUMBERING_TYPE_NAMES:
                nt_val = int(num_type_str)
            else:
                log.warning("Unknown PageNumber NumberingType: %r, defaulting to ARABIC", props.get("numbering_type"))
                nt_val = 4
            field.setPropertyValue("NumberingType", nt_val)
        except Exception as e:
            log.debug("_insert_restored_field failed to set NumberingType: %s", e)
    # DateTime.IsDate and Author.FullName are on the service just created.
    # A swallowed failure inserted the other form and still returned True.
    elif title == "date":
        field.setPropertyValue("IsDate", True)
    elif title == "time":
        field.setPropertyValue("IsDate", False)
    elif title == "author-name":
        # Same Author service as initials; FullName selects the display form.
        field.setPropertyValue("FullName", True)
    elif title == "author-initials":
        field.setPropertyValue("FullName", False)
    try:
        text = text_range.getText()
        cursor = text.createTextCursorByRange(text_range)
        placeholder = cursor.getString() or ""
        cursor.setString("")
        try:
            text.insertTextContent(cursor, field, False)
        except Exception:
            # insertTextContent failed after the token was cleared. page_set
            # does not undo the header text, so put the placeholder back.
            try:
                cursor.setString(placeholder)
            except Exception:
                log.exception(
                    "_insert_restored_field: insert failed and the %s placeholder could not be restored",
                    title,
                )
            raise
        return True
    except Exception as e:
        reraise_if_disposed(e)
        log.debug("_insert_restored_field failed title=%s", title, exc_info=True)
        raise ToolExecutionError("Could not restore the %s field." % title) from e


def _cursor_over(text_obj: Any, offset: int, needle: str) -> Any | None:
    """Cursor selecting *needle* at cursor step *offset* of *text_obj*, or None."""
    cursor = text_obj.createTextCursor()
    cursor.gotoStart(False)
    if offset and cursor.goRight(offset, False) is False:
        return None
    if cursor.goRight(len(needle), True) is False:
        return None
    if cursor.getString() != needle:
        return None
    return cursor


def _select_placeholder(text_obj: Any, needle: str) -> Any | None:
    """Cursor covering the first plain-text *needle* in *text_obj*.

    A document ``findFirst`` does not see a header or footer on a page style
    that no page uses. That is the footer of a page style just created by
    ``style_clone``. Walking portions addresses that text directly. A text
    field counts as one cursor step (probe A'); ``getString()`` expands it,
    so the index has to come from the portion walk, not from ``getString()``.
    """
    enum = uno_read(lambda: text_obj.createEnumeration(), _FAILED)
    if enum is _FAILED:
        return None
    steps = 0
    paras = 0
    while enum.hasMoreElements() is True and paras < 5000:
        paras += 1
        try:
            para = enum.nextElement()
        except Exception:
            break
        if paras > 1:
            # Why-comment: the break is a cursor step and is not in portion text.
            steps += 1
        try:
            portions = para.createEnumeration()
        except Exception:
            continue
        seen = 0
        while portions.hasMoreElements() is True and seen < 5000:
            seen += 1
            try:
                portion = portions.nextElement()
                kind = portion.getPropertyValue("TextPortionType")
            except Exception:
                break
            if kind == "TextField":
                steps += 1
                continue
            chunk = uno_read(lambda: portion.getString() or "", "")
            if not isinstance(chunk, str):
                chunk = str(chunk)
            pos = chunk.find(needle)
            if pos < 0:
                steps += len(chunk)
                continue
            return uno_read(lambda: _cursor_over(text_obj, steps + pos, needle), None)
    return None


def _parse_field_token_props(props_str: str | None) -> dict[str, str]:
    props = {}
    if props_str:
        for p in props_str.split('|'):
            if '=' in p:
                k, v = p.split('=', 1)
                props[k] = v
    return props

def _restore_fields_by_cursor(model: Any, text_obj: Any) -> int:
    """Replace ``[[WA-FIELD:…]]`` tokens inside *text_obj* without document search."""
    restored = 0
    for _unused in range(50):
        try:
            plain = text_obj.getString() or ""
        except Exception as e:
            log.debug("_restore_fields_by_cursor getString failed: %s", e)
            return restored
        if "[[WA-FIELD:" not in plain:
            return restored

        match = _FIELD_TOKEN_RE.search(plain)
        if not match:
            return restored

        token = match.group(0)
        title = match.group(1).lower()
        props = _parse_field_token_props(match.group(2))

        cursor = _select_placeholder(text_obj, token)
        if cursor is None:
            return restored

        # A False return used to stop here and leave [[WA-FIELD:…]] in the
        # region while replace_xtext_with_html still returned.
        if props:
            _insert_restored_field(model, cursor, title, props)
        else:
            _insert_restored_field(model, cursor, title)
        restored += 1
    return restored


def _restore_field_placeholders(model: Any, text_obj: Any = None) -> int:
    """Turn ``[[WA-FIELD:…]]`` tokens back into UNO fields.

    When *text_obj* is set, walk that XText first. ``findFirst`` misses a
    header/footer on a page style no page is using (a cloned page style).
    The document search remains the path for body text, and the fallback
    when the cursor walk cannot see the token.
    """
    walked = 0
    if text_obj is not None:
        walked = _restore_fields_by_cursor(model, text_obj)
        plain = uno_read(lambda: text_obj.getString() or "", _FAILED)
        if plain is not _FAILED and "[[WA-FIELD:" not in plain:
            return walked
    if model is None or not hasattr(model, "createSearchDescriptor"):
        if text_obj is not None:
            plain = uno_read(lambda: text_obj.getString() or "", "")
            if "[[WA-FIELD:" in plain:
                match = _FIELD_TOKEN_RE.search(plain)
                tok = match.group(0) if match else "[[WA-FIELD:...]]"
                raise ToolExecutionError(
                    f"The header/footer region was imported, but field placeholder {tok} remains visible in the document."
                )
        return walked
    restored = walked

    # createSearchDescriptor, findFirst, getString, and findNext do not fail
    # on a live model. Swallowing them left [[WA-FIELD:…]] in the header and
    # the write still returned.
    sd = model.createSearchDescriptor()
    sd.SearchRegularExpression = True

    for title in _EXPORTED_FIELD_TITLES:
        sd.SearchString = r"\[\[WA-FIELD:" + title + r"(?:\|[^\]]+)?\]\]"
        found = model.findFirst(sd)

        while found is not None:
            in_region = True
            if text_obj is not None:
                # A failed getText is not "in this region". uno_same stays.
                in_region = uno_same(found.getText(), text_obj)

            props: dict[str, str] = {}
            field_title = title
            token = found.getString()
            if isinstance(token, str):
                match = _FIELD_TOKEN_RE.match(token)
                if match:
                    field_title = match.group(1).lower()
                    props = _parse_field_token_props(match.group(2))

            # Capture the next hit before the insert replaces this range.
            nxt = model.findNext(found.getEnd(), sd)

            if in_region:
                if props:
                    _insert_restored_field(model, found, field_title, props)
                else:
                    _insert_restored_field(model, found, field_title)
                restored += 1

            found = nxt

    if text_obj is not None:
        plain = uno_read(lambda: text_obj.getString() or "", "")
        if "[[WA-FIELD:" in plain:
            match = _FIELD_TOKEN_RE.search(plain)
            tok = match.group(0) if match else "[[WA-FIELD:...]]"
            raise ToolExecutionError(
                f"The header/footer region was imported, but field placeholder {tok} remains visible in the document."
            )

    return restored


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
    expanded = html if isinstance(html, str) else ("" if html is None else str(html))
    expanded = expanded.replace("\\n", "\n").replace("\\t", "\t")
    expanded, ruby_spans = extract_and_strip_ruby(expanded)
    rewritten = rewrite_exported_field_spans(expanded)
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
        except Exception:
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
        _apply_ruby_spans(text_obj, ruby_spans)
        if model is not None:
            _restore_field_placeholders(model, text_obj)
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
                try:
                    titles = mgr.getAllUndoActionTitles()
                    if titles and titles[0] == title:
                        mgr.undo()
                        undone = True
                except Exception:
                    log.exception("replace_xtext_with_html: undo failed")
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


def content_has_markup(content: str | None) -> bool:
    """Return ``True`` if *content* appears to contain HTML or math markup."""
    if not content or not isinstance(content, str):
        return False
    from plugin.framework.html_stripper import text_has_real_html_tag

    if text_has_real_html_tag(content):
        return True
    return "$$" in content or r"\(" in content or r"\[" in content or "<math" in content.lower()



def _content_has_block_markup(content: str | None) -> bool:
    """Return ``True`` if *content* contains block-level HTML (paragraph-defining)."""
    if not content or not isinstance(content, str):
        return False
    lower = content.lower()
    return any(p in lower for p in _BLOCK_MARKUP_PATTERNS)



def _collect_range_portions(target_range: Any) -> tuple[bool, list[tuple[str, int]] | None]:
    """Return (has_special, portions_list_or_None)."""
    try:
        if not hasattr(target_range, "createEnumeration"):
            return False, None
        enum = target_range.createEnumeration()
        if enum is None:
            return False, None
        portions: list[tuple[str, int]] = []
        while enum.hasMoreElements():
            elem = enum.nextElement()
            if hasattr(elem, "createEnumeration"):
                penum = elem.createEnumeration()
                while penum.hasMoreElements():
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
    except Exception:
        return False, None


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

    if new_text == old_text:
        return

    # Track Changes: the char-by-char diff below records a separate redline for EACH changed
    # character, which renders as a scrambled, un-reviewable mess (old and new text interleaved).
    # When recording, replace the whole range in one shot so the edit is a single tracked
    # Delete + Insert the user can accept (-> new text) or reject (-> old text) cleanly.
    if _is_recording_changes(model):
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
        for i in range(overlap):
            sel = text.createTextCursorByRange(main_cursor)
            if not sel.goRight(1, True):
                raise ToolExecutionError(
                    "Could not replace that text in place (the selection did not advance)."
                )
            if new_text[i] != old_text[i]:
                sel.setString(new_text[i])
            main_cursor.gotoRange(sel.getEnd(), False)
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
                if not _go_right(main_cursor, slice_len, False):
                    raise ToolExecutionError(
                        "Could not replace that text in place (the selection did not advance)."
                    )
                continue

            p = 0
            while p < slice_len and old_slice[p] == new_slice[p]:
                p += 1
            if p > 0:
                if not _go_right(main_cursor, p, False):
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
                if not _go_right(sel, diff_len, True):
                    raise ToolExecutionError(
                        "Could not replace that text in place (the selection did not advance)."
                    )
                sel.setString(new_slice[p : slice_len - s])
                main_cursor.gotoRange(sel.getEnd(), False)

            if s > 0:
                if not _go_right(main_cursor, s, False):
                    raise ToolExecutionError(
                        "Could not replace that text in place (the selection did not advance)."
                    )

        if offset < overlap:
            for i in range(offset, overlap):
                sel = text.createTextCursorByRange(main_cursor)
                if not sel.goRight(1, True):
                    raise ToolExecutionError(
                        "Could not replace that text in place (the selection did not advance)."
                    )
                if new_text[i] != old_text[i]:
                    sel.setString(new_text[i])
                main_cursor.gotoRange(sel.getEnd(), False)
    else:
        i = 0
        while i < overlap:
            if new_text[i] == old_text[i]:
                run_start = i
                while i < overlap and new_text[i] == old_text[i]:
                    i += 1
                k = i - run_start
                if not _go_right(main_cursor, k, False):
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




"""CJK ruby (furigana) helpers for HTML export and import.

Extracted from plugin.writer.html_export and plugin.writer.html_import.
"""

# pyright: reportUnusedFunction=false

from __future__ import annotations

from html import escape as html_escape
import html as html_mod
import logging
import re
from typing import Any, ClassVar, Iterator, NamedTuple

from plugin.framework.errors import best_effort, reraise_if_disposed, uno_read
from plugin.writer.html_export import _iter_xtext_paragraphs

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


class _RubySpan:
    """One CJK ruby (furigana) pair collected from Writer portions.

    ``start`` / ``end`` are offsets in the paragraph's visible text (base only;
    ruby marks have empty ``getString()``). Used to keep range export from
    wrapping a base that was trimmed out of the copied window.
    """

    base: str
    reading: str
    start: int
    end: int
    __slots__: ClassVar[tuple[str, ...]] = ("base", "reading", "start", "end")

    def __init__(self, base: str, reading: str, start: int = 0, end: int = 0) -> None:
        self.base = base
        self.reading = reading
        self.start = start
        self.end = end



def _ruby_parts(span: Any) -> tuple[str, str]:
    """``(base, reading)`` from a ``_RubySpan`` or a 2-tuple (unit tests)."""
    if isinstance(span, (tuple, list)):
        return span[0], span[1]
    return span.base, span.reading



def _inside_ruby_element(html: str, index: int) -> bool:
    """True when *index* sits inside an existing ``<ruby>…</ruby>``."""
    last_open = html.rfind("<ruby", 0, index)
    if last_open < 0:
        return False
    last_close = html.rfind("</ruby>", 0, index)
    return last_close < last_open



def _index_in_tag(html: str, index: int) -> bool:
    """True when *index* is inside ``<...>`` (attribute text is not body text)."""
    last_lt = html.rfind("<", 0, index)
    if last_lt < 0:
        return False
    last_gt = html.rfind(">", 0, index)
    return last_gt < last_lt



def _find_contiguous(html: str, needle: str, start: int = 0) -> tuple[int, int] | None:
    """Offsets of *needle* as a raw substring that is not inside a tag."""
    if not html or not needle:
        return None
    idx = start
    while True:
        found = html.find(needle, idx)
        if found < 0:
            return None
        if not _index_in_tag(html, found):
            return found, found + len(needle)
        idx = found + 1



def _only_tags_or_ws(fragment: str) -> bool:
    """True when *fragment* has no body text (tags and whitespace only)."""
    i = 0
    n = len(fragment)
    while i < n:
        ch = fragment[i]
        if ch.isspace():
            i += 1
            continue
        if ch != "<":
            return False
        close = fragment.find(">", i)
        if close < 0:
            return False
        i = close + 1
    return True



def _ruby_markup(base: str, reading: str) -> str:
    return "<ruby>%s<rt>%s</rt></ruby>" % (html_escape(base), html_escape(reading))



def inject_ruby_into_html(html: str, spans: list[Any]) -> str:
    """Rewrite glued or dropped ruby encodings to semantic ``<ruby><rt>``.

    The XHTML Writer filter has no ``text:ruby`` rule, so full export concatenates
    base + reading (``漢字かんじです``). Range/portion-copy never paints ``RubyText``
    — that property lives on the empty Ruby *start* mark, not the Text run —
    so range export used to drop the reading. Both become
    ``<ruby>漢字<rt>かんじ</rt></ruby>です``. Apply recreates portions in
    ``html_import`` (strip ``<rt>``, then ``RubyText`` on the base run).

    Replacements stay inside a single text node (or wrap the base node and
    delete a following reading node). Crossing a tag with a wrapper used to
    emit ``<span><ruby>…</span>…`` — browsers recover, but it is not HTML we
    want the agent to copy.
    """
    if not html or not spans:
        return html
    pos = 0
    parts = []
    for span in spans:
        base, reading = _ruby_parts(span)
        if not base or not reading:
            continue
        glued = _find_contiguous(html, base + reading, pos)
        if glued is not None and not _inside_ruby_element(html, glued[0]):
            start, end = glued
            parts.append(html[pos:start])
            parts.append(_ruby_markup(base, reading))
            pos = end
            continue
        base_at = _find_contiguous(html, base, pos)
        if base_at is None or _inside_ruby_element(html, base_at[0]):
            continue
        start, end = base_at
        reading_at = _find_contiguous(html, reading, end)
        if (
            reading_at is not None
            and _only_tags_or_ws(html[end:reading_at[0]])
            and not _inside_ruby_element(html, reading_at[0])
        ):
            # Bold/span around the base: keep those tags, drop the glued reading.
            parts.append(html[pos:start])
            parts.append(_ruby_markup(base, reading))
            parts.append(html[end:reading_at[0]])
            pos = reading_at[1]
            continue
        parts.append(html[pos:start])
        parts.append(_ruby_markup(base, reading))
        pos = end
    parts.append(html[pos:])
    return "".join(parts)



def _iter_ruby_spans_in_paragraph(para: Any) -> Iterator[_RubySpan]:
    """Yield ``_RubySpan`` for each Ruby start/end pair in *para*.

    Writer stores ruby as ``TextPortionType="Ruby"`` marks: the start has
    ``RubyText`` / ``RubyIsAbove`` / ``RubyAdjust``; the base is a normal
    ``Text`` run; the end mark is empty. ``getString()`` on the paragraph is
    base only — reading is not a text field.
    """
    portion_enum = uno_read(lambda: para.createEnumeration(), _FAILED)
    if portion_enum is _FAILED:
        return
    in_delete = False
    in_ruby = False
    reading = ""
    base_parts: list[str] = []
    base_start = 0
    offset = 0
    while portion_enum.hasMoreElements() is True:
        try:
            portion = portion_enum.nextElement()
            kind = portion.getPropertyValue("TextPortionType")
        except Exception:
            break
        if kind == "Redline":
            try:
                if str(portion.getPropertyValue("RedlineType")) == "Delete":
                    in_delete = not in_delete
            except Exception as exc:
                log.debug("_iter_ruby_spans: RedlineType unreadable: %s", exc)
            continue
        if in_delete:
            continue
        if kind == "Ruby":
            if not in_ruby:
                reading = uno_read(lambda: str(portion.getPropertyValue("RubyText") or ""), "")
                in_ruby = True
                base_parts = []
                base_start = offset
            else:
                base = "".join(base_parts)
                if base and reading:
                    yield _RubySpan(base, reading, base_start, base_start + len(base))
                in_ruby = False
                reading = ""
                base_parts = []
            continue
        chunk = uno_read(lambda: portion.getString() or "", "")
        if not chunk:
            continue
        if in_ruby:
            base_parts.append(chunk)
        offset += len(chunk)



def iter_ruby_spans(text_obj: Any) -> Iterator[_RubySpan]:
    """Document-order ruby pairs under *text_obj* (body or cell), including tables."""
    for para in _iter_xtext_paragraphs(text_obj):
        yield from _iter_ruby_spans_in_paragraph(para)



def _ruby_spans_in_window(para: Any, trim_start: int, trim_end: int) -> list[_RubySpan]:
    """Ruby pairs whose entire base lies inside the copied visible-text window."""
    out = []
    for span in _iter_ruby_spans_in_paragraph(para):
        if span.start >= trim_start and span.end <= trim_end:
            out.append(span)
    return out



def _inject_ruby_from_model(model: Any, content: str, fodt_has_ruby: bool | None = None) -> str:
    """Walk the live model and rewrite *content* when ruby is (or may be) present.

    *fodt_has_ruby* is True/False from the paired FODT sidecar, or None when
    that export failed. False skips the portion walk on the common no-ruby path.
    """
    if not content:
        return content
    if fodt_has_ruby is False:
        return content
    spans = best_effort(lambda: list(iter_ruby_spans(model.getText())), "_inject_ruby_from_model: portion walk failed")
    if spans is None:
        return content
    return inject_ruby_into_html(content, spans)



class _ImportRubySpan(NamedTuple):
    base: str
    reading: str
    is_above: bool
    visible_offset: int



def _visible_html_text(fragment: str) -> str:
    """Tag-stripped, entity-unescaped text of an HTML fragment."""
    if not fragment:
        return ""
    return html_mod.unescape(_HTML_TAG_RE.sub("", fragment))



def extract_and_strip_ruby(html: str) -> tuple[str, list[_ImportRubySpan]]:
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
    spans: list[_ImportRubySpan] = []
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
            spans.append(_ImportRubySpan(base, reading, is_above, visible_len))
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

    got = uno_read(read_imported, None)
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
        base, reading = item[0], item[1]
        is_above = bool(item[2])
        if not base or not reading:
            continue

        # The span's fourth item is the base's visible-text offset, so a repeated
        # base (漢字 … <ruby>漢字</ruby>) gets the reading on the right copy.
        offset = item[3]
        target_offset = (anchor + offset) if offset is not None and offset >= 0 else None

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
        except Exception as exc:
            reraise_if_disposed(exc)
            log.warning("_apply_ruby_spans: failed for %r", base, exc_info=True)
            cur = None
            pos = idx + len(base)

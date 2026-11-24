"""CJK ruby (furigana) helpers for HTML export and import.

Extracted from plugin.writer.html_export and plugin.writer.html_import.
"""

from __future__ import annotations

from html import escape, unescape
import logging
import re
from typing import Any, Iterator, NamedTuple, Sequence

from plugin.doc.text_helpers import (
    FAILED,
    DeletionState,
    TrackedDeletions,
    iter_portions,
    iter_xtext_paragraphs,
)
from plugin.framework.errors import best_effort, uno_read

log = logging.getLogger("writeragent.writer")

# StarWriter HTML import has no ruby rule: <ruby>漢字<rt>かんじ</rt></ruby> becomes
# concatenated body text. Swap <ruby> for [[WA-RUBY:<nonce>:<i>]]<base> before
# import, then delete the token and set RubyText on the base.
_RUBY_BLOCK_RE = re.compile(r"<ruby\b([^>]*)>(.*?)</ruby>", re.IGNORECASE | re.DOTALL)
_RT_RE = re.compile(r"<rt\b[^>]*>(.*?)</rt>", re.IGNORECASE | re.DOTALL)
_RP_RE = re.compile(r"<rp\b[^>]*>.*?</rp>", re.IGNORECASE | re.DOTALL)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_LEADING_WS_OR_TAGS_RE = re.compile(r"(?:\s|<[^>]+>)*")
_RUBY_UNDER_RE = re.compile(r"ruby-position\s*:\s*under", re.IGNORECASE)
RUBY_TOKEN_RE = re.compile(r"\[\[WA-RUBY:([a-zA-Z0-9]+):(\d+)\]\]")


def ruby_token_prefix(nonce: str) -> str:
    """Placeholder prefix for *nonce*, used by ``field_restore.find_tokens``."""
    return f"[[WA-RUBY:{nonce}:"


class RubySpan(NamedTuple):
    """One CJK ruby (furigana) pair from a Writer portion walk.

    ``start`` / ``end`` are offsets in the paragraph's visible text (base only;
    ruby marks have empty ``getString()``). Used to keep range export from
    wrapping a base that was trimmed out of the copied window.
    """

    base: str
    reading: str
    start: int = 0
    end: int = 0
    is_above: bool = True



def _inside(html: str, index: int, open_tok: str, close_tok: str) -> bool:
    """True when *index* sits inside an unclosed pair of tokens."""
    return html.rfind(open_tok, 0, index) > html.rfind(close_tok, 0, index)



def _find_contiguous(html: str, needle: str, start: int = 0) -> tuple[int, int] | None:
    """Offsets of *needle* as a raw substring that is not inside a tag."""
    if not html or not needle:
        return None
    idx = start
    while True:
        found = html.find(needle, idx)
        if found < 0:
            return None
        if not _inside(html, found, "<", ">"):
            return found, found + len(needle)
        idx = found + 1



def _reading_after(html: str, end: int, reading: str) -> int | None:
    """Index of *reading* after *end*, crossing only whitespace and complete tags."""
    i = end
    n = len(html)
    while i < n:
        if html.startswith(reading, i):
            return i
        ch = html[i]
        if ch.isspace():
            i += 1
            continue
        if ch != "<":
            return None
        close = html.find(">", i)
        if close < 0:
            return None
        i = close + 1
    return None



def _ruby_markup(base: str, reading: str, is_above: bool = True) -> str:
    # Under-ruby would come back as over without the CSS position.
    style = "" if is_above else ' style="ruby-position: under"'
    return "<ruby%s>%s<rt>%s</rt></ruby>" % (style, escape(base), escape(reading))



def inject_ruby_into_html(html: str, spans: Sequence[RubySpan]) -> str:
    """Rewrite glued or dropped ruby encodings to semantic ``<ruby><rt>``.

    The XHTML Writer filter has no ``text:ruby`` rule, so full export concatenates
    base + reading (``漢字かんじです``). Range copy does not paint ``RubyText`` on the
    start mark, so readings are rewritten here to
    ``<ruby>漢字<rt>かんじ</rt></ruby>です``. Apply recreates portions in
    ``html_import`` (strip ``<rt>``, then ``RubyText`` on the base run).

    Replacements stay inside a single text node (or wrap the base node and
    delete a following reading node). Replacements stay within one text node so
    wrappers do not split ``<ruby>`` across tags.
    """
    if not html or not spans:
        return html
    pos = 0
    parts = []
    for span in spans:
        base, reading = span.base, span.reading
        is_above = span.is_above
        if not base or not reading:
            continue
        extra = ""
        glued = _find_contiguous(html, base + reading, pos)
        if glued is not None and not _inside(html, glued[0], "<ruby", "</ruby>"):
            start, end, next_pos = glued[0], glued[1], glued[1]
        else:
            base_at = _find_contiguous(html, base, pos)
            if base_at is None or _inside(html, base_at[0], "<ruby", "</ruby>"):
                continue
            start, end = base_at
            reading_at = _reading_after(html, end, reading)
            if reading_at is not None and not _inside(html, reading_at, "<ruby", "</ruby>"):
                # Bold/span around the base: keep those tags, drop the glued reading.
                extra, next_pos = html[end:reading_at], reading_at + len(reading)
            else:
                next_pos = end
        parts.append(html[pos:start])
        parts.append(_ruby_markup(base, reading, is_above))
        if extra:
            parts.append(extra)
        pos = next_pos
    parts.append(html[pos:])
    return "".join(parts)



def _iter_ruby_spans_in_paragraph(
    para: Any, state: DeletionState | None = None
) -> Iterator[RubySpan]:
    """Yield ``RubySpan`` for each Ruby start/end pair in *para*.

    Writer stores ruby as ``TextPortionType="Ruby"`` marks: the start has
    ``RubyText`` / ``RubyIsAbove`` / ``RubyAdjust``; the base is a normal
    ``Text`` run; the end mark is empty. ``getString()`` on the paragraph is
    base only — reading is not a text field.
    """
    portion_enum = uno_read(lambda: para.createEnumeration(), FAILED)
    if portion_enum is FAILED:
        return
    if state is None:
        state = DeletionState()
    in_ruby = False
    reading = ""
    is_above = True
    base_parts: list[str] = []
    base_start = 0
    offset = 0
    for portion, kind in iter_portions(portion_enum):
        if kind == "Redline":
            state.apply_marker(portion)
            continue
        if state.in_delete:
            continue
        if kind == "Ruby":
            if not in_ruby:
                reading = uno_read(lambda: str(portion.getPropertyValue("RubyText") or ""), "")
                is_above = uno_read(lambda: bool(portion.getPropertyValue("RubyIsAbove")), True)
                in_ruby = True
                base_parts = []
                base_start = offset
            else:
                base = "".join(base_parts)
                if base and reading:
                    yield RubySpan(base, reading, base_start, base_start + len(base), is_above)
                in_ruby = False
            continue
        chunk = uno_read(lambda: portion.getString() or "", "")
        if not chunk:
            continue
        if in_ruby:
            base_parts.append(chunk)
        offset += len(chunk)



def ruby_spans_in_window(
    para: Any, trim_start: int, trim_end: int, state: DeletionState
) -> list[RubySpan]:
    """Ruby pairs whose entire base lies inside the copied visible-text window."""
    return [
        span
        for span in _iter_ruby_spans_in_paragraph(para, state=state)
        if span.start >= trim_start and span.end <= trim_end
    ]



def inject_ruby_from_model(model: Any, content: str, fodt_has_ruby: bool | None = None) -> str:
    """Walk the live model and rewrite *content* when ruby is (or may be) present.

    *fodt_has_ruby* is True/False from the paired FODT sidecar, or None when
    that export failed. False skips the portion walk on the common no-ruby path.
    """
    if not content:
        return content
    if fodt_has_ruby is False:
        return content

    def _collect() -> list[RubySpan]:
        body = model.getText()
        return [
            span
            for para, state in iter_xtext_paragraphs(body, TrackedDeletions(model).state_at(body))
            for span in _iter_ruby_spans_in_paragraph(para, state=state)
        ]

    spans = best_effort(_collect, "inject_ruby_from_model: portion walk failed")
    if spans is None:
        return content
    return inject_ruby_into_html(content, spans)



class ImportRubySpan(NamedTuple):
    base: str
    reading: str
    is_above: bool



def _visible_html_text(fragment: str) -> str:
    """Tag-stripped, entity-unescaped text of an HTML fragment."""
    return unescape(_HTML_TAG_RE.sub("", fragment))



def _ruby_base_html(inner: str) -> str:
    return _RT_RE.sub("", _RP_RE.sub("", inner))



def strip_ruby(html: str) -> str:
    """Replace ``<ruby>`` blocks with their base HTML, dropping ``<rt>`` and ``<rp>``."""
    if not html:
        return html
    lower = html.lower()
    if "<ruby" not in lower:
        return html
    # Unclosed <ruby> makes the DOTALL regex scan to end from every open tag.
    if lower.count("<ruby") != lower.count("</ruby>"):
        return _ruby_base_html(html)
    return _RUBY_BLOCK_RE.sub(lambda m: _ruby_base_html(m.group(2) or ""), html)



def ruby_placeholders(html: str, nonce: str) -> tuple[str, list[ImportRubySpan]]:
    """Replace ``<ruby>`` with ``[[WA-RUBY:<nonce>:<i>]]<base>`` and return ``(clean_html, spans)``.

    StarWriter concatenates ruby children, so the import must see base only.
    Each span in *spans* corresponds to placeholder index ``i`` and is painted
    after import via ``html_import._place_ruby``.
    """
    if not html:
        return html, []
    lower = html.lower()
    if "<ruby" not in lower or lower.count("<ruby") != lower.count("</ruby>"):
        return strip_ruby(html), []

    spans: list[ImportRubySpan] = []

    def _replace(match: re.Match[str]) -> str:
        attrs = match.group(1) or ""
        inner = match.group(2) or ""
        rt_m = _RT_RE.search(inner)
        reading = _visible_html_text(rt_m.group(1) if rt_m else "").strip()
        base_html = _ruby_base_html(inner)
        # Strip ASCII whitespace only: StarWriter keeps U+00A0 from &nbsp; in body text.
        base = _visible_html_text(base_html).strip(" \t\r\n")
        is_above = _RUBY_UNDER_RE.search(attrs) is None
        if not base or not reading:
            return base_html
        idx = len(spans)
        spans.append(ImportRubySpan(base, reading, is_above))
        token = f"{ruby_token_prefix(nonce)}{idx}]]"
        # Place the token after leading whitespace and opening tags so it sits right
        # before the first visible character (and leading whitespace still collapses).
        lead = _LEADING_WS_OR_TAGS_RE.match(base_html)
        ws_len = lead.end() if lead else 0
        return base_html[:ws_len] + token + base_html[ws_len:]

    return _RUBY_BLOCK_RE.sub(_replace, html), spans

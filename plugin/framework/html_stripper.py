# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""Stateful HTML tag stripper that works with streamed chunks of text."""

from __future__ import annotations

import html
import re
from typing import NamedTuple

from plugin.framework.deal_shim import DEAL_MAX_HTML_CHUNK, deal, str_bounded

# Wider than DEAL_MAX_SOURCE (16 under CrossHair): _feed_chunk() must still
# reach the 256-char tag flush under pytest. Pytest binds DEAL_MAX_HTML_CHUNK=4096;
# CrossHair uses 16. Public feed() / strip_html_tags have no whole-string
# @deal.pre — they slice to DEAL_MAX_HTML_CHUNK so long _append_response
# assistant/tool-result HTML cannot PreContract in debug OXTs.
_DEAL_MAX_HTML_CHUNK = DEAL_MAX_HTML_CHUNK

# Element text used to survive (``<script>alert(1)</script>`` → ``alert(1)``).
# Drop the body until the matching close tag, not only the tag bytes.
_DISCARD_ELEMENTS = frozenset({"script", "style"})
# Tags the stripper removes. Anything else inside ``<…>`` is prose: a generic
# (``List<String>``), an autolink (``<https://example.com>``), an email
# (``<user@example.com>``), or a known name glued to a non-delimiter
# (``<a@b.com>``, ``<b, c>``, ``<em@x>``). ``b`` / ``i`` / ``script`` stay in
# this set so formatting and script-body removal are unchanged.
_HTML_ELEMENTS = frozenset({
    "a", "abbr", "acronym", "address", "area", "article", "aside", "audio",
    "b", "base", "bdi", "bdo", "big", "blockquote", "body", "br", "button",
    "canvas", "caption", "center", "cite", "code", "col", "colgroup",
    "data", "datalist", "dd", "del", "details", "dfn", "dialog", "div", "dl", "dt",
    "em", "embed",
    "fieldset", "figcaption", "figure", "font", "footer", "form",
    "h1", "h2", "h3", "h4", "h5", "h6", "head", "header", "hgroup", "hr", "html",
    "i", "iframe", "img", "input", "ins",
    "kbd",
    "label", "legend", "li", "link",
    "main", "map", "mark", "menu", "meta", "meter",
    "nav", "noscript",
    "object", "ol", "optgroup", "option", "output",
    "p", "picture", "pre", "progress",
    "q",
    "rp", "rt", "ruby",
    "s", "samp", "script", "search", "section", "select", "slot", "small",
    "source", "span", "strike", "strong", "style", "sub", "summary", "sup",
    "table", "tbody", "td", "template", "textarea", "tfoot", "th", "thead",
    "time", "title", "tr", "track", "tt",
    "u", "ul",
    "var", "video",
    "wbr",
})
_TAG_BUFFER_LIMIT = 256
_DISCARD_BODY_LIMIT = 256
# Trailing incomplete entity (``&amp`` split across chunks). A finished
# ``&amp;`` does not match. ``3 & 5`` does not end on the ampersand.
_INCOMPLETE_ENTITY_TAIL = re.compile(r"&(?:#x[0-9A-Fa-f]*|#\d*|[A-Za-z][A-Za-z0-9]{0,31})?\Z")
_ENTITY_RE = re.compile(r"&(?:[a-zA-Z0-9]+|#[0-9]+|#x[0-9a-fA-F]+);")


def _unescape_entities(text: str) -> str:
    return _ENTITY_RE.sub(lambda m: html.unescape(m.group(0)), text)


def _split_incomplete_entity(text: str) -> tuple[str, str]:
    """Return (ready_to_unescape, held_tail)."""
    if "&" not in text:
        return text, ""
    matched = _INCOMPLETE_ENTITY_TAIL.search(text)
    if matched is None or text.endswith(";"):
        return text, ""
    return text[: matched.start()], matched.group(0)


class _Tag(NamedTuple):
    name: str
    is_close: bool
    is_empty: bool
    is_real: bool


def _parse_tag(buf: str) -> _Tag:
    """Return parsed tag attributes for a tag buffer without ``>``."""
    inner = buf[1:] if buf.startswith("<") else buf
    if not inner:
        return _Tag(name="", is_close=False, is_empty=False, is_real=False)
    if inner[0] in "!?":
        return _Tag(name="", is_close=False, is_empty=False, is_real=True)
    is_close = False
    if inner[0] == "/":
        is_close = True
        inner = inner[1:].lstrip()
    name_chars: list[str] = []
    for char in inner:
        if char.isalnum() or char in "-:":
            name_chars.append(char.lower())
        else:
            break
    name = "".join(name_chars)
    rest = inner[len(name):]
    stripped = rest.rstrip()
    is_empty = False
    if not is_close and stripped.endswith("/"):
        before = stripped[:-1]
        is_empty = (not before) or before[-1].isspace()
    if is_empty:
        is_real = True
    elif name in _HTML_ELEMENTS and ((not rest) or rest[0].isspace()):
        is_real = True
    else:
        is_real = False
    return _Tag(name=name, is_close=is_close, is_empty=is_empty, is_real=is_real)


@deal.post(lambda result: isinstance(result, str))
def escape_prose_angle_brackets(text: str) -> str:
    """Escape prose angle brackets while preserving valid HTML tags."""
    # crosshair: off
    if not text or "<" not in text:
        return text

    out: list[str] = []
    in_tag = False
    tag_buffer = ""
    quote = ""

    def release_buffer(buf: str) -> None:
        if not buf:
            return
        if buf.startswith("<"):
            out.append("&lt;")
            out.append(buf[1:])
        else:
            out.append(buf)

    for char in text:
        if not in_tag:
            if char == "<":
                in_tag = True
                tag_buffer = "<"
                quote = ""
            else:
                out.append(char)
        elif quote:
            if char == quote:
                quote = ""
            tag_buffer += char
            if len(tag_buffer) > _TAG_BUFFER_LIMIT:
                release_buffer(tag_buffer)
                in_tag = False
                tag_buffer = ""
                quote = ""
        elif char in ('"', "'"):
            tag_buffer += char
            quote = char
        elif char == "<":
            release_buffer(tag_buffer)
            tag_buffer = "<"
            quote = ""
        elif char == ">":
            in_tag = False
            buf = tag_buffer
            tag_buffer = ""
            quote = ""
            if _parse_tag(buf).is_real:
                out.append(buf + ">")
            else:
                out.append("&lt;" + buf[1:] + "&gt;")
        else:
            tag_buffer += char
            if len(tag_buffer) == 2:
                first_char = tag_buffer[1]
                if not (first_char.isalpha() or first_char in ("/", "!", "?")):
                    release_buffer(tag_buffer)
                    in_tag = False
                    tag_buffer = ""
            elif len(tag_buffer) > _TAG_BUFFER_LIMIT:
                release_buffer(tag_buffer)
                in_tag = False
                tag_buffer = ""

    if in_tag and tag_buffer:
        release_buffer(tag_buffer)

    return "".join(out)


class StreamingHTMLStripper:
    """Stateful, stream-friendly HTML tag stripper.

    Allows feeding chunks of text (e.g., from an LLM response) and outputs
    the text with HTML tags stripped. It handles cases where a tag definition
    is split across chunk boundaries, and distinguishes between HTML tags and
    math comparisons (e.g. "3 < 5"). Angle-bracket prose (``<String>``, URLs,
    emails, and an element name glued to ``@`` or ``,``) is kept.
    """

    in_tag: bool
    tag_buffer: str
    # Quote character currently open inside a tag (``"`` or ``'``), else "".
    _quote: str
    # ``script`` / ``style`` whose element text is discarded, else "".
    _discard_until: str
    # Body held until the close tag, a blank line, or _DISCARD_BODY_LIMIT.
    _discard_body: str
    _entity_tail: str
    # Set when a completed buffer was a real tag. Detection reuses this machine.
    dropped_real_tag: bool

    def __init__(self) -> None:
        self.in_tag = False
        self.tag_buffer = ""
        self._quote = ""
        self._discard_until = ""
        self._discard_body = ""
        self._entity_tail = ""
        self.dropped_real_tag = False

    def _unescape_emitted(self, raw: str, *, hold_tail: bool) -> str:
        combined = self._entity_tail + raw
        if hold_tail:
            ready, self._entity_tail = _split_incomplete_entity(combined)
        else:
            ready, self._entity_tail = combined, ""
        return _unescape_entities(ready)

    def _release_tag_buffer(self, out: list[str], *, force_emit: bool) -> None:
        """Stop buffering a tag. Emit unless we are discarding element text."""
        if (force_emit or not self._discard_until) and self.tag_buffer:
            out.append(self.tag_buffer)
        self.in_tag = False
        self.tag_buffer = ""
        self._quote = ""

    def _push_tag_char(self, char: str, out: list[str], *, reject_bad_start: bool) -> None:
        self.tag_buffer += char
        if reject_bad_start and len(self.tag_buffer) == 2:
            first_char = self.tag_buffer[1]
            # Second character must look like a tag. ``3 < 5`` stays text;
            # a space (or digit) after ``<`` is not the start of a tag.
            if not (first_char.isalpha() or first_char in ("/", "!", "?")):
                self._release_tag_buffer(out, force_emit=not bool(self._discard_until))
                return
        if len(self.tag_buffer) > _TAG_BUFFER_LIMIT:
            # Never-closed '<' used to grow without bound. This cap is only
            # the tag buffer. Element body is bounded in _note_discarded_body.
            self._release_tag_buffer(out, force_emit=True)

    def _end_tag(self, out: list[str]) -> None:
        """Drop a completed real tag. Prose in angle brackets is text.

        script/style then discard until the close tag. A non-tag such as
        ``<String>`` is not a tag even while that body is held: it joins the
        held text so a later release can show it.
        """
        buf = self.tag_buffer
        tag = _parse_tag(buf)
        self.in_tag = False
        self.tag_buffer = ""
        self._quote = ""
        if tag.is_real:
            self.dropped_real_tag = True
        if self._discard_until:
            if tag.is_real and tag.is_close and tag.name == self._discard_until:
                self._discard_until = ""
                self._discard_body = ""
                return
            if not tag.is_real:
                self._discard_body += buf + ">"
                if len(self._discard_body) > _DISCARD_BODY_LIMIT or "\n\n" in self._discard_body:
                    out.append(self._discard_body)
                    self._discard_until = ""
                    self._discard_body = ""
            return
        if not tag.is_real:
            out.append(buf + ">")
            return
        if tag.name in _DISCARD_ELEMENTS and not tag.is_close and not tag.is_empty:
            self._discard_until = tag.name

    @deal.pre(lambda self, chunk: str_bounded(chunk, _DEAL_MAX_HTML_CHUNK))
    @deal.post(lambda result: isinstance(result, str))
    def _feed_chunk(self, chunk: str) -> str:
        """Process one deal-bounded slice. feed() slices so callers never trip this pre.

        Debug OXTs keep live @deal.pre. _append_response used to pass a whole
        assistant chunk here; one slice >4096 raised PreContractError, which
        suppress_disposed swallowed (UI Ready, log PreContractError=1).
        """
        # crosshair: off  # char-by-char tag machine (cover-all 33451622787: ~1800s module, 2 examples despite DEAL_MAX_HTML_CHUNK=16). Doable later: dual-profile ASCII + smaller chunk.
        out: list[str] = []
        for char in chunk:
            if not self.in_tag:
                if char == "<":
                    self.in_tag = True
                    self.tag_buffer = "<"
                    self._quote = ""
                elif not self._discard_until:
                    out.append(char)
                else:
                    self._note_discarded_body(char, out)
            elif self._quote:
                # The first '>' used to end the tag even inside quotes, so
                # ``<img alt="a>b" src="x">`` leaked ``b" src="x">``.
                if char == self._quote:
                    self._quote = ""
                self._push_tag_char(char, out, reject_bad_start=False)
            elif char in ('"', "'"):
                self._push_tag_char(char, out, reject_bad_start=True)
                if self.in_tag:
                    self._quote = char
            elif char == "<":
                # A new '<' while inside a tag means the previous one was not a tag.
                # Flush the previous buffer and start a new one.
                if not self._discard_until:
                    out.append(self.tag_buffer)
                self.tag_buffer = "<"
                self._quote = ""
            elif char == ">":
                self._end_tag(out)
            else:
                self._push_tag_char(char, out, reject_bad_start=True)
        return "".join(out)

    def _note_discarded_body(self, char: str, out: list[str]) -> None:
        """Hold script/style body until the close tag, then drop it.

        Body characters are not deleted until ``</script>`` or ``</style>``.
        A reply that mentions ``<script>`` would otherwise lose everything
        after that tag. The 256-character cap only bounds an unclosed tag
        name, not this body. A blank line or more than
        ``_DISCARD_BODY_LIMIT`` body characters means the close tag is not
        coming; show that text. A close tag inside the limit still drops it.
        """
        self._discard_body += char
        if len(self._discard_body) > _DISCARD_BODY_LIMIT or "\n\n" in self._discard_body:
            out.append(self._discard_body)
            self._discard_until = ""
            self._discard_body = ""

    @deal.post(lambda result: isinstance(result, str))
    def feed(self, chunk: str) -> str:
        """Feed a chunk of text, return the approved cleaned string without HTML tags.

        Holds back any potential HTML tags in a buffer until they are either confirmed
        (closed with '>') or rejected (invalid tag start, new '<', or size limit exceeded).

        Slices to _DEAL_MAX_HTML_CHUNK like strip_html_tags so a single long
        assistant append cannot trip debug @deal.pre on _feed_chunk.
        """
        # crosshair: off  # unbounded stream wrapper; deal bound lives on _feed_chunk.
        if not chunk:
            return ""
        size = _DEAL_MAX_HTML_CHUNK
        if len(chunk) <= size:
            raw = self._feed_chunk(chunk)
        else:
            raw = "".join(self._feed_chunk(chunk[i : i + size]) for i in range(0, len(chunk), size))
        return self._unescape_emitted(raw, hold_tail=True)

    @deal.post(lambda result: isinstance(result, str))
    def finalize(self) -> str:
        """Return any remaining buffered text when the stream is completed."""
        # No close tag arrived. Dropping the hold used to erase a short
        # mention ("use a <script> tag") when the reply ended under the cap.
        if self._discard_until:
            held = self._discard_body
            if self.in_tag and self.tag_buffer:
                held += self.tag_buffer
            self.in_tag = False
            self.tag_buffer = ""
            self._quote = ""
            self._discard_until = ""
            self._discard_body = ""
            return self._unescape_emitted(held, hold_tail=False)
        if self.in_tag and self.tag_buffer:
            buf = self.tag_buffer
            self.in_tag = False
            self.tag_buffer = ""
            self._quote = ""
            return self._unescape_emitted(buf, hold_tail=False)
        return self._unescape_emitted("", hold_tail=False)


def text_has_real_html_tag(text: str) -> bool:
    """True when *text* contains a tag :func:`strip_html_tags` would remove.

    Same machine as the live stream, so a generic token the stripper keeps
    is not reported as HTML left in the hidden document.
    """
    if not text or "<" not in text:
        return False
    stripper = StreamingHTMLStripper()
    stripper.feed(text)
    stripper.finalize()
    return stripper.dropped_real_tag


# feed() slices so live deal never requires the whole string ≤ DEAL_MAX_HTML_CHUNK.
@deal.post(lambda result: isinstance(result, str))
def strip_html_tags(text: str) -> str:
    """Synchronous utility to strip HTML tags from a complete string."""
    # crosshair: off  # wraps feed; whole-text @deal.pre removed — crashed long tool-result chat appends in debug.
    if not text:
        return ""
    # Plain text without markup or entities needs no stripping.
    if "<" not in text and "&" not in text:
        return text
    stripper = StreamingHTMLStripper()
    return stripper.feed(text) + stripper.finalize()


_SEARCH_DISCARD_TAGS = frozenset({"script", "style", "head"})
_SEARCH_BLOCK_TAGS = frozenset({
    "p", "div", "h1", "h2", "h3", "h4", "h5", "h6",
    "li", "ul", "ol",
    "blockquote", "pre",
    "table", "thead", "tbody", "tfoot", "tr", "td", "th",
    "hr", "section", "article", "header", "footer", "nav", "aside",
    "figure", "figcaption", "dd", "dt", "dl",
})


@deal.post(lambda result: isinstance(result, str))
def html_to_search_text(text: str) -> str:
    """Convert HTML markup to plain search text matching LibreOffice paragraph structure.

    Paragraphs, headings, list items, and table elements are separated by newlines;
    line breaks (<br>) emit a newline. Intra-block whitespace collapses to a single space.
    """
    # crosshair: off
    if not text:
        return ""
    if "<" not in text and "&" not in text:
        return re.sub(r"[ \t\r\n]+", " ", text).strip()

    lines: list[str] = []
    current_words: list[str] = []
    current_word: list[str] = []

    def flush_word() -> None:
        if current_word:
            current_words.append("".join(current_word))
            current_word.clear()

    def flush_block() -> None:
        flush_word()
        if current_words:
            lines.append(" ".join(current_words))
            current_words.clear()

    def flush_line_break() -> None:
        flush_word()
        if current_words:
            lines.append(" ".join(current_words))
            current_words.clear()
        else:
            lines.append("")

    def add_text(chunk: str) -> None:
        for c in chunk:
            if c.isspace():
                flush_word()
            else:
                current_word.append(c)

    in_tag = False
    tag_buf: list[str] = []
    quote = ""
    discard_until = ""
    discard_body: list[str] = []

    i = 0
    n = len(text)
    while i < n:
        char = text[i]
        if not in_tag:
            if char == "<":
                in_tag = True
                tag_buf = ["<"]
                quote = ""
            elif discard_until:
                discard_body.append(char)
                if len(discard_body) > _DISCARD_BODY_LIMIT or "\n\n" in "".join(discard_body):
                    add_text("".join(discard_body))
                    discard_until = ""
                    discard_body.clear()
            elif char.isspace():
                flush_word()
            else:
                current_word.append(char)
        elif quote:
            if char == quote:
                quote = ""
            tag_buf.append(char)
            if len(tag_buf) > _TAG_BUFFER_LIMIT:
                add_text("".join(tag_buf))
                in_tag = False
                tag_buf.clear()
        elif char in ('"', "'"):
            tag_buf.append(char)
            quote = char
        elif char == "<":
            add_text("".join(tag_buf))
            tag_buf = ["<"]
            quote = ""
        elif char == ">":
            buf_str = "".join(tag_buf)
            in_tag = False
            tag_buf.clear()
            quote = ""
            tag = _parse_tag(buf_str)
            if discard_until:
                if tag.is_real and tag.is_close and tag.name == discard_until:
                    discard_until = ""
                    discard_body.clear()
                else:
                    discard_body.append(buf_str + ">")
            elif not tag.is_real:
                add_text(buf_str + ">")
            elif tag.name in _SEARCH_DISCARD_TAGS and not tag.is_close and not tag.is_empty:
                discard_until = tag.name
                discard_body.clear()
            elif tag.name == "br":
                flush_line_break()
            elif tag.name in _SEARCH_BLOCK_TAGS:
                flush_block()
        else:
            tag_buf.append(char)
            if len(tag_buf) == 2:
                fc = tag_buf[1]
                if not (fc.isalpha() or fc in ("/", "!", "?")):
                    add_text("".join(tag_buf))
                    in_tag = False
                    tag_buf.clear()
            elif len(tag_buf) > _TAG_BUFFER_LIMIT:
                add_text("".join(tag_buf))
                in_tag = False
                tag_buf.clear()
        i += 1

    if in_tag and tag_buf:
        add_text("".join(tag_buf))

    flush_block()
    out = "\n".join(lines)
    return _unescape_entities(out)



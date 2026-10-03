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
import os
import re

from plugin.framework.deal_shim import CROSSHAIR_ENV, DEAL_MAX_HTML_CHUNK, ascii_bounded, str_bounded, deal

# Wider than DEAL_MAX_SOURCE (16 under CrossHair): _feed_chunk() must still
# reach the 256-char tag flush under pytest. Pytest binds DEAL_MAX_HTML_CHUNK=4096;
# CrossHair uses 16. Public feed() / strip_html_tags have no whole-string
# @deal.pre — they slice to DEAL_MAX_HTML_CHUNK so long _append_response
# assistant/tool-result HTML cannot PreContract in debug OXTs.
_DEAL_MAX_HTML_CHUNK = DEAL_MAX_HTML_CHUNK
# Import-time only: pytest keeps Unicode body text (café); CrossHair uses ASCII
# so SMT is not on 16-char Unicode (strip_html_tags 2:16, check-all 32877875221).
_HTML_CROSSHAIR = os.environ.get(CROSSHAIR_ENV) == "1"
_deal_strip_html_ok = ascii_bounded if _HTML_CROSSHAIR else str_bounded

# Element text used to survive (``<script>alert(1)</script>`` → ``alert(1)``).
# Drop the body until the matching close tag, not only the tag bytes.
_DISCARD_ELEMENTS = frozenset({"script", "style"})
# Tags the stripper removes. Anything else inside ``<…>`` is prose: a generic
# (``List<String>``), an autolink (``<https://example.com>``), or an email
# (``<user@example.com>``). ``b`` / ``i`` / ``script`` stay in this set so
# formatting and script-body removal are unchanged.
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
# The tag-buffer cap (256) does not cover element body. An unclosed
# ``<script>`` used to drop every later character of the reply. Hold at most
# this many body chars; past that, or on a blank line, it was prose.
_DISCARD_BODY_LIMIT = 256
# Trailing incomplete entity (``&amp`` split across chunks). A finished
# ``&amp;`` does not match. ``3 & 5`` does not end on the ampersand.
_INCOMPLETE_ENTITY_TAIL = re.compile(r"&(?:#x[0-9A-Fa-f]*|#\d*|[A-Za-z][A-Za-z0-9]{0,31})?$")


def _split_incomplete_entity(text: str) -> tuple[str, str]:
    """Return (ready_to_unescape, held_tail)."""
    if "&" not in text:
        return text, ""
    matched = _INCOMPLETE_ENTITY_TAIL.search(text)
    if matched is None or text.endswith(";"):
        return text, ""
    return text[: matched.start()], matched.group(0)


def _html_tag_name(buf: str) -> tuple[str, bool, bool]:
    """Return ``(lower_name, is_close, is_empty)`` for a tag buffer without ``>``.

    ``buf`` is everything after ``<`` was seen and before an unquoted ``>``.
    """
    inner = buf[1:] if buf.startswith("<") else buf
    if not inner:
        return "", False, False
    is_close = False
    if inner[0] == "/":
        is_close = True
        inner = inner[1:].lstrip()
    elif inner[0] in "!?":
        return "", False, False
    name: list[str] = []
    for char in inner:
        if char.isalnum() or char in "-:":
            name.append(char.lower())
        else:
            break
    # ``<script/>`` and ``<script />`` have no element body to discard.
    # What was wrong: any buffer ending in ``/`` was empty, so an unquoted
    # attribute such as ``<script src=https://cdn.example.com/>`` kept the
    # script body. The slash closes the tag only when it is its own token.
    rest = inner[len(name):]
    stripped = rest.rstrip()
    is_empty = False
    if not is_close and stripped.endswith("/"):
        before = stripped[:-1]
        is_empty = (not before) or before[-1].isspace()
    return "".join(name), is_close, is_empty


def _is_real_html_tag(buf: str) -> bool:
    """True when a tag buffer (no closing ``>``) should be removed, not shown.

    What was wrong: the second character being a letter was enough, so
    ``<String>``, ``<https://example.com>``, and ``<user@example.com>`` were
    tags. The stream stripper and the committed paint both deleted them.
    A real tag names a known element, starts with ``!`` or ``?`` (comment,
    doctype, processing instruction), or is self-closing (``<br/>``).
    """
    inner = buf[1:] if buf.startswith("<") else buf
    if not inner:
        return False
    if inner[0] in "!?":
        return True
    name, _is_close, is_empty = _html_tag_name(buf)
    if is_empty:
        return True
    return name in _HTML_ELEMENTS


class StreamingHTMLStripper:
    """Stateful, stream-friendly HTML tag stripper.

    Allows feeding chunks of text (e.g., from an LLM response) and outputs
    the text with HTML tags stripped. It handles cases where a tag definition
    is split across chunk boundaries, and distinguishes between HTML tags and
    math comparisons (e.g. "3 < 5"). Angle-bracket prose (``<String>``, URLs,
    emails) is kept.
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
        return html.unescape(ready)

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
        if len(self.tag_buffer) > 256:
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
        name, is_close, is_empty = _html_tag_name(buf)
        real = _is_real_html_tag(buf)
        self.in_tag = False
        self.tag_buffer = ""
        self._quote = ""
        if real:
            self.dropped_real_tag = True
        if self._discard_until:
            if real and is_close and name == self._discard_until:
                self._discard_until = ""
                self._discard_body = ""
                return
            if not real:
                self._discard_body += buf + ">"
                if len(self._discard_body) > _DISCARD_BODY_LIMIT or "\n\n" in self._discard_body:
                    out.append(self._discard_body)
                    self._discard_until = ""
                    self._discard_body = ""
            return
        if not real:
            out.append(buf + ">")
            return
        if name in _DISCARD_ELEMENTS and not is_close and not is_empty:
            self._discard_until = name

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

        What was wrong: body characters were deleted until ``</script>`` or
        ``</style>``, so a reply that mentioned ``<script>`` lost everything
        after that tag. The 256-character cap only bounds an unclosed tag
        name, not this body. Why this emits: a blank line or more than
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
    stripper = StreamingHTMLStripper()
    return stripper.feed(text) + stripper.finalize()

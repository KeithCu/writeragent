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
    is_empty = (not is_close) and buf.rstrip().endswith("/")
    return "".join(name), is_close, is_empty


class StreamingHTMLStripper:
    """Stateful, stream-friendly HTML tag stripper.

    Allows feeding chunks of text (e.g., from an LLM response) and outputs
    the text with HTML tags stripped. It handles cases where a tag definition
    is split across chunk boundaries, and distinguishes between HTML tags and
    math comparisons (e.g. "3 < 5").
    """

    in_tag: bool
    tag_buffer: str
    # Quote character currently open inside a tag (``"`` or ``'``), else "".
    _quote: str
    # ``script`` / ``style`` whose element text is discarded, else "".
    _discard_until: str
    _entity_tail: str

    def __init__(self) -> None:
        self.in_tag = False
        self.tag_buffer = ""
        self._quote = ""
        self._discard_until = ""
        self._entity_tail = ""

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
            # Never-closed '<' used to grow without bound. The cap still
            # applies inside script/style: a missed close tag must not
            # swallow the rest of the stream. Flush emits those bytes.
            self._release_tag_buffer(out, force_emit=True)

    def _end_tag(self) -> None:
        """Drop a completed tag. script/style then discard until the close tag."""
        name, is_close, is_empty = _html_tag_name(self.tag_buffer)
        self.in_tag = False
        self.tag_buffer = ""
        self._quote = ""
        if self._discard_until:
            if is_close and name == self._discard_until:
                self._discard_until = ""
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
                # else: element text of script/style is discarded
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
                self._end_tag()
            else:
                self._push_tag_char(char, out, reject_bad_start=True)
        return "".join(out)

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
        # Unclosed script/style must not leak the held tag tail on stream end.
        if self._discard_until:
            self.in_tag = False
            self.tag_buffer = ""
            self._quote = ""
            self._discard_until = ""
            return self._unescape_emitted("", hold_tail=False)
        if self.in_tag and self.tag_buffer:
            buf = self.tag_buffer
            self.in_tag = False
            self.tag_buffer = ""
            self._quote = ""
            return self._unescape_emitted(buf, hold_tail=False)
        return self._unescape_emitted("", hold_tail=False)


# feed() slices so live deal never requires the whole string ≤ DEAL_MAX_HTML_CHUNK.
@deal.post(lambda result: isinstance(result, str))
def strip_html_tags(text: str) -> str:
    """Synchronous utility to strip HTML tags from a complete string."""
    # crosshair: off  # wraps feed; whole-text @deal.pre removed — crashed long tool-result chat appends in debug.
    if not text:
        return ""
    stripper = StreamingHTMLStripper()
    return stripper.feed(text) + stripper.finalize()

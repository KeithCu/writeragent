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

import os

from plugin.framework.deal_shim import (
    CROSSHAIR_ENV,
    DEAL_MAX_HTML_CHUNK,
    ascii_bounded,
    str_bounded,
    deal,
)

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


class StreamingHTMLStripper:
    """Stateful, stream-friendly HTML tag stripper.

    Allows feeding chunks of text (e.g., from an LLM response) and outputs
    the text with HTML tags stripped. It handles cases where a tag definition
    is split across chunk boundaries, and distinguishes between HTML tags and
    math comparisons (e.g. "3 < 5").
    """

    in_tag: bool
    tag_buffer: str

    def __init__(self) -> None:
        self.in_tag = False
        self.tag_buffer = ""

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
                else:
                    out.append(char)
            else:
                if char == "<":
                    # A new '<' while inside a tag means the previous one was not a tag.
                    # Flush the previous buffer and start a new one.
                    out.append(self.tag_buffer)
                    self.tag_buffer = "<"
                elif char == ">":
                    # Tag is completed! Strip it by discarding the buffer.
                    self.in_tag = False
                    self.tag_buffer = ""
                else:
                    self.tag_buffer += char
                    # If we just started buffering, make sure it looks like a tag.
                    if len(self.tag_buffer) == 2:
                        first_char = self.tag_buffer[1]
                        if not (first_char.isalpha() or first_char in ("/", "!", "?")):
                            # Not a valid HTML tag start (e.g. "< 5"). Flush buffer.
                            self.in_tag = False
                            out.append(self.tag_buffer)
                            self.tag_buffer = ""
                    elif len(self.tag_buffer) > 256:
                        # Exceeded safe limit for an LLM HTML tag. Flush buffer.
                        self.in_tag = False
                        out.append(self.tag_buffer)
                        self.tag_buffer = ""
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
            return self._feed_chunk(chunk)
        parts = [self._feed_chunk(chunk[i : i + size]) for i in range(0, len(chunk), size)]
        return "".join(parts)

    @deal.post(lambda result: isinstance(result, str))
    def finalize(self) -> str:
        """Return any remaining buffered text when the stream is completed."""
        if self.in_tag and self.tag_buffer:
            buf = self.tag_buffer
            self.in_tag = False
            self.tag_buffer = ""
            return buf
        return ""


# feed() slices so live deal never requires the whole string ≤ DEAL_MAX_HTML_CHUNK.
@deal.post(lambda result: isinstance(result, str))
def strip_html_tags(text: str) -> str:
    """Synchronous utility to strip HTML tags from a complete string."""
    # crosshair: off  # wraps feed; whole-text @deal.pre removed — crashed long tool-result chat appends in debug.
    if not text:
        return ""
    stripper = StreamingHTMLStripper()
    return stripper.feed(text) + stripper.finalize()

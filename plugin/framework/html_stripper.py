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


class StreamingHTMLStripper:
    """Stateful, stream-friendly HTML tag stripper.

    Allows feeding chunks of text (e.g., from an LLM response) and outputs
    the text with HTML tags stripped. It handles cases where a tag definition
    is split across chunk boundaries, and distinguishes between HTML tags and
    math comparisons (e.g. "3 < 5").
    """

    def __init__(self) -> None:
        self.in_tag = False
        self.tag_buffer = ""

    def feed(self, chunk: str) -> str:
        """Feed a chunk of text, return the approved cleaned string without HTML tags.
        
        Holds back any potential HTML tags in a buffer until they are either confirmed
        (closed with '>') or rejected (invalid tag start, new '<', or size limit exceeded).
        """
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

    def finalize(self) -> str:
        """Return any remaining buffered text when the stream is completed."""
        if self.in_tag and self.tag_buffer:
            buf = self.tag_buffer
            self.in_tag = False
            self.tag_buffer = ""
            return buf
        return ""


def strip_html_tags(text: str) -> str:
    """Synchronous utility to strip HTML tags from a complete string."""
    if not text:
        return ""
    stripper = StreamingHTMLStripper()
    res = stripper.feed(text)
    return res + stripper.finalize()

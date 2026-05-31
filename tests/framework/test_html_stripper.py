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
"""Unit tests for plugin.framework.html_stripper."""

from __future__ import annotations

import pytest

from plugin.framework.html_stripper import StreamingHTMLStripper, strip_html_tags


def test_strip_html_tags_simple():
    text = "<p>Hello <strong>World</strong>!</p>"
    assert strip_html_tags(text) == "Hello World!"


def test_strip_html_tags_math_comparison():
    # "3 < 5" should not be treated as HTML tag
    text = "If 3 < 5 and y > 2, then <p>success</p>."
    assert strip_html_tags(text) == "If 3 < 5 and y > 2, then success."


def test_streaming_html_stripper_chunks():
    stripper = StreamingHTMLStripper()
    chunks = [
        "Hello ",
        "<st",
        "rong",
        ">Wo",
        "rld</",
        "strong",
        ">!",
    ]
    cleaned = [stripper.feed(c) for c in chunks]
    assert "".join(cleaned) == "Hello World!"


def test_streaming_html_stripper_incomplete_math():
    stripper = StreamingHTMLStripper()
    chunks = [
        "x < ",
        " 5",
    ]
    cleaned = [stripper.feed(c) for c in chunks]
    assert "".join(cleaned) == "x <  5"


def test_streaming_html_stripper_incomplete_non_tag():
    # If we stream "a <b" and it never closes, finalize() should release it.
    stripper = StreamingHTMLStripper()
    assert stripper.feed("a <b") == "a "
    assert stripper.finalize() == "<b"


def test_streaming_html_stripper_safety_cap():
    stripper = StreamingHTMLStripper()
    # A `<` followed by a massive string without `>` should be flushed
    chunk1 = "<" + "a" * 260
    cleaned1 = stripper.feed(chunk1)
    # The cap is 256, so it will exceed and flush
    assert len(cleaned1) > 250


def test_strip_html_tags_incomplete_non_tag():
    # Synchronous utility should automatically finalize and return "a <b"
    assert strip_html_tags("a <b") == "a <b"


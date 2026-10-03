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


from plugin.framework.deal_shim import DEAL_MAX_HTML_CHUNK
from plugin.framework.html_stripper import StreamingHTMLStripper, strip_html_tags


def test_strip_html_tags_simple():
    text = "<p>Hello <strong>World</strong>!</p>"
    assert strip_html_tags(text) == "Hello World!"


def test_strip_html_tags_unicode():
    text = "<p>café — naïve</p>"
    assert strip_html_tags(text) == "café — naïve"


def test_strip_html_tags_math_comparison():
    # "3 < 5" should not be treated as HTML tag
    text = "If 3 < 5 and y > 2, then <p>success</p>."
    assert strip_html_tags(text) == "If 3 < 5 and y > 2, then success."


def test_strip_html_tags_unquoted_url_slash_still_drops_script_body():
    # A trailing slash inside an unquoted attribute is not an empty element.
    # Treating it as ``<script/>`` used to keep alert(1).
    assert strip_html_tags("<script src=https://cdn.example.com/>alert(1)</script>ok") == "ok"
    assert strip_html_tags("<script/>alert(1)") == "alert(1)"
    assert strip_html_tags("<script />alert(1)") == "alert(1)"


def test_strip_html_tags_drops_script_and_style_bodies():
    # Tag bytes used to be dropped but element text survived.
    assert strip_html_tags("<script>alert(1)</script><p>ok</p>") == "ok"
    assert strip_html_tags("<style>p > b { color: red }</style><p>ok</p>") == "ok"
    assert strip_html_tags("<SCRIPT>alert(1)</SCRIPT>ok") == "ok"


def test_unclosed_script_does_not_swallow_the_rest():
    # A reply that mentions <script> without a close tag used to drop
    # everything after the tag, including text after the stream ended.
    assert strip_html_tags("See <script> for details") == "See  for details"
    assert strip_html_tags("use a <script> tag.\n\nKeep this.") == "use a  tag.\n\nKeep this."
    tail = "y" * 40
    body = "x" * 300
    result = strip_html_tags("<script>" + body + tail)
    assert result.endswith(tail)
    assert "x" * 40 in result


def test_strip_html_tags_quoted_gt_does_not_end_tag():
    # The first '>' inside quotes used to end the tag and leak the tail.
    assert strip_html_tags('<img alt="a>b" src="x">tail') == "tail"
    assert strip_html_tags("<img alt='a>b' src='x'>tail") == "tail"


def test_strip_html_tags_comparison_stays_text():
    assert strip_html_tags("3 < 5") == "3 < 5"


def test_strip_html_tags_unescapes_entities():
    assert strip_html_tags("a &amp; b") == "a & b"
    assert strip_html_tags("<p>3 &lt; 5</p>") == "3 < 5"


def test_streaming_html_stripper_holds_split_entity():
    stripper = StreamingHTMLStripper()
    assert stripper.feed("a &am") == "a "
    assert stripper.feed("p; b") + stripper.finalize() == "& b"


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


def test_streaming_html_stripper_feed_over_deal_max_chunk():
    """A single feed() larger than DEAL_MAX_HTML_CHUNK must not PreContract.

    _append_response used to pass a whole assistant chunk to feed(); debug
    @deal.pre required each slice ≤ 4096. This test would have caught that.
    """
    body = "Hello <b>" + ("x" * (DEAL_MAX_HTML_CHUNK + 10)) + "</b> world"
    stripper = StreamingHTMLStripper()
    out = stripper.feed(body) + stripper.finalize()
    assert out == "Hello " + ("x" * (DEAL_MAX_HTML_CHUNK + 10)) + " world"
    assert out == strip_html_tags(body)


def test_streaming_html_stripper_feed_tag_spans_deal_slice():
    """A tag that starts at the last char of one deal slice must still strip."""
    # '<' is the last char of the first _DEAL_MAX_HTML_CHUNK slice.
    text = ("a" * (DEAL_MAX_HTML_CHUNK - 1)) + "<b>z</b>"
    stripper = StreamingHTMLStripper()
    assert stripper.feed(text) + stripper.finalize() == ("a" * (DEAL_MAX_HTML_CHUNK - 1)) + "z"


# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""deal / Hypothesis / CrossHair verification for html_stripper and auth helpers."""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

import deal
import pytest

from plugin.framework.deal_shim import DEAL_MAX_HTML_CHUNK
from plugin.framework.html_stripper import StreamingHTMLStripper, strip_html_tags
from tests.harness.strip_bundle import deal_pre_present


_PLAIN_SEGMENT = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",), blacklist_characters="<>&", max_codepoint=127),
    max_size=40,
)
_TAG_NAMES = st.sampled_from(["p", "div", "b", "i", "em", "strong", "span", "a", "h1", "li", "ul", "code", "pre"])


@st.composite
def _tagged_html_with_plain_parts(draw: st.DrawFn) -> tuple[str, str]:
    """Build an HTML string from real tags and plain segments, returning (html, expected_plain)."""
    parts_html: list[str] = []
    parts_plain: list[str] = []
    count = draw(st.integers(min_value=1, max_value=6))
    for _idx in range(count):
        plain = draw(_PLAIN_SEGMENT)
        tag = draw(_TAG_NAMES)
        kind = draw(st.sampled_from(["pair", "attr_pair", "self_close", "comment", "discard"]))
        if kind == "pair":
            parts_html.append(f"<{tag}>{plain}</{tag}>")
            parts_plain.append(plain)
        elif kind == "attr_pair":
            parts_html.append(f'<{tag} class="x" data-gt="a>b">{plain}</{tag}>')
            parts_plain.append(plain)
        elif kind == "self_close":
            parts_html.append(f"{plain}<br/>")
            parts_plain.append(plain)
        elif kind == "comment":
            parts_html.append(f"<!-- note -->{plain}")
            parts_plain.append(plain)
        else:
            discard_tag = draw(st.sampled_from(["script", "style"]))
            body = draw(
                st.text(
                    alphabet=st.characters(blacklist_categories=("Cs",), blacklist_characters="<>&\n", max_codepoint=127),
                    max_size=40,
                )
            )
            parts_html.append(f"<{discard_tag}>{body}</{discard_tag}>{plain}")
            parts_plain.append(plain)
    return "".join(parts_html), "".join(parts_plain)


@given(sample=_tagged_html_with_plain_parts())
@settings(max_examples=100)
def test_hypothesis_strip_html_tags_removes_real_tags(sample: tuple[str, str]) -> None:
    html_text, expected_plain = sample
    res = strip_html_tags(html_text)
    assert res == expected_plain
    # Implied by equality; states the sanitization oracle explicitly.
    assert "<" not in res
    assert ">" not in res


@given(
    text=st.text(alphabet=st.characters(blacklist_categories=("Cs",), max_codepoint=127), max_size=DEAL_MAX_HTML_CHUNK),
    data=st.data(),
)
@settings(max_examples=100)
def test_hypothesis_strip_html_tags_no_crash_on_malformed_markup(text: str, data: st.DataObject) -> None:
    # Two-chunk streaming across any split point must match whole-string stripping on malformed markup.
    split_at = data.draw(st.integers(min_value=0, max_value=len(text)))
    stripper = StreamingHTMLStripper()
    assert stripper.feed(text[:split_at]) + stripper.feed(text[split_at:]) + stripper.finalize() == strip_html_tags(text)


def test_strip_html_tags_streaming_equivalence() -> None:
    full_text = "<div class='test'>Hello <b>World</b>! 3 < 5</div>"
    s1 = strip_html_tags(full_text)
    
    stripper = StreamingHTMLStripper()
    chunk1 = stripper.feed("<div class='test'>Hello ")
    chunk2 = stripper.feed("<b>World</b>! 3 < 5")
    chunk3 = stripper.feed("</div>")
    final = stripper.finalize()
    s2 = chunk1 + chunk2 + chunk3 + final
    
    assert s1 == s2
    assert "Hello World! 3 < 5" in s1


def test_strip_html_tags_overflow_pre_fails_closed() -> None:
    if not deal_pre_present(StreamingHTMLStripper._feed_chunk):
        pytest.skip("@deal.pre stripped in release bundle")
    # Public feed() slices like strip_html_tags; overflow is per-slice on _feed_chunk.
    strip_html_tags("x" * (DEAL_MAX_HTML_CHUNK + 1))
    StreamingHTMLStripper().feed("x" * (DEAL_MAX_HTML_CHUNK + 1))
    with pytest.raises(deal.PreContractError):
        StreamingHTMLStripper()._feed_chunk("x" * (DEAL_MAX_HTML_CHUNK + 1))
    # Pytest 4096 still reaches the 256-char tag-flush path.
    assert "<" in strip_html_tags("<" + ("a" * 256))

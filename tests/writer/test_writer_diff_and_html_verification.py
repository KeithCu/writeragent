# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""deal / Hypothesis verification for word_diff_split and xhtml_style_postprocess."""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from plugin.writer.word_diff_split import (
    SplitResult,
    Token,
    split_change,
    tokenize,
)
from plugin.writer.xhtml_style_postprocess import (
    compact_lo_style_name,
    decode_lo_css_class_suffix,
)


@given(text=st.text())
def test_tokenize_reconstruction_invariant(text: str) -> None:
    tokens = tokenize(text)
    assert isinstance(tokens, list)
    reconstructed = "".join(t.text for t in tokens)
    assert reconstructed == text

    for token in tokens:
        assert isinstance(token, Token)
        assert text[token.start:token.end] == token.text


@given(old=st.text(max_size=50), new=st.text(max_size=50))
@settings(max_examples=100)
def test_split_change_fraction_bounds(old: str, new: str) -> None:
    res = split_change(old, new)
    assert isinstance(res, SplitResult)
    assert 0.0 <= res.fraction_changed <= 1.0

    for sub in res.sub_edits:
        assert 0 <= sub.old_start <= sub.old_end <= len(old)


@given(suffix=st.text())
def test_decode_lo_css_class_suffix_returns_str(suffix: str) -> None:
    decoded = decode_lo_css_class_suffix(suffix)
    assert isinstance(decoded, str)


@given(name=st.text())
def test_compact_lo_style_name_removes_spaces(name: str) -> None:
    compacted = compact_lo_style_name(name)
    assert isinstance(compacted, str)
    assert " " not in compacted


from plugin.writer.xhtml_style_postprocess import (
    extract_autostyle_parents_from_fodt,
    parse_style_block,
)


@given(fodt=st.text(max_size=200))
def test_extract_autostyle_parents_from_fodt_invariant(fodt: str) -> None:
    res = extract_autostyle_parents_from_fodt(fodt)
    assert isinstance(res, dict)
    for k, v in res.items():
        assert isinstance(k, str)
        assert isinstance(v, str)


@given(xhtml=st.text(max_size=200))
def test_parse_style_block_invariant(xhtml: str) -> None:
    raw_map, norm_map = parse_style_block(xhtml)
    assert isinstance(raw_map, dict)
    assert isinstance(norm_map, dict)


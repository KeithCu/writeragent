# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""deal / Hypothesis verification for word_diff_split and xhtml_style_postprocess.

Hypothesis: light under ``make verify``; deep via ``make vhs`` (capped alphabets).
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

import deal
import pytest

from plugin.framework.deal_shim import DEAL_MAX_SOURCE
from plugin.writer.word_diff_split import (
    SplitResult,
    Token,
    split_change,
    tokenize,
)
from tests.strip_bundle import deal_pre_present
from plugin.writer.xhtml_style_postprocess import (
    compact_lo_style_name,
    decode_lo_css_class_suffix,
    extract_autostyle_overrides_from_fodt,
    extract_autostyle_parents_from_fodt,
    parse_style_block,
)
from tests.vhs_budget import vhs_max_examples

# Cap size/alphabet so deep VHS does not fight greedy regex oracles (FV §8.1 C).
_SAFE_TEXT = st.text(
    alphabet=st.characters(blacklist_categories=("Cs", "Cc"), blacklist_characters="\x00"),
    max_size=40,
)
_SHORT_TEXT = st.text(
    alphabet=st.characters(blacklist_categories=("Cs", "Cc"), blacklist_characters="\x00"),
    max_size=80,
)


@given(text=_SAFE_TEXT)
@settings(max_examples=vhs_max_examples(80, 800), deadline=None)
def test_hypothesis_tokenize_reconstruction_invariant(text: str) -> None:
    """Phase 8 #3: tokenize then rejoin preserves exact characters."""
    tokens = tokenize(text)
    assert isinstance(tokens, list)
    reconstructed = "".join(t.text for t in tokens)
    assert reconstructed == text

    for token in tokens:
        assert isinstance(token, Token)
        assert text[token.start : token.end] == token.text


def test_split_change_overflow_pre_fails_closed() -> None:
    if not deal_pre_present(split_change):
        pytest.skip("@deal.pre stripped in release bundle")
    too_long = "x" * (DEAL_MAX_SOURCE + 1)
    with pytest.raises(deal.PreContractError):
        split_change(too_long, "a")
    with pytest.raises(deal.PreContractError):
        split_change("a", too_long)


@given(old=_SAFE_TEXT, new=_SAFE_TEXT)
@settings(max_examples=vhs_max_examples(100, 1000), deadline=None)
def test_hypothesis_split_change_fraction_bounds(old: str, new: str) -> None:
    res = split_change(old, new)
    assert isinstance(res, SplitResult)
    assert 0.0 <= res.fraction_changed <= 1.0

    for sub in res.sub_edits:
        assert 0 <= sub.old_start <= sub.old_end <= len(old)


@given(suffix=_SHORT_TEXT)
@settings(max_examples=vhs_max_examples(40, 400), deadline=None)
def test_hypothesis_decode_lo_css_class_suffix_returns_str(suffix: str) -> None:
    decoded = decode_lo_css_class_suffix(suffix)
    assert isinstance(decoded, str)


@given(name=_SHORT_TEXT)
@settings(max_examples=vhs_max_examples(40, 400), deadline=None)
def test_hypothesis_compact_lo_style_name_removes_spaces(name: str) -> None:
    compacted = compact_lo_style_name(name)
    assert isinstance(compacted, str)
    assert " " not in compacted


@given(fodt=_SHORT_TEXT)
@settings(max_examples=vhs_max_examples(40, 400), deadline=None)
def test_hypothesis_extract_autostyle_parents_from_fodt_invariant(fodt: str) -> None:
    res = extract_autostyle_parents_from_fodt(fodt)
    assert isinstance(res, dict)
    for k, v in res.items():
        assert isinstance(k, str)
        assert isinstance(v, str)


@given(fodt=_SHORT_TEXT)
@settings(max_examples=vhs_max_examples(40, 400), deadline=None)
def test_hypothesis_extract_autostyle_overrides_from_fodt_invariant(fodt: str) -> None:
    res = extract_autostyle_overrides_from_fodt(fodt)
    assert isinstance(res, dict)
    for k, v in res.items():
        assert isinstance(k, str)
        assert isinstance(v, str)


@given(xhtml=_SHORT_TEXT)
@settings(max_examples=vhs_max_examples(40, 400), deadline=None)
def test_hypothesis_parse_style_block_invariant(xhtml: str) -> None:
    raw_map, norm_map = parse_style_block(xhtml)
    assert isinstance(raw_map, dict)
    assert isinstance(norm_map, dict)

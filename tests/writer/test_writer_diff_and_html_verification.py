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


from plugin.framework.deal_shim import DEAL_MAX_SOURCE
from plugin.writer.word_diff_split import (
    SplitResult,
    split_change,
)
from tests.harness.vhs_budget import vhs_max_examples

# Cap size/alphabet so deep VHS does not fight greedy regex oracles (FV §8.1 C).
_SAFE_TEXT = st.text(
    alphabet=st.characters(blacklist_categories=("Cs", "Cc"), blacklist_characters="\x00"),
    max_size=40,
)


def test_split_change_longer_than_source_runs() -> None:
    """Selection text past DEAL_MAX_SOURCE is a real diff, not a contract error."""
    too_long = "x " * (DEAL_MAX_SOURCE + 1)
    left = split_change(too_long, "a")
    right = split_change("a", too_long)
    assert left.fraction_changed >= 0.0
    assert right.fraction_changed >= 0.0


@given(old=_SAFE_TEXT, new=_SAFE_TEXT)
@settings(max_examples=vhs_max_examples(100, 1000), deadline=None)
def test_hypothesis_split_change_fraction_bounds(old: str, new: str) -> None:
    res = split_change(old, new)
    assert isinstance(res, SplitResult)
    assert 0.0 <= res.fraction_changed <= 1.0

    for sub in res.sub_edits:
        assert 0 <= sub.old_start <= sub.old_end <= len(old)

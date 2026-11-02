"""Deal contracts for plugin.writer.word_diff_split."""

from __future__ import annotations

import deal

from plugin.writer.word_diff_split import (
    _DEFAULT_THRESHOLD,
    SplitResult,
    split_change as _split_change,
    tokenize as _tokenize,
)
from tests.contracts.bounds import DEAL_MAX_SOURCE, str_bounded

tokenize = deal.post(lambda result: isinstance(result, list))(_tokenize)

split_change = deal.pre(
    lambda old, new, threshold=_DEFAULT_THRESHOLD: (
        not isinstance(old, str) or str_bounded(old, DEAL_MAX_SOURCE)
    )
    and (not isinstance(new, str) or str_bounded(new, DEAL_MAX_SOURCE))
)(
    deal.post(
        lambda result: isinstance(result, SplitResult) and 0.0 <= result.fraction_changed <= 1.0
    )(_split_change)
)

PYTEST = ["tokenize", "split_change"]
TARGETS: list[str] = []

# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for pure grammar queue state helpers (no threads)."""

from __future__ import annotations

from plugin.writer.locale.grammar_work_queue import (
    GrammarWorkItem,
    inflight_superseded,
    is_stale,
    record_enqueue_latest,
    tail_enqueue_operation,
)


def _item(seq: int, key: str = "d|en-US|0") -> GrammarWorkItem:
    return GrammarWorkItem(
        ctx=None,
        full_text="x",
        n_start=0,
        n_end=1,
        grammar_bcp47="en-US",
        partial_sentence=False,
        doc_id="d",
        inflight_key=key,
        enqueue_seq=seq,
    )


def test_record_enqueue_latest_updates_map() -> None:
    d, out_of_order, prev_bad = record_enqueue_latest({}, _item(1))
    assert d["d|en-US|0"] == 1
    assert out_of_order is False
    assert prev_bad is None


def test_record_enqueue_latest_detects_out_of_order() -> None:
    d = {"d|en-US|0": 10}
    d2, out_of_order, prev_bad = record_enqueue_latest(d, _item(5))
    assert out_of_order is True
    assert prev_bad == 10
    assert d2["d|en-US|0"] == 5


def test_tail_enqueue_operation() -> None:
    a = _item(1)
    b = _item(2)
    c = _item(1, key="other")
    assert tail_enqueue_operation(None, a) == "append"
    assert tail_enqueue_operation(a, b) == "replace_tail"
    assert tail_enqueue_operation(b, a) == "skip_tail"
    assert tail_enqueue_operation(a, c) == "append"


def test_is_stale_and_inflight_superseded() -> None:
    latest = {"k": 9}
    old = _item(7, key="k")
    assert is_stale(latest, old) is True
    assert inflight_superseded(latest, "k", 7) is True
    cur = _item(9, key="k")
    assert is_stale(latest, cur) is False

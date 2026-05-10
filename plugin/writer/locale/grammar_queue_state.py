# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Pure helpers for grammar work-queue supersede / stale detection (testable without threads)."""

from __future__ import annotations

from typing import Literal, Mapping

from .grammar_proofread_work_item import GrammarWorkItem

TailEnqueueOp = Literal["replace_tail", "append", "skip_tail"]


def record_enqueue_latest(prev: dict[str, int], item: GrammarWorkItem) -> tuple[dict[str, int], bool, int | None]:
    """Return updated ``latest_seq``, whether incoming seq was out-of-order, and prior seq for logging."""
    key = item.inflight_key
    prev_seq = prev.get(key)
    out_of_order = prev_seq is not None and item.enqueue_seq < prev_seq
    new_d = dict(prev)
    new_d[key] = item.enqueue_seq
    return new_d, out_of_order, prev_seq if out_of_order else None


def is_stale(latest_seq: Mapping[str, int], item: GrammarWorkItem) -> bool:
    """True if a newer enqueue has been recorded for this ``inflight_key``."""
    latest = latest_seq.get(item.inflight_key)
    return latest is not None and item.enqueue_seq < latest


def inflight_superseded(latest_seq: Mapping[str, int], inflight_key: str, enqueue_seq: int) -> bool:
    """True if ``enqueue_seq`` is older than the latest known generation for ``inflight_key``."""
    latest = latest_seq.get(inflight_key)
    return latest is not None and enqueue_seq < latest


def tail_enqueue_operation(tail: GrammarWorkItem | None, incoming: GrammarWorkItem) -> TailEnqueueOp:
    """O(1) tail decision: replace newest same-key, append different key, or skip stale same-key."""
    if tail is None:
        return "append"
    if tail.inflight_key != incoming.inflight_key:
        return "append"
    if incoming.enqueue_seq > tail.enqueue_seq:
        return "replace_tail"
    return "skip_tail"

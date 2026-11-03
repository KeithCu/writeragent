# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Per-document undo snapshots for /undo (last agent edit as one step).

Undo titles are the only cheap stack identity UNO exposes; we refuse when the
current stack no longer matches the recorded ``after`` snapshot. When the undo
stack hits LibreOffice's cap (default 100) and a new step's title matches a
step that fell off the bottom, ``after`` can equal ``before`` and we record nothing.
Module-level entries are tiny and are not pruned when a document closes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from plugin.doc.document_helpers import compute_doc_key, is_cacheable_doc_key
from plugin.framework.errors import reraise_if_disposed
from plugin.framework.i18n import _

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class _AgentUndoRecord:
    before: tuple[str, ...]
    after: tuple[str, ...]


_records: dict[str, _AgentUndoRecord] = {}
_pending_before: dict[str, tuple[str, ...]] = {}


def _undo_titles(doc: Any) -> tuple[str, ...] | None:
    if not hasattr(doc, "getUndoManager"):
        return None
    try:
        um = doc.getUndoManager()
        if um is None:
            return None
        raw = um.getAllUndoActionTitles()
        return tuple(raw or ())
    except Exception as exc:
        reraise_if_disposed(exc)
        log.exception("agent_undo: could not read undo titles")
        return None


def compute_undo_steps(before: tuple[str, ...], after: tuple[str, ...]) -> int:
    """Smallest k>0 with after[k:] == before[:len(after) - k] (newest-first stacks)."""
    # Match prefix of ``before`` to suffix of ``after``: new undo steps sit at the
    # front of the newest-first title list; k is how many were pushed on top.
    for k in range(1, len(after) + 1):
        tail = len(after) - k
        if after[k:] == before[:tail]:
            return k
    return 0


def record_agent_turn_before(doc: Any) -> None:
    key = compute_doc_key(doc)
    if not is_cacheable_doc_key(key):
        return
    titles = _undo_titles(doc)
    if titles is not None:
        _pending_before[key] = titles


def cancel_pending_agent_turn(doc: Any) -> None:
    """Drop a turn-start snapshot without recording (e.g. /formula owns undo)."""
    key = compute_doc_key(doc)
    if is_cacheable_doc_key(key):
        _pending_before.pop(key, None)


def record_agent_turn_after(doc: Any) -> None:
    key = compute_doc_key(doc)
    if not is_cacheable_doc_key(key):
        return
    before = _pending_before.pop(key, None)
    if before is None:
        return
    after = _undo_titles(doc)
    if after is None or after == before:
        return
    if compute_undo_steps(before, after) <= 0:
        return
    _records[key] = _AgentUndoRecord(before=before, after=after)


def clear_agent_undo_record(doc: Any) -> None:
    key = compute_doc_key(doc)
    _records.pop(key, None)
    _pending_before.pop(key, None)


def undo_last_agent_edit(doc: Any) -> str:
    key = compute_doc_key(doc)
    record = _records.get(key)
    if record is None:
        return _("There is no agent edit to undo.")
    current = _undo_titles(doc)
    if current is None:
        return _("There is no agent edit to undo.")
    if current != record.after:
        return _("The document was edited since the last agent edit.")
    steps = compute_undo_steps(record.before, record.after)
    if steps <= 0:
        return _("There is no agent edit to undo.")
    try:
        um = doc.getUndoManager()
        if um is None:
            return _("There is no agent edit to undo.")
        for _unused in range(steps):
            um.undo()
    except Exception as exc:
        reraise_if_disposed(exc)
        log.exception("agent_undo: undo failed")
        return _("Could not undo the last agent edit.")
    clear_agent_undo_record(doc)
    return _("Undid the last agent edit ({0} steps)").format(steps)

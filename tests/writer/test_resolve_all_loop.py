# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless tests for resolve-all's per-change loop.

The mixed-redlines loop must process EVERY agent change -- resolving what it can and skipping the
ones blocked by a user redline -- and terminate without a fixed iteration cap, so a document with
many changes is never truncated (which would make the feedback misattribute unprocessed changes).
"""
from unittest.mock import MagicMock, patch

from plugin.writer.inline_review import (
    RedlineRow,
    RedlineView,
    ResolveAllResult,
    ResolveResult,
    ResolveStatus,
    _resolve_all_agent_changes,
    resolve_all_agent_changes,
    resolve_all_with_feedback,
)


_DUMMY = object()


def _run_loop(tokens, blocked):
    """Drive resolve_all_agent_changes through the mixed-redlines loop path with a fake document
    whose agent change TOKENS are *tokens*; tokens in *blocked* refuse to resolve (user redline).
    """
    state = {"tokens": list(tokens)}

    def fake_view(model):
        rows = tuple(
            RedlineRow(token=t, identifier=t, start=_DUMMY, end=_DUMMY, kind="Insert", text=_DUMMY)
            for t in state["tokens"]
        )
        return RedlineView(reliable=True, total=len(rows), rows=rows)

    def fake_resolve(model, ctx, token, accept, refuse_sibling_agent=True, prefer_exact=True, before=None):
        if token in blocked:
            return ResolveResult(ResolveStatus.REFUSED_FOREIGN), (before or fake_view(model))
        if token in state["tokens"]:
            state["tokens"].remove(token)
        return ResolveResult(ResolveStatus.RESOLVED, 1), fake_view(model)

    with patch("plugin.writer.inline_review.scan_redline_view", side_effect=fake_view), \
         patch("plugin.writer.inline_review.resolve_agent_change", side_effect=fake_resolve), \
         patch("plugin.writer.inline_review._is_all_redlines_agent", return_value=False):
        n = resolve_all_agent_changes(MagicMock(), MagicMock(), True)
    return n, state["tokens"]


def test_loop_resolves_everything():
    n, left = _run_loop(["A", "B", "C"], blocked=set())
    assert n == 3 and left == []


def test_loop_skips_blocked_resolves_rest():
    n, left = _run_loop(["A", "B", "C"], blocked={"C"})
    assert n == 2 and left == ["C"]   # C left for the native Manage dialog


def test_loop_terminates_when_all_blocked():
    # Every change blocked by a user redline -> loop still terminates, resolves nothing.
    n, left = _run_loop(["A", "B"], blocked={"A", "B"})
    assert n == 0 and left == ["A", "B"]


def test_loop_handles_many_changes_without_cap():
    # Far more than the old magic-200 bound: all must resolve, proving there's no truncation.
    tokens = ["t%d" % i for i in range(500)]
    n, left = _run_loop(tokens, blocked=set())
    assert n == 500 and left == []


def test_feedback_message_partial_vs_all_vs_none():
    with patch("plugin.writer.inline_review._resolve_all_agent_changes",
               return_value=ResolveAllResult(ResolveStatus.RESOLVED, 2, 2)):
        n, msg = resolve_all_with_feedback(MagicMock(), MagicMock(), True)
        assert n == 2 and msg == ""           # everything resolved -> no message
    with patch("plugin.writer.inline_review._resolve_all_agent_changes",
               return_value=ResolveAllResult(ResolveStatus.REFUSED_FOREIGN, 1, 2)):
        n, msg = resolve_all_with_feedback(MagicMock(), MagicMock(), True)
        assert n == 1 and "Resolved 1 of 2" in msg
    with patch("plugin.writer.inline_review._resolve_all_agent_changes",
               return_value=ResolveAllResult(ResolveStatus.REFUSED_FOREIGN, 0, 1)):
        n, msg = resolve_all_with_feedback(MagicMock(), MagicMock(), True)
        assert n == 0 and "None could be resolved" in msg


def test_resolve_all_aborts_when_before_snapshot_unreliable():
    with patch("plugin.writer.inline_review.scan_redline_view",
               return_value=RedlineView(reliable=False, total=0, rows=())):
        res = _resolve_all_agent_changes(MagicMock(), MagicMock(), True)
    assert res.status == ResolveStatus.UNRELIABLE
    assert res.resolved == 0


def test_resolve_all_aborts_when_snapshot_unreliable_mid_loop():
    rows = (RedlineRow(token="A", identifier="A", start=_DUMMY, end=_DUMMY, kind="Insert", text=_DUMMY),)
    entry_view = RedlineView(reliable=True, total=1, rows=rows)
    unreliable_view = RedlineView(reliable=False, total=1, rows=rows)

    with patch("plugin.writer.inline_review.scan_redline_view", return_value=entry_view), \
         patch("plugin.writer.inline_review.resolve_agent_change",
               return_value=(ResolveResult(ResolveStatus.REFUSED_FOREIGN), unreliable_view)), \
         patch("plugin.writer.inline_review._is_all_redlines_agent", return_value=False):
        res = _resolve_all_agent_changes(MagicMock(), MagicMock(), True)
    assert res.status == ResolveStatus.UNRELIABLE
    assert res.resolved == 0


def test_feedback_unreliable_snapshot_reports_try_again():
    with patch("plugin.writer.inline_review._resolve_all_agent_changes",
               return_value=ResolveAllResult(ResolveStatus.UNRELIABLE, 0, 0)):
        n, msg = resolve_all_with_feedback(MagicMock(), MagicMock(), True)
    assert n == 0 and "try again" in msg.lower()


def test_resolve_all_global_unconfirmed_after_dispatch():
    views = [
        RedlineView(reliable=True, total=2, rows=(
            RedlineRow(token="A", identifier="A", start=MagicMock(), end=MagicMock(), kind="Insert", text=MagicMock()),
            RedlineRow(token="B", identifier="B", start=MagicMock(), end=MagicMock(), kind="Insert", text=MagicMock()),
        )),
        RedlineView(reliable=False, total=0, rows=()),
    ]
    calls = {"n": 0}

    def next_view(m):
        idx = min(calls["n"], len(views) - 1)
        calls["n"] += 1
        return views[idx]

    with patch("plugin.writer.inline_review.scan_redline_view", side_effect=next_view), \
         patch("plugin.writer.inline_review._execute_dispatch"):
        res = _resolve_all_agent_changes(MagicMock(), MagicMock(), True)
    assert res.status == ResolveStatus.PARTIAL
    assert res.resolved == 0


def test_resolve_all_unconfirmed_when_unreliable_after_a_resolve():
    calls = {"n": 0}

    def flaky_view(model):
        calls["n"] += 1
        reliable = calls["n"] <= 1
        rows = (RedlineRow(token="A", identifier="A", start=MagicMock(), end=MagicMock(), kind="Insert", text=MagicMock()),)
        return RedlineView(reliable=reliable, total=1, rows=rows)

    with patch("plugin.writer.inline_review.scan_redline_view", side_effect=flaky_view), \
         patch("plugin.writer.inline_review.resolve_agent_change",
               return_value=(ResolveResult(ResolveStatus.RESOLVED, 1), RedlineView(reliable=False, total=1, rows=()))), \
         patch("plugin.writer.inline_review._is_all_redlines_agent", return_value=False):
        res = _resolve_all_agent_changes(MagicMock(), MagicMock(), True)
    assert res.status == ResolveStatus.PARTIAL
    assert res.resolved == 0


def test_feedback_unconfirmed_reports_partial_not_nothing():
    with patch("plugin.writer.inline_review._resolve_all_agent_changes",
               return_value=ResolveAllResult(ResolveStatus.PARTIAL, 0, 2)):
        n, msg = resolve_all_with_feedback(MagicMock(), MagicMock(), True)
    assert n == 0
    assert "nothing was changed" not in msg.lower()
    assert "couldn't confirm" in msg.lower() and "review" in msg.lower()


def test_fast_path_noop_message_no_user_tracked_changes():
    rows = (
        RedlineRow(token="A", identifier="A", start=_DUMMY, end=_DUMMY, kind="Insert", text=_DUMMY),
        RedlineRow(token="B", identifier="B", start=_DUMMY, end=_DUMMY, kind="Insert", text=_DUMMY),
    )
    view = RedlineView(reliable=True, total=2, rows=rows)
    with patch("plugin.writer.inline_review.scan_redline_view", return_value=view), \
         patch("plugin.writer.inline_review._execute_dispatch"):
        res = _resolve_all_agent_changes(MagicMock(), MagicMock(), True)
        assert res.status == ResolveStatus.NO_OP
        assert res.resolved == 0
        n, msg = resolve_all_with_feedback(MagicMock(), MagicMock(), True)
        assert n == 0
        assert "your own tracked changes" not in msg
        assert "Nothing was changed" in msg


def test_fast_path_partial_clear_message_no_user_tracked_changes():
    before_rows = (
        RedlineRow(token="A", identifier="A", start=_DUMMY, end=_DUMMY, kind="Insert", text=_DUMMY),
        RedlineRow(token="B", identifier="B", start=_DUMMY, end=_DUMMY, kind="Insert", text=_DUMMY),
    )
    after_rows = (
        RedlineRow(token="B", identifier="B", start=_DUMMY, end=_DUMMY, kind="Insert", text=_DUMMY),
    )
    views = [
        RedlineView(reliable=True, total=2, rows=before_rows),
        RedlineView(reliable=True, total=1, rows=after_rows),
    ]
    calls = {"n": 0}

    def next_view(m):
        idx = min(calls["n"], len(views) - 1)
        calls["n"] += 1
        return views[idx]

    with patch("plugin.writer.inline_review.scan_redline_view", side_effect=next_view), \
         patch("plugin.writer.inline_review._execute_dispatch"):
        res = _resolve_all_agent_changes(MagicMock(), MagicMock(), True)
        assert res.status == ResolveStatus.PARTIAL
        assert res.resolved == 1
        calls["n"] = 0
        n, msg = resolve_all_with_feedback(MagicMock(), MagicMock(), True)
        assert n == 1
        assert "your own tracked changes" not in msg
        assert "Started resolving changes" in msg


def test_resolve_all_mixed_foreign_and_predispatch_failure_summary_not_shares_paragraph():
    rows = (
        RedlineRow(token="A", identifier="A", start=_DUMMY, end=_DUMMY, kind="Insert", text=_DUMMY),
        RedlineRow(token="B", identifier="B", start=_DUMMY, end=_DUMMY, kind="Insert", text=_DUMMY),
    )
    view = RedlineView(reliable=True, total=2, rows=rows)

    def fake_resolve(model, ctx, token, accept, refuse_sibling_agent=True, prefer_exact=True, before=None):
        if token == "A":
            return ResolveResult(ResolveStatus.REFUSED_FOREIGN), view
        return ResolveResult(ResolveStatus.UNRELIABLE), view

    with patch("plugin.writer.inline_review.scan_redline_view", return_value=view), \
         patch("plugin.writer.inline_review.resolve_agent_change", side_effect=fake_resolve), \
         patch("plugin.writer.inline_review._is_all_redlines_agent", return_value=False):
        res = _resolve_all_agent_changes(MagicMock(), MagicMock(), True)
        assert res.status == ResolveStatus.PARTIAL
        n, msg = resolve_all_with_feedback(MagicMock(), MagicMock(), True)
        assert n == 0
        assert "shares a paragraph with" not in msg
        assert "Started resolving changes" in msg

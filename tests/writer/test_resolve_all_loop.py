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

from plugin.writer.inline_review import resolve_all_agent_changes, resolve_all_with_feedback


def _run_loop(tokens, blocked):
    """Drive resolve_all_agent_changes through the mixed-redlines loop path with a fake document
    whose agent change TOKENS are *tokens*; tokens in *blocked* refuse to resolve (user redline).
    Loop control now runs off the reliability-aware _agent_change_tokens (returns (set, True))."""
    state = {"tokens": list(tokens)}

    def fake_tokens(model):
        return set(state["tokens"]), True

    def fake_resolve(model, ctx, token, accept, refuse_sibling_agent=True, prefer_exact=True):
        if token in blocked:
            return False
        if token in state["tokens"]:
            state["tokens"].remove(token)
        return True

    def fake_combined(model):
        toks, ok = fake_tokens(model)
        return (toks, set(), ok, True)

    with patch("plugin.writer.inline_review._agent_change_tokens", side_effect=fake_tokens), \
         patch("plugin.writer.inline_review._agent_and_foreign_redline_snapshot", side_effect=fake_combined), \
         patch("plugin.writer.inline_review.resolve_agent_change", side_effect=fake_resolve), \
         patch("plugin.writer.inline_review._all_redlines_are_agent", return_value=False):  # -> loop path
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
    # resolve_all_with_feedback wording, driven by resolve_all_agent_changes' count. The total now
    # comes from the reliability-aware _agent_change_tokens, not the best-effort agent_changes.
    with patch("plugin.writer.inline_review._agent_change_tokens", return_value=({"A", "B"}, True)), \
         patch("plugin.writer.inline_review.resolve_all_agent_changes", return_value=2):
        n, msg = resolve_all_with_feedback(MagicMock(), MagicMock(), True)
        assert n == 2 and msg == ""           # everything resolved -> no message
    with patch("plugin.writer.inline_review._agent_change_tokens", return_value=({"A", "B"}, True)), \
         patch("plugin.writer.inline_review.resolve_all_agent_changes", return_value=1):
        n, msg = resolve_all_with_feedback(MagicMock(), MagicMock(), True)
        assert n == 1 and "Resolved 1 of 2" in msg
    with patch("plugin.writer.inline_review._agent_change_tokens", return_value=({"A"}, True)), \
         patch("plugin.writer.inline_review.resolve_all_agent_changes", return_value=0):
        n, msg = resolve_all_with_feedback(MagicMock(), MagicMock(), True)
        assert n == 0 and "None could be resolved" in msg


def test_resolve_all_aborts_when_before_snapshot_unreliable():
    # The redline snapshot can't be enumerated reliably -> resolve-all refuses entirely with the
    # sentinel (never a partial count derived from a truncated scan).
    from plugin.writer.inline_review import _RESOLVE_ALL_UNRELIABLE
    with patch("plugin.writer.inline_review._agent_change_tokens", return_value=(set(), False)):
        n = resolve_all_agent_changes(MagicMock(), MagicMock(), True)
    assert n == _RESOLVE_ALL_UNRELIABLE


def test_resolve_all_aborts_when_snapshot_unreliable_mid_loop():
    # Reliable at entry, then the snapshot goes unreliable on a later pass -> abort with the sentinel
    # rather than terminate early and silently leave changes pending.
    from plugin.writer.inline_review import _RESOLVE_ALL_UNRELIABLE
    calls = {"n": 0}

    def flaky_tokens(model):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"A", "B"}, True        # entry snapshot: reliable
        return {"A", "B"}, False           # every later read: unreliable

    def _c_fake(m):
        t, o = flaky_tokens(m)
        return (t, set(), o, True)
    with patch("plugin.writer.inline_review._agent_change_tokens", side_effect=flaky_tokens), \
         patch("plugin.writer.inline_review._agent_and_foreign_redline_snapshot", side_effect=_c_fake), \
         patch("plugin.writer.inline_review.resolve_agent_change", return_value=True), \
         patch("plugin.writer.inline_review._all_redlines_are_agent", return_value=False):
        n = resolve_all_agent_changes(MagicMock(), MagicMock(), True)
    assert n == _RESOLVE_ALL_UNRELIABLE


def test_feedback_unreliable_snapshot_reports_try_again():
    # resolve_all_with_feedback surfaces an honest "try again" (and 0 resolved) on an unreliable
    # snapshot, never a misleading completion message.
    with patch("plugin.writer.inline_review._agent_change_tokens", return_value=(set(), False)):
        n, msg = resolve_all_with_feedback(MagicMock(), MagicMock(), True)
    assert n == 0 and "try again" in msg.lower()


def test_resolve_all_global_unconfirmed_after_dispatch():
    # Global fast path: the dispatch ALREADY ran, then the after-scan is unreliable -> UNCONFIRMED
    # (changes may have resolved), never "nothing changed".
    from plugin.writer.inline_review import _RESOLVE_ALL_UNCONFIRMED
    seq = [({"A", "B"}, True), (set(), False)]
    i = {"n": 0}
    def _next_tok(m):
        n = min(i["n"], len(seq)-1)
        i["n"] += 1
        return seq[n]
    def _next_c(m):
        t, o = _next_tok(m)
        return (t, set(), o, True)
    with patch("plugin.writer.inline_review._agent_change_tokens", side_effect=_next_tok), \
         patch("plugin.writer.inline_review._agent_and_foreign_redline_snapshot", side_effect=_next_c), \
         patch("plugin.writer.inline_review._all_redlines_are_agent", return_value=True):
        n = resolve_all_agent_changes(MagicMock(), MagicMock(), True)
    assert n == _RESOLVE_ALL_UNCONFIRMED


def test_resolve_all_unconfirmed_when_unreliable_after_a_resolve():
    # Loop path: a resolve already ran, THEN the snapshot goes unreliable -> UNCONFIRMED, not -1.
    from plugin.writer.inline_review import _RESOLVE_ALL_UNCONFIRMED
    calls = {"n": 0}

    def flaky_tokens(model):
        calls["n"] += 1
        # 1: entry (reliable); 2: loop-top pass 1 (reliable) -> resolve runs; 3: after-resolve (unreliable)
        return ({"A", "B"}, True) if calls["n"] <= 2 else ({"A", "B"}, False)

    def _c_fake(m):
        t, o = flaky_tokens(m)
        return (t, set(), o, True)
    with patch("plugin.writer.inline_review._agent_change_tokens", side_effect=flaky_tokens), \
         patch("plugin.writer.inline_review._agent_and_foreign_redline_snapshot", side_effect=_c_fake), \
         patch("plugin.writer.inline_review.resolve_agent_change", return_value=True), \
         patch("plugin.writer.inline_review._all_redlines_are_agent", return_value=False):
        n = resolve_all_agent_changes(MagicMock(), MagicMock(), True)
    assert n == _RESOLVE_ALL_UNCONFIRMED


def test_feedback_unconfirmed_reports_partial_not_nothing():
    # The -2 sentinel must NEVER produce a "nothing was changed" message; it says some may have
    # resolved and to review the rest.
    from plugin.writer.inline_review import _RESOLVE_ALL_UNCONFIRMED
    with patch("plugin.writer.inline_review._agent_change_tokens", return_value=({"A", "B"}, True)), \
         patch("plugin.writer.inline_review.resolve_all_agent_changes", return_value=_RESOLVE_ALL_UNCONFIRMED):
        n, msg = resolve_all_with_feedback(MagicMock(), MagicMock(), True)
    assert n == 0
    assert "nothing was changed" not in msg.lower()
    assert "couldn't confirm" in msg.lower() and "review" in msg.lower()

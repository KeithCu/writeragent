# WriterAgent - Tests for Tool Call Timing Analyzer
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for scripts/analyze_tool_call_timing.py."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from analyze_tool_call_timing import (  # noqa: E402
    DONE_MARKERS,
    SEND_MARKER,
    analyze,
    find_log_path,
)


def _write_log(path: Path, rows: list[str]) -> None:
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")


def test_normal_two_round_turn(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Normal 2-round turn computes T1 for both rounds and T2 between them."""
    log_file = tmp_path / "writeragent_debug.log"
    _write_log(
        log_file,
        [
            "2026-10-25 10:00:00,000 | plugin.chatbot.tool_loop | DEBUG | Tool loop round 0: sending 2 messages to API...",
            "2026-10-25 10:00:01,000 | plugin.framework.client.llm_client | INFO | streaming_loop: [DONE] received",
            "2026-10-25 10:00:01,500 | plugin.chatbot.tool_loop | DEBUG | Tool loop round 1: sending 4 messages to API...",
            "2026-10-25 10:00:03,500 | plugin.framework.client.llm_client | INFO | LLM response stream finished: provider='mock'",
        ],
    )
    rc = analyze(log_file)
    assert rc == 0
    out = capsys.readouterr().out
    assert "Round 0: send at 10:00:00.000 -> stream end at 10:00:01.000  =>  T1 = 1.00 s (API/stream)" in out
    assert "stream end -> next round send at 10:00:01.500  =>  T2 = 0.50 s (our code)" in out
    assert "Round 1: send at 10:00:01.500 -> stream end at 10:00:03.500  =>  T1 = 2.00 s (API/stream)" in out


def test_comma_and_dot_millisecond_separators(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Both comma and dot millisecond separators in timestamps parse correctly."""
    log_file = tmp_path / "dot_ms.log"
    _write_log(
        log_file,
        [
            "2026-10-25 10:00:00.123 | plugin.chatbot.tool_loop | DEBUG | Tool loop round 0: sending 2 messages to API...",
            "2026-10-25 10:00:01.123 | plugin.framework.client.llm_client | INFO | streaming_loop: [DONE] received",
        ],
    )
    assert analyze(log_file) == 0
    out = capsys.readouterr().out
    assert "T1 = 1.00 s (API/stream)" in out


def test_out_of_order_physical_lines(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Out-of-order physical log lines are sorted by chronological timestamp."""
    log_file = tmp_path / "unordered.log"
    _write_log(
        log_file,
        [
            "2026-10-25 10:00:02,000 | plugin.framework.client.llm_client | INFO | streaming_loop: [DONE] received",
            "2026-10-25 10:00:00,000 | plugin.chatbot.tool_loop | DEBUG | Tool loop round 0: sending 1 messages to API...",
        ],
    )
    assert analyze(log_file) == 0
    out = capsys.readouterr().out
    assert "Round 0: send at 10:00:00.000 -> stream end at 10:00:02.000  =>  T1 = 2.00 s (API/stream)" in out


def test_missing_done_marker_with_stream_finished(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """When [DONE] is missing, 'LLM response stream finished:' still ends the round."""
    log_file = tmp_path / "finished.log"
    _write_log(
        log_file,
        [
            "2026-10-25 10:00:00,000 | plugin.chatbot.tool_loop | DEBUG | Tool loop round 0: sending 2 messages to API...",
            "2026-10-25 10:00:02,500 | plugin.framework.client.llm_client | INFO | LLM response stream finished: provider='anthropic'",
        ],
    )
    assert analyze(log_file) == 0
    out = capsys.readouterr().out
    assert "T1 = 2.50 s (API/stream)" in out


def test_duplicate_identical_lines_collapse(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Duplicate identical log lines at the same millisecond collapse via set deduplication."""
    log_file = tmp_path / "dups.log"
    _write_log(
        log_file,
        [
            "2026-10-25 10:00:00,000 | plugin.chatbot.tool_loop | DEBUG | Tool loop round 0: sending 2 messages to API...",
            "2026-10-25 10:00:00,000 | plugin.chatbot.tool_loop | DEBUG | Tool loop round 0: sending 2 messages to API...",
            "2026-10-25 10:00:01,000 | plugin.framework.client.llm_client | INFO | streaming_loop: [DONE] received",
            "2026-10-25 10:00:01,000 | plugin.framework.client.llm_client | INFO | streaming_loop: [DONE] received",
        ],
    )
    assert analyze(log_file) == 0
    out = capsys.readouterr().out
    assert out.count("Round 0:") == 1


def test_session_restart_prints_new_session_and_no_spanning_t2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Session restart (0, 1, 0, 1) prints 'new session' and suppresses T2 across sessions."""
    log_file = tmp_path / "restart.log"
    _write_log(
        log_file,
        [
            # Session 1
            "2026-10-25 10:00:00,000 | plugin.chatbot.tool_loop | DEBUG | Tool loop round 0: sending 2 messages to API...",
            "2026-10-25 10:00:01,000 | plugin.framework.client.llm_client | INFO | streaming_loop: [DONE] received",
            "2026-10-25 10:00:02,000 | plugin.chatbot.tool_loop | DEBUG | Tool loop round 1: sending 3 messages to API...",
            "2026-10-25 10:00:03,000 | plugin.framework.client.llm_client | INFO | streaming_loop: [DONE] received",
            # Session 2 (starts 5 minutes later at round 0)
            "2026-10-25 10:05:00,000 | plugin.chatbot.tool_loop | DEBUG | Tool loop round 0: sending 2 messages to API...",
            "2026-10-25 10:05:01,000 | plugin.framework.client.llm_client | INFO | streaming_loop: [DONE] received",
            "2026-10-25 10:05:02,000 | plugin.chatbot.tool_loop | DEBUG | Tool loop round 1: sending 3 messages to API...",
            "2026-10-25 10:05:03,000 | plugin.framework.client.llm_client | INFO | streaming_loop: [DONE] received",
        ],
    )
    assert analyze(log_file) == 0
    out = capsys.readouterr().out
    assert "(new session: round 0 after round 1)" in out
    # Both in-session T2s are present
    assert out.count("T2 = 1.00 s (our code)") == 2
    # No cross-session T2 (~297s) exists
    assert "297" not in out


def test_incomplete_round(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """An incomplete round (send 0, send 1, done) reports incomplete for round 0 and reports round 1."""
    log_file = tmp_path / "incomplete.log"
    _write_log(
        log_file,
        [
            "2026-10-25 10:00:00,000 | plugin.chatbot.tool_loop | DEBUG | Tool loop round 0: sending 2 messages to API...",
            "2026-10-25 10:00:02,000 | plugin.chatbot.tool_loop | DEBUG | Tool loop round 1: sending 3 messages to API...",
            "2026-10-25 10:00:04,000 | plugin.framework.client.llm_client | INFO | streaming_loop: [DONE] received",
        ],
    )
    assert analyze(log_file) == 0
    out = capsys.readouterr().out
    assert "Round 0: send at 10:00:00.000 -> no stream end before the next send (incomplete)" in out
    assert "Round 1: send at 10:00:02.000 -> stream end at 10:00:04.000  =>  T1 = 2.00 s (API/stream)" in out


def test_missing_log_path_returns_one(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A missing log file prints an error to stderr and returns exit code 1."""
    rc = analyze(tmp_path / "does_not_exist.log")
    assert rc == 1
    err = capsys.readouterr().err
    assert "Log file not found" in err


def test_marker_drift() -> None:
    """Ensure SEND_MARKER and DONE_MARKERS match the actual log messages in host code."""
    repo_root = Path(__file__).resolve().parents[2]
    tool_loop_src = (repo_root / "plugin" / "chatbot" / "tool_loop.py").read_text(encoding="utf-8")
    llm_client_src = (repo_root / "plugin" / "framework" / "client" / "llm_client.py").read_text(encoding="utf-8")

    assert SEND_MARKER in tool_loop_src, f"SEND_MARKER {SEND_MARKER!r} not found in tool_loop.py"
    for marker in DONE_MARKERS:
        assert marker in llm_client_src, f"DONE_MARKERS item {marker!r} not found in llm_client.py"


def test_find_log_path_exposed() -> None:
    """find_log_path is exported and callable."""
    assert callable(find_log_path)

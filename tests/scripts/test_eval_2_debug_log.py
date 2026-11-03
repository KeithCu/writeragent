# WriterAgent tests for scripts/eval_2_debug_log.py / save_eval2_debug_log.py
from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from eval_2_debug_log import (  # noqa: E402
    DEBUG_LOG_FILENAME,
    copy_debug_log,
    debug_log_candidates,
    find_debug_log,
    lo_user_profile_dirs,
    snapshot_debug_log,
)
from eval_2_headed import writeragent_json_candidates  # noqa: E402
from save_eval2_debug_log import main as save_main  # noqa: E402


def test_filename_matches_plugin_logging() -> None:
    from plugin.framework.logging import DEBUG_LOG_FILENAME as plugin_name

    assert DEBUG_LOG_FILENAME == plugin_name == "writeragent_debug.log"


def test_debug_log_candidates_share_json_profile_dirs() -> None:
    json_parents = [path.parent for path in writeragent_json_candidates()]
    log_parents = [path.parent for path in debug_log_candidates()]
    assert json_parents == log_parents == lo_user_profile_dirs()
    assert all(path.name == DEBUG_LOG_FILENAME for path in debug_log_candidates())


def test_find_debug_log_prefers_first_existing(tmp_path: Path) -> None:
    missing = tmp_path / "missing" / DEBUG_LOG_FILENAME
    first = tmp_path / "first" / DEBUG_LOG_FILENAME
    second = tmp_path / "second" / DEBUG_LOG_FILENAME
    first.parent.mkdir()
    second.parent.mkdir()
    first.write_text("FIRST\n", encoding="utf-8")
    second.write_text("SECOND\n", encoding="utf-8")
    assert find_debug_log(candidates=[missing, first, second]) == first
    assert find_debug_log(explicit=second) == second
    assert find_debug_log(explicit=missing) is None
    assert find_debug_log(candidates=[missing]) is None


def test_copy_debug_log_writes_full_file(tmp_path: Path) -> None:
    live = tmp_path / "live" / DEBUG_LOG_FILENAME
    live.parent.mkdir()
    body = "Debug log active: /tmp/live\nround 1 sending\n"
    live.write_text(body, encoding="utf-8")
    dest_dir = tmp_path / "runs" / "20260909-1748"
    source, dest = copy_debug_log(dest_dir, source=live)
    assert source == live
    assert dest == dest_dir / DEBUG_LOG_FILENAME
    assert dest.read_text(encoding="utf-8") == body
    # Live file is unchanged (no truncate).
    assert live.read_text(encoding="utf-8") == body


def test_copy_debug_log_missing_raises(tmp_path: Path) -> None:
    missing = tmp_path / "nope" / DEBUG_LOG_FILENAME
    with pytest.raises(FileNotFoundError, match="writeragent_debug.log"):
        copy_debug_log(tmp_path / "dest", source=missing)


def test_copy_debug_log_same_path_is_noop(tmp_path: Path) -> None:
    live = tmp_path / DEBUG_LOG_FILENAME
    live.write_text("same\n", encoding="utf-8")
    source, dest = copy_debug_log(tmp_path, source=live)
    assert source == dest == live
    assert live.read_text(encoding="utf-8") == "same\n"


def test_snapshot_debug_log_warns_when_missing(tmp_path: Path) -> None:
    buf = io.StringIO()
    missing = tmp_path / "nope.log"
    result = snapshot_debug_log(tmp_path / "dest", source=missing, file=buf)
    assert result is None
    assert not (tmp_path / "dest" / DEBUG_LOG_FILENAME).exists()
    assert "Warning:" in buf.getvalue()
    assert "writeragent_debug.log" in buf.getvalue()


def test_snapshot_debug_log_returns_dest(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    live = tmp_path / "live.log"
    # Binary write: Path.write_text("trace\n") becomes 7 bytes on Windows (CRLF).
    body = b"trace\n"
    live.write_bytes(body)
    dest_dir = tmp_path / "stamp"
    result = snapshot_debug_log(dest_dir, source=live)
    assert result == dest_dir / DEBUG_LOG_FILENAME
    assert result is not None
    assert result.read_bytes() == body
    printed = capsys.readouterr().out
    assert str(live) in printed
    assert f"{len(body)} bytes" in printed



def test_save_eval2_debug_log_helper_prints_source_and_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    live = tmp_path / "profile" / DEBUG_LOG_FILENAME
    live.parent.mkdir()
    live.write_text("stall-trace\n", encoding="utf-8")
    dest_dir = tmp_path / "run"
    monkeypatch.setattr("eval_2_debug_log.debug_log_candidates", lambda: [live])
    assert save_main([str(dest_dir)]) == 0
    out = capsys.readouterr().out
    dest = dest_dir / DEBUG_LOG_FILENAME
    assert f"source: {live}" in out
    assert f"dest: {dest} ({dest.stat().st_size} bytes)" in out
    assert dest.read_text(encoding="utf-8") == "stall-trace\n"


def test_save_eval2_debug_log_helper_exits_nonzero_when_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        "eval_2_debug_log.debug_log_candidates",
        lambda: [tmp_path / "absent.log"],
    )
    assert save_main([str(tmp_path / "dest")]) == 1
    err = capsys.readouterr().err
    assert "Could not find writeragent_debug.log" in err
    assert not (tmp_path / "dest" / DEBUG_LOG_FILENAME).exists()


def test_analyze_tool_call_timing_uses_shared_finder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from analyze_tool_call_timing import find_log_path

    live = tmp_path / DEBUG_LOG_FILENAME
    live.write_text("x\n", encoding="utf-8")
    monkeypatch.setattr("eval_2_debug_log.debug_log_candidates", lambda: [live])
    assert find_log_path() == live

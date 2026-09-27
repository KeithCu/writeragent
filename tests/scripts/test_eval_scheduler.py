# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Dual-lane scheduler and per-task backend selection (no soffice, no API)."""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

_PO = Path(__file__).resolve().parents[2] / "scripts" / "prompt_optimization"
if str(_PO) not in sys.path:
    sys.path.insert(0, str(_PO))

from dataset import ALL_EXAMPLES, PYTHON_SHAPES_FLAG_ASK, to_eval_examples  # noqa: E402
from eval_scheduler import (  # noqa: E402
    LoLane,
    declared_backend,
    resolve_backend,
    run_dual_lane,
    select_pack,
)


def _events() -> tuple[list[tuple[str, float]], threading.Lock]:
    return [], threading.Lock()


def test_declared_backend_defaults_string_and_flag_is_lo() -> None:
    kinds = {ex["task_id"]: declared_backend(ex) for ex in ALL_EXAMPLES}
    assert kinds["python_shapes_flag"] == "lo"
    assert kinds["table_from_mess"] == "string"
    assert kinds["py_refuse_overlap"] == "string"
    assert sum(1 for kind in kinds.values() if kind == "lo") == 1
    assert len(ALL_EXAMPLES) == 18
    flag = next(ex for ex in ALL_EXAMPLES if ex["task_id"] == "python_shapes_flag")
    assert flag["user_question"] == PYTHON_SHAPES_FLAG_ASK
    assert flag["document_content"] == ""
    prompt = (
        Path(__file__).resolve().parents[2]
        / "docs/eval/eval-2/python-shapes-flag/prompt.writeragent.txt"
    )
    assert prompt.read_text(encoding="utf-8").strip() == PYTHON_SHAPES_FLAG_ASK


def test_resolve_backend_cli_overrides_row() -> None:
    flag = next(ex for ex in ALL_EXAMPLES if ex["task_id"] == "python_shapes_flag")
    plain = next(ex for ex in ALL_EXAMPLES if ex["task_id"] == "table_from_mess")
    assert resolve_backend(flag, "auto") == "lo"
    assert resolve_backend(plain, "auto") == "string"
    assert resolve_backend(flag, "string") == "string"
    assert resolve_backend(plain, "lo") == "lo"


def test_select_pack_string_skips_flag_unless_explicit() -> None:
    examples = to_eval_examples(ALL_EXAMPLES)
    skipped = select_pack(examples, cli_backend="string", explicit=False, student="llm")
    assert skipped.error is None
    assert len(skipped.examples) == 17
    assert "python_shapes_flag" in skipped.notes[0]
    # An explicit -e that includes the flag is an error, not a silent drop.
    mixed = select_pack(examples, cli_backend="string", explicit=True, student="llm")
    assert mixed.error
    assert mixed.examples == []
    only_flag = [ex for ex in examples if ex.task_id == "python_shapes_flag"]
    refused = select_pack(only_flag, cli_backend="string", explicit=True, student="llm")
    assert refused.error
    assert "backend=lo" in refused.error


def test_select_pack_scripted_skips_flag_on_auto() -> None:
    examples = to_eval_examples(ALL_EXAMPLES)
    selected = select_pack(examples, cli_backend="auto", explicit=False, student="scripted")
    assert selected.error is None
    assert len(selected.examples) == 17
    assert "no scripted replay" in selected.notes[0]
    only_flag = [ex for ex in examples if ex.task_id == "python_shapes_flag"]
    refused = select_pack(only_flag, cli_backend="auto", explicit=True, student="scripted")
    assert refused.error
    assert "python_shapes_flag" in refused.error


def test_select_pack_lo_keeps_flag_and_drops_py_rows() -> None:
    examples = to_eval_examples(ALL_EXAMPLES)
    selected = select_pack(examples, cli_backend="lo", explicit=False, student="llm")
    ids = [ex.task_id for ex in selected.examples]
    assert "python_shapes_flag" in ids
    assert "py_refuse_overlap" not in ids
    assert "py_no_bulk_read" not in ids
    # Explicit -e of a =PY row still runs it (the old non-explicit filter).
    py = [ex for ex in examples if ex.task_id == "py_refuse_overlap"]
    kept = select_pack(py, cli_backend="lo", explicit=True, student="llm")
    assert [ex.task_id for ex in kept.examples] == ["py_refuse_overlap"]


def test_lo_lane_is_fifo() -> None:
    lane = LoLane()
    seen: list[int] = []

    def job(n: int) -> int:
        seen.append(n)
        time.sleep(0.05)
        return n

    try:
        first = lane.submit(lambda: job(1))
        second = lane.submit(lambda: job(2))
        assert first.result(timeout=2) == 1
        assert second.result(timeout=2) == 2
        assert seen == [1, 2]
    finally:
        lane.close()
    with pytest.raises(RuntimeError, match="closed"):
        lane.submit(lambda: None)


def test_lanes_overlap_and_lo_stays_fifo() -> None:
    events, lock = _events()

    def mark(name: str) -> None:
        with lock:
            events.append((name, time.monotonic()))

    def lo_job(name: str, seconds: float) -> str:
        mark(name + "-start")
        time.sleep(seconds)
        mark(name + "-end")
        return name

    def string_job() -> str:
        mark("string-start")
        time.sleep(0.2)
        mark("string-end")
        return "string"

    started = time.monotonic()
    results = run_dual_lane(
        [
            ("lo", lambda: lo_job("lo1", 0.15)),
            ("string", string_job),
            ("lo", lambda: lo_job("lo2", 0.05)),
        ],
        string_workers=2,
    )
    wall = time.monotonic() - started
    assert results == ["lo1", "string", "lo2"]
    times = {name: stamp for name, stamp in events}
    # String work is in flight while the first LO job is still running.
    assert times["string-start"] < times["lo1-end"]
    assert times["lo1-start"] < times["string-end"]
    # Second LO job waits for the first. It does not start in parallel.
    assert times["lo2-start"] >= times["lo1-end"]
    # Serial sum is 0.40s. Overlap should land near max(0.20, 0.20).
    assert wall < 0.35


def test_string_job_does_not_wait_on_lo_queue() -> None:
    events, lock = _events()

    def mark(name: str) -> None:
        with lock:
            events.append((name, time.monotonic()))

    def lo_job() -> str:
        mark("lo-start")
        time.sleep(0.25)
        mark("lo-end")
        return "lo"

    def string_job() -> str:
        mark("string-start")
        time.sleep(0.05)
        mark("string-end")
        return "string"

    results = run_dual_lane(
        [("lo", lo_job), ("string", string_job)],
        string_workers=2,
    )
    assert results == ["lo", "string"]
    times = {name: stamp for name, stamp in events}
    assert times["string-end"] < times["lo-end"]


def test_auto_eval_overlaps_string_and_lo(monkeypatch: pytest.MonkeyPatch) -> None:
    """run_eval_on_examples_llm must not finish every string row before LO."""
    events, lock = _events()

    def fake_run(**kwargs: object) -> tuple:
        backend = str(kwargs["backend"])
        task_id = str(kwargs["task_id"])
        with lock:
            events.append((task_id + "-start", time.monotonic(), backend))
        time.sleep(0.15)
        with lock:
            events.append((task_id + "-end", time.monotonic(), backend))
        return ("ok", {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}, None, [])

    monkeypatch.setattr("llm_chat_eval.run_llm_chat_eval", fake_run)
    from eval_core import run_eval_on_examples_llm

    string_ex = SimpleNamespace(
        document_content="x",
        user_question="q",
        task_id="comment_management",
        expected_contains=[],
        reject_contains=[],
        rubric="",
        gold_document="gold",
        is_non_trivial=False,
        category="structural",
        use_quality_judge=False,
        backend="string",
    )
    lo_ex = SimpleNamespace(
        document_content="x",
        user_question="q",
        task_id="table_from_mess",
        expected_contains=[],
        reject_contains=[],
        rubric="",
        gold_document="gold",
        is_non_trivial=False,
        category="structural",
        use_quality_judge=False,
        backend="lo",
    )
    started = time.monotonic()
    results = run_eval_on_examples_llm(
        [string_ex, lo_ex],
        endpoint="https://openrouter.ai/api/v1",
        api_key="",
        model="scripted",
        backend="auto",
        student="llm",
        no_judge=True,
        bust_cache=False,
        quiet=True,
        string_jobs=2,
    )
    wall = time.monotonic() - started
    assert [r.task_id for r in results] == ["comment_management", "table_from_mess"]
    times = {name: stamp for name, stamp, _backend in events}
    assert times["comment_management-start"] < times["table_from_mess-end"]
    assert times["table_from_mess-start"] < times["comment_management-end"]
    # Two 0.15s jobs staged would be ~0.30s. Overlap stays under that.
    assert wall < 0.28


def test_flag_row_bumps_tool_rounds(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_run(**kwargs: object) -> tuple:
        captured["rounds"] = kwargs["max_tool_rounds"]
        captured["backend"] = kwargs["backend"]
        captured["prompt"] = kwargs["system_prompt"]
        return ("", {"total_tokens": 0}, None, [])

    monkeypatch.setattr("llm_chat_eval.run_llm_chat_eval", fake_run)
    from eval_core import run_eval_on_examples_llm

    flag = next(ex for ex in to_eval_examples(ALL_EXAMPLES) if ex.task_id == "python_shapes_flag")
    # CLI string forces the string world. Scoring fails closed; no soffice.
    rows = run_eval_on_examples_llm(
        [flag],
        endpoint="https://openrouter.ai/api/v1",
        api_key="",
        model="m",
        backend="string",
        student="llm",
        no_judge=True,
        bust_cache=False,
        quiet=True,
        max_tool_rounds=25,
    )
    assert captured["rounds"] == 50
    assert captured["backend"] == "string"
    assert "domain=\"python\"" in str(captured["prompt"])
    assert rows[0].oracle_failures
    assert "string simulator" in rows[0].oracle_failures[0]


def test_string_cli_refuses_explicit_flag(capsys: pytest.CaptureFixture[str]) -> None:
    import run_eval

    code = run_eval.main(["--backend", "string", "-e", "python_shapes_flag", "--no-judge"])
    err = capsys.readouterr().err
    assert code == 1
    assert "backend=lo" in err


def test_string_scripted_cli_skips_flag(capsys: pytest.CaptureFixture[str]) -> None:
    import run_eval

    code = run_eval.main(
        ["--backend", "string", "--student", "scripted", "--no-bust-cache"]
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "Skipping python_shapes_flag" in out
    assert "Scripted result pass: 17/17" in out


def test_auto_scripted_cli_skips_flag(capsys: pytest.CaptureFixture[str]) -> None:
    import run_eval

    code = run_eval.main(
        ["--backend", "auto", "--student", "scripted", "--no-bust-cache"]
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "no scripted replay" in out
    assert "Scripted result pass: 17/17" in out

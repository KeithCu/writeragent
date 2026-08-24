# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Playback tests for the scripted eval student (no soffice, no API)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_PO = Path(__file__).resolve().parents[2] / "scripts" / "prompt_optimization"
if str(_PO) not in sys.path:
    sys.path.insert(0, str(_PO))

from dataset import ALL_EXAMPLES, task_kind  # noqa: E402
from scripted_student import SCRIPTS, ScriptedStudent  # noqa: E402


def test_task_kind_from_task_id() -> None:
    assert task_kind("flowchart_gen") == "draw"
    assert task_kind("data_sorting") == "calc"
    assert task_kind("tax_column") == "calc"
    assert task_kind("table_from_mess") == "writer"


def test_scripts_cover_all_examples() -> None:
    ids = {ex["task_id"] for ex in ALL_EXAMPLES}
    assert ids <= set(SCRIPTS)


def test_playback_order_and_stop_on_content_only() -> None:
    student = ScriptedStudent("table_from_mess")
    first = student.request_with_tools([{"role": "user", "content": "x"}], tools=[])
    assert first["tool_calls"]
    assert first["tool_calls"][0]["function"]["name"] == "apply_document_content"
    second = student.request_with_tools([], tools=[])
    assert not second.get("tool_calls")
    assert second.get("content")
    third = student.request_with_tools([], tools=[])
    assert not third.get("tool_calls")


def test_unknown_task_raises() -> None:
    with pytest.raises(KeyError, match="nope"):
        ScriptedStudent("nope")


def test_tax_and_sort_use_production_names() -> None:
    sort_round = SCRIPTS["data_sorting"][0]
    assert sort_round["tool_calls"][0]["function"]["name"] == "sort_range"
    tax_names = [
        tc["function"]["name"]
        for rnd in SCRIPTS["tax_column"]
        for tc in (rnd.get("tool_calls") or [])
    ]
    assert "write_formula_range" in tax_names
    assert "get_sheet_summary" in tax_names

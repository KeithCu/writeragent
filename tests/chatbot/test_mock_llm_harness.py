# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for mock-LLM dual-sidebar harness helpers (no live soffice)."""

from __future__ import annotations

import zipfile

from tests.chatbot.mock_llm_harness import (
    finish_immediately_after_peer_sends,
    write_budget_csv,
    write_minimal_ods,
)


def test_write_budget_csv(tmp_path):
    path = tmp_path / "BudgetPeer.csv"
    write_budget_csv(str(path))
    text = path.read_text(encoding="utf-8")
    assert "Amount" in text
    assert "Apples" in text


def test_write_minimal_ods_is_ods_zip(tmp_path):
    path = tmp_path / "BudgetPeer.ods"
    write_minimal_ods(str(path))
    with zipfile.ZipFile(path) as zf:
        names = set(zf.namelist())
        assert "mimetype" in names
        assert "content.xml" in names
        assert zf.read("mimetype") == b"application/vnd.oasis.opendocument.spreadsheet"
        assert b"Sheet1" in zf.read("content.xml")


def test_finish_immediately_after_peer_sends():
    captures = [
        {"decided_tools": ["delegate_to_specialized_writer_toolset"]},
        {"decided_tools": ["send_peer_work"]},
        {"decided_tools": ["specialized_workflow_finished"]},
        {"decided_tools": ["write_formula_range"]},
        {"decided_tools": ["send_peer_work"]},
        {"decided_tools": ["final_answer"]},
    ]
    assert finish_immediately_after_peer_sends(captures) is True
    hang = [
        {"decided_tools": ["send_peer_work"]},
        {"decided_tools": ["list_nearby_files"]},
        {"decided_tools": ["list_nearby_files"]},
    ]
    assert finish_immediately_after_peer_sends(hang) is False
    assert finish_immediately_after_peer_sends([{"decided_tools": ["hello"]}]) is False

# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for mock-LLM dual-sidebar harness helpers (no live soffice)."""

from __future__ import annotations

from tests.chatbot.mock_llm_harness import finish_immediately_after_peer_sends


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

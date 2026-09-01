# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for the Writer eval harness system prompt."""

from scripts.prompt_optimization.eval_prompts import get_writer_eval_chat_system_prompt


def test_get_writer_eval_chat_system_prompt_lists_eval_tools() -> None:
    p = get_writer_eval_chat_system_prompt()
    assert "get_document_content" in p
    assert "apply_document_content" in p
    assert "search_in_document" in p
    assert "unsupported_in_eval" in p
    assert "Eval harness" in p
    assert "Only get_document_content" not in p


def test_get_writer_eval_chat_system_prompt_includes_format_rules() -> None:
    p = get_writer_eval_chat_system_prompt()
    assert "APPLY_DOCUMENT_CONTENT" in p or "HTML" in p

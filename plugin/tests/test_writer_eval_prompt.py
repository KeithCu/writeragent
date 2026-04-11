# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for the Writer eval harness system prompt."""

from plugin.framework.constants import get_writer_eval_chat_system_prompt


def test_get_writer_eval_chat_system_prompt_lists_eval_tools() -> None:
    p = get_writer_eval_chat_system_prompt()
    assert "get_document_content" in p
    assert "apply_document_content" in p
    assert "find_text" in p
    assert "Eval harness" in p


def test_get_writer_eval_chat_system_prompt_includes_format_rules() -> None:
    p = get_writer_eval_chat_system_prompt()
    assert "APPLY_DOCUMENT_CONTENT" in p or "HTML" in p

# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
"""Tests for document_research chat status blocks."""

from __future__ import annotations

from plugin.chatbot.document_research_chat import (
    display_name_for_path_or_name,
    document_open_preview_line,
    document_open_step_chat_text,
)


def test_display_name_uses_basename_for_absolute_path():
    assert display_name_for_path_or_name("/tmp/Budget_2026.ods") == "Budget_2026.ods"


def test_display_name_keeps_relative_name():
    assert display_name_for_path_or_name("Budget.ods") == "Budget.ods"


def test_step_zero_tool_and_preview_only():
    q = "/tmp/Budget_2026.ods"
    text = document_open_step_chat_text(q, 0)
    assert "Tool: delegate_read_document" in text
    assert "Budget_2026.ods" in text
    assert "read-only" in text.lower()
    assert "[Document research]" not in text
    assert "[Additional document research]" not in text


def test_step_one_same_format_as_first():
    q = "/tmp/Brief.odt"
    first = document_open_step_chat_text(q, 0)
    second = document_open_step_chat_text(q, 1)
    assert first == second
    assert "[Additional document research]" not in second
    assert "[Document research]" not in second


def test_step_index_negative_treated_as_first():
    q = "Budget.ods"
    assert document_open_step_chat_text(q, -1) == document_open_step_chat_text(q, 0)


def test_document_open_preview_line():
    line = document_open_preview_line("/tmp/foo.ods")
    assert "foo.ods" in line
    assert "read-only" in line.lower()

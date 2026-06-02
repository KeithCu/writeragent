# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

from plugin.framework.client.stream_normalizer import _extract_thinking_from_delta


def test_extract_thinking_from_delta_reasoning_field():
    """Ollama OpenAI-compat streams Qwen3 thinking on delta.reasoning, not reasoning_content."""
    chunk = {"choices": [{"delta": {"reasoning": "Let me think about this..."}}]}
    assert _extract_thinking_from_delta(chunk) == "Let me think about this..."


def test_extract_thinking_from_delta_reasoning_content_field():
    chunk = {"choices": [{"delta": {"reasoning_content": "Chain of thought here."}}]}
    assert _extract_thinking_from_delta(chunk) == "Chain of thought here."


def test_extract_thinking_from_delta_prefers_reasoning_content_over_reasoning():
    chunk = {
        "choices": [
            {
                "delta": {
                    "reasoning_content": "official trace",
                    "reasoning": "ollama trace",
                }
            }
        ]
    }
    assert _extract_thinking_from_delta(chunk) == "official trace"


def test_extract_thinking_from_delta_nested_delta_only():
    delta = {"thinking": "native thinking chunk"}
    assert _extract_thinking_from_delta(delta) == "native thinking chunk"


def test_extract_thinking_from_delta_empty_when_no_fields():
    assert _extract_thinking_from_delta({"choices": [{"delta": {"content": "hello"}}]}) == ""


def test_extract_thinking_from_delta_reasoning_details_text():
    chunk = {
        "choices": [
            {
                "delta": {
                    "reasoning_details": [
                        {"type": "reasoning.text", "text": "Let me think", "index": 0},
                    ]
                }
            }
        ]
    }
    assert _extract_thinking_from_delta(chunk) == "Let me think"


def test_extract_thinking_from_delta_reasoning_details_metadata_only():
    """OpenRouter may send type/format/index before text arrives (pydantic-ai#3658)."""
    chunk = {
        "choices": [
            {
                "delta": {
                    "reasoning_details": [
                        {"type": "reasoning.text", "format": "anthropic-claude-v1", "index": 0},
                    ]
                }
            }
        ]
    }
    assert _extract_thinking_from_delta(chunk) == ""


def test_extract_thinking_from_delta_nested_choices_one_level_only():
    """Pathological nested choices: one normalize step, no recursion hang."""
    chunk = {"choices": [{"delta": {"choices": [{"delta": {"reasoning": "deep"}}]}}]}
    assert _extract_thinking_from_delta(chunk) == ""

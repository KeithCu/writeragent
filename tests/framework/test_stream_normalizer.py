# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

from typing import Any

from plugin.framework.client import stream_normalizer as sn
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


def test_extract_reasoning_replay_reasoning_content_only():
    replay = sn.extract_reasoning_replay_from_response(
        message_snapshot={"reasoning_content": "trace-a"},
    )
    assert replay == {"reasoning_content": "trace-a"}


def test_extract_reasoning_replay_reasoning_details_and_string():
    """Sync path prefers reasoning_details; does not also echo reasoning string."""
    replay = sn.extract_reasoning_replay_from_response(
        sync_message={
            "reasoning": "also",
            "reasoning_details": [{"type": "reasoning.text", "text": "step", "index": 0}],
        },
    )
    assert replay == {"reasoning_details": [{"type": "reasoning.text", "text": "step", "index": 0}]}


def test_accumulate_streaming_thinking_concatenates_chunks():
    parts: list[str] = []
    meta = sn.new_streaming_thinking_meta()
    for piece in ("Let me ", "check ", "the weather."):
        sn.accumulate_streaming_thinking(parts, meta, {"reasoning_content": piece})
    assert "".join(parts) == "Let me check the weather."
    assert meta["source"] == "reasoning_content"


def test_extract_reasoning_replay_one_block_reasoning_details():
    parts: list[str] = []
    meta = sn.new_streaming_thinking_meta()
    sn.accumulate_streaming_thinking(
        parts,
        meta,
        {"reasoning_details": [{"type": "reasoning.text", "format": "anthropic-claude-v1", "index": 0}]},
    )
    sn.accumulate_streaming_thinking(
        parts,
        meta,
        {"reasoning_details": [{"type": "reasoning.text", "text": "Let me ", "format": "unknown", "index": 0}]},
    )
    sn.accumulate_streaming_thinking(
        parts,
        meta,
        {"reasoning_details": [{"type": "reasoning.text", "text": "think.", "format": "unknown", "index": 0}]},
    )
    replay = sn.extract_reasoning_replay_from_response(streaming_text="".join(parts), streaming_meta=meta)
    assert "reasoning" not in replay
    assert len(replay["reasoning_details"]) == 1
    assert replay["reasoning_details"][0]["text"] == "Let me think."
    assert replay["reasoning_details"][0]["format"] == "anthropic-claude-v1"


def test_extract_reasoning_replay_one_block_reasoning_string():
    parts: list[str] = []
    meta = sn.new_streaming_thinking_meta()
    sn.accumulate_streaming_thinking(parts, meta, {"reasoning": "Let me "})
    sn.accumulate_streaming_thinking(parts, meta, {"reasoning": "think."})
    replay = sn.extract_reasoning_replay_from_response(streaming_text="".join(parts), streaming_meta=meta)
    assert replay == {"reasoning": "Let me think."}
    assert "reasoning_details" not in replay


def test_extract_reasoning_replay_streaming_ignores_snapshot():
    parts: list[str] = []
    meta = sn.new_streaming_thinking_meta()
    sn.accumulate_streaming_thinking(parts, meta, {"reasoning_details": [{"type": "reasoning.text", "text": "a", "index": 0}]})
    replay = sn.extract_reasoning_replay_from_response(
        streaming_text="".join(parts),
        streaming_meta=meta,
        message_snapshot={
            "reasoning": "duplicate",
            "reasoning_details": [
                {"type": "reasoning.text", "text": "b", "index": 0},
                {"type": "reasoning.text", "text": "c", "index": 0},
            ],
        },
    )
    assert replay == {"reasoning_details": [{"type": "reasoning.text", "text": "a", "index": 0}]}


def test_merge_reasoning_details_merges_same_index():
    merged = sn._merge_reasoning_details(
        [
            {"type": "reasoning.text", "text": "Let me ", "format": "unknown", "index": 0},
            {"type": "reasoning.text", "text": "think.", "format": "unknown", "index": 0},
        ]
    )
    assert len(merged) == 1
    assert merged[0]["text"] == "Let me think."
    assert merged[0]["format"] == "unknown"


def test_merge_reasoning_details_preserves_signature_from_later_fragment():
    merged = sn._merge_reasoning_details(
        [
            {"type": "reasoning.text", "text": "step ", "index": 0},
            {"type": "reasoning.text", "text": "two", "index": 0, "signature": "sig-abc"},
        ]
    )
    assert merged[0]["text"] == "step two"
    assert merged[0]["signature"] == "sig-abc"


def test_merge_reasoning_details_concatenates_encrypted_data():
    merged = sn._merge_reasoning_details(
        [
            {"type": "reasoning.encrypted", "data": "abc", "format": "anthropic-claude-v1", "index": 1},
            {"type": "reasoning.encrypted", "data": "def", "format": "anthropic-claude-v1", "index": 1},
        ]
    )
    assert len(merged) == 1
    assert merged[0]["data"] == "abcdef"
    assert merged[0]["format"] == "anthropic-claude-v1"


def test_streaming_replay_includes_encrypted_with_text():
    parts: list[str] = []
    meta = sn.new_streaming_thinking_meta()
    sn.accumulate_streaming_thinking(
        parts,
        meta,
        {"reasoning_details": [{"type": "reasoning.text", "text": "think", "index": 0, "format": "anthropic-claude-v1"}]},
    )
    sn.accumulate_streaming_thinking(
        parts,
        meta,
        {
            "reasoning_details": [
                {
                    "type": "reasoning.encrypted",
                    "data": "opaque-blob",
                    "format": "anthropic-claude-v1",
                    "index": 1,
                    "id": "enc-1",
                }
            ]
        },
    )
    replay = sn.extract_reasoning_replay_from_response(streaming_text="".join(parts), streaming_meta=meta)
    assert len(replay["reasoning_details"]) == 2
    assert replay["reasoning_details"][0]["type"] == "reasoning.text"
    assert replay["reasoning_details"][0]["text"] == "think"
    assert replay["reasoning_details"][1]["type"] == "reasoning.encrypted"
    assert replay["reasoning_details"][1]["data"] == "opaque-blob"


def test_streaming_replay_encrypted_only():
    parts: list[str] = []
    meta = sn.new_streaming_thinking_meta()
    sn.accumulate_streaming_thinking(
        parts,
        meta,
        {
            "reasoning_details": [
                {"type": "reasoning.encrypted", "data": "only-encrypted", "format": "google-gemini-v1", "index": 0}
            ]
        },
    )
    replay = sn.extract_reasoning_replay_from_response(streaming_text="".join(parts), streaming_meta=meta)
    assert replay == {
        "reasoning_details": [
            {"type": "reasoning.encrypted", "data": "only-encrypted", "format": "google-gemini-v1", "index": 0}
        ]
    }


def test_extract_reasoning_replay_disabled():
    old = sn.PRESERVE_REASONING_IN_SESSION
    try:
        sn.PRESERVE_REASONING_IN_SESSION = False
        assert sn.extract_reasoning_replay_from_response(message_snapshot={"reasoning": "x"}) == {}
    finally:
        sn.PRESERVE_REASONING_IN_SESSION = old


def test_reasoning_replay_from_assistant_response():
    response = {
        "role": "assistant",
        "content": "hi",
        "reasoning": "think",
        "tool_calls": [{"id": "1"}],
    }
    assert sn.reasoning_replay_from_assistant_response(response) == {"reasoning": "think"}

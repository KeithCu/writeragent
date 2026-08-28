# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later


from plugin.framework.client import stream_normalizer as sn
from plugin.framework.client.stream_normalizer import _extract_thinking_from_delta, iterate_sse
from plugin.tests.testing_utils import create_mock_http_response


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


def test_think_tag_stream_splitter_simple():
    splitter = sn.ThinkTagStreamSplitter()
    out = splitter.feed("Hello <think>analyzing formula</think> world")
    assert out == [
        (False, "Hello "),
        (True, "analyzing formula"),
        (False, " world"),
    ]
    assert splitter.flush() == []


def test_think_tag_stream_splitter_chunk_boundaries():
    splitter = sn.ThinkTagStreamSplitter()
    # Tag split across multiple chunk feeds
    out1 = splitter.feed("Result: <th")
    assert out1 == [(False, "Result: ")]
    out2 = splitter.feed("ink>internal reasoning")
    assert out2 == [(True, "internal reasoning")]
    out3 = splitter.feed("</th")
    assert out3 == []
    out4 = splitter.feed("ink>42")
    assert out4 == [(False, "42")]
    assert splitter.flush() == []


def test_think_tag_stream_splitter_no_tags():
    splitter = sn.ThinkTagStreamSplitter()
    assert splitter.feed("Normal plain text stream.") == [(False, "Normal plain text stream.")]
    assert splitter.flush() == []


def test_think_tag_stream_splitter_unclosed_tag():
    splitter = sn.ThinkTagStreamSplitter()
    out1 = splitter.feed("<think>started thinking but never finished")
    assert out1 == [(True, "started thinking but never finished")]
    out2 = splitter.flush()
    assert out2 == []


def test_think_tag_stream_splitter_trailing_buffer_flush():
    splitter = sn.ThinkTagStreamSplitter()
    out1 = splitter.feed("Value is <")
    assert out1 == [(False, "Value is ")]
    out2 = splitter.flush()
    assert out2 == [(False, "<")]


def test_strip_think_tags():
    clean, thinking = sn.strip_think_tags("<think>step 1\nstep 2</think>The final answer is 42.")
    assert clean == "The final answer is 42."
    assert thinking == "step 1\nstep 2"


def test_strip_think_tags_no_tags():
    clean, thinking = sn.strip_think_tags("Direct output without tags.")
    assert clean == "Direct output without tags."
    assert thinking is None


def test_strip_think_tags_unclosed():
    clean, thinking = sn.strip_think_tags("<think>only thinking here")
    assert clean == ""
    assert thinking == "only thinking here"


def test_streaming_replay_truncates_encrypted_fragments_to_shape_dim():
    from plugin.framework.deal_shim import DEAL_MAX_SHAPE_DIM

    meta = {
        "source": "reasoning_details",
        "encrypted_fragments": [{"type": "reasoning.encrypted", "data": "x", "index": i} for i in range(DEAL_MAX_SHAPE_DIM + 1)],
    }
    replay = sn._streaming_replay("", meta)
    assert isinstance(replay, dict)
    details = replay.get("reasoning_details", [])
    assert len(details) == DEAL_MAX_SHAPE_DIM


def test_iterate_sse_skips_comments_blanks_and_keeps_data_and_raw_json():
    """SSE comments / blank lines are dropped; data: and raw JSON lines are payloads."""
    stream = create_mock_http_response(
        sse_lines=[
            b": keep-alive",
            b"",
            b'data: {"choices": [{"delta": {"content": "Hi"}}]}',
            b"data: [DONE]",
            b'{"choices": [{"delta": {"content": "raw"}}]}',
        ]
    )
    assert list(iterate_sse(stream)) == [
        '{"choices": [{"delta": {"content": "Hi"}}]}',
        "[DONE]",
        '{"choices": [{"delta": {"content": "raw"}}]}',
    ]


def test_iterate_sse_truncated_and_non_utf8_lines_still_yield_later_payloads():
    """A truncated data: line is still yielded (JSON decode is the client's job)."""
    stream = create_mock_http_response(
        sse_lines=[
            b'data: {"choices": [{"delta": {"content": "hel',
            b'data: {"choices": [{"delta": {"content": "lo"}}]}',
            b"data: [DONE]",
        ]
    )
    payloads = list(iterate_sse(stream))
    assert payloads[0].startswith('{"choices":')
    assert '"hel' in payloads[0]
    assert payloads[1] == '{"choices": [{"delta": {"content": "lo"}}]}'
    assert payloads[2] == "[DONE]"



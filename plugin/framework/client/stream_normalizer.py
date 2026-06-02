# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

import logging

log = logging.getLogger(__name__)

# OpenAI-compat stream: thinking lives on choices[0].delta (see docs/streaming-and-threading.md).
_THINKING_STRING_FIELDS = ("reasoning_content", "reasoning", "thought", "thinking")
_THINKING_HINT_KEYS = frozenset(_THINKING_STRING_FIELDS) | {"reasoning_details"}


def iterate_sse(stream):
    """
    Iterate over SSE (Server-Sent Events) data payloads from a stream of lines (bytes).
    Yields the payload string. Supports standard 'data:' prefix and raw JSON lines.
    """
    for line in stream:
        line_str = line.strip()
        if not line_str or line_str.startswith(b":"):
            continue

        if line_str.startswith(b"data:"):
            # Payload is everything after the first ":"
            idx = line_str.find(b":") + 1
            payload = line_str[idx:].decode("utf-8").strip()
            yield payload
        elif line_str.startswith(b"{"):
            # Raw JSON line (common in some streaming formats like Google Gemini raw stream)
            yield line_str.decode("utf-8").strip()


def _normalize_stream_delta(chunk_or_delta):
    """Return choices[0].delta for a chat completion chunk, else the dict as-is (bare delta)."""
    if not isinstance(chunk_or_delta, dict):
        return {}
    choices = chunk_or_delta.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0]
        if isinstance(first, dict):
            delta = first.get("delta")
            if isinstance(delta, dict):
                return delta
    return chunk_or_delta


def _thinking_text_from_delta(delta):
    """Extract thinking from a normalized delta (no choices wrapper)."""
    # Ollama /v1 often uses "reasoning", not "reasoning_content" (Qwen-Agent #789, ollama#12628).
    for field in _THINKING_STRING_FIELDS:
        thinking = delta.get(field)
        if isinstance(thinking, str) and thinking:
            return thinking

    details = delta.get("reasoning_details")
    if isinstance(details, list):
        parts = []
        for item in details:
            if isinstance(item, dict):
                item_type = item.get("type")
                if item_type in ("reasoning.text", "thought", "reasoning"):
                    parts.append(item.get("text") or "")
                elif item_type == "reasoning.summary":
                    parts.append(item.get("summary") or "")
        if parts:
            return "".join(parts)
    return ""


def _extract_thinking_from_delta(chunk_or_delta):
    """Extract reasoning/thinking text from a stream chunk or bare delta for display in UI."""
    delta = _normalize_stream_delta(chunk_or_delta)
    result = _thinking_text_from_delta(delta)
    if not result and isinstance(delta, dict):
        hints = {k: delta.get(k) for k in _THINKING_HINT_KEYS if k in delta}
        if hints:
            # Enable debug logging (writeragent_debug.log) when a provider sends thinking-shaped
            # fields we do not parse — e.g. metadata-only reasoning_details (OpenRouter first chunk).
            log.debug("stream thinking: no extractable text; delta hints=%s", hints)
    return result


def _normalize_message_content(raw):
    """Return a single string from API message content (string or list of parts)."""
    if raw is None:
        return None
    if isinstance(raw, str):
        return raw
    if isinstance(raw, list):
        parts = []
        for item in raw:
            if isinstance(item, dict):
                if item.get("type") == "text":
                    parts.append(item.get("text") or "")
                elif "text" in item:
                    parts.append(item.get("text") or "")
        return "".join(parts) if parts else None
    return str(raw)


def _normalize_delta(delta):
    """Normalize delta for Mistral/Azure compat before accumulate_delta.
    LiteLLM: streaming_handler.py ~L847 (role), ~L853 (type), ~L820 (arguments).
    """
    if not isinstance(delta, dict):
        return
    # LiteLLM: streaming_handler.py ~L847 "mistral's api returns role as None"
    if "role" in delta and delta["role"] is None:
        delta["role"] = "assistant"
    for tc in delta.get("tool_calls") or []:
        if not isinstance(tc, dict):
            continue
        # LiteLLM: streaming_handler.py ~L853 "mistral's api returns type: None"
        if tc.get("type") is None:
            tc["type"] = "function"
        fn = tc.get("function")
        # LiteLLM: streaming_handler.py ~L820 "## AZURE - check if arguments is not None"
        if isinstance(fn, dict) and fn.get("arguments") is None:
            fn["arguments"] = ""

# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

import copy
import logging
from typing import Any, Mapping

log = logging.getLogger(__name__)

# Echo reasoning on assistant messages for multi-turn tool loops (session only, not SQLite).
# Set PRESERVE_REASONING_IN_SESSION = False to restore legacy drop-on-round-2 behavior.
PRESERVE_REASONING_IN_SESSION = True
# Truncate string reasoning fields only; 0 = unlimited. Never truncates reasoning_details.
PRESERVE_REASONING_MAX_CHARS = 32000

# OpenAI-compat stream: thinking lives on choices[0].delta (see docs/streaming-and-threading.md).
_THINKING_STRING_FIELDS = ("reasoning_content", "reasoning", "thought", "thinking")
_THINKING_HINT_KEYS = frozenset(_THINKING_STRING_FIELDS) | {"reasoning_details"}
_REASONING_REPLAY_STRING_KEYS = ("reasoning", "reasoning_content")
# Stripped from message_snapshot during streaming so thinking is not double-collected.
THINKING_DELTA_KEYS = frozenset(_THINKING_STRING_FIELDS) | {"reasoning_details"}
_DETAIL_TEXT_FIELDS = ("text", "summary", "data")


def new_streaming_thinking_meta() -> dict[str, Any]:
    """Initial meta for ``accumulate_streaming_thinking`` / streaming replay.

    **OpenRouter (implemented):** ``reasoning_details`` replay with one merged
    ``reasoning.text`` entry plus ``reasoning.encrypted`` blobs (``data`` merged by
    index). See docs/streaming-and-threading.md §3.4 and OpenRouter reasoning-tokens docs.

    **Future provider-specific work (not implemented — extend here or in a small
    replay filter before the next request):**
    - **Gemini / provider switch:** drop or replace stale ``reasoning.encrypted`` when
      the upstream provider changes mid tool-loop (OpenRouter ai-sdk-provider#491).
    - **Anthropic via OpenRouter:** ``reasoning.text`` needs valid ``signature`` on replay
      (we keep last fragment's signature in ``meta['signature']``).
    - **DeepSeek / Kimi / Ollama:** some paths want ``reasoning_content`` or ``reasoning``
      string replay instead of ``reasoning_details`` — pick wire shape from first delta.
    - **reasoning.summary:** same index-merge as text; add to streaming acc if models emit it.
    """
    return {"source": None, "format": None, "signature": None, "index": 0, "encrypted_fragments": []}


def _truncate_reasoning_string(value: str) -> str:
    max_len = PRESERVE_REASONING_MAX_CHARS
    if max_len <= 0 or len(value) <= max_len:
        return value
    return value[:max_len]


def accumulate_streaming_thinking(text_parts: list[str], meta: dict[str, Any], delta: Mapping[str, Any]) -> None:
    """Append thinking text as each SSE delta arrives; meta records replay shape (set once)."""
    if not isinstance(delta, dict):
        return
    if meta.get("source") is None:
        if isinstance(delta.get("reasoning_details"), list) and delta["reasoning_details"]:
            meta["source"] = "reasoning_details"
        elif isinstance(delta.get("reasoning_content"), str) and delta["reasoning_content"]:
            meta["source"] = "reasoning_content"
        elif any(isinstance(delta.get(f), str) and delta[f] for f in ("reasoning", "thought", "thinking")):
            meta["source"] = "reasoning"
    chunk = _thinking_text_from_delta(delta)
    if chunk:
        text_parts.append(chunk)
    details = delta.get("reasoning_details")
    if not isinstance(details, list):
        return
    for item in details:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        # OpenRouter: opaque blobs must be echoed back inside reasoning_details (not readable text).
        if item_type == "reasoning.encrypted":
            meta["source"] = "reasoning_details"
            meta.setdefault("encrypted_fragments", []).append(copy.deepcopy(item))
            continue
        if meta.get("format") is None and item.get("format") is not None:
            meta["format"] = item.get("format")
        if item.get("signature") is not None:
            meta["signature"] = item.get("signature")
        if isinstance(item.get("index"), int):
            meta["index"] = item.get("index")


def _merge_reasoning_details(entries: list[Any]) -> list[Any]:
    """Merge streaming fragments (same type + index) for sync/non-stream replay."""
    if not entries:
        return []
    merged: dict[tuple[Any, Any], dict[str, Any]] = {}
    order: list[tuple[Any, Any]] = []
    extra: list[Any] = []
    for item in entries:
        if not isinstance(item, dict):
            continue
        idx = item.get("index")
        if not isinstance(idx, int):
            extra.append(copy.deepcopy(item))
            continue
        key = (item.get("type"), idx)
        if key not in merged:
            merged[key] = copy.deepcopy(item)
            order.append(key)
            continue
        dest = merged[key]
        for field in _DETAIL_TEXT_FIELDS:
            piece = item.get(field)
            if isinstance(piece, str) and piece:
                dest[field] = (dest.get(field) or "") + piece
        if item.get("signature") is not None:
            dest["signature"] = item.get("signature")
        for field in ("format", "id"):
            if field in item and dest.get(field) is None:
                dest[field] = item.get(field)
    return [merged[k] for k in order] + extra


def _streaming_replay(text: str, meta: Mapping[str, Any]) -> dict[str, Any]:
    text = _truncate_reasoning_string(text)
    encrypted_raw = meta.get("encrypted_fragments")
    encrypted_fragments: list[Any] = encrypted_raw if isinstance(encrypted_raw, list) else []
    merged_encrypted = _merge_reasoning_details(encrypted_fragments)
    source = meta.get("source")

    # OpenRouter structured replay: reasoning.text + reasoning.encrypted in one array, sorted by index.
    if source == "reasoning_details" or merged_encrypted:
        details: list[Any] = []
        if text:
            entry: dict[str, Any] = {"type": "reasoning.text", "text": text, "index": meta.get("index", 0)}
            if meta.get("format") is not None:
                entry["format"] = meta.get("format")
            if meta.get("signature") is not None:
                entry["signature"] = meta.get("signature")
            details.append(entry)
        details.extend(merged_encrypted)
        if not details:
            return {}
        details.sort(key=lambda d: d.get("index", 0) if isinstance(d, dict) else 0)
        return {"reasoning_details": details}

    if not text:
        return {}
    if source == "reasoning_content":
        return {"reasoning_content": text}
    return {"reasoning": text}


def extract_reasoning_replay_from_response(
    message_snapshot: Mapping[str, Any] | None = None,
    streaming_text: str | None = None,
    streaming_meta: Mapping[str, Any] | None = None,
    sync_message: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one consolidated reasoning block for the next API request. See docs/streaming-and-threading.md §3.4."""
    if not PRESERVE_REASONING_IN_SESSION:
        return {}
    if streaming_text is not None:
        return _streaming_replay(streaming_text, streaming_meta or {})
    msg = sync_message if isinstance(sync_message, dict) else message_snapshot
    if not isinstance(msg, dict):
        return {}
    details = msg.get("reasoning_details")
    if isinstance(details, list) and details:
        return {"reasoning_details": _merge_reasoning_details(details)}
    for key in _REASONING_REPLAY_STRING_KEYS:
        val = msg.get(key)
        if isinstance(val, str) and val:
            return {key: _truncate_reasoning_string(val)}
    return {}


def reasoning_replay_from_assistant_response(response: Mapping[str, Any] | None) -> dict[str, Any]:
    """Pick reasoning replay keys already merged onto an assistant API response dict."""
    if not PRESERVE_REASONING_IN_SESSION or not response:
        return {}
    return extract_reasoning_replay_from_response(sync_message=response)


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

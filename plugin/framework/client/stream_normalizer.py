# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import copy
import logging
import re
from typing import Any, Mapping, cast

from plugin.framework.deal_shim import DEAL_MAX_SHAPE_DIM, deal

log = logging.getLogger(__name__)

# Echo reasoning on assistant messages for multi-turn tool loops (session only, not SQLite).
# Set PRESERVE_REASONING_IN_SESSION = False to restore legacy drop-on-round-2 behavior.
PRESERVE_REASONING_IN_SESSION = True
# Truncate string reasoning fields only; 0 = unlimited. Never truncates reasoning_details.
PRESERVE_REASONING_MAX_CHARS = 32000

# OpenAI-compat stream: thinking lives on choices[0].delta (see docs/framework/streaming-and-threading.md).
_THINKING_STRING_FIELDS = ("reasoning_content", "reasoning", "thought", "thinking")
_THINKING_HINT_KEYS = frozenset(_THINKING_STRING_FIELDS) | {"reasoning_details"}
_REASONING_REPLAY_STRING_KEYS = ("reasoning", "reasoning_content")
# Stripped from message_snapshot during streaming so thinking is not double-collected.
THINKING_DELTA_KEYS = frozenset(_THINKING_STRING_FIELDS) | {"reasoning_details"}
_DETAIL_TEXT_FIELDS = ("text", "summary", "data")
# meta["source"] values set by accumulate_streaming_thinking / honored by ensure.
_STREAMING_SOURCE_VALUES = frozenset({"reasoning_details", "reasoning_content", "reasoning"})


def new_streaming_thinking_meta() -> dict[str, Any]:
    """Initial meta for ``accumulate_streaming_thinking`` / streaming replay.

    **OpenRouter (implemented):** ``reasoning_details`` replay with one merged
    ``reasoning.text`` entry plus ``reasoning.encrypted`` blobs (``data`` merged by
    index). See docs/framework/streaming-and-threading.md §3.4 and OpenRouter reasoning-tokens docs.

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


@deal.pre(
    # Streaming can exceed DEAL_MAX_SHAPE_DIM (grid side); chunk count is not a shape dim.
    lambda text_parts, meta, delta: isinstance(text_parts, list)
    and type(meta) is dict
    and (meta.get("source") is None or meta.get("source") in _STREAMING_SOURCE_VALUES)
)
@deal.ensure(
    lambda text_parts, meta, delta, result: meta.get("source") is None
    or meta.get("source") in _STREAMING_SOURCE_VALUES
)
def accumulate_streaming_thinking(text_parts: list[str], meta: dict[str, Any], delta: Mapping[str, Any]) -> None:
    """Append thinking text as each SSE delta arrives; meta records replay shape (set once)."""
    # delta string fields are product-sized (up to PRESERVE_REASONING_MAX_CHARS);
    # cannot shrink without a new DEAL_MAX split. Pytest still runs @deal.
    # crosshair: off
    # Plain dict only — CrossHair AttrDict is isinstance(dict) but field access can crash.
    if type(meta) is not dict:
        return
    if type(delta) is not dict:
        return
    # Shim path (deal absent): clear garbage source so ensure-equivalent invariant holds.
    src = meta.get("source")
    if src is not None and src not in _STREAMING_SOURCE_VALUES:
        meta["source"] = None
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
    if type(details) is not list:
        return
    for item in details:
        # Plain dict only — CrossHair AttrDict deepcopy can KeyError.
        if type(item) is not dict:
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


@deal.pre(lambda entries: isinstance(entries, list) and len(entries) <= DEAL_MAX_SHAPE_DIM)
@deal.post(lambda result: isinstance(result, list))
@deal.ensure(
    lambda entries, result: len(result) <= sum(1 for e in entries if type(e) is dict)
)
def _merge_reasoning_details(entries: list[Any]) -> list[Any]:
    """Merge streaming fragments (same type + index) for sync/non-stream replay."""
    # deepcopy of dict fragments with unbounded string fields.
    # crosshair: off
    if not entries:
        return []
    merged: dict[tuple[Any, Any], dict[str, Any]] = {}
    order: list[tuple[Any, Any]] = []
    extra: list[Any] = []
    for item in entries:
        # Plain dict only — CrossHair AttrDict is isinstance(dict) but deepcopy can KeyError.
        if type(item) is not dict:
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


@deal.post(
    lambda result: isinstance(result, dict)
    and set(result.keys()) <= {"reasoning", "reasoning_content", "reasoning_details"}
)
def _streaming_replay(text: str, meta: Mapping[str, Any]) -> dict[str, Any]:
    # Unbounded reasoning text plus 32k truncate; pytest covers replay shape.
    # crosshair: off
    text = _truncate_reasoning_string(text)
    encrypted_raw = meta.get("encrypted_fragments")
    encrypted_fragments: list[Any] = encrypted_raw if isinstance(encrypted_raw, list) else []
    # _merge_reasoning_details @deal.pre caps list length (CrossHair shape_dim is 4).
    if len(encrypted_fragments) > DEAL_MAX_SHAPE_DIM:
        encrypted_fragments = encrypted_fragments[:DEAL_MAX_SHAPE_DIM]
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
    """Build one consolidated reasoning block for the next API request. See docs/framework/streaming-and-threading.md §3.4."""
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


@deal.post(lambda result: isinstance(result, dict))
def _normalize_stream_delta(chunk_or_delta: object) -> dict[str, Any]:
    """Return choices[0].delta for a chat completion chunk, else the dict as-is (bare delta)."""
    # crosshair: off
    # Plain dict only — CrossHair AttrDict / Literal TypedDict heap crashes on isinstance paths.
    if type(chunk_or_delta) is not dict:
        return {}
    # type(...) is dict is untyped for ty; cast so .get / return stay dict[str, Any].
    chunk = cast("dict[str, Any]", chunk_or_delta)
    choices = chunk.get("choices")
    if type(choices) is list and choices:
        first = choices[0]
        if type(first) is dict:
            delta = first.get("delta")
            if type(delta) is dict:
                return cast("dict[str, Any]", delta)
    return chunk


@deal.pre(lambda delta: isinstance(delta, dict))
@deal.post(lambda result: isinstance(result, str))
def _thinking_text_from_delta(delta: dict[str, Any]) -> str:
    """Extract thinking from a normalized delta (no choices wrapper)."""
    # Nested reasoning_details + unbounded reasoning strings (32k product).
    # crosshair: off
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


def _extract_thinking_from_delta(chunk_or_delta):  # pyright: ignore[reportUnusedFunction]  # used by stream normalizer tests
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


def _normalize_message_content(raw):  # pyright: ignore[reportUnusedFunction]  # used by llm_client / response_normalizers
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


def _normalize_delta_tool_calls_ok(delta: dict[str, Any]) -> bool:
    """Postcondition helper: Mistral/Azure null type/arguments repaired on dict tool_calls."""
    tool_calls = delta.get("tool_calls")
    if type(tool_calls) is not list:
        return True
    for tc in tool_calls:
        if type(tc) is not dict:
            continue
        if tc.get("type") is None:
            return False
        fn = tc.get("function")
        if type(fn) is dict and fn.get("arguments") is None:
            return False
    return True


@deal.pre(
    lambda delta: type(delta) is not dict
    or (
        len(delta) <= DEAL_MAX_SHAPE_DIM
        and (
            not isinstance(delta.get("tool_calls"), list)
            or len(delta["tool_calls"]) <= DEAL_MAX_SHAPE_DIM
        )
    )
)
@deal.ensure(lambda delta, result: type(delta) is not dict or "role" not in delta or delta.get("role") is not None)
@deal.ensure(lambda delta, result: type(delta) is not dict or _normalize_delta_tool_calls_ok(delta))
def _normalize_delta(delta: object) -> None:  # pyright: ignore[reportUnusedFunction]  # used by llm_client / response_normalizers
    """Normalize delta for Mistral/Azure compat before accumulate_delta.
    LiteLLM: streaming_handler.py ~L847 (role), ~L853 (type), ~L820 (arguments).
    """
    # Plain dict only. Non-dicts and dict subclasses (OrderedDict, CrossHair AttrDict)
    # no-op: pre used to require `type is dict` and crashed MiniMax/OpenRouter eval
    # (`expected type(delta) is dict`). See docs/eval/stream-normalizer-delta-crash.md.
    if type(delta) is not dict:
        return
    delta = cast("dict[str, Any]", delta)
    # LiteLLM: streaming_handler.py ~L847 "mistral's api returns role as None"
    if "role" in delta and delta["role"] is None:
        delta["role"] = "assistant"
    # Truthy non-lists (e.g. tool_calls=2) must not be iterated — CrossHair counterexample.
    tool_calls = delta.get("tool_calls")
    if type(tool_calls) is not list:
        return
    for tc in tool_calls:
        if type(tc) is not dict:
            continue
        # LiteLLM: streaming_handler.py ~L853 "mistral's api returns type: None"
        if tc.get("type") is None:
            tc["type"] = "function"
        fn = tc.get("function")
        # LiteLLM: streaming_handler.py ~L820 "## AZURE - check if arguments is not None"
        if type(fn) is dict and fn.get("arguments") is None:
            fn["arguments"] = ""


class ThinkTagStreamSplitter:
    """Stateful stream splitter for inline <think>...</think> tags in content stream."""

    def __init__(self) -> None:
        self.in_thinking = False
        self._buf = ""

    def feed(self, chunk: str) -> list[tuple[bool, str]]:
        """Process incoming text chunk and return list of (is_thinking, text_segment)."""
        if not chunk:
            return []

        text = self._buf + chunk
        self._buf = ""
        results: list[tuple[bool, str]] = []

        while text:
            if not self.in_thinking:
                idx = text.find("<think>")
                if idx != -1:
                    if idx > 0:
                        results.append((False, text[:idx]))
                    self.in_thinking = True
                    text = text[idx + 7:]
                    continue
                # Check for possible partial prefix of '<think>' at the end of text
                partial_match = False
                for p_len in range(min(6, len(text)), 0, -1):
                    suffix = text[-p_len:]
                    if "<think>".startswith(suffix):
                        if len(text) > p_len:
                            results.append((False, text[:-p_len]))
                        self._buf = suffix
                        partial_match = True
                        break
                if partial_match:
                    break
                results.append((False, text))
                break
            else:
                idx = text.find("</think>")
                if idx != -1:
                    if idx > 0:
                        results.append((True, text[:idx]))
                    self.in_thinking = False
                    text = text[idx + 8:]
                    continue
                # Check for possible partial prefix of '</think>' at the end of text
                partial_match = False
                for p_len in range(min(7, len(text)), 0, -1):
                    suffix = text[-p_len:]
                    if "</think>".startswith(suffix):
                        if len(text) > p_len:
                            results.append((True, text[:-p_len]))
                        self._buf = suffix
                        partial_match = True
                        break
                if partial_match:
                    break
                results.append((True, text))
                break

        return [(is_t, s) for is_t, s in results if s]

    def flush(self) -> list[tuple[bool, str]]:
        """Flush any remaining buffered text."""
        if not self._buf:
            return []
        res = [(self.in_thinking, self._buf)]
        self._buf = ""
        return res


_THINK_TAG_BLOCK_RE = re.compile(r"<think>(.*?)</think>", re.DOTALL)
_THINK_UNCLOSED_RE = re.compile(r"<think>(.*)", re.DOTALL)


@deal.post(
    lambda result: isinstance(result, tuple)
    and len(result) == 2
    and isinstance(result[0], str)
    and (result[1] is None or isinstance(result[1], str))
)
@deal.ensure(lambda text, result: _THINK_TAG_BLOCK_RE.search(result[0]) is None)
def strip_think_tags(text: str | None) -> tuple[str, str | None]:
    """Strip <think>...</think> tags from text, returning (clean_content, extracted_thinking)."""
    # DOTALL greedy <think> regex hangs deep check even on short ASCII.
    # crosshair: off
    if not text:
        return "", None
    thoughts: list[str] = []

    def repl(m: re.Match[str]) -> str:
        thoughts.append(m.group(1))
        return ""

    clean = _THINK_TAG_BLOCK_RE.sub(repl, text)
    if "<think>" in clean:
        m = _THINK_UNCLOSED_RE.search(clean)
        if m:
            thoughts.append(m.group(1))
            clean = clean[: m.start()]

    clean = clean.strip()
    thinking = "\n\n".join(t.strip() for t in thoughts if t.strip()) if thoughts else None
    return clean, thinking



# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Anthropic native API provider shim."""

from __future__ import annotations

import json
import logging
from typing import Any

from plugin.framework.errors import NetworkError
from plugin.framework.i18n import _
from plugin.framework.url_utils import get_url_path_and_query
from .base_provider_shim import BaseProviderShim, coerce_raw_b64, inline_image_mime

log = logging.getLogger(__name__)

# messages[] on /v1/messages. system and developer are lifted into the top-level
# system string. Anything else (function, …) is a 400 if it is sent through.
_ANTHROPIC_MESSAGE_ROLES = frozenset({"user", "assistant"})


def _image_url_value(image_url: Any) -> str:
    """OpenAI image part: a dict ``{"url": ...}`` or, on some paths, a bare string."""
    if isinstance(image_url, dict):
        url = image_url.get("url", "")
        return url if isinstance(url, str) else ""
    if isinstance(image_url, str):
        return image_url
    return ""


def _anthropic_image_block(url_val: Any) -> dict[str, Any] | None:
    """Turn an OpenAI image part into an Anthropic image block.

    A data URL with no comma used to raise ``ValueError`` from ``split`` and
    abort the whole request. Skip that part instead.

    An http(s) URL used to be dropped with the data-URL check, so the model
    never saw the image. Messages accepts ``source.type`` ``url``
    (see docs/images/generation.md). ``file_id`` is not produced here.
    """
    if not isinstance(url_val, str) or not url_val:
        return None
    if url_val.lower().startswith(("http://", "https://")):
        return {"type": "image", "source": {"type": "url", "url": url_val}}
    if not url_val.startswith("data:"):
        return None
    if "," not in url_val:
        log.warning("Anthropic image data URL has no comma; skipping image part")
        return None
    data = coerce_raw_b64(url_val)
    if not data:
        return None
    return {"type": "image", "source": {"type": "base64", "media_type": inline_image_mime(url_val), "data": data}}


def _anthropic_tool_def(tool: Any) -> dict[str, Any] | None:
    """Accept an OpenAI tool wrapper or a flat Anthropic tool dict.

    Chat sends ``{"type":"function","function":{name,description,parameters}}``.
    Indexing ``t["name"]`` on that shape raised ``KeyError`` before the request
    was sent.
    """
    if not isinstance(tool, dict):
        return None
    fn = tool.get("function")
    src = fn if isinstance(fn, dict) else tool
    name = src.get("name")
    if not isinstance(name, str) or not name:
        return None
    schema = src.get("input_schema")
    if not isinstance(schema, dict):
        schema = src.get("parameters")
    if not isinstance(schema, dict):
        schema = {"type": "object", "properties": {}}
    description = src.get("description") or ""
    return {"name": name, "description": description, "input_schema": schema}


def _dict_parts(content: Any) -> list[dict[str, Any]]:
    """Content blocks that are dicts. Null, a string, or a non-dict part is skipped.

    Calling ``.get`` on sync ``content: null`` or a non-dict part raises
    TypeError / AttributeError. The Google image path already treats null
    content as no blocks.
    """
    if not isinstance(content, list):
        return []
    return [part for part in content if isinstance(part, dict)]


def _parse_tool_input(args: Any) -> dict[str, Any] | None:
    """Parse tool arguments into an object. Never substitute ``{}`` for bad JSON.

    An empty object would look like a successful call with no arguments.
    ``None`` means the tool_use must be omitted.
    """
    if isinstance(args, dict):
        return args
    if not isinstance(args, str):
        log.warning("Anthropic tool arguments were not JSON; omitting tool_use")
        return None
    if not args.strip():
        return {}
    try:
        parsed = json.loads(args)
    except json.JSONDecodeError:
        log.warning("Anthropic tool arguments were not valid JSON; omitting tool_use")
        return None
    if not isinstance(parsed, dict):
        log.warning("Anthropic tool arguments JSON was not an object; omitting tool_use")
        return None
    return parsed


def _convert_tool_call(tc: dict[str, Any]) -> dict[str, Any] | None:
    """One OpenAI tool call as an Anthropic ``tool_use`` block, or None to skip.

    The caller records a non-empty id when this returns None so the matching
    tool result is dropped. An empty id is not recorded: it is not a real
    ``tool_use`` id, and storing ``None`` or ``""`` used to swallow a later
    tool message that also had no id.
    """
    raw_fn = tc.get("function")
    fn = raw_fn if isinstance(raw_fn, dict) else {}
    name = fn.get("name")
    tc_id = tc.get("id")
    if not isinstance(name, str) or not name:
        return None
    if not isinstance(tc_id, str) or not tc_id:
        return None
    if "arguments" not in fn:
        return None
    args_obj = _parse_tool_input(fn.get("arguments"))
    if args_obj is None:
        return None
    return {"type": "tool_use", "id": tc_id, "name": name, "input": args_obj}


def _system_text(content: Any) -> str:
    """Text of one system or developer message. List parts keep their order."""
    if isinstance(content, list):
        return "\n\n".join(p.get("text", "") for p in _dict_parts(content) if p.get("type") == "text")
    return str(content or "")


def _convert_parts(content: Any, *, allow_images: bool) -> list[dict[str, Any]]:
    """OpenAI content to Anthropic text and image blocks. Empty text is omitted.

    A string becomes one text block. Assistant turns that already carry
    ``tool_use`` pass ``allow_images=False`` so only the text beside the call
    is kept. Plain user messages that are a bare string do not use this helper:
    Anthropic accepts that string as ``content`` directly.
    """
    blocks: list[dict[str, Any]] = []
    if isinstance(content, list):
        for part in _dict_parts(content):
            if part.get("type") == "text":
                text = part.get("text", "")
                if text:
                    blocks.append({"type": "text", "text": text})
            elif allow_images and part.get("type") == "image_url":
                image_block = _anthropic_image_block(_image_url_value(part.get("image_url")))
                if image_block is not None:
                    blocks.append(image_block)
        return blocks
    if content:
        text = content if isinstance(content, str) else str(content)
        if text:
            blocks.append({"type": "text", "text": text})
    return blocks


def _remember_dropped_id(dropped_tool_ids: set[str], tc: dict[str, Any]) -> None:
    """Record a skipped call so its tool result is not sent as an orphan.

    Only a non-empty string id can match a later ``tool_call_id``. ``None``
    and ``""`` used to sit in this set, and ``in`` then ate every tool message
    that also lacked an id.
    """
    tc_id = tc.get("id")
    if isinstance(tc_id, str) and tc_id:
        dropped_tool_ids.add(tc_id)


def _append_tool_result(converted: list[dict[str, Any]], block: dict[str, Any]) -> None:
    """Attach ``block`` to the open user tool_result message, or start one.

    The chat loop stores one message per tool call. Anthropic requires every
    tool_result for one assistant turn in a single user message; a separate
    user message per result is a 400.
    """
    prev = converted[-1] if converted else None
    prev_content = prev.get("content") if isinstance(prev, dict) else None
    if isinstance(prev, dict) and prev.get("role") == "user" and isinstance(prev_content, list) and prev_content and all(isinstance(part, dict) and part.get("type") == "tool_result" for part in prev_content):
        prev_content.append(block)
        return
    converted.append({"role": "user", "content": [block]})


def _convert_messages(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """OpenAI-shaped chat messages to ``(system text, Anthropic messages)``."""
    system_parts: list[str] = []
    converted: list[dict[str, Any]] = []
    dropped_tool_ids: set[str] = set()

    for raw in messages:
        if not isinstance(raw, dict):
            continue
        m = raw
        role = m.get("role")
        content = m.get("content")

        if role in ("system", "developer"):
            # Join every system message in order. Replacing the previous one
            # dropped date/dev/instructions earlier in the list.
            # ``developer`` is OpenAI's system stand-in. Anthropic has no such
            # role; sending it unchanged was HTTP 400.
            text = _system_text(content)
            if text:
                system_parts.append(text)
            continue

        if role == "tool":
            tool_use_id = m.get("tool_call_id")
            # A tool name is not a tool_use id. ``or m.get("name")`` sent
            # tool_use_id="search" and Anthropic returned 400. Drop the result
            # the same way an orphan id is dropped.
            if not isinstance(tool_use_id, str) or not tool_use_id:
                log.warning("Anthropic tool result has no tool_call_id; dropping it")
                continue
            if tool_use_id in dropped_tool_ids:
                continue
            result_block = {"type": "tool_result", "tool_use_id": tool_use_id, "content": _convert_parts(content, allow_images=True)}
            _append_tool_result(converted, result_block)
            continue

        tool_calls = m.get("tool_calls")
        if tool_calls:
            anth_content = _convert_parts(content, allow_images=False)
            if not isinstance(tool_calls, list):
                if anth_content:
                    converted.append({"role": "assistant", "content": anth_content})
                continue
            for tc in tool_calls:
                if not isinstance(tc, dict):
                    continue
                tool_block = _convert_tool_call(tc)
                if tool_block is None:
                    _remember_dropped_id(dropped_tool_ids, tc)
                    continue
                anth_content.append(tool_block)
            if anth_content:
                converted.append({"role": "assistant", "content": anth_content})
            continue

        if role not in _ANTHROPIC_MESSAGE_ROLES:
            # ``function`` and any other role are not valid in messages[].
            # Coercing them to user would present tool plumbing as a question.
            log.warning("Anthropic shim skipping unsupported role %r", role)
            continue

        if isinstance(content, list):
            anth_content = _convert_parts(content, allow_images=True)
            if anth_content or role != "assistant":
                converted.append({"role": role, "content": anth_content})
        else:
            text = str(content or "")
            if text or role != "assistant":
                converted.append({"role": role, "content": text})

    return "\n\n".join(system_parts), converted


def _normalize_usage(raw: Any) -> dict[str, Any]:
    """Copy Anthropic usage and add the OpenAI token names callers already read.

    Librarian and PPT host use ``prompt_tokens`` / ``completion_tokens``.
    Anthropic sends ``input_tokens`` / ``output_tokens``. Cache fields stay.
    """
    if not isinstance(raw, dict) or not raw:
        return {}
    usage = dict(raw)
    if "prompt_tokens" not in usage and "input_tokens" in usage:
        usage["prompt_tokens"] = usage["input_tokens"]
    if "completion_tokens" not in usage and "output_tokens" in usage:
        usage["completion_tokens"] = usage["output_tokens"]
    return usage


def _stash_usage(stream_state: dict[str, Any] | None, usage: dict[str, Any]) -> None:
    """Merge one usage snapshot onto the stream. Later keys replace earlier ones.

    ``message_delta.usage`` is a cumulative total, not a delta to add.
    ``message_start`` reports ``output_tokens: 1`` and the final event reports
    ``15``; adding those yields 16.
    """
    if stream_state is None or not usage:
        return
    prev = stream_state.get("_anthropic_usage")
    merged = dict(prev) if isinstance(prev, dict) else {}
    merged.update(usage)
    stream_state["_anthropic_usage"] = merged


# OpenAI-style finish_reason values the tool loop already understands.
# Unknown Anthropic reasons pass through so a new API value is not hidden.
_ANTHROPIC_FINISH_REASONS = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "tool_use": "tool_calls",
    "max_tokens": "length",
    "refusal": "content_filter",
}


def _map_anthropic_finish_reason(reason: Any) -> str | None:
    """Map an Anthropic ``stop_reason`` onto the finish_reason the tool loop checks."""
    if not isinstance(reason, str) or not reason:
        return None
    return _ANTHROPIC_FINISH_REASONS.get(reason, reason)


def _tool_calls_from_content_blocks(content_parts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sync ``tool_use`` blocks as OpenAI tool calls. Unpairable blocks are skipped.

    An empty id used to become ``id: ""``. The tool loop ran the call, then the
    next request dropped both the call and its result because ``tool_use_id``
    must be a non-empty string. A list ``input`` is not an object; replaying it
    failed the same way after the tool had run on ``{}``. Missing ``input`` is
    a no-argument call.
    """
    tool_calls: list[dict[str, Any]] = []
    for part in content_parts:
        if part.get("type") != "tool_use":
            continue
        name = part.get("name")
        if not isinstance(name, str) or not name:
            continue
        tc_id = part.get("id")
        if not isinstance(tc_id, str) or not tc_id:
            log.warning("Anthropic tool_use has no id; omitting it")
            continue
        raw_input = part.get("input", None)
        if raw_input is None:
            raw_input = {}
        elif not isinstance(raw_input, dict):
            log.warning("Anthropic tool_use input was not an object; omitting it")
            continue
        tool_calls.append({"id": tc_id, "type": "function", "function": {"name": name, "arguments": json.dumps(raw_input)}})
    return tool_calls


def _thinking_text(content_parts: list[dict[str, Any]]) -> str | None:
    """Join sync ``thinking`` blocks. The stream path already returns thinking_delta."""
    parts: list[str] = []
    for part in content_parts:
        if part.get("type") != "thinking":
            continue
        text = part.get("thinking")
        if isinstance(text, str) and text:
            parts.append(text)
    if not parts:
        return None
    return "\n\n".join(parts)


class AnthropicShim(BaseProviderShim):
    """Shim for Anthropic native Messages API."""

    def _tool_delta(self, block_index: Any, *, arguments: str, indexes: dict[int, int], tool_id: Any = None, name: str | None = None) -> dict[str, Any] | None:
        """One streamed tool-call fragment. ``arguments`` is a piece, not the final object.

        ``content_block_start`` passes ``arguments=""``. A no-argument tool may
        send no ``input_json_delta``. The tool loop treats that empty string as
        ``{}``. Do not seed ``"{}"``: the next fragment would concatenate to
        ``'{}{"x":1}'``.
        """
        if not isinstance(block_index, int):
            return None
        tool_index = indexes.get(block_index)
        if tool_index is None:
            tool_index = len(indexes)
            indexes[block_index] = tool_index
        function: dict[str, Any] = {"arguments": arguments}
        call: dict[str, Any] = {"index": tool_index, "function": function}
        if tool_id is not None:
            call["id"] = tool_id
        if name is not None:
            call["type"] = "function"
            function["name"] = name
        return {"tool_calls": [call]}

    def build_chat_request(
        self, messages: list[dict[str, Any]], max_tokens: int, temperature: float | None, tools: list[dict[str, Any]] | None, stream: bool, model_name: str | None, response_format: dict[str, Any] | None, chat_extra: dict[str, Any] | None = None
    ) -> tuple[str, str, bytes, dict[str, str]]:
        endpoint = self.client._endpoint()
        url = f"{endpoint}/v1/messages"
        model = model_name.strip() if isinstance(model_name, str) else ""
        # A retired hardcoded id (claude-3-5-sonnet-20241022) used to fill a
        # blank Settings model and fail at Anthropic with a confusing 404.
        # The caller already passed config["model"]; an empty value is a
        # missing setting, not a license to invent one.
        if not model:
            raise NetworkError(_("Anthropic model is not set. Choose a model in Settings."), code="BAD_REQUEST")
        system_msg, converted = _convert_messages(messages)
        data: dict[str, Any] = {"model": model, "messages": converted, "max_tokens": max_tokens, "stream": stream}
        if temperature is not None:
            data["temperature"] = max(0.0, min(1.0, temperature))
        # Anthropic has no OpenAI response_format field. A json_object grammar
        # call would go out as free text, so one system line is the hint.
        if isinstance(response_format, dict) and response_format.get("type") == "json_object":
            hint = "Respond with a single JSON object and no other text."
            system_msg = f"{system_msg}\n\n{hint}" if system_msg else hint
        if system_msg:
            data["system"] = system_msg
        if tools:
            converted_tools = [tool_def for tool_def in (_anthropic_tool_def(t) for t in tools) if tool_def]
            if converted_tools:
                data["tools"] = converted_tools
        # Grammar sends OpenRouter-shaped chat_extra={"reasoning":{"effort":"minimal"}}.
        # Pasting that object onto /v1/messages would 400; map known effort values
        # onto Anthropic output_config and ignore other extra keys.
        if isinstance(chat_extra, dict):
            reasoning = chat_extra.get("reasoning")
            effort = reasoning.get("effort") if isinstance(reasoning, dict) else None
            if effort == "minimal":
                effort = "low"
            if effort in ("low", "medium", "high", "max"):
                data["output_config"] = {"effort": effort}

        path = get_url_path_and_query(url)
        return "POST", path, json.dumps(data).encode("utf-8"), self.client._headers()

    def parse_response_chunk(self, chunk: dict[str, Any], stream_state: dict[str, Any] | None = None) -> tuple[str, str | None, str | None, dict[str, Any]]:
        msg_type = chunk.get("type", "")
        content = ""
        finish_reason = None
        thinking = None
        delta: dict[str, Any] = {}
        indexes = stream_state.setdefault("_stream_tool_indexes", {}) if stream_state is not None else {}

        if msg_type == "message_start":
            indexes.clear()
            # input_tokens live under message.usage, not on the event. The
            # stream loop never saw them, so prompt_tokens stayed missing.
            message = chunk.get("message")
            usage = _normalize_usage(message.get("usage") if isinstance(message, dict) else None)
            _stash_usage(stream_state, usage)
            if usage:
                delta = {"usage": usage}
        elif msg_type == "content_block_start":
            block = chunk.get("content_block")
            if isinstance(block, dict) and block.get("type") == "tool_use":
                tool_delta = self._tool_delta(chunk.get("index"), arguments="", indexes=indexes, tool_id=block.get("id"), name=block.get("name") or "")
                if tool_delta:
                    delta = tool_delta
        elif msg_type == "content_block_delta":
            raw_delta = chunk.get("delta")
            d: dict[str, Any] = raw_delta if isinstance(raw_delta, dict) else {}
            if d.get("type") == "text_delta":
                content = d.get("text") or ""
                if content:
                    delta = {"content": content}
            elif d.get("type") == "thinking_delta":
                # text_delta maps onto content. Extended-thinking chunks use the
                # same return slot the OpenAI shim already calls thinking.
                # signature_delta stays ignored.
                thinking = d.get("thinking") or ""
                if thinking:
                    delta = {"thinking": thinking}
            elif d.get("type") == "input_json_delta":
                # partial_json is a fragment. accumulate_delta concatenates
                # function.arguments across chunks that share an index.
                partial = d.get("partial_json") or ""
                tool_delta = self._tool_delta(chunk.get("index"), arguments=partial if isinstance(partial, str) else str(partial), indexes=indexes)
                if tool_delta:
                    delta = tool_delta
        elif msg_type == "message":
            # SYNC response. content: null and a non-dict part used to raise.
            content_parts = _dict_parts(chunk.get("content", []))
            content = "".join([p.get("text", "") for p in content_parts if p.get("type") == "text"])
            thinking = _thinking_text(content_parts)
            finish_reason = _map_anthropic_finish_reason(chunk.get("stop_reason"))
            tool_calls = _tool_calls_from_content_blocks(content_parts)
            delta = {"role": "assistant", "content": content}
            # Sync thinking used to be dropped. The stream path already surfaces
            # thinking_delta. ``reasoning`` is the key replay already reads.
            # Signature bytes are still not echoed; that is a separate feature.
            if thinking:
                delta["reasoning"] = thinking
            if tool_calls:
                delta["tool_calls"] = tool_calls
        elif msg_type == "message_delta":
            # Other Anthropic branches already ignore a non-dict delta. A
            # missing or string delta used to raise AttributeError here.
            raw_delta = chunk.get("delta")
            if isinstance(raw_delta, dict):
                finish_reason = _map_anthropic_finish_reason(raw_delta.get("stop_reason"))
            # Top-level usage is the cumulative output count. Normalize it here
            # so the stream loop does not forward raw output_tokens only.
            usage = _normalize_usage(chunk.get("usage"))
            _stash_usage(stream_state, usage)
            if usage:
                delta = {"usage": usage}
        elif msg_type == "message_stop":
            # message_stop carries no stop_reason. The stream loop keeps the
            # last non-empty reason, and Anthropic sends stop_reason on
            # message_delta then this bare event. Returning "stop" here would
            # overwrite max_tokens / tool_use, so a truncated reply looks
            # finished. Leave the reason unset.
            finish_reason = None
        return content, finish_reason, thinking, delta

    def parse_sync_response(self, response_data: dict[str, Any]) -> tuple[str, str | None, list[dict[str, Any]] | None, dict[str, Any], list[str], dict[str, Any]]:
        content, finish_reason, _thinking, delta = self.parse_response_chunk(response_data, {})
        tool_calls = delta.get("tool_calls")
        usage = _normalize_usage(response_data.get("usage") or {})
        images = delta.get("images") or []
        return content, finish_reason, tool_calls, usage, images, delta

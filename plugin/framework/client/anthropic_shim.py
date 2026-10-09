# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Anthropic native API provider shim."""

from __future__ import annotations

import json
import logging
from typing import Any

from plugin.framework.url_utils import get_url_path_and_query
from .base_provider_shim import BaseProviderShim, coerce_raw_b64, inline_image_mime

log = logging.getLogger(__name__)

_BAD_TOOL_INPUT = object()


def _anthropic_image_block(url_val: Any) -> dict[str, Any] | None:
    """Turn an OpenAI data-URL image part into an Anthropic base64 image block.

    A data URL with no comma used to raise ``ValueError`` from ``split`` and
    abort the whole request. Skip that part instead.
    """
    if not isinstance(url_val, str) or not url_val.startswith("data:"):
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


def _parse_tool_input(args: Any) -> dict[str, Any] | object:
    """Parse tool arguments into an object. Never substitute ``{}`` for bad JSON.

    An empty object would look like a successful call with no arguments.
    """
    if isinstance(args, dict):
        return args
    if not isinstance(args, str):
        log.warning("Anthropic tool arguments were not JSON; omitting tool_use")
        return _BAD_TOOL_INPUT
    if not args.strip():
        return {}
    try:
        parsed = json.loads(args)
    except json.JSONDecodeError:
        log.warning("Anthropic tool arguments were not valid JSON; omitting tool_use")
        return _BAD_TOOL_INPUT
    if not isinstance(parsed, dict):
        log.warning("Anthropic tool arguments JSON was not an object; omitting tool_use")
        return _BAD_TOOL_INPUT
    return parsed


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


class AnthropicShim(BaseProviderShim):
    """Shim for Anthropic native Messages API."""

    def _tool_delta(self, block_index: Any, function: dict[str, Any], tool_id: str | None = None, name: str | None = None, indexes: dict[int, int] | None = None) -> dict[str, Any] | None:
        if not isinstance(block_index, int):
            return None
        if indexes is None:
            indexes = {}
        tool_index = indexes.get(block_index)
        if tool_index is None:
            tool_index = len(indexes)
            indexes[block_index] = tool_index
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
        system_parts: list[str] = []
        converted: list[dict[str, Any]] = []
        dropped_tool_ids: set[str | None] = set()

        for m in messages:
            role = m.get("role")
            content = m.get("content")

            if role == "system":
                # Join every system message in order. Replacing the previous one
                # dropped date/dev/instructions earlier in the list.
                # OpenAI-compat keeps every system block.
                if isinstance(content, list):
                    text = "\n\n".join([p.get("text", "") for p in _dict_parts(content) if p.get("type") == "text"])
                else:
                    text = str(content or "")
                if text:
                    system_parts.append(text)
                continue

            anth_content: list[dict[str, Any]] = []

            # 1. Handle tool response messages (role == "tool")
            if role == "tool":
                tool_use_id = m.get("tool_call_id") or m.get("name")
                if tool_use_id in dropped_tool_ids:
                    continue
                result_blocks: list[dict[str, Any]] = []
                if isinstance(content, list):
                    for part in _dict_parts(content):
                        if part.get("type") == "text":
                            text = part.get("text", "")
                            if text:
                                result_blocks.append({"type": "text", "text": text})
                        elif part.get("type") == "image_url":
                            image_url = part.get("image_url")
                            url_val = image_url.get("url", "") if isinstance(image_url, dict) else ""
                            image_block = _anthropic_image_block(url_val)
                            if image_block is not None:
                                result_blocks.append(image_block)
                else:
                    text = str(content or "")
                    if text:
                        result_blocks.append({"type": "text", "text": text})

                block = {"type": "tool_result", "tool_use_id": tool_use_id, "content": result_blocks}
                # The chat loop stores one message per tool call. Anthropic
                # requires every tool_result for one assistant turn in a single
                # user message; a separate user message per result is a 400.
                prev = converted[-1] if converted else None
                prev_content = prev.get("content") if isinstance(prev, dict) else None
                if isinstance(prev, dict) and prev.get("role") == "user" and isinstance(prev_content, list) and prev_content and all(isinstance(part, dict) and part.get("type") == "tool_result" for part in prev_content):
                    prev_content.append(block)
                else:
                    converted.append({"role": "user", "content": [block]})
                continue

            # 2. Handle assistant messages with tool calls
            tool_calls = m.get("tool_calls")
            if tool_calls:
                if isinstance(content, list):
                    for part in _dict_parts(content):
                        if part.get("type") == "text":
                            text = part.get("text", "")
                            if text:
                                anth_content.append({"type": "text", "text": text})
                elif content:
                    text = str(content)
                    if text:
                        anth_content.append({"type": "text", "text": text})

                if not isinstance(tool_calls, list):
                    if anth_content:
                        converted.append({"role": "assistant", "content": anth_content})
                    continue
                for tc in tool_calls:
                    if not isinstance(tc, dict):
                        continue
                    tc_id = tc.get("id")
                    raw_fn = tc.get("function")
                    fn = raw_fn if isinstance(raw_fn, dict) else {}
                    name = fn.get("name")
                    # A skipped tool call (non-dict, empty name or id, missing
                    # arguments, bad JSON) must record its id in dropped_tool_ids.
                    # Otherwise the matching role=="tool" result is an orphan and
                    # Anthropic returns HTTP 400.
                    if not isinstance(name, str) or not name:
                        dropped_tool_ids.add(tc_id)
                        continue
                    if not isinstance(tc_id, str) or not tc_id:
                        dropped_tool_ids.add(tc_id)
                        continue
                    if "arguments" not in fn:
                        dropped_tool_ids.add(tc_id)
                        continue
                    args_obj = _parse_tool_input(fn.get("arguments"))
                    if args_obj is _BAD_TOOL_INPUT:
                        dropped_tool_ids.add(tc_id)
                        continue
                    anth_content.append({"type": "tool_use", "id": tc_id, "name": name, "input": args_obj})
                if anth_content:
                    converted.append({"role": "assistant", "content": anth_content})
                continue

            # 3. Handle standard user/assistant messages with potential images
            if isinstance(content, list):
                for part in _dict_parts(content):
                    if part.get("type") == "text":
                        text = part.get("text", "")
                        if text:
                            anth_content.append({"type": "text", "text": text})
                    elif part.get("type") == "image_url":
                        image_url = part.get("image_url")
                        url_val = image_url.get("url", "") if isinstance(image_url, dict) else ""
                        image_block = _anthropic_image_block(url_val)
                        if image_block is not None:
                            anth_content.append(image_block)
                if anth_content or role != "assistant":
                    converted.append({"role": role or "user", "content": anth_content})
            else:
                text = str(content or "")
                if text or role != "assistant":
                    converted.append({"role": role or "user", "content": text})

        data: dict[str, Any] = {"model": model_name or "claude-3-5-sonnet-20241022", "messages": converted, "max_tokens": max_tokens, "stream": stream}
        if temperature is not None:
            data["temperature"] = max(0.0, min(1.0, temperature))
        system_msg = "\n\n".join(system_parts)
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
        elif msg_type == "content_block_start":
            block = chunk.get("content_block")
            if isinstance(block, dict) and block.get("type") == "tool_use":
                tool_delta = self._tool_delta(chunk.get("index"), {"name": block.get("name") or "", "arguments": ""}, tool_id=block.get("id"), name=block.get("name") or "", indexes=indexes)
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
                tool_delta = self._tool_delta(chunk.get("index"), {"arguments": d.get("partial_json") or ""}, indexes=indexes)
                if tool_delta:
                    delta = tool_delta
        elif msg_type == "message":
            # SYNC response. content: null and a non-dict part used to raise.
            content_parts = _dict_parts(chunk.get("content", []))
            content = "".join([p.get("text", "") for p in content_parts if p.get("type") == "text"])
            finish_reason = _map_anthropic_finish_reason(chunk.get("stop_reason"))
            # Handle tools
            tool_calls = []
            for p in content_parts:
                if p.get("type") == "tool_use":
                    # A partial tool block has no name. p["name"] used to KeyError
                    # and abort the whole response.
                    name = p.get("name")
                    if not isinstance(name, str) or not name:
                        continue
                    raw_input = p.get("input")
                    if not isinstance(raw_input, (dict, list)):
                        raw_input = {}
                    tool_calls.append({"id": p.get("id") or "", "type": "function", "function": {"name": name, "arguments": json.dumps(raw_input)}})
            delta = {"role": "assistant", "content": content}
            if tool_calls:
                delta["tool_calls"] = tool_calls
        elif msg_type == "message_delta":
            # Other Anthropic branches already ignore a non-dict delta. A
            # missing or string delta used to raise AttributeError here.
            raw_delta = chunk.get("delta")
            if isinstance(raw_delta, dict):
                finish_reason = _map_anthropic_finish_reason(raw_delta.get("stop_reason"))
        elif msg_type == "message_stop":
            # message_stop carries no stop_reason. The stream loop keeps the
            # last non-empty reason, and Anthropic sends stop_reason on
            # message_delta then this bare event. Returning "stop" here would
            # overwrite max_tokens / tool_use, so a truncated reply looks
            # finished. Leave the reason unset.
            finish_reason = None
        return content, finish_reason, thinking, delta

    def parse_sync_response(self, response_data: dict[str, Any]) -> tuple[str, str | None, list[dict[str, Any]] | None, dict[str, Any], list[str], dict[str, Any]]:
        content, finish_reason, _unused, delta = self.parse_response_chunk(response_data, {})
        tool_calls = delta.get("tool_calls")
        usage = response_data.get("usage") or {}
        if isinstance(usage, dict):
            # The librarian reads OpenAI names. Anthropic sends input_tokens.
            usage = dict(usage)
            if "prompt_tokens" not in usage and "input_tokens" in usage:
                usage["prompt_tokens"] = usage["input_tokens"]
            if "completion_tokens" not in usage and "output_tokens" in usage:
                usage["completion_tokens"] = usage["output_tokens"]
        images = delta.get("images") or []
        return content, finish_reason, tool_calls, usage, images, delta

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


def _parse_tool_input(args: Any) -> dict[str, Any] | None:
    """Parse tool arguments into an object. Never substitute ``{}`` for bad JSON.

    An empty object would look like a successful call with no arguments.
    """
    if isinstance(args, dict):
        return args
    if not isinstance(args, str):
        log.warning("Anthropic tool arguments were not JSON; omitting tool_use")
        return None
    try:
        parsed = json.loads(args)
    except json.JSONDecodeError:
        log.warning("Anthropic tool arguments were not valid JSON; omitting tool_use")
        return None
    if not isinstance(parsed, dict):
        log.warning("Anthropic tool arguments JSON was not an object; omitting tool_use")
        return None
    return parsed


class AnthropicShim(BaseProviderShim):
    """Shim for Anthropic native Messages API."""

    def __init__(self, client: Any) -> None:
        super().__init__(client)
        # Content-block index → OpenAI tool_calls index for the current stream.
        # Cleared on each build_chat_request so a reused shim cannot leak indexes.
        self._stream_tool_indexes: dict[int, int] = {}

    def _reset_stream_tools(self) -> None:
        self._stream_tool_indexes = {}

    def _tool_delta(self, block_index: Any, function: dict[str, Any], tool_id: str | None = None, name: str | None = None) -> dict[str, Any] | None:
        if not isinstance(block_index, int):
            return None
        tool_index = self._stream_tool_indexes.get(block_index)
        if tool_index is None:
            tool_index = len(self._stream_tool_indexes)
            self._stream_tool_indexes[block_index] = tool_index
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
        self._reset_stream_tools()
        endpoint = self.client._endpoint()
        url = f"{endpoint}/v1/messages"
        system_msg = ""
        converted: list[dict[str, Any]] = []

        for m in messages:
            role = m.get("role")
            content = m.get("content")

            if role == "system":
                if isinstance(content, list):
                    system_msg = "\n\n".join([p.get("text", "") for p in content if p.get("type") == "text"])
                else:
                    system_msg = str(content or "")
                continue

            anth_content: list[dict[str, Any]] = []

            # 1. Handle tool response messages (role == "tool")
            if role == "tool":
                tool_use_id = m.get("tool_call_id") or m.get("name")
                result_blocks: list[dict[str, Any]] = []
                if isinstance(content, list):
                    for part in content:
                        if part.get("type") == "text":
                            result_blocks.append({"type": "text", "text": part.get("text", "")})
                        elif part.get("type") == "image_url":
                            image_url = part.get("image_url")
                            url_val = image_url.get("url", "") if isinstance(image_url, dict) else ""
                            image_block = _anthropic_image_block(url_val)
                            if image_block is not None:
                                result_blocks.append(image_block)
                else:
                    result_blocks.append({"type": "text", "text": str(content or "")})

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
                    for part in content:
                        if part.get("type") == "text":
                            anth_content.append({"type": "text", "text": part.get("text", "")})
                elif content:
                    anth_content.append({"type": "text", "text": str(content)})

                for tc in tool_calls:
                    fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
                    args_obj = _parse_tool_input(fn.get("arguments", "{}"))
                    if args_obj is None:
                        continue
                    anth_content.append({"type": "tool_use", "id": tc.get("id"), "name": fn.get("name"), "input": args_obj})
                converted.append({"role": "assistant", "content": anth_content})
                continue

            # 3. Handle standard user/assistant messages with potential images
            if isinstance(content, list):
                for part in content:
                    if part.get("type") == "text":
                        anth_content.append({"type": "text", "text": part.get("text", "")})
                    elif part.get("type") == "image_url":
                        image_url = part.get("image_url")
                        url_val = image_url.get("url", "") if isinstance(image_url, dict) else ""
                        image_block = _anthropic_image_block(url_val)
                        if image_block is not None:
                            anth_content.append(image_block)
                converted.append({"role": role or "user", "content": anth_content})
            else:
                converted.append({"role": role or "user", "content": str(content or "")})

        data: dict[str, Any] = {"model": model_name or "claude-3-5-sonnet-20241022", "messages": converted, "max_tokens": max_tokens, "stream": stream}
        if temperature is not None:
            data["temperature"] = temperature
        if system_msg:
            data["system"] = system_msg
        if tools:
            converted_tools = [tool_def for tool_def in (_anthropic_tool_def(t) for t in tools) if tool_def]
            if converted_tools:
                data["tools"] = converted_tools

        path = get_url_path_and_query(url)
        return "POST", path, json.dumps(data).encode("utf-8"), self.client._headers()

    def parse_response_chunk(self, chunk: dict[str, Any]) -> tuple[str, str | None, str | None, dict[str, Any]]:
        msg_type = chunk.get("type", "")
        content = ""
        finish_reason = None
        thinking = None
        delta: dict[str, Any] = {}

        if msg_type == "message_start":
            self._reset_stream_tools()
        elif msg_type == "content_block_start":
            block = chunk.get("content_block")
            if isinstance(block, dict) and block.get("type") == "tool_use":
                tool_delta = self._tool_delta(chunk.get("index"), {"name": block.get("name") or "", "arguments": ""}, tool_id=block.get("id"), name=block.get("name") or "")
                if tool_delta:
                    delta = tool_delta
        elif msg_type == "content_block_delta":
            raw_delta = chunk.get("delta")
            d: dict[str, Any] = raw_delta if isinstance(raw_delta, dict) else {}
            if d.get("type") == "text_delta":
                content = d.get("text") or ""
                if content:
                    delta = {"content": content}
            elif d.get("type") == "input_json_delta":
                # partial_json is a fragment. accumulate_delta concatenates
                # function.arguments across chunks that share an index.
                tool_delta = self._tool_delta(chunk.get("index"), {"arguments": d.get("partial_json") or ""})
                if tool_delta:
                    delta = tool_delta
        elif msg_type == "message":
            # SYNC response
            content_parts = chunk.get("content", [])
            content = "".join([p.get("text", "") for p in content_parts if p.get("type") == "text"])
            finish_reason = chunk.get("stop_reason")
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
                finish_reason = raw_delta.get("stop_reason")
        elif msg_type == "message_stop":
            finish_reason = "stop"
        return content, finish_reason, thinking, delta

    def parse_sync_response(self, response_data: dict[str, Any]) -> tuple[str, str | None, list[dict[str, Any]] | None, dict[str, Any], list[str], dict[str, Any]]:
        content, finish_reason, _unused, delta = self.parse_response_chunk(response_data)
        tool_calls = delta.get("tool_calls")
        usage = response_data.get("usage") or {}
        images = delta.get("images") or []
        return content, finish_reason, tool_calls, usage, images, delta

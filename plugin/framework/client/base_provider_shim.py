# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Base provider shim interface with default OpenAI-compatible API implementation."""

from __future__ import annotations

import json
from typing import Any

from plugin.framework.url_utils import get_url_path_and_query


class BaseProviderShim:
    """Base provider shim implementing standard OpenAI-compatible API format by default."""

    def __init__(self, client: Any) -> None:
        self.client = client

    def build_chat_request(
        self,
        messages: list[dict[str, Any]],
        max_tokens: int,
        temperature: float,
        tools: list[dict[str, Any]] | None,
        stream: bool,
        model_name: str | None,
        response_format: dict[str, Any] | None,
        chat_extra: dict[str, Any] | None = None,
    ) -> tuple[str, str, bytes, dict[str, str]]:
        from .llm_client import merge_openrouter_chat_extra

        endpoint = self.client._endpoint()
        api_path = self.client._api_path()
        url = endpoint + api_path + "/chat/completions"

        data: dict[str, Any] = {
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "top_p": 0.9,
            "stream": stream,
        }
        if model_name:
            data["model"] = model_name
        if tools:
            data["tools"] = tools
            data["tool_choice"] = "auto"
            data["parallel_tool_calls"] = False
        if response_format:
            data["response_format"] = response_format

        if self.client.config.get("is_openrouter"):
            extra = self.client.config.get("openrouter_chat_extra")
            if isinstance(extra, dict) and extra:
                merge_openrouter_chat_extra(data, extra)
        if isinstance(chat_extra, dict) and chat_extra:
            merge_openrouter_chat_extra(data, chat_extra)

        json_data = json.dumps(data).encode("utf-8")
        path = get_url_path_and_query(url)
        return "POST", path, json_data, self.client._headers()

    def parse_response_chunk(self, chunk: dict[str, Any]) -> tuple[str, str | None, str | None, dict[str, Any]]:
        from .stream_normalizer import _extract_thinking_from_delta

        choices = chunk.get("choices", [])
        # Unexpected schema (string "choices", non-dict first element) used to
        # AttributeError on .get and abort the whole stream. Treat as no choice
        # so later valid chunks can still apply.
        choice = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
        delta_raw = choice.get("delta", {})
        delta = delta_raw if isinstance(delta_raw, dict) else {}

        finish_reason = choice.get("finish_reason") if choice else None
        if not finish_reason:
            finish_reason = chunk.get("finish_reason") or chunk.get("done_reason")
        if not finish_reason and choices:
            for c in choices:
                if isinstance(c, dict) and c.get("finish_reason"):
                    finish_reason = c.get("finish_reason")
                    break

        content = (delta.get("content") or "") if delta else ""
        thinking = _extract_thinking_from_delta(chunk)
        return content, finish_reason, thinking, delta

    def parse_sync_response(
        self, response_data: dict[str, Any]
    ) -> tuple[str, str | None, list[dict[str, Any]] | None, dict[str, Any], list[str], dict[str, Any]]:
        from .stream_normalizer import _normalize_delta, _normalize_message_content

        # OpenAI-compatible / local models response parsing
        # What was wrong: Local models (e.g. Ollama) returning {"done_reason": "stop", "message": ...}
        # without a top-level "choices" list fell through to chunk parsing and lost done_reason and content.
        # This change handles choices[0] if present while falling back to top-level message/done_reason.
        choices = response_data.get("choices")
        choice = choices[0] if (isinstance(choices, list) and choices and isinstance(choices[0], dict)) else {}
        message = choice.get("message") or response_data.get("message") or {}
        if not isinstance(message, dict):
            message = {}
        _normalize_delta(message)
        finish_reason = choice.get("finish_reason") or response_data.get("finish_reason") or response_data.get("done_reason")

        raw_content = message.get("content")
        content = _normalize_message_content(raw_content) or ""
        images = message.get("images") or []
        tool_calls = message.get("tool_calls")
        usage = response_data.get("usage", {})

        return content, finish_reason, tool_calls, usage, images, message

    def build_image_request(
        self,
        prompt: str,
        model: str | None,
        width: int,
        height: int,
        steps: int | None = None,
        source_image: str | None = None,
        image_url: str | None = None,
    ) -> tuple[str, str, bytes, dict[str, str]]:
        """Build an image generation request (standard OpenAI format)."""
        endpoint = self.client._endpoint()
        api_path = self.client._api_path()
        url = endpoint + api_path + "/images/generations"
        data: dict[str, Any] = {"prompt": prompt, "n": 1, "size": f"{width}x{height}", "response_format": "b64_json"}
        if model:
            data["model"] = model
        if steps:
            data["steps"] = steps

        if image_url:
            data["image_url"] = image_url
        elif source_image:
            if source_image.startswith("data:image"):
                data["image_url"] = source_image
            else:
                data["image_url"] = "data:image/png;base64," + source_image

        path = get_url_path_and_query(url)
        return "POST", path, json.dumps(data).encode("utf-8"), self.client._headers()

    def parse_image_responses(self, response_data: dict[str, Any]) -> list[str]:
        """Extract list of base64 image data from response (standard OpenAI format)."""
        items = response_data.get("data", [])
        out = []
        for it in items:
            if b64 := it.get("b64_json"):
                out.append(b64)
        return out

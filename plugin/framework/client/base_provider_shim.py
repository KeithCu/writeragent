# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Base provider shim interface with default OpenAI-compatible API implementation."""

from __future__ import annotations

import json
import re
from typing import Any

from plugin.framework.url_utils import get_url_path_and_query

# Named UI / tool values → OpenRouter / Gemini aspect_ratio strings.
_NAMED_ASPECT_RATIOS: dict[str, str] = {
    "square": "1:1",
    "1:1": "1:1",
    "landscape_16_9": "16:9",
    "landscape_16:9": "16:9",
    "16:9": "16:9",
    "portrait_9_16": "9:16",
    "portrait_9:16": "9:16",
    "9:16": "9:16",
    "landscape_3_2": "3:2",
    "landscape_3:2": "3:2",
    "3:2": "3:2",
    "portrait_2_3": "2:3",
    "portrait_2:3": "2:3",
    "2:3": "2:3",
    "4:3": "4:3",
    "3:4": "3:4",
    "21:9": "21:9",
    "4:5": "4:5",
    "5:4": "5:4",
}

# Closest-match table for WxH. Keep 16:9 before 3:2 so 1792x1024 stays 16:9.
_PIXEL_ASPECT_RATIOS: tuple[tuple[str, float], ...] = (("1:1", 1.0), ("16:9", 16 / 9), ("9:16", 9 / 16), ("4:3", 4 / 3), ("3:4", 3 / 4), ("3:2", 1.5), ("2:3", 2 / 3), ("21:9", 21 / 9), ("4:5", 4 / 5), ("5:4", 5 / 4))


def canonical_aspect_ratio(width: int | None = None, height: int | None = None, named: str | None = None) -> str | None:
    """Return a provider aspect-ratio hint (``1:1``, ``16:9``, …) or None.

    Named UI/tool values win. Pixel WxH maps to the closest standard ratio
    within a small tolerance so 64-pixel rounding (``896x512``) still yields
    ``16:9``.
    """
    if named:
        key = str(named).strip().lower().replace(" ", "_").replace("(", "").replace(")", "")
        mapped = _NAMED_ASPECT_RATIOS.get(key)
        if mapped:
            return mapped
    if not width or not height or width < 1 or height < 1:
        return None
    ratio = width / height
    label, expected = min(_PIXEL_ASPECT_RATIOS, key=lambda item: abs(ratio - item[1]))
    if abs(ratio - expected) <= 0.12:
        return label
    return None


def canonical_resolution(width: int | None = None, height: int | None = None, *, family: str = "standard") -> str | None:
    """Map max(width, height) to a vendor resolution tier.

    Standard (OpenRouter ``/images`` resolution, Google native): ``512``, ``1K``,
    ``2K``, ``4K``. OpenRouter chat ``image_config.image_size`` uses ``0.5K`` for
    the low tier (``family="openrouter_chat"``). Grok only documents ``1k`` /
    ``2k``. Imagen only documents ``1K`` / ``2K``.
    """
    if not width or not height or width < 1 or height < 1:
        return None
    edge = max(width, height)
    if edge <= 768:
        tier = "512"
    elif edge <= 1536:
        tier = "1K"
    elif edge <= 3072:
        tier = "2K"
    else:
        tier = "4K"
    if family == "grok":
        return "2k" if tier in ("2K", "4K") else "1k"
    if family == "imagen":
        if tier == "512":
            return "1K"
        if tier == "4K":
            return "2K"
        return tier
    # OpenRouter chat modalities path rejects "512"; enum is 0.5K|1K|2K|4K.
    if family == "openrouter_chat":
        return "0.5K" if tier == "512" else tier
    return tier


def adjust_image_body_for_rejection(body: bytes, error_text: str) -> bytes | None:
    """
    Parse provider errors (like OpenRouter 'not supported. Accepted: <v1>') and adjust the requested params.
    Returns the new body bytes, or None if no changes were made.
    """
    try:
        data = json.loads(body.decode("utf-8"))
    except Exception:
        return None

    changed = False
    # Example text: "resolution: not supported. Accepted: 1K | Google AI Studio: resolution: not supported. Accepted: 1K"
    # or "aspect_ratio: not supported."
    for param in ("resolution", "aspect_ratio", "size", "output_format"):
        if param not in data:
            continue

        pattern = rf"\b{param}\b: not supported\.(?: Accepted: ([^|}}\n\"]+))?"
        matches = list(re.finditer(pattern, error_text))
        if matches:
            # all providers rejecting this param must agree on the accepted values
            accepted_sets = []
            for m in matches:
                accepted_str = m.group(1)
                if accepted_str:
                    accepted_sets.append([x.strip() for x in accepted_str.split(",")])
                else:
                    accepted_sets.append([])

            # find intersection of all non-empty accepted sets
            # if they disagree, or no accepted values provided, delete the param
            valid_accepted = [s for s in accepted_sets if s]
            if valid_accepted:
                common = set(valid_accepted[0])
                for s in valid_accepted[1:]:
                    common.intersection_update(s)
                if common:
                    # Just take the first one from the first set that is in common to preserve order preference
                    new_val = next(x for x in valid_accepted[0] if x in common)
                    data[param] = new_val
                    changed = True
                else:
                    del data[param]
                    changed = True
            else:
                del data[param]
                changed = True

    if changed:
        return json.dumps(data).encode("utf-8")
    return None


def coerce_image_data_url(image_url: str | None = None, source_image: str | None = None) -> str | None:
    """Normalize a source image to a data URL or http(s) URL for JSON image APIs."""
    ref = image_url or source_image
    if not ref:
        return None
    if ref.startswith(("data:image", "http://", "https://")):
        return ref
    return "data:image/png;base64," + ref


def coerce_raw_b64(image_url: str | None = None, source_image: str | None = None) -> str | None:
    """Normalize a source image to raw base64 (Ollama ``images`` / Gemini ``inlineData``)."""
    ref = image_url or source_image
    if not ref:
        return None
    if ref.startswith("data:") and "," in ref:
        return ref.split(",", 1)[1]
    return ref


def inline_image_mime(image_url: str | None = None, source_image: str | None = None) -> str:
    """MIME type for an inline image part; default png when the source is raw base64."""
    ref = image_url or source_image
    if ref and ref.startswith("data:") and ";" in ref:
        mime = ref[5:].split(";", 1)[0]
        if mime:
            return mime
    return "image/png"


class BaseProviderShim:
    """Base provider shim implementing standard OpenAI-compatible API format by default."""

    client: Any

    def __init__(self, client: Any) -> None:
        self.client = client

    def build_chat_request(
        self, messages: list[dict[str, Any]], max_tokens: int, temperature: float | None, tools: list[dict[str, Any]] | None, stream: bool, model_name: str | None, response_format: dict[str, Any] | None, chat_extra: dict[str, Any] | None = None
    ) -> tuple[str, str, bytes, dict[str, str]]:
        from .llm_client import merge_openrouter_chat_extra

        endpoint = self.client._endpoint()
        api_path = self.client._api_path()
        url = endpoint + api_path + "/chat/completions"

        # top_p used to be hardcoded to 0.9 on every OpenAI-compatible chat.
        # Models that reject temperature and top_p together returned HTTP 400,
        # and there is no top_p setting. Omit it, same as a missing temperature.
        data: dict[str, Any] = {"messages": messages, "max_tokens": max_tokens, "stream": stream}
        if temperature is not None:
            data["temperature"] = temperature
        if model_name:
            data["model"] = model_name
        if tools:
            data["tools"] = tools
            data["tool_choice"] = "auto"
            # Always false until the tool loop can apply parallel calls. The
            # config key is stored but not read here; do not send True.
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

    def parse_response_chunk(self, chunk: dict[str, Any], stream_state: dict[str, Any] | None = None) -> tuple[str, str | None, str | None, dict[str, Any]]:
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

    def parse_sync_response(self, response_data: dict[str, Any]) -> tuple[str, str | None, list[dict[str, Any]] | None, dict[str, Any], list[str], dict[str, Any]]:
        from .stream_normalizer import _normalize_delta, _normalize_message_content

        # Local models (Ollama) can return {"done_reason": "stop", "message": ...}
        # with no top-level "choices". Prefer choices[0] when present, and fall
        # back to the top-level message / done_reason so content is kept.
        choices = response_data.get("choices")
        choice = choices[0] if (isinstance(choices, list) and choices and isinstance(choices[0], dict)) else {}
        message = choice.get("message") or response_data.get("message") or {}
        if not isinstance(message, dict):
            message = {}
        _normalize_delta(message)
        finish_reason = choice.get("finish_reason") or response_data.get("finish_reason") or response_data.get("done_reason")
        err_obj = choice.get("error") or response_data.get("error")
        if finish_reason == "error" or err_obj:
            from plugin.framework.errors import NetworkError
            from plugin.framework.i18n import _

            err_msg = ""
            if isinstance(err_obj, dict):
                err_msg = str(err_obj.get("message") or err_obj.get("type") or "").strip()
            elif isinstance(err_obj, str):
                err_msg = err_obj.strip()
            if not err_msg:
                err_msg = _("Stream ended with finish_reason=error")
            # The streaming loop raises NetworkError on finish_reason == 'error'
            # and choices[0].error. The sync path must do the same, or an empty
            # body looks like a successful reply and the caller burns turns.
            raise NetworkError(err_msg, code="STREAM_ERROR")

        raw_content = message.get("content")
        content = _normalize_message_content(raw_content) or ""
        images = message.get("images") or []
        tool_calls = message.get("tool_calls")
        # JSON "usage": null makes .get("usage", {}) return None, because the
        # key is present. ``or {}`` normalizes that to a dict.
        usage = response_data.get("usage") or {}

        return content, finish_reason, tool_calls, usage, images, message

    def build_image_request(self, prompt: str, model: str | None, width: int, height: int, steps: int | None = None, source_image: str | None = None, image_url: str | None = None) -> tuple[str, str, bytes, dict[str, str]]:
        """Build an image generation request (standard OpenAI format)."""
        endpoint = self.client._endpoint()
        api_path = self.client._api_path()
        url = endpoint + api_path + "/images/generations"
        data: dict[str, Any] = {"prompt": prompt, "n": 1, "size": f"{width}x{height}", "response_format": "b64_json"}
        if model:
            data["model"] = model
        if steps:
            data["steps"] = steps

        ref = coerce_image_data_url(image_url, source_image)
        if ref:
            data["image_url"] = ref

        path = get_url_path_and_query(url)
        return "POST", path, json.dumps(data).encode("utf-8"), self.client._headers()

    def parse_image_responses(self, response_data: dict[str, Any]) -> list[str]:
        """Extract list of base64 image data from response (standard OpenAI format)."""
        items = response_data.get("data", [])
        # A null or non-object entry used to raise AttributeError on .get.
        if not isinstance(items, list):
            return []
        out = []
        for it in items:
            if isinstance(it, dict) and (b64 := it.get("b64_json")):
                out.append(b64)
        return out

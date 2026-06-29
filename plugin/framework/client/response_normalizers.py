# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""LLM response normalizers and provider shims.

Contains parser classes and shims to handle provider quirks and formats
for OpenAI, Ollama, OpenRouter, Anthropic, and Google Gemini.
"""

import copy
import json
import logging
import re
from typing import Any, cast

from plugin.framework.constants import LLM_DEV_BUILD_SYSTEM_PREFIX, should_prepend_dev_llm_system_prefix
from plugin.framework.url_utils import get_url_path_and_query

log = logging.getLogger(__name__)

# Local / Harmony-style models sometimes leak chat-template control tokens.
_CHAT_TEMPLATE_CONTROL_TOKEN_RE = re.compile(r"<\|[a-zA-Z0-9_]+\|>")
_DATA_URI_IMAGE_RE = re.compile(r'data:image/([a-zA-Z+.-]+);base64,([a-zA-Z0-9+/=\s]+)')


def strip_leaked_chat_template_control_tokens(content: str | None) -> str:
    """Remove ``<|name|>`` chat-template tokens that models sometimes emit in plain text."""
    if not content:
        return ""
    return _CHAT_TEMPLATE_CONTROL_TOKEN_RE.sub("", content).strip()


def extract_and_strip_images_from_message(message: dict[str, Any], strip_structured_image_blocks: bool = True) -> list[dict[str, Any]]:
    """Scan message content, extract base64 images, and replace them with markers.

    Returns a list of extracted image dicts:
        [{"mime_type": "image/png", "data": "<base64>"}]
    """
    extracted_images: list[dict[str, Any]] = []
    content = message.get("content")
    if not content:
        return extracted_images

    if isinstance(content, str):
        # Scan for inline data:image URIs
        def repl(match):
            ext = match.group(1)
            b64 = "".join(match.group(2).split())  # strip whitespace/newlines
            mime_type = f"image/{ext}"
            extracted_images.append({"mime_type": mime_type, "data": b64})
            return "[Image Ref]"

        new_content_str = _DATA_URI_IMAGE_RE.sub(repl, content)
        message["content"] = new_content_str

    elif isinstance(content, list):
        new_content_list: list[Any] = []
        for part in content:
            if not isinstance(part, dict):
                new_content_list.append(part)
                continue

            p_type = part.get("type")
            if p_type == "text":
                text = part.get("text", "")
                def repl(match):
                    ext = match.group(1)
                    b64 = "".join(match.group(2).split())
                    mime_type = f"image/{ext}"
                    extracted_images.append({"mime_type": mime_type, "data": b64})
                    return "[Image Ref]"
                new_text = _DATA_URI_IMAGE_RE.sub(repl, text)
                part["text"] = new_text
                new_content_list.append(part)
            elif p_type == "image_url":
                if strip_structured_image_blocks:
                    url_val = part.get("image_url", {}).get("url", "")
                    if url_val.startswith("data:"):
                        match = _DATA_URI_IMAGE_RE.search(url_val)
                        if match:
                            ext = match.group(1)
                            b64 = "".join(match.group(2).split())
                            mime_type = f"image/{ext}"
                            extracted_images.append({"mime_type": mime_type, "data": b64})
                    # Replace the image_url block with a text part so it is stripped from text/HTML
                    new_content_list.append({"type": "text", "text": "[Image Ref]"})
                else:
                    new_content_list.append(part)
            else:
                new_content_list.append(part)
        message["content"] = new_content_list

    return extracted_images


def normalize_multimodal_messages(messages: list[dict[str, Any]], provider: str) -> None:
    """Normalize multimodal messages containing base64 images according to provider rules.

    1. Extract all base64 images from every message using `extract_and_strip_images_from_message`.
    2. Re-attach them:
       - To the same message if the role is 'user'.
       - To the same message if the role is 'tool' and the provider is 'anthropic'.
       - Otherwise, move them to the nearest preceding 'user' message in the history.
    """
    all_extracted = []
    for idx, m in enumerate(messages):
        role = m.get("role")
        keep_in_place = (role == "user") or (role == "tool" and provider == "anthropic")
        imgs = extract_and_strip_images_from_message(m, strip_structured_image_blocks=not keep_in_place)
        all_extracted.append((idx, m, imgs))

    for idx, m, imgs in all_extracted:
        if not imgs:
            continue

        role = m.get("role")
        keep_in_place = (role == "user") or (role == "tool" and provider == "anthropic")

        target_message = None
        if keep_in_place:
            target_message = m
        else:
            try:
                curr_idx = messages.index(m)
            except ValueError:
                curr_idx = idx

            for prev_idx in range(curr_idx - 1, -1, -1):
                if messages[prev_idx].get("role") == "user":
                    target_message = messages[prev_idx]
                    break

            if target_message is None:
                target_message = {"role": "user", "content": "[Image attached by tool/system]"}
                insert_idx = 0
                for i in range(len(messages)):
                    if messages[i].get("role") != "system":
                        insert_idx = i
                        break
                messages.insert(insert_idx, target_message)

        # Attach images to target_message
        target_dict = cast(dict[str, Any], target_message)
        content = target_dict.get("content")
        new_content: list[Any] = []
        if isinstance(content, str):
            if content:
                new_content.append({"type": "text", "text": content})
        elif isinstance(content, list):
            new_content.extend(content)

        for img in imgs:
            new_content.append({
                "type": "image_url",
                "image_url": {
                    "url": f"data:{img['mime_type']};base64,{img['data']}"
                }
            })
        target_dict["content"] = new_content


def prepend_dev_build_system_prefix_to_messages(messages: list) -> None:
    """If this is a non-release bundle, prepend a dev-oriented line to the first system message."""
    if not should_prepend_dev_llm_system_prefix():
        return
    prefix = LLM_DEV_BUILD_SYSTEM_PREFIX
    for m in messages:
        if m.get("role") != "system":
            continue
        c = m.get("content")
        if isinstance(c, str):
            if c.startswith(prefix):
                return
            m["content"] = f"{prefix}\n\n{c}"
            return
        if isinstance(c, list):
            # Prepend to the first text block if it doesn't already have it
            for item in c:
                if isinstance(item, dict) and item.get("type") == "text":
                    text = item.get("text", "")
                    if text.startswith(prefix):
                        return
                    item["text"] = f"{prefix}\n\n{text}" if text else prefix
                    return
            # No text block? Insert one at the beginning
            c.insert(0, {"type": "text", "text": prefix})
            return


class BaseProviderShim:
    """Base class for provider-specific shims (Anthropic, Google, OpenAI)."""

    def __init__(self, client: Any):
        self.client = client

    def build_chat_request(self, messages, max_tokens, temperature, tools, stream, model_name, response_format, chat_extra=None):
        raise NotImplementedError()

    def parse_response_chunk(self, chunk):
        """Extract content, finish_reason, thinking, and delta from a response chunk."""
        raise NotImplementedError()

    def parse_sync_response(self, response_data):
        """Extract elements from full sync response data.

        Returns tuple: (content: str, finish_reason: str|None, tool_calls: list|None, usage: dict, images: list, message: dict)
        """
        raise NotImplementedError()

    def build_image_request(self, prompt, model, width, height, steps=None, source_image=None, image_url=None):
        """Build an image generation request."""
        raise NotImplementedError()

    def parse_image_responses(self, response_data):
        """Extract list of base64 image data from response."""
        raise NotImplementedError()


class OpenAIShim(BaseProviderShim):
    """Shim for OpenAI-compatible providers."""

    def build_chat_request(self, messages, max_tokens, temperature, tools, stream, model_name, response_format, chat_extra=None):
        from .llm_client import merge_openrouter_chat_extra
        endpoint = self.client._endpoint()
        api_path = self.client._api_path()
        url = endpoint + api_path + "/chat/completions"

        data = {"messages": messages, "max_tokens": max_tokens, "temperature": temperature, "top_p": 0.9, "stream": stream}
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

    def parse_response_chunk(self, chunk):
        from .stream_normalizer import _extract_thinking_from_delta
        choices = chunk.get("choices", [])
        choice = choices[0] if choices else {}
        delta = choice.get("delta", {})

        finish_reason = choice.get("finish_reason") if choice else None
        if not finish_reason:
            finish_reason = chunk.get("finish_reason")
        if not finish_reason and choices:
            for c in choices:
                if isinstance(c, dict) and c.get("finish_reason"):
                    finish_reason = c.get("finish_reason")
                    break

        content = (delta.get("content") or "") if delta else ""
        thinking = _extract_thinking_from_delta(chunk)
        return content, finish_reason, thinking, delta

    def parse_sync_response(self, response_data):
        from .stream_normalizer import _normalize_delta, _normalize_message_content
        choice = response_data.get("choices", [{}])[0] if response_data.get("choices") else {}
        if choice is None:
            choice = {}
        message = choice.get("message") or response_data.get("message") or {}
        _normalize_delta(message)
        finish_reason = choice.get("finish_reason") or response_data.get("done_reason")

        raw_content = message.get("content")
        content = _normalize_message_content(raw_content)
        images = message.get("images") or []
        tool_calls = message.get("tool_calls")
        usage = response_data.get("usage", {})

        return content, finish_reason, tool_calls, usage, images, message

    def build_image_request(self, prompt, model, width, height, steps=None, source_image=None, image_url=None):
        endpoint = self.client._endpoint()
        api_path = self.client._api_path()
        url = endpoint + api_path + "/images/generations"
        data = {"prompt": prompt, "n": 1, "size": f"{width}x{height}", "response_format": "b64_json"}
        if model:
            data["model"] = model
        if steps:
            data["steps"] = steps

        # img2img extension for Together/Fal/Replicate
        if image_url:
            data["image_url"] = image_url
        elif source_image:
            if source_image.startswith("data:image"):
                data["image_url"] = source_image
            else:
                data["image_url"] = "data:image/png;base64," + source_image

        path = get_url_path_and_query(url)
        return "POST", path, json.dumps(data).encode("utf-8"), self.client._headers()

    def parse_image_responses(self, response_data):
        items = response_data.get("data", [])
        out = []
        for it in items:
            if b64 := it.get("b64_json"):
                out.append(b64)
        return out


class OllamaShim(OpenAIShim):
    """Shim for Ollama specifically (handles native /api endpoints if needed)."""

    def build_image_request(self, prompt, model, width, height, steps=None, source_image=None, image_url=None):
        endpoint = self.client._endpoint()
        url = f"{endpoint}/api/generate"
        eff_model = model
        if not eff_model:
            eff_model = "flux"

        data = {"model": eff_model, "prompt": prompt, "stream": False}
        path = get_url_path_and_query(url)
        return "POST", path, json.dumps(data).encode("utf-8"), self.client._headers()

    def parse_image_responses(self, response_data):
        images = response_data.get("images")
        if images and isinstance(images, list):
            return images
        if img := response_data.get("image"):
            return [img]
        if "data" in response_data:
            return super().parse_image_responses(response_data)
        return []


class OpenRouterShim(OpenAIShim):
    """Shim for OpenRouter specifically (handles dedicated /images endpoint)."""

    def build_image_request(self, prompt, model, width, height, steps=None, source_image=None, image_url=None):
        endpoint = self.client._endpoint()
        api_path = self.client._api_path()
        url = endpoint + api_path + "/images"
        data = {"prompt": prompt, "model": model, "n": 1, "output_format": "webp"}
        if width and height:
            data["size"] = f"{width}x{height}"
            ratio = width / height
            if abs(ratio - 1.0) < 0.05:
                data["aspect_ratio"] = "1:1"
            elif abs(ratio - (16/9)) < 0.05:
                data["aspect_ratio"] = "16:9"
            elif abs(ratio - (4/3)) < 0.05:
                data["aspect_ratio"] = "4:3"
            elif abs(ratio - (9/16)) < 0.05:
                data["aspect_ratio"] = "9:16"
            elif abs(ratio - (3/4)) < 0.05:
                data["aspect_ratio"] = "3:4"

        if image_url:
            data["image_url"] = image_url
        elif source_image:
            if source_image.startswith("data:image"):
                data["image_url"] = source_image
            else:
                data["image_url"] = "data:image/png;base64," + source_image

        path = get_url_path_and_query(url)
        return "POST", path, json.dumps(data).encode("utf-8"), self.client._headers()

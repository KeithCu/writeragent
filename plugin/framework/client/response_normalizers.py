# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""LLM response normalizers and message preprocessing.

Contains helpers to normalize multimodal messages, strip leaked chat-template
control tokens, and extract base64 images.
"""

from __future__ import annotations

import logging
import re
from typing import Any, cast

from plugin.framework.deal_shim import deal

__all__ = [
    "LLM_DEV_BUILD_SYSTEM_PREFIX",
    "extract_and_strip_images_from_message",
    "normalize_multimodal_messages",
    "prepend_dev_build_system_prefix_to_messages",
    "should_prepend_dev_llm_system_prefix",
    "strip_leaked_chat_template_control_tokens",
]

log = logging.getLogger(__name__)

# Prepended to the first string `system` message in LlmClient for non-release bundles only
# (``make build`` includes ``plugin/tests``; ``make release`` / ``--no-tests`` does not).
# See `should_prepend_dev_llm_system_prefix()`.
LLM_DEV_BUILD_SYSTEM_PREFIX = (
    "[WriterAgent development build]\n"
    "You are running a development version of the WriterAgent extension. The user is a plugin developer. "
    "If you run into a problem, explain in detail what failed and why so they can improve the extension. "
    "If they ask detailed questions about tool-calling APIs, prompts, or how the software works, answer helpfully so developers can improve the plugin."
)


def should_prepend_dev_llm_system_prefix() -> bool:
    """True when this bundle includes test modules (same signal as the optional Debug / in-OXT tests)."""
    try:
        import importlib.util

        return importlib.util.find_spec("plugin.tests") is not None
    except Exception:
        return False

# Local / Harmony-style models sometimes leak chat-template control tokens.
_CHAT_TEMPLATE_CONTROL_TOKEN_RE = re.compile(r"<\|[a-zA-Z0-9_]+\|>")
_DATA_URI_IMAGE_RE = re.compile(r"data:image/([a-zA-Z+.-]+);base64,([a-zA-Z0-9+/=\s]+)")


def _extracted_images_well_formed(result: list[dict[str, Any]]) -> bool:
    return isinstance(result, list) and all(
        isinstance(x, dict) and isinstance(x.get("mime_type"), str) and isinstance(x.get("data"), str) for x in result
    )


def _string_content_has_no_data_uri(message: dict[str, Any]) -> bool:
    content = message.get("content")
    if not isinstance(content, str):
        return True
    return _DATA_URI_IMAGE_RE.search(content) is None


@deal.post(lambda result: isinstance(result, str))
@deal.ensure(lambda content, result: _CHAT_TEMPLATE_CONTROL_TOKEN_RE.search(result) is None)
@deal.ensure(lambda content, result: len(result) <= len(content or ""))
def strip_leaked_chat_template_control_tokens(content: str | None) -> str:
    """Remove ``<|name|>`` chat-template tokens that models sometimes emit in plain text."""
    # Greedy ``<|…|>`` regex on unbounded ASCII hangs deep check.
    # crosshair: off
    if not content:
        return ""
    return _CHAT_TEMPLATE_CONTROL_TOKEN_RE.sub("", content).strip()


# Optional strip_structured_image_blocks is often omitted; deal forwards provided args + result=.
@deal.pre(lambda *args, **kwargs: bool(args) and isinstance(args[0], dict))
@deal.post(lambda result: _extracted_images_well_formed(result))
@deal.ensure(lambda *args, result=None, **kwargs: _string_content_has_no_data_uri(args[0]))
def extract_and_strip_images_from_message(
    message: dict[str, Any], strip_structured_image_blocks: bool = True
) -> list[dict[str, Any]]:
    """Scan message content, extract base64 images, and replace them with markers.

    Returns a list of extracted image dicts:
        [{"mime_type": "image/png", "data": "<base64>"}]
    """
    # Greedy data:image/…;base64 regex hangs deep check; pytest keeps product sizes.
    # crosshair: off
    extracted_images: list[dict[str, Any]] = []
    content = message.get("content")
    if not content:
        return extracted_images

    if isinstance(content, str):
        # Scan for inline data:image URIs
        def repl(match: re.Match[str]) -> str:
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

                def repl(match: re.Match[str]) -> str:
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
        target_dict = cast("dict[str, Any]", target_message)
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
                "image_url": {"url": f"data:{img['mime_type']};base64,{img['data']}"},
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

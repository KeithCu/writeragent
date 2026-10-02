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

import copy
import datetime
import logging
import re
from typing import Any, cast

from plugin.framework.deal_shim import deal

__all__ = ["LLM_DEV_BUILD_SYSTEM_PREFIX", "extract_and_strip_images_from_message", "normalize_multimodal_messages", "prepare_chat_messages", "prepend_dev_build_system_prefix_to_messages", "should_prepend_dev_llm_system_prefix", "strip_leaked_chat_template_control_tokens"]

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
    return isinstance(result, list) and all(isinstance(x, dict) and isinstance(x.get("mime_type"), str) and isinstance(x.get("data"), str) for x in result)


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
def extract_and_strip_images_from_message(message: dict[str, Any], strip_structured_image_blocks: bool = True) -> list[dict[str, Any]]:
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
                    # ``image_url`` is a dict on OpenAI and a string on some
                    # local hosts. A present null used to crash .get before HTTP.
                    image_url = part.get("image_url")
                    if isinstance(image_url, dict):
                        url_val = image_url.get("url", "")
                    elif isinstance(image_url, str):
                        url_val = image_url
                    else:
                        url_val = ""
                    if isinstance(url_val, str) and url_val.startswith("data:"):
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
            # Identity, not equality. messages.index(m) attached both copies of
            # two equal assistant messages to the first one, so the later
            # image landed on the earlier user turn.
            curr_idx = idx
            for i, msg in enumerate(messages):
                if msg is m:
                    curr_idx = i
                    break

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
            new_content.append({"type": "image_url", "image_url": {"url": f"data:{img['mime_type']};base64,{img['data']}"}})
        target_dict["content"] = new_content


def prepare_chat_messages(messages: list[Any], provider: str, *, prepend_dev_build_system_prefix: bool = True) -> list[Any]:
    """Coalesce consecutive system messages, inject the date line, then flatten text-only system content.

    Dev-build prefix and ``normalize_multimodal_messages`` stay between the date
    line and the flatten. That was the order inside ``make_chat_request``;
    pulling the three blocks out must not reorder them around image normalization.
    """
    coalesced_messages: list[Any] = []
    coalesced_any = False
    for m in messages:
        if coalesced_messages and m.get("role") == "system" and coalesced_messages[-1].get("role") == "system":
            prev_content = coalesced_messages[-1].get("content", "")
            curr_content = m.get("content", "")

            # Merge logic supporting both str and list content
            if isinstance(prev_content, str) and isinstance(curr_content, str):
                coalesced_messages[-1]["content"] = prev_content + "\n\n" + curr_content
            else:
                # Normalize both to list and extend
                merged = []
                if isinstance(prev_content, str):
                    merged.append({"type": "text", "text": prev_content})
                elif isinstance(prev_content, list):
                    merged.extend(prev_content)

                if isinstance(curr_content, str):
                    merged.append({"type": "text", "text": curr_content})
                elif isinstance(curr_content, list):
                    merged.extend(curr_content)

                coalesced_messages[-1]["content"] = merged

            coalesced_any = True
        else:
            coalesced_messages.append(copy.deepcopy(m) if isinstance(m, dict) else m)

    if coalesced_any:
        log.debug("make_chat_request: Coalesced multiple consecutive system messages.")

    messages = coalesced_messages

    # Inject date into the first system message
    today = datetime.date.today().strftime("%A, %Y-%m-%d")
    date_msg = f"Today's date is {today}."
    system_message: Any = None
    for m in messages:
        if m.get("role") == "system":
            system_message = m
            break

    if system_message:
        old_content = system_message.get("content")
        if isinstance(old_content, str):
            already_has_date_line = old_content.startswith(date_msg) or old_content.startswith("Today's date is ") or date_msg in old_content
            if not already_has_date_line:
                system_message["content"] = f"{date_msg}\n\n{old_content}" if old_content else date_msg
        elif isinstance(old_content, list):
            already_has_date_line = False
            text_item = None
            for item in old_content:
                if isinstance(item, dict) and item.get("type") == "text":
                    if text_item is None:
                        text_item = item
                    t = item.get("text", "")
                    if date_msg in t or "Today's date is " in t:
                        already_has_date_line = True
                        break

            if not already_has_date_line:
                if text_item:
                    t = text_item.get("text", "")
                    text_item["text"] = f"{date_msg}\n\n{t}" if t else date_msg
                else:
                    old_content.insert(0, {"type": "text", "text": date_msg})
    else:
        messages.insert(0, {"role": "system", "content": date_msg})

    if prepend_dev_build_system_prefix:
        prepend_dev_build_system_prefix_to_messages(messages)

    normalize_multimodal_messages(messages, provider)

    # Flatten system message back to string if it only contains text (for max compatibility)
    for m in messages:
        if m.get("role") == "system":
            content = m.get("content")
            if isinstance(content, list):
                all_text = []
                only_text = True
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "text":
                        all_text.append(item.get("text", ""))
                    else:
                        only_text = False
                        break
                if only_text:
                    m["content"] = "\n\n".join(all_text)
            break

    return messages


def prepend_dev_build_system_prefix_to_messages(messages: list[dict[str, Any]]) -> None:
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

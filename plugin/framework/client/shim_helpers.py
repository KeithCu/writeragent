"""Pure helpers shared by the provider shims (no plugin imports, so the LibreHarper and LibrePy bundles can ship it)."""

from __future__ import annotations

from typing import Any


SNAPSHOT_HEADER = "[DOCUMENT CONTENT — current state, not written by the user]"  # U+2014


def snapshot_text(doc_text: str) -> str:
    """Format document text with snapshot header and closing marker for prompt caching."""
    if not doc_text:
        return ""
    return f"{SNAPSHOT_HEADER}\n{doc_text}\n[END DOCUMENT]"


def extract_snapshot_text(content: Any) -> str | None:
    """Extract document snapshot text from message content, or None if absent."""
    if isinstance(content, str):
        if content.startswith(SNAPSHOT_HEADER):
            return content
        if SNAPSHOT_HEADER in content:
            return content[content.find(SNAPSHOT_HEADER) :]
    elif isinstance(content, list) and content:
        last = content[-1]
        if isinstance(last, dict) and last.get("type") == "text":
            txt = last.get("text")
            if isinstance(txt, str) and SNAPSHOT_HEADER in txt:
                return txt[txt.find(SNAPSHOT_HEADER) :]
    return None


def extract_snapshot_from_messages(messages: list[Any]) -> str | None:
    """Extract document snapshot text from the most recent message containing it."""
    for msg in reversed(messages):
        if isinstance(msg, dict):
            snap = extract_snapshot_text(msg.get("content"))
            if snap is not None:
                return snap
    return None


def annotate_anthropic_cache(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Place Anthropic ephemeral cache marker before snapshot in user send or tool loop."""
    if not messages:
        return messages
    last_msg = messages[-1]
    if not isinstance(last_msg, dict) or last_msg.get("role") != "user":
        return messages

    c = last_msg.get("content")
    snap_text: str | None = None
    if isinstance(c, str) and c.startswith(SNAPSHOT_HEADER):
        snap_text = c
    elif isinstance(c, list) and len(c) == 1 and isinstance(c[0], dict) and c[0].get("type") == "text":
        txt = c[0].get("text")
        if isinstance(txt, str) and txt.startswith(SNAPSHOT_HEADER):
            snap_text = txt

    # Tool loop: marker on turn immediately before snapshot
    if snap_text is not None and len(messages) >= 2:
        prev_msg = messages[-2]
        if isinstance(prev_msg, dict):
            # Anthropic native: previous message is user turn with tool_result blocks.
            # Merge snapshot text into that turn (Anthropic rejects adjacent user turns).
            if prev_msg.get("role") == "user":
                prev_content = prev_msg.get("content")
                if (
                    isinstance(prev_content, list)
                    and prev_content
                    and all(isinstance(b, dict) and b.get("type") == "tool_result" for b in prev_content)
                ):
                    messages.pop()
                    prev_content[-1]["cache_control"] = {"type": "ephemeral"}
                    prev_content.append({"type": "text", "text": snap_text})
                    return messages
            # OpenRouter: previous message is role "tool"
            elif prev_msg.get("role") == "tool":
                tool_content = prev_msg.get("content")
                if isinstance(tool_content, list):
                    parts = [dict(p) if isinstance(p, dict) else p for p in tool_content]
                    if parts and isinstance(parts[-1], dict):
                        parts[-1] = dict(parts[-1], cache_control={"type": "ephemeral"})
                elif isinstance(tool_content, str):
                    parts = [{"type": "text", "text": tool_content, "cache_control": {"type": "ephemeral"}}]
                else:
                    parts = [{"type": "text", "text": str(tool_content or ""), "cache_control": {"type": "ephemeral"}}]
                new_tool_msg = dict(prev_msg, content=parts)
                new_messages = list(messages)
                new_messages[-2] = new_tool_msg
                return new_messages

    # User send: content list has user prompt block followed by snapshot block
    if isinstance(c, list) and len(c) >= 2:
        last_block = c[-1]
        if isinstance(last_block, dict) and last_block.get("type") == "text":
            txt = last_block.get("text")
            if isinstance(txt, str) and txt.startswith(SNAPSHOT_HEADER):
                prev_part = c[-2]
                if isinstance(prev_part, dict):
                    new_prev = dict(prev_part, cache_control={"type": "ephemeral"})
                elif isinstance(prev_part, str):
                    new_prev = {"type": "text", "text": prev_part, "cache_control": {"type": "ephemeral"}}
                else:
                    new_prev = {"type": "text", "text": str(prev_part or ""), "cache_control": {"type": "ephemeral"}}
                new_parts = list(c[:-2]) + [new_prev, last_block]
                new_last = dict(last_msg, content=new_parts)
                return list(messages[:-1]) + [new_last]

    return messages


def _dict_parts(content: Any) -> list[dict[str, Any]]:  # pyright: ignore[reportUnusedFunction]
    """Content blocks that are dicts. Null, a string, or a non-dict part is skipped.

    Calling ``.get`` on sync ``content: null`` or a non-dict part raises
    TypeError / AttributeError. The Google image path already treats null
    content as no blocks.
    """
    if not isinstance(content, list):
        return []
    return [part for part in content if isinstance(part, dict)]


def _system_text(content: Any) -> str:  # pyright: ignore[reportUnusedFunction]
    """Text of one system or developer message. List parts keep their order."""
    if isinstance(content, list):
        return "\n\n".join(str(p.get("text", "")) for p in _dict_parts(content) if p.get("type") == "text")
    return str(content or "")


def _normalize_usage(raw: Any) -> dict[str, Any]:  # pyright: ignore[reportUnusedFunction]
    """Copy provider usage and add the OpenAI token names callers already read.

    Librarian and PPT host use ``prompt_tokens`` / ``completion_tokens``.
    Anthropic/Responses sends ``input_tokens`` / ``output_tokens``. Cache fields stay.
    """
    if not isinstance(raw, dict) or not raw:
        return {}
    usage = dict(raw)
    if "prompt_tokens" not in usage and "input_tokens" in usage:
        usage["prompt_tokens"] = usage["input_tokens"]
    if "completion_tokens" not in usage and "output_tokens" in usage:
        usage["completion_tokens"] = usage["output_tokens"]
    return usage


def _openai_model_leaf(model_name: str | None) -> str:  # pyright: ignore[reportUnusedFunction]
    """Strip ft: and vendor prefix (e.g. openai/...) from a model identifier.

    Fine-tune ids are ft:<base>:<org>:<suffix>:<id>. The base is the field after
    ft:, including when a provider prefix sits in front (openai/ft:...). A '/'
    inside that base (ft:openai/gpt-5-mini:...) is stripped afterwards.
    https://developers.openai.com/api/reference/resources/fine_tuning/subresources/jobs/methods/create
    """
    name = str(model_name or "").strip().lower()
    ft_at = name.find("ft:")
    if ft_at != -1 and (ft_at == 0 or name[ft_at - 1] in "/:"):
        base = name[ft_at + 3 :].split(":", 1)[0]
        if base:
            name = base
    if "/" in name:
        name = name.rsplit("/", 1)[-1]
    return name


def cache_read_count(usage: Any) -> int | None:
    """Return cached input token count from Anthropic or OpenAI usage dict, or None."""
    if not isinstance(usage, dict):
        return None
    c = usage.get("cache_read_input_tokens")
    if isinstance(c, int) and not isinstance(c, bool):
        return c
    details = usage.get("prompt_tokens_details")
    if isinstance(details, dict):
        c = details.get("cached_tokens")
        if isinstance(c, int) and not isinstance(c, bool):
            return c
    return None

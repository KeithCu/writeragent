"""Pure helpers shared by the provider shims (no plugin imports, so the LibreHarper and LibrePy bundles can ship it)."""

from __future__ import annotations

from typing import Any, TypeGuard


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


def fold_trailing_user_message(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fold a trailing user message into the preceding user turn as a content block.

    Models using strict alternating-role templates (e.g. Gemma, Mistral on
    LM Studio, llama.cpp, vLLM) reject requests with two adjacent user
    messages. Folding also allows Anthropic ephemeral cache markers to be
    placed on the block immediately preceding the dynamic tail text.
    """
    if len(messages) < 2:
        return messages
    if not isinstance(messages[-1], dict) or messages[-1].get("role") != "user":
        return messages
    if not isinstance(messages[-2], dict) or messages[-2].get("role") != "user":
        return messages

    last_msg = messages[-1]
    prev_msg = messages[-2]

    last_content = last_msg.get("content")
    if isinstance(last_content, str):
        tail_text = last_content
    elif isinstance(last_content, list):
        if any(isinstance(p, dict) and p.get("type") == "tool_result" for p in last_content):
            return messages
        parts: list[str] = []
        for p in last_content:
            if isinstance(p, dict) and p.get("type") == "text":
                parts.append(str(p.get("text") or ""))
            elif isinstance(p, str):
                parts.append(p)
        tail_text = "".join(parts)
    else:
        tail_text = str(last_content or "")

    if not tail_text:
        return messages

    prev_content = prev_msg.get("content")
    if isinstance(prev_content, list):
        new_content = [dict(b) if isinstance(b, dict) else b for b in prev_content] + [{"type": "text", "text": tail_text}]
    elif isinstance(prev_content, str):
        new_content = [{"type": "text", "text": prev_content}, {"type": "text", "text": tail_text}]
    else:
        new_content = [{"type": "text", "text": str(prev_content or "")}, {"type": "text", "text": tail_text}]

    new_prev = dict(prev_msg, content=new_content)
    return list(messages[:-2]) + [new_prev]


def _is_tail_text(txt: Any) -> bool:
    return isinstance(txt, str) and txt.startswith(SNAPSHOT_HEADER)


def _is_tail_block(block: Any) -> bool:
    if isinstance(block, dict) and block.get("type") == "text":
        return _is_tail_text(block.get("text"))
    return False


def split_snapshot_text(content: Any) -> tuple[Any, str | None]:
    """Split message content into (content_without_snapshot, snapshot_text)."""
    snap = extract_snapshot_text(content)
    if not snap:
        return content, None
    if isinstance(content, str):
        if content.startswith(SNAPSHOT_HEADER):
            return "", snap
        sep = f"\n\n{SNAPSHOT_HEADER}"
        if sep in content:
            return content[: content.find(sep)], snap
        idx = content.find(SNAPSHOT_HEADER)
        return content[:idx].rstrip("\r\n"), snap
    if isinstance(content, list) and content:
        last = content[-1]
        if isinstance(last, dict) and last.get("type") == "text":
            txt = last.get("text")
            if isinstance(txt, str) and SNAPSHOT_HEADER in txt:
                sub_body, _ = split_snapshot_text(txt)
                if sub_body:
                    new_parts = list(content[:-1]) + [dict(last, text=sub_body)]
                else:
                    new_parts = list(content[:-1])
                return new_parts, snap
    return content, snap


def _attach_cache_control(content: Any, control: dict[str, str]) -> Any | None:
    """Attach cache_control to the last non-empty text block in content.

    Returns the updated content structure (without mutating the input), or
    None if the content has no non-empty text (or is whitespace-only). A plain
    string becomes a single text block list so cache_control can be attached.
    """
    if isinstance(content, str):
        if not content.strip():
            return None
        return [{"type": "text", "text": content, "cache_control": control}]
    if isinstance(content, list):
        for i in range(len(content) - 1, -1, -1):
            part = content[i]
            if isinstance(part, dict) and part.get("type") == "text":
                txt = part.get("text")
                if isinstance(txt, str) and txt.strip():
                    new_part = dict(part, cache_control=control)
                    new_content = list(content)
                    new_content[i] = new_part
                    return new_content
            elif isinstance(part, str) and part.strip():
                new_part = {"type": "text", "text": part, "cache_control": control}
                new_content = list(content)
                new_content[i] = new_part
                return new_content
        return None
    return None


def _mark_opening_system(
    messages: list[dict[str, Any]], control: dict[str, str]
) -> list[dict[str, Any]]:
    """Attach control to the last opening system or developer message's last text block.

    Walks from index 0 while the role is system or developer. Stops at the first
    other role so a mid-conversation system message is never marked as the system
    breakpoint. Returns messages unchanged if no opening system message with non-empty
    text exists.
    """
    last_idx = -1
    for i, msg in enumerate(messages):
        if not isinstance(msg, dict) or msg.get("role") not in ("system", "developer"):
            break
        last_idx = i
    if last_idx < 0:
        return messages
    msg = messages[last_idx]
    new_content = _attach_cache_control(msg.get("content"), control)
    if new_content is None:
        return messages
    new_messages = list(messages)
    new_messages[last_idx] = dict(msg, content=new_content)
    return new_messages


def _mark_part(messages: list[dict[str, Any]], msg_i: int, part_i: int) -> list[dict[str, Any]]:
    """Return a shallow copy of messages with an ephemeral cache marker on the chosen block."""
    msg = messages[msg_i]
    content = msg.get("content")
    if not isinstance(content, list):
        return messages
    part = content[part_i]
    if isinstance(part, dict):
        new_part = dict(part, cache_control={"type": "ephemeral"})
    elif isinstance(part, str):
        new_part = {"type": "text", "text": part, "cache_control": {"type": "ephemeral"}}
    else:
        new_part = {"type": "text", "text": str(part or ""), "cache_control": {"type": "ephemeral"}}
    new_content = list(content)
    new_content[part_i] = new_part
    new_msg = dict(msg, content=new_content)
    new_messages = list(messages)
    new_messages[msg_i] = new_msg
    return new_messages


def annotate_anthropic_cache(
    messages: list[dict[str, Any]],
    *,
    system_cache_control: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Place cache breakpoints on the opening system prompt and before the trailing snapshot.

    When system_cache_control is provided, the last opening system or developer
    message gets that breakpoint attached to its last text block.
    For a user send (or native Anthropic tool round where the snapshot is
    placed as a trailing text block), an ephemeral marker is set on the block
    preceding the tail block. For an OpenRouter Claude tool round ending in a
    tool message with a snapshot, the snapshot is stripped into a trailing user
    message and the tool result is marked. The live snapshot itself is never
    marked.
    """
    if not messages:
        return messages

    if system_cache_control is not None:
        messages = _mark_opening_system(messages, system_cache_control)

    last_msg = messages[-1]
    if not isinstance(last_msg, dict):
        return messages

    last_role = last_msg.get("role")

    # User send (or native Anthropic tool round where _convert_messages already
    # appended the snapshot as a trailing text block to the user tool_result message):
    # content list has block(s) followed by the tail snapshot block.
    if last_role == "user":
        c = last_msg.get("content")
        if isinstance(c, list) and len(c) >= 2 and _is_tail_block(c[-1]):
            return _mark_part(messages, -1, -2)
        return messages

    # OpenRouter Claude tool round: the last message is role:"tool" carrying the
    # snapshot. Strip the snapshot from that tool result, mark its text with
    # cache_control, and append the snapshot as a trailing user message so the
    # live snapshot stays outside the cached prefix.
    if last_role == "tool":
        body_content, snap = split_snapshot_text(last_msg.get("content"))
        if not snap:
            return messages

        if isinstance(body_content, list):
            new_tool_content = [dict(b) if isinstance(b, dict) else {"type": "text", "text": str(b or "")} for b in body_content]
            if new_tool_content:
                last_part = new_tool_content[-1]
                new_tool_content[-1] = dict(last_part, cache_control={"type": "ephemeral"})
            else:
                new_tool_content = [{"type": "text", "text": "", "cache_control": {"type": "ephemeral"}}]
        elif isinstance(body_content, str):
            new_tool_content = [{"type": "text", "text": body_content, "cache_control": {"type": "ephemeral"}}]
        else:
            new_tool_content = [{"type": "text", "text": str(body_content or ""), "cache_control": {"type": "ephemeral"}}]

        marked_tool = dict(last_msg, content=new_tool_content)
        return list(messages[:-1]) + [marked_tool, {"role": "user", "content": snap}]

    return messages


def dict_parts(content: Any) -> list[dict[str, Any]]:
    """Content blocks that are dicts. Null, a string, or a non-dict part is skipped.

    Calling ``.get`` on sync ``content: null`` or a non-dict part raises
    TypeError / AttributeError. The Google image path already treats null
    content as no blocks.
    """
    if not isinstance(content, list):
        return []
    return [part for part in content if isinstance(part, dict)]


def system_text(content: Any) -> str:
    """Text of one system or developer message. List parts keep their order."""
    if isinstance(content, list):
        return "\n\n".join(str(p.get("text", "")) for p in dict_parts(content) if p.get("type") == "text")
    return str(content or "")


def normalize_usage(raw: Any) -> dict[str, Any]:
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


def openai_model_leaf(model_name: str | None) -> str:
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


def is_anthropic_message_id(value: Any) -> TypeGuard[str]:
    """Return True if value matches Anthropic message ID shape (msg_ prefix, max 256 chars)."""
    return isinstance(value, str) and value.startswith("msg_") and len(value) <= 256


def anthropic_response_cache_facts(chunk: Any) -> tuple[str | None, Any] | None:
    """Extract message ID and diagnostics object from an Anthropic SSE chunk or sync response.

    Returns (message_id, diagnostics) for 'message_start' SSE events and sync
    'message' envelopes, or None for all other chunks. The message_id slot is
    populated only when the ID is a string starting with 'msg_' of at most 256
    characters; otherwise it is None.
    """
    if not isinstance(chunk, dict):
        return None

    msg_type = chunk.get("type")
    if msg_type == "message_start":
        message = chunk.get("message")
        if isinstance(message, dict):
            raw_id = message.get("id")
            diag = message.get("diagnostics")
        else:
            raw_id = None
            diag = None
    elif msg_type == "message":
        raw_id = chunk.get("id")
        diag = chunk.get("diagnostics")
    else:
        return None

    msg_id = raw_id if is_anthropic_message_id(raw_id) else None
    return msg_id, diag


def cache_read_count(usage: Any) -> int | None:
    """Return cached input token count from Anthropic, OpenAI, or DeepSeek usage dict, or None."""
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
    c = usage.get("prompt_cache_hit_tokens")
    if isinstance(c, int) and not isinstance(c, bool):
        return c
    return None

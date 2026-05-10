def iterate_sse(stream):
    """
    Iterate over SSE (Server-Sent Events) data payloads from a stream of lines (bytes).
    Yields the payload string. Supports standard 'data:' prefix and raw JSON lines.
    """
    for line in stream:
        line_str = line.strip()
        if not line_str or line_str.startswith(b":"):
            continue

        if line_str.startswith(b"data:"):
            # Payload is everything after the first ":"
            idx = line_str.find(b":") + 1
            payload = line_str[idx:].decode("utf-8").strip()
            yield payload
        elif line_str.startswith(b"{"):
            # Raw JSON line (common in some streaming formats like Google Gemini raw stream)
            yield line_str.decode("utf-8").strip()


def _extract_thinking_from_delta(chunk_delta):
    """Extract reasoning/thinking text from a stream delta for display in UI."""
    # Try direct fields first
    for field in ["reasoning_content", "thought", "thinking"]:
        thinking = chunk_delta.get(field)
        if isinstance(thinking, str) and thinking:
            return thinking

    # Try reasoning_details array
    details = chunk_delta.get("reasoning_details")
    if isinstance(details, list):
        parts = []
        for item in details:
            if isinstance(item, dict):
                item_type = item.get("type")
                if item_type in ("reasoning.text", "thought", "reasoning"):
                    parts.append(item.get("text") or "")
                elif item_type == "reasoning.summary":
                    parts.append(item.get("summary") or "")
        if parts:
            return "".join(parts)

    # Try choices[0].delta if not found at top level
    choices = chunk_delta.get("choices")
    if choices and isinstance(choices, list) and len(choices) > 0:
        delta = choices[0].get("delta", {})
        if delta:
            return _extract_thinking_from_delta(delta)

    return ""


def _normalize_message_content(raw):
    """Return a single string from API message content (string or list of parts)."""
    if raw is None:
        return None
    if isinstance(raw, str):
        return raw
    if isinstance(raw, list):
        parts = []
        for item in raw:
            if isinstance(item, dict):
                if item.get("type") == "text":
                    parts.append(item.get("text") or "")
                elif "text" in item:
                    parts.append(item.get("text") or "")
        return "".join(parts) if parts else None
    return str(raw)


def _normalize_delta(delta):
    """Normalize delta for Mistral/Azure compat before accumulate_delta.
    LiteLLM: streaming_handler.py ~L847 (role), ~L853 (type), ~L820 (arguments).
    """
    if not isinstance(delta, dict):
        return
    # LiteLLM: streaming_handler.py ~L847 "mistral's api returns role as None"
    if "role" in delta and delta["role"] is None:
        delta["role"] = "assistant"
    for tc in delta.get("tool_calls") or []:
        if not isinstance(tc, dict):
            continue
        # LiteLLM: streaming_handler.py ~L853 "mistral's api returns type: None"
        if tc.get("type") is None:
            tc["type"] = "function"
        fn = tc.get("function")
        # LiteLLM: streaming_handler.py ~L820 "## AZURE - check if arguments is not None"
        if isinstance(fn, dict) and fn.get("arguments") is None:
            fn["arguments"] = ""

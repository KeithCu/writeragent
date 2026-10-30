# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Portions adapted from hermes-agent (Nous Research), MIT License:
# Copyright (c) 2025 Nous Research
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
"""Chat-completions history to the OpenAI Responses API, and the stream back.

Used by OpenAIShim for provider openai, gpt-6* models, and by OpenRouterShim
for openai/* models (except gpt-oss) when openai_shim.OPENROUTER_USE_RESPONSES
is True, when tools are present. The tool loop still sees chat deltas.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from plugin.framework.errors import NetworkError

log = logging.getLogger(__name__)


def _openai_model_leaf(model_name: str | None) -> str:
    """Same ft: and vendor-prefix strip as OpenAIShim's reasoning check.

    Duplicated so the Responses predicate does not read _OPENAI_REASONING_PREFIXES.
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


def uses_responses_api(model_name: str | None, tools: list[dict[str, Any]] | None) -> bool:
    if not isinstance(tools, list) or not tools:
        return False
    return _openai_model_leaf(model_name).startswith("gpt-6")


def responses_effort(endpoint_effort: str, chat_extra: dict[str, Any] | None) -> str:
    """The string OpenAIShim's chat body would send as reasoning_effort.

    chat_extra.reasoning.effort wins, matching base_provider_shim._chat_payload
    lines 234–244. Blank means omit the reasoning object. No mapping table.
    If a provider effort-mapping function exists on HEAD, the caller applies
    it to this return value. This function does not.
    """
    if isinstance(chat_extra, dict):
        reasoning = chat_extra.get("reasoning")
        if isinstance(reasoning, dict):
            raw = reasoning.get("effort")
            if isinstance(raw, str) and raw.strip():
                return raw.strip()
    if isinstance(endpoint_effort, str) and endpoint_effort.strip():
        return endpoint_effort.strip()
    return ""


def _responses_text_format(response_format: dict[str, Any] | None) -> dict[str, Any] | None:
    """Chat response_format to Responses text.format.

    json_object is already flat: {"type": "json_object"} (grammar_worker.py).
    Chat json_schema nests under json_schema.{name, schema, strict}
    (smolagents CODEAGENT_RESPONSE_FORMAT). Responses wants those keys beside type.
    https://developers.openai.com/api/docs/guides/migrate-to-responses (step 6).
    """
    if not isinstance(response_format, dict) or not response_format:
        return None
    if response_format.get("type") == "json_schema":
        nested = response_format.get("json_schema")
        if isinstance(nested, dict):
            flat: dict[str, Any] = {"type": "json_schema"}
            for key in ("name", "schema", "strict", "description"):
                if key in nested:
                    flat[key] = nested[key]
            return flat
    return dict(response_format)


def _responses_tools(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        fn = tool.get("function")
        src = fn if isinstance(fn, dict) else tool
        name = src.get("name")
        if not isinstance(name, str) or not name:
            continue
        schema = src.get("parameters")
        if not isinstance(schema, dict):
            schema = {"type": "object", "properties": {}}
        out.append({
            "type": "function",
            "name": name,
            "description": src.get("description") or "",
            "strict": False,
            "parameters": schema,
        })
    return out


class _WireCallIds:
    def __init__(self) -> None:
        self._seen: dict[str, int] = {}
        self._queue: dict[str, list[str]] = {}

    def for_call(self, call_id: str) -> str:
        n = self._seen.get(call_id, 0)
        self._seen[call_id] = n + 1
        wire = call_id if n == 0 else f"{call_id}_dup{n}"
        self._queue.setdefault(call_id, []).append(wire)
        return wire

    def for_output(self, call_id: str) -> str:
        queue = self._queue.get(call_id)
        return queue.pop(0) if queue else call_id


def _arguments_json(raw: Any) -> str:
    if isinstance(raw, str):
        return raw if raw.strip() else "{}"
    if isinstance(raw, dict):
        return json.dumps(raw, ensure_ascii=False)
    return "{}"


def _system_text(content: Any) -> str:
    if isinstance(content, list):
        return "\n\n".join(str(p.get("text", "")) for p in content if isinstance(p, dict) and p.get("type") == "text")
    return str(content or "")


def _input_image_part(part: dict[str, Any]) -> dict[str, Any] | None:
    url_val = part.get("image_url")
    url = url_val.get("url") if isinstance(url_val, dict) else url_val
    if not isinstance(url, str) or not url.strip():
        return None
    image_part: dict[str, Any] = {"type": "input_image", "image_url": url.strip()}
    detail = part.get("detail") or (url_val.get("detail") if isinstance(url_val, dict) else None)
    if isinstance(detail, str) and detail.strip():
        image_part["detail"] = detail.strip()
    return image_part


def _chat_content_to_responses_parts(content: Any, *, role: str) -> list[dict[str, Any]]:
    text_type = "output_text" if role == "assistant" else "input_text"
    converted: list[dict[str, Any]] = []
    if isinstance(content, list):
        for part in content:
            if isinstance(part, str):
                if part:
                    converted.append({"type": text_type, "text": part})
            elif isinstance(part, dict):
                ptype = part.get("type")
                if ptype in ("text", "output_text", "input_text") or ("text" in part and ptype != "image_url"):
                    text = part.get("text")
                    if isinstance(text, str) and text:
                        converted.append({"type": text_type, "text": text})
                elif ptype == "image_url" or "image_url" in part:
                    if role != "assistant":
                        img = _input_image_part(part)
                        if img is not None:
                            converted.append(img)
    elif content is not None:
        text = str(content)
        if text or role == "user":
            converted.append({"type": text_type, "text": text})
    return converted


def _reasoning_items(msg: dict[str, Any], *, model_leaf: str) -> list[dict[str, Any]]:
    details = msg.get("reasoning_details")
    if not isinstance(details, list):
        return []
    items: list[dict[str, Any]] = []
    for entry in details:
        if not isinstance(entry, dict) or entry.get("type") != "reasoning.encrypted":
            continue
        data = entry.get("data")
        if not isinstance(data, str) or not data:
            continue
        stamped = entry.get("model")
        if isinstance(stamped, str) and stamped.strip() and _openai_model_leaf(stamped) != model_leaf:
            log.warning("Dropping encrypted reasoning minted by %s for %s", stamped, model_leaf)
            continue
        # id is omitted on purpose. store:false makes the server 404 a replayed rs_* id.
        items.append({"type": "reasoning", "encrypted_content": data, "summary": []})
    return items


def responses_input(messages: list[dict[str, Any]], *, model_name: str | None) -> tuple[str, list[dict[str, Any]]]:
    """Return (instructions, input items).

    system and developer join into instructions with \\n\\n, in order.
    They are not copied into input.
    user -> message role user, content parts input_text / input_image.
    assistant -> reasoning items, then one output_text message if there is
    text, then function_call items. No item id on any of them.
    role tool -> function_call_output. String content stays a string.
    A list content becomes input_text / input_image parts (tool images).
    Any other role is skipped with log.warning.
    """
    instructions_parts: list[str] = []
    items: list[dict[str, Any]] = []
    wire_ids = _WireCallIds()
    dropped_tool_ids: set[str] = set()
    model_leaf = _openai_model_leaf(model_name)

    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        content = msg.get("content")

        if role in ("system", "developer"):
            text = _system_text(content)
            if text:
                instructions_parts.append(text)
            continue

        if role == "user":
            parts = _chat_content_to_responses_parts(content, role="user")
            items.append({"type": "message", "role": "user", "content": parts})
            continue

        if role == "assistant":
            r_items = _reasoning_items(msg, model_leaf=model_leaf)
            items.extend(r_items)

            text_parts = _chat_content_to_responses_parts(content, role="assistant")
            assistant_text = "".join(p.get("text", "") for p in text_parts if p.get("type") == "output_text")

            fc_items: list[dict[str, Any]] = []
            tool_calls = msg.get("tool_calls")
            if isinstance(tool_calls, list):
                for tc in tool_calls:
                    if not isinstance(tc, dict):
                        continue
                    call_id = tc.get("id")
                    fn_raw = tc.get("function")
                    fn: dict[str, Any] = fn_raw if isinstance(fn_raw, dict) else {}
                    name = fn.get("name")
                    if not isinstance(call_id, str) or not call_id.strip() or not isinstance(name, str) or not name.strip():
                        log.warning("Responses function_call missing id or name; dropping it")
                        if isinstance(call_id, str) and call_id.strip():
                            dropped_tool_ids.add(call_id.strip())
                        continue
                    wire_id = wire_ids.for_call(call_id.strip())
                    fc_items.append({
                        "type": "function_call",
                        "call_id": wire_id,
                        "name": name.strip(),
                        "arguments": _arguments_json(fn.get("arguments")),
                    })

            if assistant_text:
                items.append({
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": assistant_text}],
                })
            elif r_items and not fc_items:
                items.append({
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": " "}],
                })

            items.extend(fc_items)
            continue

        if role == "tool":
            raw_call_id = msg.get("tool_call_id")
            if not isinstance(raw_call_id, str) or not raw_call_id.strip():
                log.warning("Responses tool output missing tool_call_id; dropping it")
                continue
            call_id = raw_call_id.strip()
            if call_id in dropped_tool_ids:
                log.warning("Responses tool output matches dropped call id %s; dropping it", call_id)
                continue
            wire_id = wire_ids.for_output(call_id)
            if isinstance(content, list):
                output_val: Any = _chat_content_to_responses_parts(content, role="tool")
            else:
                output_val = str(content or "")
            items.append({
                "type": "function_call_output",
                "call_id": wire_id,
                "output": output_val,
            })
            continue

        log.warning("Responses input skipping unknown role: %s", role)

    instructions = "\n\n".join(instructions_parts)
    return instructions, items


def responses_payload(
    messages: list[dict[str, Any]],
    max_tokens: int,
    temperature: float | None,
    tools: list[dict[str, Any]] | None,
    stream: bool,
    model_name: str | None,
    response_format: dict[str, Any] | None,
    *,
    endpoint_effort: str,
    chat_extra: dict[str, Any] | None,
) -> dict[str, Any]:
    instructions, items = responses_input(messages, model_name=model_name)
    data: dict[str, Any] = {
        "input": items,
        "max_output_tokens": max_tokens,
        "stream": stream,
        "store": False,
        "include": ["reasoning.encrypted_content"],
    }
    if isinstance(model_name, str) and model_name.strip():
        data["model"] = model_name.strip()
    if instructions:
        data["instructions"] = instructions
    converted = _responses_tools(tools)
    if converted:
        data["tools"] = converted
        data["tool_choice"] = "auto"
        data["parallel_tool_calls"] = False  # chat path, base_provider_shim.py lines 212–215
    effort = responses_effort(endpoint_effort, chat_extra)
    # Caller may wrap this with the effort-PR mapper. Do not map here.
    if effort:
        data["reasoning"] = {"effort": effort, "summary": "auto"}
    if effort == "none" and temperature is not None:
        data["temperature"] = temperature
    text_format = _responses_text_format(response_format)
    if text_format is not None:
        data["text"] = {"format": text_format}
    return data


def _normalize_usage(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or not raw:
        return {}
    usage = dict(raw)
    if "prompt_tokens" not in usage and "input_tokens" in usage:
        usage["prompt_tokens"] = usage["input_tokens"]
    if "completion_tokens" not in usage and "output_tokens" in usage:
        usage["completion_tokens"] = usage["output_tokens"]
    return usage


def _event_text(chunk: dict[str, Any]) -> str:
    delta = chunk.get("delta")
    if isinstance(delta, str):
        return delta
    if isinstance(delta, dict):
        text = delta.get("text")
        if isinstance(text, str):
            return text
    part = chunk.get("part")
    if isinstance(part, dict) and isinstance(part.get("text"), str):
        return part["text"]
    return ""


def responses_failed_message(chunk: dict[str, Any]) -> str:
    response = chunk.get("response")
    err = response.get("error") if isinstance(response, dict) else None
    if isinstance(err, dict):
        message = err.get("message") or err.get("code") or err.get("type")
        if message:
            return str(message)
    if isinstance(err, str) and err.strip():
        return err.strip()
    top_err = chunk.get("error")
    if isinstance(top_err, dict):
        message = top_err.get("message") or top_err.get("code") or top_err.get("type")
        if message:
            return str(message)
    if isinstance(top_err, str) and top_err.strip():
        return top_err.strip()
    return "response.failed"


def parse_responses_chunk(
    chunk: dict[str, Any],
    stream_state: dict[str, Any] | None,
    *,
    model_name: str | None,
) -> tuple[str, str | None, str | None, dict[str, Any]]:
    if stream_state is None:
        stream_state = {}

    tool_indexes: dict[int, int] = stream_state.setdefault("_responses_tool_indexes", {})
    args_deltas: set[int] = stream_state.setdefault("_responses_args_deltas", set())

    resp_raw = chunk.get("response")
    resp: dict[str, Any] = resp_raw if isinstance(resp_raw, dict) else {}
    raw_model = chunk.get("model") or resp.get("model") or stream_state.get("_responses_model") or model_name
    if raw_model and not stream_state.get("_responses_model"):
        stream_state["_responses_model"] = _openai_model_leaf(str(raw_model))
    model_leaf = stream_state.get("_responses_model") or (_openai_model_leaf(str(raw_model)) if raw_model else "")

    event_type = chunk.get("type")
    if not isinstance(event_type, str):
        return "", None, None, {}

    if event_type in ("response.output_text.delta", "response.content_part.delta", "response.refusal.delta"):
        text = _event_text(chunk)
        text_delta: dict[str, Any] = {"content": text} if text else {}
        return text, None, None, text_delta

    if event_type in ("response.reasoning_text.delta", "response.reasoning_summary_text.delta", "response.reasoning.delta"):
        thinking = _event_text(chunk)
        thinking_delta: dict[str, Any] = {"thinking": thinking} if thinking else {}
        return "", None, thinking, thinking_delta

    if event_type == "response.output_item.added":
        item_raw = chunk.get("item")
        item: dict[str, Any] = item_raw if isinstance(item_raw, dict) else {}
        if item.get("type") == "function_call":
            output_index = chunk.get("output_index")
            if isinstance(output_index, int):
                if output_index not in tool_indexes:
                    tool_indexes[output_index] = len(tool_indexes)
                call_idx = tool_indexes[output_index]
                stream_state["_responses_saw_call"] = True
                call_id = item.get("call_id") or item.get("id") or ""
                name = item.get("name") or ""
                add_delta: dict[str, Any] = {
                    "tool_calls": [
                        {
                            "index": call_idx,
                            "id": call_id,
                            "type": "function",
                            "function": {"name": name, "arguments": ""},
                        }
                    ]
                }
                return "", None, None, add_delta
        return "", None, None, {}

    if event_type == "response.function_call_arguments.delta":
        output_index = chunk.get("output_index")
        if isinstance(output_index, int):
            if output_index not in tool_indexes:
                tool_indexes[output_index] = len(tool_indexes)
            call_idx = tool_indexes[output_index]
            args_deltas.add(output_index)
            stream_state["_responses_saw_call"] = True
            arg_delta = chunk.get("delta")
            arg_str = arg_delta if isinstance(arg_delta, str) else (arg_delta.get("arguments", "") if isinstance(arg_delta, dict) else "")
            arg_delta_dict: dict[str, Any] = {
                "tool_calls": [
                    {
                        "index": call_idx,
                        "function": {"arguments": arg_str},
                    }
                ]
            }
            return "", None, None, arg_delta_dict
        return "", None, None, {}

    if event_type == "response.function_call_arguments.done":
        output_index = chunk.get("output_index")
        if isinstance(output_index, int):
            if output_index in args_deltas:
                return "", None, None, {}
            if output_index not in tool_indexes:
                tool_indexes[output_index] = len(tool_indexes)
            call_idx = tool_indexes[output_index]
            stream_state["_responses_saw_call"] = True
            raw_args = chunk.get("arguments")
            arg_str = str(raw_args) if raw_args is not None else ""
            done_delta: dict[str, Any] = {
                "tool_calls": [
                    {
                        "index": call_idx,
                        "function": {"arguments": arg_str},
                    }
                ]
            }
            return "", None, None, done_delta
        return "", None, None, {}

    if event_type == "response.output_item.done":
        item_raw = chunk.get("item")
        item = item_raw if isinstance(item_raw, dict) else {}
        if item.get("type") == "reasoning":
            enc = item.get("encrypted_content")
            if isinstance(enc, str) and enc:
                n = stream_state.get("_responses_reasoning_index", 0)
                stream_state["_responses_reasoning_index"] = n + 1
                enc_delta: dict[str, Any] = {
                    "reasoning_details": [
                        {
                            "type": "reasoning.encrypted",
                            "data": enc,
                            "format": "openai-responses-v1",
                            "index": n,
                            "model": model_leaf,
                        }
                    ]
                }
                return "", None, None, enc_delta
        return "", None, None, {}

    if event_type in ("response.completed", "response.done"):
        finish_reason = "tool_calls" if stream_state.get("_responses_saw_call") else "stop"
        delta_ret: dict[str, Any] = {}
        resp_usage = resp.get("usage")
        chunk_usage = chunk.get("usage")
        usage_obj = resp_usage if isinstance(resp_usage, dict) else chunk_usage
        if isinstance(usage_obj, dict):
            delta_ret["usage"] = _normalize_usage(usage_obj)
        resp_m = resp.get("model")
        chunk_m = chunk.get("model")
        m = resp_m if isinstance(resp_m, str) else chunk_m
        if isinstance(m, str) and m:
            delta_ret["model"] = m
        return "", finish_reason, None, delta_ret

    if event_type == "response.incomplete":
        details_val = resp.get("incomplete_details") or chunk.get("incomplete_details")
        details: dict[str, Any] = details_val if isinstance(details_val, dict) else {}
        raw_reason = details.get("reason")
        if raw_reason == "max_output_tokens":
            finish_reason = "length"
        elif raw_reason == "content_filter":
            finish_reason = "content_filter"
        elif isinstance(raw_reason, str) and raw_reason.strip():
            finish_reason = raw_reason.strip()
        else:
            finish_reason = "length"
        delta_ret = {}
        resp_usage = resp.get("usage")
        chunk_usage = chunk.get("usage")
        usage_obj = resp_usage if isinstance(resp_usage, dict) else chunk_usage
        if isinstance(usage_obj, dict):
            delta_ret["usage"] = _normalize_usage(usage_obj)
        resp_m = resp.get("model")
        chunk_m = chunk.get("model")
        m = resp_m if isinstance(resp_m, str) else chunk_m
        if isinstance(m, str) and m:
            delta_ret["model"] = m
        return "", finish_reason, None, delta_ret

    if event_type == "response.failed":
        raise NetworkError(responses_failed_message(chunk), code="STREAM_ERROR")

    return "", None, None, {}


def is_responses_body(data: dict[str, Any]) -> bool:
    if not isinstance(data, dict):
        return False
    event_type = data.get("type")
    if isinstance(event_type, str) and event_type.startswith("response."):
        return False
    if data.get("object") == "response":
        return True
    output = data.get("output")
    if not isinstance(output, list) or "choices" in data:
        return False
    return any(
        isinstance(item, dict) and item.get("type") in {"message", "function_call", "reasoning"}
        for item in output
    )


def parse_responses_sync(
    response_data: dict[str, Any],
) -> tuple[str, str | None, list[dict[str, Any]] | None, dict[str, Any], list[str], dict[str, Any]]:
    status = response_data.get("status")
    if status == "failed" or "error" in response_data or (isinstance(response_data.get("response"), dict) and "error" in response_data["response"]):
        raise NetworkError(responses_failed_message(response_data), code="STREAM_ERROR")

    output = response_data.get("output")
    content_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    summary_parts: list[str] = []
    details: list[dict[str, Any]] = []
    model_leaf = _openai_model_leaf(str(response_data.get("model") or ""))

    if isinstance(output, list):
        for item in output:
            if not isinstance(item, dict):
                continue
            itype = item.get("type")
            if itype == "message":
                c = item.get("content")
                if isinstance(c, list):
                    for part in c:
                        if isinstance(part, dict) and part.get("type") in ("output_text", "refusal"):
                            txt = part.get("text") or part.get("refusal")
                            if isinstance(txt, str):
                                content_parts.append(txt)
                elif isinstance(c, str):
                    content_parts.append(c)
            elif itype == "function_call":
                call_id = item.get("call_id") or item.get("id")
                name = item.get("name")
                if not isinstance(call_id, str) or not call_id or not isinstance(name, str) or not name:
                    continue
                args = item.get("arguments")
                args_str = _arguments_json(args)
                tool_calls.append({
                    "id": call_id,
                    "type": "function",
                    "function": {"name": name, "arguments": args_str},
                })
            elif itype == "reasoning":
                summary = item.get("summary")
                if isinstance(summary, list):
                    for s in summary:
                        if isinstance(s, dict) and isinstance(s.get("text"), str):
                            summary_parts.append(s["text"])
                enc = item.get("encrypted_content")
                if isinstance(enc, str) and enc:
                    idx = 1 if summary_parts else 0
                    if summary_parts:
                        details.append({"type": "reasoning.text", "text": "\n\n".join(summary_parts), "index": 0})
                    details.append({
                        "type": "reasoning.encrypted",
                        "data": enc,
                        "format": "openai-responses-v1",
                        "index": idx,
                        "model": model_leaf,
                    })

    content = "".join(content_parts)
    tool_calls_ret = tool_calls if tool_calls else None
    usage = _normalize_usage(response_data.get("usage"))
    images: list[str] = []

    message: dict[str, Any] = {"role": "assistant", "content": content}
    summary_text = "\n\n".join(summary_parts)
    if details:
        message["reasoning_details"] = details
    elif summary_text:
        message["reasoning"] = summary_text

    if status == "incomplete":
        details_obj = response_data.get("incomplete_details")
        raw_reason = details_obj.get("reason") if isinstance(details_obj, dict) else None
        if raw_reason == "max_output_tokens":
            finish_reason = "length"
        elif raw_reason == "content_filter":
            finish_reason = "content_filter"
        elif isinstance(raw_reason, str) and raw_reason.strip():
            finish_reason = raw_reason.strip()
        else:
            finish_reason = "length"
    elif tool_calls_ret:
        finish_reason = "tool_calls"
    else:
        finish_reason = "stop"

    return content, finish_reason, tool_calls_ret, usage, images, message

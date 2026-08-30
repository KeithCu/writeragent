#!/usr/bin/env python3
# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""OpenAI-compatible mock LLM for sidebar soak tests (HTML chat + tool loops).

Default bind is 127.0.0.1:18766 — not 8765 (historical MCP) or 18765 (current MCP).

Usage (repo root):
  .venv/bin/python scripts/mock_llm_server.py
  make mock-llm
  .venv/bin/python scripts/mock_llm_server.py --delay-ms 40 --scenario ramble

Point WriterAgent Settings at http://127.0.0.1:18766 and model writeragent-mock.

Phrase-triggered journeys (see docs/chat/rich-text-control-sidebar.md) soak Stop,
empty replies, reasoning fields, delegate, parallel tools, HTTP errors, and scroll.

Native audio: chat completions with ``input_audio`` (sidebar Record). STT soak:
POST /v1/audio/transcriptions and model id writeragent-mock-whisper.
"""

from __future__ import annotations

import argparse
import base64
import html
import io
import json
import re
import socket
import sys
import threading
import time
import uuid
import wave
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Iterator
from urllib.parse import urlparse

DEFAULT_HOST = "127.0.0.1"
# Not 8765 (historical MCP) or 18765 (current MCP).
DEFAULT_PORT = 18766
MOCK_MODEL_ID = "writeragent-mock"
# Name includes "whisper" so local /v1/models audio heuristics list it for STT.
MOCK_STT_MODEL_ID = "writeragent-mock-whisper"
DEFAULT_TRANSCRIPT = "Hello from the mock microphone."
_STT_PROMPT_NEEDLE = "transcribe this audio exactly"

RAMBLE_PARTS = 200
FLOOD_PARAS = 40
DEFAULT_CHUNK_CHARS = 24
DEFAULT_FAIL_AFTER_CHUNKS = 4

_COMMENT_RE = re.compile(r"\bcomments?\b", re.IGNORECASE)
_RESEARCH_RE = re.compile(
    r"\b(research|search|look up|look-up|latest|news|who is|what is)\b",
    re.IGNORECASE,
)
_HTTP_URL_RE = re.compile(r"https?://[^\s)>\"]+", re.IGNORECASE)
# Smolagents records prior steps as Action JSON in user/assistant *content*,
# not OpenAI assistant.tool_calls. Require a line starting with Action: so the
# system-prompt example ("Example Action:") is ignored even if a later blob
# copies it. Packet E live soak: without this, every nested round re-issues
# web_search (or skips get_document_tree).
_ACTION_NAME_RE = re.compile(r'(?m)^Action:\s*\{\s*"name"\s*:\s*"([^"]+)"')
# Inner document_research often has no get_document_tree. Call *one* discovery
# tool first so Packet E7 shows nested status and E8 has time to click Stop.
# Do not walk every discovery tool — delegate_read_document with an empty path
# opens junk files and never reaches specialized_workflow_finished.
_SPECIALIZED_INNER_PRE_FINISH = (
    "get_document_tree",
    "list_nearby_files",
    "search_nearby_files",
    "grep_nearby_files",
)

# Phrase → scenario. First match wins. Keep distinct from research/comment keywords.
_SCENARIO_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("hang", re.compile(r"\bhang the stream\b", re.IGNORECASE)),
    ("fail_http", re.compile(r"\b(crash the stream|error\s*500)\b", re.IGNORECASE)),
    ("rate_limit", re.compile(r"\b(rate limit|error\s*429)\b", re.IGNORECASE)),
    ("auth_401", re.compile(r"\b(error\s*401|unauthorized)\b", re.IGNORECASE)),
    ("auth_403", re.compile(r"\b(error\s*403|forbidden)\b", re.IGNORECASE)),
    ("connection_reset", re.compile(r"\bconnection reset\b", re.IGNORECASE)),
    ("empty_body", re.compile(r"\bempty body\b", re.IGNORECASE)),
    ("malformed_sse", re.compile(r"\bmalformed sse\b", re.IGNORECASE)),
    ("truncated_json", re.compile(r"\btruncated json\b", re.IGNORECASE)),
    ("two_dones", re.compile(r"\btwo dones\b", re.IGNORECASE)),
    ("event_ping", re.compile(r"\bevent ping\b", re.IGNORECASE)),
    ("ramble", re.compile(r"\b(keep talking|ramble|stop me)\b", re.IGNORECASE)),
    # Packet C5: empty content + finish_reason=content_filter. Before empty/empty_stop
    # so "content filter" is not length or Debug.
    ("content_filter", re.compile(r"\b(content filter|filtered reply)\b", re.IGNORECASE)),
    # Packet C4: empty content + finish_reason=stop (Debug banner). Must win
    # before ``empty`` so "empty finish stop" is not ``say nothing`` / length.
    ("empty_stop", re.compile(r"\b(empty finish stop|blank stop reason)\b", re.IGNORECASE)),
    ("empty", re.compile(r"\b(say nothing|empty reply)\b", re.IGNORECASE)),
    ("think_details", re.compile(r"\breasoning details\b", re.IGNORECASE)),
    ("think_content", re.compile(r"\bthink tags\b", re.IGNORECASE)),
    ("think", re.compile(r"\b(think out loud|show thinking)\b", re.IGNORECASE)),
    ("flood", re.compile(r"\b(fill the sidebar|very long)\b", re.IGNORECASE)),
    ("table", re.compile(r"\b(show a table|send a table|table please)\b", re.IGNORECASE)),
    ("delegate", re.compile(r"\b(outline this|use the writer toolset)\b", re.IGNORECASE)),
    ("parallel", re.compile(r"\b(two tools|in parallel)\b", re.IGNORECASE)),
    ("mutate", re.compile(r"\b(insert filler|append a paragraph)\b", re.IGNORECASE)),
    ("ping", re.compile(r"\bsse pings\b", re.IGNORECASE)),
    ("list_sheets", re.compile(r"\blist sheets\b", re.IGNORECASE)),
    ("list_pages", re.compile(r"\blist pages\b", re.IGNORECASE)),
)

# Packet F HTTP/SSE faults that apply on the user turn (not tool follow-ups).
_FAULT_SCENARIOS = frozenset(
    {
        "fail_http",
        "rate_limit",
        "hang",
        "auth_401",
        "auth_403",
        "connection_reset",
        "empty_body",
        "malformed_sse",
        "truncated_json",
        "two_dones",
        "event_ping",
    }
)

SCENARIO_IDS = frozenset(
    {
        "none",
        "ramble",
        "empty",
        "empty_stop",
        "think",
        "think_content",
        "think_details",
        "flood",
        "delegate",
        "tree",
        "parallel",
        "mutate",
        "fail_http",
        "rate_limit",
        "hang",
        "auth_401",
        "auth_403",
        "connection_reset",
        "empty_body",
        "malformed_sse",
        "truncated_json",
        "two_dones",
        "event_ping",
        "ping",
        "list_sheets",
        "list_pages",
    }
)
FAIL_MODES = ("none", "http500", "http429", "hang")

_DELEGATE_WRITER = "delegate_to_specialized_writer_toolset"


@dataclass
class MockLLMConfig:
    delay_ms: int = 25
    # None means use delay_ms. Packet E8: keep SSE snappy, stretch nested stream=False POSTs.
    sync_delay_ms: int | None = None
    offline: bool = False
    always_research: bool = False
    scenario: str = "none"
    chunk_chars: int = DEFAULT_CHUNK_CHARS
    fail: str = "none"
    fail_after_chunks: int = DEFAULT_FAIL_AFTER_CHUNKS
    sse_comments: bool = False
    transcript: str = DEFAULT_TRANSCRIPT
    # Packet E10: HTTP 500 on chat POSTs whose last message is role=tool (mid-loop).
    fail_tool_followup: bool = False
    # Packet E oracles (E1 CURRENT QUERY, E5 doc length, E6/E7 tool names).
    captures: list[dict[str, Any]] = field(default_factory=list)
    _capture_lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)


def response_delay_s(config: MockLLMConfig, *, stream: bool) -> float:
    """Seconds to sleep between SSE chunks, or once before a non-streaming JSON body."""
    if stream:
        ms = config.delay_ms
    else:
        ms = config.delay_ms if config.sync_delay_ms is None else config.sync_delay_ms
    return max(0, int(ms)) / 1000.0


@dataclass
class Completion:
    content: str | None = None
    reasoning: str | None = None
    tool_name: str | None = None
    tool_args: dict[str, Any] | None = None
    # Extra parallel calls after tool_name/tool_args (name, args).
    extra_tool_calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    finish_reason: str = "stop"
    ramble_parts: int = 0
    reasoning_mode: str = "reasoning"  # reasoning | reasoning_content | details | think_tags
    http_error: int | None = None
    hang: bool = False
    sse_comments: bool = False
    # Packet F stream quirks (not HTTP status): handled in do_POST.
    # event_ping | two_dones | malformed | truncated | empty_body | connection_reset
    sse_quirk: str | None = None


def completion_tool_calls(completion: Completion) -> list[tuple[str, dict[str, Any]]]:
    calls: list[tuple[str, dict[str, Any]]] = []
    if completion.tool_name:
        calls.append((completion.tool_name, completion.tool_args or {}))
    calls.extend(completion.extra_tool_calls)
    return calls


_CURRENT_QUERY_MARK = "### CURRENT QUERY:"


def document_content_len(messages: list[Any]) -> int:
    """Characters between ``[DOCUMENT CONTENT]`` and ``[END DOCUMENT]`` (0 if absent)."""
    for msg in messages:
        if not isinstance(msg, dict) or msg.get("role") != "system":
            continue
        text = _as_text(msg.get("content"))
        start = text.find("[DOCUMENT CONTENT]")
        if start < 0:
            continue
        start += len("[DOCUMENT CONTENT]")
        end = text.find("[END DOCUMENT]", start)
        blob = text[start:end] if end >= 0 else text[start:]
        return len(blob.strip())
    return 0


def summarize_chat_payload(
    payload: dict[str, Any],
    completion: Completion | None = None,
    config: MockLLMConfig | None = None,
) -> dict[str, Any]:
    """Compact request snapshot for Packet E tests (thread-safe append via record_capture)."""
    messages = payload.get("messages") if isinstance(payload.get("messages"), list) else []
    tools = _tool_names(payload.get("tools"))
    user_text = _last_user_text(messages)
    query = current_query_text(user_text)
    last_user = _last_user_message(messages)
    decided: list[str] = []
    if completion is not None:
        decided = [name for name, _args in completion_tool_calls(completion)]
    rec: dict[str, Any] = {
        "stream": bool(payload.get("stream")),
        "last_role": _last_role(messages),
        "user_text": user_text,
        "current_query": query,
        "has_current_query_mark": _CURRENT_QUERY_MARK in user_text
        or any(
            isinstance(m, dict)
            and m.get("role") != "system"
            and _CURRENT_QUERY_MARK in _as_text(m.get("content"))
            for m in messages
        ),
        "advertised_tools": sorted(tools),
        "called_tools": _called_tool_names(messages),
        "last_assistant_tool_calls": _last_assistant_tool_names(messages),
        "decided_tools": decided,
        "doc_content_len": document_content_len(messages),
        "has_input_audio": bool(last_user is not None and _content_has_audio(last_user.get("content"))),
        "path": "/v1/chat/completions",
    }
    if config is not None:
        rec["forced_scenario"] = config.scenario
    if completion is not None:
        rec["finish_reason"] = completion.finish_reason
        rec["empty_content"] = completion.content is None
    return rec


def record_capture(config: MockLLMConfig, rec: dict[str, Any]) -> None:
    with config._capture_lock:
        config.captures.append(rec)


def snapshot_captures(config: MockLLMConfig) -> list[dict[str, Any]]:
    with config._capture_lock:
        return list(config.captures)


def clear_captures(config: MockLLMConfig) -> None:
    with config._capture_lock:
        config.captures.clear()


def current_query_text(user_text: str) -> str:
    """Phrase-match the current user turn, not librarian/smol conversation history.

    Sub-agents wrap the task as ``### CONVERSATION HISTORY:`` plus
    ``### CURRENT QUERY:``. Matching the whole blob would keep firing
    ``crash the stream`` / ``hang the stream`` on a later ``hello``.
    """
    text = user_text or ""
    idx = text.rfind(_CURRENT_QUERY_MARK)
    if idx >= 0:
        return text[idx + len(_CURRENT_QUERY_MARK) :].strip()
    return text.strip()


def detect_scenario(user_text: str, forced: str = "none") -> str:
    forced = (forced or "none").strip().lower()
    if forced and forced != "none":
        return forced
    text = current_query_text(user_text)
    for sid, pattern in _SCENARIO_PATTERNS:
        if pattern.search(text):
            return sid
    return ""


def _as_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text" or "text" in item:
                    parts.append(str(item.get("text") or ""))
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(parts)
    return str(content)


def canned_transcript(config: MockLLMConfig) -> str:
    text = (config.transcript or DEFAULT_TRANSCRIPT).strip()
    return text or DEFAULT_TRANSCRIPT


def _looks_like_stt_prompt(text: str) -> bool:
    return _STT_PROMPT_NEEDLE in (text or "").lower()


def _content_has_audio(content: Any) -> bool:
    if isinstance(content, dict):
        if content.get("type") == "input_audio" or isinstance(content.get("input_audio"), dict):
            return True
        return False
    if isinstance(content, list):
        return any(_content_has_audio(item) for item in content)
    return False


def _last_user_message(messages: list[Any]) -> dict[str, Any] | None:
    for msg in reversed(messages):
        if isinstance(msg, dict) and msg.get("role") == "user":
            return msg
    return None


def _first_audio_b64(content: Any) -> str:
    if isinstance(content, dict):
        ia = content.get("input_audio")
        if isinstance(ia, dict) and ia.get("data"):
            return str(ia.get("data") or "")
        if content.get("type") == "input_audio" and content.get("data"):
            return str(content.get("data") or "")
        return ""
    if isinstance(content, list):
        for item in content:
            got = _first_audio_b64(item)
            if got:
                return got
    return ""


def audio_duration_s(b64_or_wav: str | bytes) -> float | None:
    """Duration of a WAV from base64 or raw bytes. None if not a valid WAV."""
    try:
        raw = b64_or_wav if isinstance(b64_or_wav, bytes) else base64.b64decode(b64_or_wav)
        with wave.open(io.BytesIO(raw), "rb") as wf:
            rate = wf.getframerate()
            if not rate:
                return None
            return wf.getnframes() / float(rate)
    except Exception:
        return None


def _html_audio_reply(user_text: str, transcript: str, duration_s: float | None) -> str:
    safe_t = html.escape(transcript)
    dur = f" (~{duration_s:.1f}s)" if duration_s is not None else ""
    extra = ""
    typed = (user_text or "").strip()
    if typed and not _looks_like_stt_prompt(typed):
        extra = f"<p>You also typed: {html.escape(typed[:120])}.</p>"
    return (
        f"<p>I heard the recording{dur}. Mock transcript: <strong>{safe_t}</strong>.</p>"
        f"{extra}"
        "<p>This is native <code>input_audio</code> on the mock chat model — not a real STT engine.</p>"
    )


def _tool_names(tools: Any) -> set[str]:
    names: set[str] = set()
    if not isinstance(tools, list):
        return names
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        fn = tool.get("function")
        if isinstance(fn, dict) and fn.get("name"):
            names.add(str(fn["name"]))
        elif tool.get("name"):
            names.add(str(tool["name"]))
    return names


def _last_user_text(messages: list[Any]) -> str:
    for msg in reversed(messages):
        if isinstance(msg, dict) and msg.get("role") == "user":
            return current_query_text(_as_text(msg.get("content")))
    return ""


def _last_role(messages: list[Any]) -> str:
    for msg in reversed(messages):
        if isinstance(msg, dict) and msg.get("role"):
            return str(msg["role"])
    return ""


def _tool_names_from_message(msg: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for tc in msg.get("tool_calls") or []:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") or {}
        if isinstance(fn, dict) and fn.get("name"):
            names.append(str(fn["name"]))
    return names


def _assistant_tool_names(messages: list[Any]) -> list[str]:
    names: list[str] = []
    for msg in messages:
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        names.extend(_tool_names_from_message(msg))
    return names


def _last_assistant_tool_names(messages: list[Any]) -> list[str]:
    """tool_calls on the most recent assistant message (Packet D4)."""
    for msg in reversed(messages):
        if isinstance(msg, dict) and msg.get("role") == "assistant":
            return _tool_names_from_message(msg)
    return []


def _action_tool_names(messages: list[Any]) -> list[str]:
    """Tool names from smolagents Action JSON in non-system message content."""
    names: list[str] = []
    for msg in messages:
        if not isinstance(msg, dict) or msg.get("role") == "system":
            continue
        text = _as_text(msg.get("content"))
        names.extend(_ACTION_NAME_RE.findall(text))
    return names


def _called_tool_names(messages: list[Any]) -> list[str]:
    """Native tool_calls plus smolagents Action-in-content history."""
    return _assistant_tool_names(messages) + _action_tool_names(messages)


def _current_query(messages: list[Any], fallback: str = "") -> str:
    """``### CURRENT QUERY:`` from non-system messages; last marker wins.

    Smol later turns are often only ``Step budget:`` plus Action/Observation,
    so `_last_user_text` is the banner. Scan earlier messages and reuse
    `current_query_text` (rfind) so a double marker keeps the last suffix.
    """
    for msg in reversed(messages):
        if not isinstance(msg, dict) or msg.get("role") == "system":
            continue
        text = _as_text(msg.get("content"))
        if _CURRENT_QUERY_MARK in text:
            return current_query_text(text)
    return current_query_text(fallback)


def _is_smol_research(tool_names: set[str]) -> bool:
    # final_answer alone is also used by specialized inner agents; require search tools.
    return bool(tool_names & {"web_search", "visit_webpage"})


def _is_specialized_inner(tool_names: set[str]) -> bool:
    # Live document_research inner HTTP advertises specialized_workflow_finished
    # plus domain tools; get_document_tree is often *not* on that list (core
    # tree is not a specialized_domain tool). Phrase "outline this" must not
    # fall through to the main-chat delegate scenario (Packet E7).
    if "specialized_workflow_finished" in tool_names:
        return True
    return "get_document_tree" in tool_names and bool(
        tool_names & {"final_answer", "specialized_workflow_finished"}
    )


def _is_main_chat(tool_names: set[str]) -> bool:
    return bool(
        tool_names
        & {
            "web_research",
            "add_comment",
            "apply_document_content",
            "search_in_document",
            "get_document_tree",
            _DELEGATE_WRITER,
            "list_sheets",
            "list_pages",
        }
    )


def _looks_like_research(text: str) -> bool:
    return bool(_RESEARCH_RE.search(text or ""))


def _looks_like_comment(text: str) -> bool:
    return bool(_COMMENT_RE.search(text or ""))


def _extract_document_words(messages: list[Any]) -> list[str]:
    """Extract plain words from the [DOCUMENT CONTENT] block in conversation messages."""
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        content = _as_text(msg.get("content"))
        if "[DOCUMENT CONTENT]" in content:
            doc_section = content.split("[DOCUMENT CONTENT]", 1)[1]
            if "[END DOCUMENT]" in doc_section:
                doc_section = doc_section.split("[END DOCUMENT]", 1)[0]
            cleaned = re.sub(r"\[DOCUMENT (?:START|END)\]", " ", doc_section)
            cleaned = re.sub(r"\[\.\.\..*?\.\.\.\]", " ", cleaned)
            cleaned = re.sub(r"\[Document (?:content unavailable|text reading failed[^\]]*)\]", " ", cleaned)
            cleaned = re.sub(r"^Document length:\s*\d+\s*characters\.", " ", cleaned, flags=re.MULTILINE)
            words = re.findall(r"\b[A-Za-z0-9_-]+\b", cleaned)
            return words
    return []


def _first_url(text: str) -> str:
    match = _HTTP_URL_RE.search(text or "")
    if match:
        return match.group(0).rstrip(".,;\"'")
    return "https://example.com/mock-research"


def _plain_research_report(query: str) -> str:
    topic = (query or "the topic").strip()[:200]
    return (
        f"Findings for {topic}\n"
        "\n"
        "Summary\n"
        f"- Mock research report for: {topic}\n"
        "- Sources are canned (this is not a live model).\n"
        "\n"
        "Notes\n"
        "- Use this endpoint to soak-test sidebar scrolling and the web-research tool loop.\n"
        "- No HTML in this sub-agent answer; the main chat formats HTML after the tool returns."
    )


_HTML_TEMPLATES = (
    (
        "<p>Here is a <strong>mock</strong> take on {topic}. The first paragraph is padding so the "
        "sidebar has something to stream and then re-render as rich text.</p>"
        "<p>Second paragraph: keep sending messages to fill the transcript. This endpoint is "
        "<em>not</em> a real model; it only exists so scrolling and HTML paste can be tested.</p>"
    ),
    (
        "<p>Chatting about {topic}. Below is a short list so lists render in the rich control.</p>"
        "<ul><li>Streamed as plain text first</li><li>Then HTML is pasted after STREAM_DONE</li>"
        "<li>Caret follow is the scroll path</li></ul>"
        "<p>Second paragraph continues so you get two blocks of body text every turn.</p>"
    ),
    (
        "<h2>Mock notes</h2>"
        "<p>Topic: {topic}. Numbered steps exercise ordered lists in the narrow sidebar.</p>"
        "<ol><li>Send a message</li><li>Watch the stream</li><li>Confirm formatted rerender</li></ol>"
        "<p>Second paragraph is filler for scroll height. Repeat until the control is long.</p>"
    ),
    (
        "<p>A tiny table about {topic} — check that cells survive the hidden-Writer paste.</p>"
        "<table><tr><th>Col A</th><th>Col B</th></tr><tr><td>stream</td><td>plain</td></tr>"
        "<tr><td>done</td><td>HTML</td></tr></table>"
        "<p>Second paragraph after the table so the message is still two blocks tall.</p>"
    ),
    (
        "<p>Code-shaped reply for {topic}. The pre block should stay monospaced after rerender.</p>"
        "<pre><code>print('mock-llm')\n# two lines on purpose</code></pre>"
        "<p>Second paragraph: more chat text so history and scroll still have weight.</p>"
    ),
)


def _html_chat(topic: str, turn: int) -> str:
    safe = html.escape((topic or "your message").strip()[:120] or "your message")
    template = _HTML_TEMPLATES[turn % len(_HTML_TEMPLATES)]
    return template.format(topic=safe)


def _html_table(topic: str) -> str:
    """Deterministic HTML table for sidebar header-row QA (not the rotating hello templates)."""
    return (
        "<p>Produce inventory:</p>"
        "<table>"
        "<tr><th>Item</th><th>Description</th><th>Quantity</th></tr>"
        "<tr><td>Apples</td><td>Fresh red apples</td><td>12</td></tr>"
        "<tr><td>Bananas</td><td>Ripe yellow bananas</td><td>8</td></tr>"
        "<tr><td>Oranges</td><td>Juicy orange citrus</td><td>15</td></tr>"
        "</table>"
    )


def _html_research_summary(topic: str, tool_text: str) -> str:
    safe = html.escape((topic or "that query").strip()[:120] or "that query")
    snippet = html.escape((tool_text or "").strip().replace("\n", " ")[:280])
    return (
        f"<p>I looked that up. Mock summary for <strong>{safe}</strong>.</p>"
        f"<p>Research sub-agent returned: {snippet or '(empty)'}</p>"
        "<ul><li>example.com/mock-research</li><li>Canned source two</li></ul>"
    )


def _html_tool_wrapup(user_text: str, tool_name: str, tool_text: str) -> str:
    """Main-chat HTML after a tool round. Research wording is only for web_research."""
    if tool_name == "web_research":
        return _html_research_summary(user_text, tool_text)
    snippet = html.escape((tool_text or "").strip().replace("\n", " ")[:280])
    safe_user = html.escape((user_text or "that request").strip()[:80] or "that request")
    if tool_name == "apply_document_content":
        return f"<p>Inserted content into the document.</p><p>{snippet or '(empty)'}</p>"
    if tool_name.startswith("delegate_to_specialized"):
        return (
            f"<p>Specialized agent finished <strong>{safe_user}</strong>.</p>"
            f"<p>{snippet or '(empty)'}</p>"
        )
    if tool_name in {"search_in_document", "get_document_tree", "list_sheets", "list_pages"}:
        return (
            f"<p>Ran <code>{html.escape(tool_name)}</code>.</p>"
            f"<p>{snippet or '(empty)'}</p>"
        )
    safe_tool = html.escape(tool_name or "tool")
    return (
        f"<p>Finished <code>{safe_tool}</code> for <strong>{safe_user}</strong>.</p>"
        f"<p>{snippet or '(empty)'}</p>"
    )


def _html_flood(topic: str) -> str:
    safe = html.escape((topic or "flood").strip()[:80] or "flood")
    paras = "".join(f"<p>Flood paragraph {i} about {safe}. Padding for VisArea and caret-follow.</p>" for i in range(1, FLOOD_PARAS + 1))
    table = (
        "<table>"
        + "".join(f"<tr><td>r{r}c1</td><td>r{r}c2</td><td>r{r}c3</td><td>wide-cell-{r}</td></tr>" for r in range(8))
        + "</table>"
    )
    nested = "<ul>" + "".join(f"<li>outer {i}<ul><li>inner {i}a</li><li>inner {i}b</li></ul></li>" for i in range(1, 6)) + "</ul>"
    return f"<h2>Flood: {safe}</h2>{paras}{table}{nested}"


def _ramble_text() -> str:
    return " ".join(f"word{i}" for i in range(RAMBLE_PARTS))


def _think_html(topic: str, turn: int) -> str:
    return _html_chat(topic, turn)


@dataclass
class _TurnState:
    n: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def next_turn(self) -> int:
        with self.lock:
            self.n += 1
            return self.n


def _tool_or_html(
    tool_names: set[str],
    name: str,
    args: dict[str, Any],
    topic: str,
    turn: int,
    extra: list[tuple[str, dict[str, Any]]] | None = None,
) -> Completion:
    if name not in tool_names:
        return Completion(content=_html_chat(topic, turn), reasoning="Mock thinking: tool not advertised, HTML fallback.")
    return Completion(
        reasoning="Mock thinking: pick HTML chat or call tool.",
        tool_name=name,
        tool_args=args,
        extra_tool_calls=extra or [],
        finish_reason="tool_calls",
    )


def _smol_research_completion(messages: list[Any], tool_names: set[str], config: MockLLMConfig, user_text: str) -> Completion:
    called = _called_tool_names(messages)
    query = _current_query(messages, user_text) or "mock research"
    if config.offline:
        return Completion(
            tool_name="final_answer",
            tool_args={"answer": _plain_research_report(query)},
            finish_reason="tool_calls",
        )
    if "visit_webpage" in called:
        return Completion(
            tool_name="final_answer",
            tool_args={"answer": _plain_research_report(query)},
            finish_reason="tool_calls",
        )
    if "web_search" in called:
        last_tool_text = ""
        for msg in reversed(messages):
            if not isinstance(msg, dict):
                continue
            blob = _as_text(msg.get("content"))
            if msg.get("role") == "tool" or "Observation:" in blob or "<h2>Search Results</h2>" in blob:
                last_tool_text = blob
                if _first_url(last_tool_text) != "https://example.com/mock-research":
                    break
        return Completion(
            tool_name="visit_webpage",
            tool_args={"url": _first_url(last_tool_text)},
            finish_reason="tool_calls",
        )
    return Completion(
        tool_name="web_search",
        tool_args={"query": query, "recency": "any"},
        finish_reason="tool_calls",
    )


def _specialized_inner_args(name: str) -> dict[str, Any]:
    if name == "get_document_tree":
        return {"strategy": "heading_only", "depth": 1}
    if name == "search_nearby_files":
        return {"query": "outline"}
    if name == "grep_nearby_files":
        return {"pattern": "outline"}
    return {}


def _specialized_inner_completion(messages: list[Any], tool_names: set[str]) -> Completion:
    called = _called_tool_names(messages)
    finish_name = "final_answer" if "final_answer" in tool_names else "specialized_workflow_finished"
    # One discovery step, then finish. Calling every advertised tool (especially
    # delegate_read_document with path="") loops the inner agent (Packet E7 soak).
    if any(name in called for name in _SPECIALIZED_INNER_PRE_FINISH):
        return Completion(
            tool_name=finish_name,
            tool_args={"answer": "Mock outline complete. Nested document_research tools finished."},
            finish_reason="tool_calls",
        )
    for name in _SPECIALIZED_INNER_PRE_FINISH:
        if name in tool_names:
            return Completion(
                tool_name=name,
                tool_args=_specialized_inner_args(name),
                finish_reason="tool_calls",
            )
    return Completion(
        tool_name=finish_name,
        tool_args={"answer": "Mock outline complete. Nested document_research tools finished."},
        finish_reason="tool_calls",
    )


def _scenario_user_turn(
    scenario: str,
    tool_names: set[str],
    user_text: str,
    messages: list[Any],
    turn: int,
    config: MockLLMConfig,
) -> Completion | None:
    reasoning = "Mock thinking: pick HTML chat or call tool."
    if scenario == "fail_http":
        return Completion(http_error=500, finish_reason="stop")
    if scenario == "rate_limit":
        return Completion(http_error=429, finish_reason="stop")
    if scenario == "auth_401":
        return Completion(http_error=401, finish_reason="stop")
    if scenario == "auth_403":
        return Completion(http_error=403, finish_reason="stop")
    if scenario == "connection_reset":
        return Completion(sse_quirk="connection_reset", finish_reason="stop")
    if scenario == "empty_body":
        return Completion(sse_quirk="empty_body", finish_reason="stop")
    if scenario == "malformed_sse":
        return Completion(
            content=_html_chat(user_text, turn),
            reasoning=reasoning,
            sse_quirk="malformed",
            finish_reason="stop",
        )
    if scenario == "truncated_json":
        return Completion(
            content=_html_chat(user_text, turn),
            reasoning=reasoning,
            sse_quirk="truncated",
            finish_reason="stop",
        )
    if scenario == "two_dones":
        return Completion(
            content=_html_chat(user_text, turn),
            reasoning=reasoning,
            sse_quirk="two_dones",
            finish_reason="stop",
        )
    if scenario == "event_ping":
        return Completion(
            content=_html_chat(user_text, turn),
            reasoning=reasoning,
            sse_quirk="event_ping",
            finish_reason="stop",
        )
    if scenario == "hang":
        return Completion(content=_ramble_text(), hang=True, ramble_parts=RAMBLE_PARTS, finish_reason="stop")
    if scenario == "ramble":
        return Completion(content=_ramble_text(), ramble_parts=RAMBLE_PARTS, reasoning=reasoning, finish_reason="stop")
    if scenario == "content_filter":
        # Packet C5: empty content with finish_reason=content_filter (not length/Debug).
        return Completion(content=None, finish_reason="content_filter")
    if scenario == "empty_stop":
        # Packet C4: empty content with finish_reason=stop paints the Debug banner.
        # ``empty`` / say nothing is finish_reason=length (truncated banner instead).
        return Completion(content=None, finish_reason="stop")
    if scenario == "empty":
        return Completion(content=None, finish_reason="length")
    if scenario == "flood":
        return Completion(content=_html_flood(user_text), reasoning=reasoning, finish_reason="stop")
    if scenario == "table":
        return Completion(content=_html_table(user_text), reasoning=reasoning, finish_reason="stop")
    if scenario == "think":
        return Completion(
            content=_think_html(user_text, turn),
            reasoning="Step one: notice the user. Step two: stream HTML. Step three: done.",
            reasoning_mode="reasoning",
            finish_reason="stop",
        )
    if scenario == "think_content":
        body = _think_html(user_text, turn)
        return Completion(
            content="<think>\nMock chain of thought for soak tests.\n</think>\n" + body,
            reasoning_mode="think_tags",
            finish_reason="stop",
        )
    if scenario == "think_details":
        return Completion(
            content=_think_html(user_text, turn),
            reasoning="Separated reasoning_content then reasoning_details then HTML.",
            reasoning_mode="details",
            finish_reason="stop",
        )
    if scenario == "ping":
        return Completion(
            content=_html_chat(user_text, turn),
            reasoning=reasoning,
            sse_comments=True,
            finish_reason="stop",
        )
    if scenario == "delegate":
        return _tool_or_html(
            tool_names,
            _DELEGATE_WRITER,
            {"domain": "document_research", "task": user_text or "outline the document"},
            user_text,
            turn,
        )
    if scenario == "parallel":
        have_search = "search_in_document" in tool_names
        have_tree = "get_document_tree" in tool_names
        if have_search and have_tree:
            words = _extract_document_words(messages)
            pattern = words[0] if words else "the"
            return Completion(
                reasoning=reasoning,
                tool_name="search_in_document",
                tool_args={"pattern": pattern},
                extra_tool_calls=[("get_document_tree", {"strategy": "heading_only", "depth": 1})],
                finish_reason="tool_calls",
            )
        return Completion(content=_html_chat(user_text, turn), reasoning=reasoning, finish_reason="stop")
    if scenario == "mutate":
        return _tool_or_html(
            tool_names,
            "apply_document_content",
            {"target": "end", "content": ["<p>Mock filler paragraph from soak server.</p>"]},
            user_text,
            turn,
        )
    if scenario == "list_sheets":
        return _tool_or_html(tool_names, "list_sheets", {}, user_text, turn)
    if scenario == "list_pages":
        return _tool_or_html(tool_names, "list_pages", {}, user_text, turn)
    if scenario == "tree":
        return _specialized_inner_completion(messages, tool_names) if _is_specialized_inner(tool_names) else Completion(
            content=_html_chat(user_text, turn), reasoning=reasoning
        )
    return None


def decide_completion(payload: dict[str, Any], config: MockLLMConfig, turns: _TurnState | None = None) -> Completion:
    """Scripted main-chat / smol-research / soak-scenario policy. No real model."""
    messages = payload.get("messages") if isinstance(payload.get("messages"), list) else []
    tool_names = _tool_names(payload.get("tools"))
    user_text = _last_user_text(messages)
    last_role = _last_role(messages)
    called = _assistant_tool_names(messages)
    scenario = detect_scenario(user_text, config.scenario)

    if _is_smol_research(tool_names):
        return _smol_research_completion(messages, tool_names, config, user_text)

    if _is_specialized_inner(tool_names):
        return _specialized_inner_completion(messages, tool_names)

    turn = turns.next_turn() if turns is not None else 1
    reasoning = "Mock thinking: pick HTML chat or call tool."

    # Soak HTTP/stream faults apply on the user turn (and any later POST that still
    # matches the last user phrase / --scenario).
    if scenario in _FAULT_SCENARIOS and last_role != "tool":
        forced = _scenario_user_turn(scenario, tool_names, user_text, messages, turn, config)
        if forced is not None:
            return forced

    if _is_main_chat(tool_names) and last_role == "tool":
        last_called_tool = called[-1] if called else ""
        if last_called_tool == "add_comment":
            return Completion(
                content="<p>Comment inserted successfully.</p>",
                reasoning=reasoning,
                finish_reason="stop",
            )
        if last_called_tool == "apply_document_content" and _looks_like_comment(user_text):
            words = _extract_document_words(messages)
            first_word = words[0] if words else "Hello"
            return Completion(
                reasoning="Mock thinking: inserted text, now adding comment to first word.",
                tool_name="add_comment",
                tool_args={"search": first_word, "content": f"Mock comment on '{first_word}'"},
                finish_reason="tool_calls",
            )
        tool_text = ""
        for msg in reversed(messages):
            if isinstance(msg, dict) and msg.get("role") == "tool":
                tool_text = _as_text(msg.get("content"))
                break
        return Completion(
            content=_html_tool_wrapup(user_text, last_called_tool, tool_text),
            reasoning=reasoning,
            finish_reason="stop",
        )

    if last_role != "tool":
        soak = _scenario_user_turn(scenario, tool_names, user_text, messages, turn, config)
        if soak is not None:
            if config.sse_comments:
                soak.sse_comments = True
            return soak

    if _is_main_chat(tool_names) and _looks_like_comment(user_text):
        words = _extract_document_words(messages)
        if words:
            first_word = words[0]
            return Completion(
                reasoning="Mock thinking: found document text, adding comment to first word.",
                tool_name="add_comment",
                tool_args={"search": first_word, "content": f"Mock comment on '{first_word}'"},
                finish_reason="tool_calls",
            )
        return Completion(
            reasoning="Mock thinking: document is empty, inserting initial text with apply_document_content.",
            tool_name="apply_document_content",
            tool_args={"target": "beginning", "content": ["<p>Hello world from mock LLM.</p>"]},
            finish_reason="tool_calls",
        )

    if _is_main_chat(tool_names) and (config.always_research or _looks_like_research(user_text)):
        return Completion(
            reasoning=reasoning,
            tool_name="web_research",
            tool_args={"query": user_text or "mock research"},
            finish_reason="tool_calls",
        )

    last_user = _last_user_message(messages)
    last_content = last_user.get("content") if last_user else None
    if last_role != "tool" and _content_has_audio(last_content):
        transcript = canned_transcript(config)
        if _looks_like_stt_prompt(user_text):
            return Completion(content=transcript, finish_reason="stop")
        dur = audio_duration_s(_first_audio_b64(last_content))
        out = Completion(
            content=_html_audio_reply(user_text, transcript, dur),
            reasoning="Mock thinking: native audio in the user message.",
            finish_reason="stop",
        )
        if config.sse_comments:
            out.sse_comments = True
        return out

    out = Completion(
        content=_html_chat(user_text, turn),
        reasoning=reasoning,
        finish_reason="stop",
    )
    if config.sse_comments:
        out.sse_comments = True
    return out


def _completion_id() -> str:
    return "chatcmpl-mock-" + uuid.uuid4().hex[:12]


def _chunk_obj(model: str, delta: dict[str, Any], finish_reason: str | None = None) -> dict[str, Any]:
    return {
        "id": _completion_id(),
        "object": "chat.completion.chunk",
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }


def _iter_reasoning_chunks(completion: Completion, model: str) -> Iterator[dict[str, Any]]:
    mode = completion.reasoning_mode or "reasoning"
    text = completion.reasoning or ""
    if mode == "think_tags":
        return
    if mode == "reasoning_content" or mode == "details":
        if text:
            yield _chunk_obj(model, {"reasoning_content": text})
        if mode == "details":
            yield _chunk_obj(
                model,
                {"reasoning_details": [{"type": "reasoning.text", "text": text or "mock details"}]},
            )
        return
    if text:
        # Several small reasoning deltas so [Thinking] paints before HTML.
        words = text.split(" ")
        buf: list[str] = []
        for word in words:
            buf.append(word)
            if len(buf) >= 3:
                yield _chunk_obj(model, {"reasoning": " ".join(buf) + " "})
                buf = []
        if buf:
            yield _chunk_obj(model, {"reasoning": " ".join(buf) + " "})


def iter_sse_payloads(completion: Completion, model: str, chunk_chars: int = DEFAULT_CHUNK_CHARS) -> Any:
    """Yield SSE JSON objects; caller writes ``data:`` lines and ``[DONE]``."""
    yield from _iter_reasoning_chunks(completion, model)
    calls = completion_tool_calls(completion)
    if calls:
        step = max(1, int(chunk_chars))
        for index, (name, args_dict) in enumerate(calls):
            call_id = "call_mock_" + uuid.uuid4().hex[:8]
            args = json.dumps(args_dict or {}, ensure_ascii=False)
            yield _chunk_obj(
                model,
                {
                    "tool_calls": [
                        {
                            "index": index,
                            "id": call_id,
                            "type": "function",
                            "function": {"name": name, "arguments": ""},
                        }
                    ]
                },
            )
            for i in range(0, len(args), step):
                yield _chunk_obj(
                    model,
                    {"tool_calls": [{"index": index, "function": {"arguments": args[i : i + step]}}]},
                )
        yield _chunk_obj(model, {}, finish_reason="tool_calls")
        return
    text = completion.content or ""
    if completion.ramble_parts:
        for piece in text.split(" "):
            yield _chunk_obj(model, {"content": piece + " "})
    else:
        for word in text.split(" "):
            piece = word + " "
            yield _chunk_obj(model, {"content": piece})
    yield _chunk_obj(model, {}, finish_reason=completion.finish_reason or "stop")


def sync_response_body(completion: Completion, model: str) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": completion.content}
    if completion.reasoning and completion.reasoning_mode != "think_tags":
        if completion.reasoning_mode in {"reasoning_content", "details"}:
            message["reasoning_content"] = completion.reasoning
        else:
            message["reasoning"] = completion.reasoning
        if completion.reasoning_mode == "details":
            message["reasoning_details"] = [{"type": "reasoning.text", "text": completion.reasoning}]
    calls = completion_tool_calls(completion)
    if calls:
        message["content"] = None
        message["tool_calls"] = [
            {
                "id": "call_mock_" + uuid.uuid4().hex[:8],
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": json.dumps(args or {}, ensure_ascii=False),
                },
            }
            for name, args in calls
        ]
    return {
        "id": _completion_id(),
        "object": "chat.completion",
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": completion.finish_reason,
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


def models_list_body() -> dict[str, Any]:
    chat = {
        "id": MOCK_MODEL_ID,
        "object": "model",
        "owned_by": "writeragent-mock",
        "architecture": {"input_modalities": ["text", "audio"], "output_modalities": ["text"]},
    }
    stt = {
        "id": MOCK_STT_MODEL_ID,
        "object": "model",
        "owned_by": "writeragent-mock",
        "architecture": {"input_modalities": ["audio"], "output_modalities": ["text"]},
    }
    return {"object": "list", "data": [chat, stt]}


def transcription_response_body(transcript: str) -> dict[str, Any]:
    return {"text": transcript}


def _multipart_file_bytes(content_type: str, raw: bytes) -> bytes | None:
    bound = ""
    for part in content_type.split(";"):
        item = part.strip()
        if item.lower().startswith("boundary="):
            bound = item.split("=", 1)[1].strip().strip('"')
            break
    if not bound:
        return None
    marker = b"--" + bound.encode("utf-8", errors="replace")
    for chunk in raw.split(marker):
        header_blob = chunk.split(b"\r\n\r\n", 1)
        if len(header_blob) != 2:
            continue
        headers, body = header_blob
        if b'name="file"' in headers or b"name=file" in headers:
            return body.rstrip(b"\r\n-")
    return None


def _input_audio_from_json(payload: dict[str, Any]) -> str:
    ia = payload.get("input_audio")
    if isinstance(ia, dict):
        return str(ia.get("data") or "")
    return ""


def openai_error_body(message: str, err_type: str = "server_error") -> dict[str, Any]:
    return {"error": {"message": message, "type": err_type, "code": err_type}}


def _effective_fail(config: MockLLMConfig, completion: Completion) -> tuple[int | None, bool]:
    """Return (http_status or None, hang). Config.fail wins for whole-server soaks."""
    if config.fail == "http500":
        return 500, False
    if config.fail == "http429":
        return 429, False
    if config.fail == "hang":
        return None, True
    if completion.http_error:
        return int(completion.http_error), False
    if completion.hang:
        return None, True
    return None, False


def make_handler_class(config: MockLLMConfig, turns: _TurnState | None = None) -> type[BaseHTTPRequestHandler]:
    state = turns if turns is not None else _TurnState()

    class MockLLMHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

        def _send_json(self, status: int, body: dict[str, Any]) -> None:
            raw = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if path in ("/health", "/"):
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"ok")
                return
            if path in ("/v1/models", "/models"):
                self._send_json(200, models_list_body())
                return
            self.send_error(404)

        def _fail_or_hang_headers(self, dummy: Completion, *, stream: bool) -> bool:
            """Apply --fail / phrase http_error. Returns True if the request is fully handled."""
            status, hang = _effective_fail(config, dummy)
            if status is not None:
                if status == 429:
                    err_type = "rate_limit_error"
                elif status in (401, 403):
                    err_type = "authentication_error"
                else:
                    err_type = "server_error"
                self._send_json(status, openai_error_body("mock LLM soak failure", err_type))
                return True
            if hang and not stream:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self._drop_client_socket()
                return True
            return False

        def _drop_client_socket(self) -> None:
            """Packet F hang: half-close the socket (no [DONE]). Returning
            from do_POST on HTTP/1.1 keep-alive would leave the client
            blocked on readline until request_timeout. SHUT_WR (not
            SHUT_RDWR) lets the client see EOF after flushed chunks
            without RST that can drop unread send-buffer data.
            """
            self.close_connection = True
            try:
                self.connection.shutdown(socket.SHUT_WR)
            except OSError:
                pass

        def _apply_sse_quirk_preamble(self, quirk: str | None) -> bool:
            """Handle Packet F quirks that finish the response without a normal body.

            Returns True when the request is fully handled.
            """
            if quirk == "connection_reset":
                # RST / close before any status line (F13).
                self.close_connection = True
                try:
                    self.connection.close()
                except OSError:
                    pass
                return True
            if quirk == "empty_body":
                # HTTP 200 with zero-length body (F12).
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return True
            return False

        def do_POST(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            length = int(self.headers.get("Content-Length") or "0")
            raw = self.rfile.read(length) if length else b""
            if path in ("/v1/audio/transcriptions", "/audio/transcriptions"):
                record_capture(config, {"path": path, "stt": True, "has_input_audio": False})
                dummy = Completion()
                if self._fail_or_hang_headers(dummy, stream=False):
                    return
                ctype = str(self.headers.get("Content-Type") or "")
                transcript = canned_transcript(config)
                if "multipart/form-data" in ctype.lower():
                    wav = _multipart_file_bytes(ctype, raw)
                    if wav is None:
                        self.send_error(400, "missing file")
                        return
                    self._send_json(200, transcription_response_body(transcript))
                    return
                try:
                    payload = json.loads(raw.decode("utf-8") or "{}")
                except json.JSONDecodeError:
                    self.send_error(400, "invalid json")
                    return
                if not isinstance(payload, dict) or not _input_audio_from_json(payload):
                    self.send_error(400, "expected input_audio")
                    return
                self._send_json(200, transcription_response_body(transcript))
                return
            if path not in ("/v1/chat/completions", "/chat/completions"):
                self.send_error(404)
                return
            try:
                payload = json.loads((raw.decode("utf-8") if raw else "") or "{}")
            except json.JSONDecodeError:
                self.send_error(400, "invalid json")
                return
            if not isinstance(payload, dict):
                self.send_error(400, "expected object")
                return
            model = str(payload.get("model") or MOCK_MODEL_ID)
            completion = decide_completion(payload, config, state)
            record_capture(config, summarize_chat_payload(payload, completion, config))
            stream = bool(payload.get("stream"))
            messages = payload.get("messages") if isinstance(payload.get("messages"), list) else []
            if config.fail_tool_followup and _last_role(messages) == "tool":
                self._send_json(500, openai_error_body("mock LLM tool-follow-up failure", "server_error"))
                return
            if self._apply_sse_quirk_preamble(completion.sse_quirk):
                return
            if self._fail_or_hang_headers(completion, stream=stream):
                return
            unused_status, hang = _effective_fail(config, completion)
            if unused_status is not None:
                return
            delay = response_delay_s(config, stream=stream)
            if not stream:
                # Nested smol / specialized agents use stream=False. Without this
                # sleep, Packet E7/E8 nested work finishes in a few milliseconds.
                # --sync-delay-ms stretches only that path so Stop is clickable
                # without slowing main-chat SSE (which would eat the nested window).
                if delay:
                    time.sleep(delay)
                self._send_json(200, sync_response_body(completion, model))
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            quirk = completion.sse_quirk
            # F9 / F10: bad line first; then a normal stream so the client can recover.
            if quirk == "malformed":
                self.wfile.write(b"data: {not json}\n\n")
                self.wfile.flush()
            elif quirk == "truncated":
                self.wfile.write(b"data: {\n\n")
                self.wfile.flush()
            comments = bool(config.sse_comments or completion.sse_comments)
            event_ping = quirk == "event_ping"
            max_chunks = int(config.fail_after_chunks) if hang else None
            written = 0
            for obj in iter_sse_payloads(completion, model, chunk_chars=config.chunk_chars):
                if comments:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                if event_ping:
                    # F18: named SSE events are ignored by iterate_sse (data: only).
                    self.wfile.write(b"event: ping\ndata: ignored\n\n")
                    self.wfile.flush()
                line = "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"
                self.wfile.write(line.encode("utf-8"))
                self.wfile.flush()
                written += 1
                if max_chunks is not None and written >= max_chunks:
                    self._drop_client_socket()
                    return
                if delay:
                    time.sleep(delay)
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            if quirk == "two_dones":
                # F11: second [DONE] must not start another drain terminal.
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()

    return MockLLMHandler


def serve(host: str, port: int, config: MockLLMConfig) -> None:
    httpd = ThreadingHTTPServer((host, port), make_handler_class(config))
    print(
        f"Mock LLM on http://{host}:{port}/v1 (model {MOCK_MODEL_ID}; "
        f"offline={config.offline} always_research={config.always_research} "
        f"scenario={config.scenario} fail={config.fail} delay_ms={config.delay_ms} "
        f"sync_delay_ms={config.sync_delay_ms})",
        flush=True,
    )
    httpd.serve_forever()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="WriterAgent mock OpenAI chat endpoint")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--delay-ms",
        type=int,
        default=25,
        help="Pause between SSE chunks (and before sync JSON unless --sync-delay-ms is set)",
    )
    parser.add_argument(
        "--sync-delay-ms",
        type=int,
        default=None,
        help="Pause before each non-streaming JSON response (nested smol/specialized). Default: --delay-ms",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Smol research path skips web_search/visit_webpage (final_answer only)",
    )
    parser.add_argument(
        "--always-research",
        action="store_true",
        help="Main chat always emits web_research on user turns",
    )
    parser.add_argument(
        "--scenario",
        default="none",
        choices=sorted(SCENARIO_IDS),
        help="Force a soak scenario on user turns (overrides phrase matching)",
    )
    parser.add_argument("--chunk-chars", type=int, default=DEFAULT_CHUNK_CHARS, help="SSE tool-arg fragment size")
    parser.add_argument("--fail", default="none", choices=FAIL_MODES, help="Fail every request this way")
    parser.add_argument(
        "--fail-after-chunks",
        type=int,
        default=DEFAULT_FAIL_AFTER_CHUNKS,
        help="When hang: write this many SSE objects then drop the socket",
    )
    parser.add_argument(
        "--sse-comments",
        action="store_true",
        help="Emit ': ping' SSE comments between data events",
    )
    parser.add_argument(
        "--transcript",
        default=DEFAULT_TRANSCRIPT,
        help="Canned STT / native-audio transcript (no real ASR)",
    )
    args = parser.parse_args(argv)
    config = MockLLMConfig(
        delay_ms=args.delay_ms,
        sync_delay_ms=args.sync_delay_ms,
        offline=args.offline,
        always_research=args.always_research,
        scenario=args.scenario,
        chunk_chars=args.chunk_chars,
        fail=args.fail,
        fail_after_chunks=args.fail_after_chunks,
        sse_comments=args.sse_comments,
        transcript=args.transcript,
    )
    serve(args.host, args.port, config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

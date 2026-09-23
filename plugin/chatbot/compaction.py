# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""UNO-free conversation compaction. Cached view over a duck-typed session
(``messages``, ``compaction``). Does not import ``panel``, ``tool_loop``,
or UNO. Must run inside the caller's ``llm_request_lane`` hold — this
module must not take that lock.

Policy: ``docs/chat/compaction-dev-plan.md`` (Compaction v1).

Hermes Agent 0.21.1 (MIT, Nous Research), tag ``v2026.9.7``:
https://github.com/NousResearch/hermes-agent/tree/v2026.9.7
Near copies: ``estimate_tokens_rough``, ``_temporal_anchoring_rule`` (MIT
comments at those defs). Algorithms: ``compaction_ratio``,
``ensure_last_user_in_tail``, ``pressure_stub_newest_tool_group``,
``serialize_for_summary`` stubs, ``should_retry_overflow`` 5% gate.
Not a port of ``ContextCompressor`` (4931 LOC).

OpenClaw file:line cites remain on overflow markers, keepRecent cap,
``IMAGE_BLOCK_TOKENS``, and the heading skeleton.
"""

from __future__ import annotations

import datetime
import json
import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

from plugin.framework.client.model_fetcher import cached_v1_context_tokens, query_ollama_runtime_num_ctx
from plugin.framework.config import get_config_bool_safe
from plugin.framework.default_models import DEFAULT_MODELS, resolve_model_id
from plugin.framework.openrouter_model_id import openrouter_model_ids_equivalent

log = logging.getLogger(__name__)

CHARS_PER_TOKEN = 4
# Tier constants. COMPACTION_RATIO_SMALL is the llama.cpp product number.
# 75% / 50% / 512k from Hermes _SMALL_CTX_* + _effective_threshold_percent
# (context_compressor.py:986-989, :2235-2239). 70% is WA, not Hermes.
# https://github.com/NousResearch/hermes-agent/blob/v2026.9.7/agent/context_compressor.py#L2235-L2239
COMPACTION_RATIO_SMALL = 0.70  # W <= 8192  (WA product; Hermes 85% is too late on 4k)
COMPACTION_RATIO_DEFAULT = 0.75  # 8192 < W < 512_000  (Hermes small-context floor)
COMPACTION_RATIO_LARGE = 0.50  # W >= 512_000  (Hermes large-window default)
SMALL_CTX_WINDOW_LIMIT = 512_000
MAX_OVERFLOW_COMPACTION_ATTEMPTS = 3  # OpenClaw agent-compaction-constants.ts:30
# Hermes TurnOverflow.compress_scored_by_tokens: shrank iff after < before * 0.95
# https://github.com/NousResearch/hermes-agent/blob/v2026.9.7/agent/turn_overflow.py#L203
MIN_SHRINK_RATIO = 0.95
KEEP_RECENT_FRACTION = 0.30
KEEP_RECENT_FLOOR = 2048  # preference, not a hard min after remaining-budget clamp
KEEP_RECENT_CAP = 20_000  # OpenClaw DEFAULT_COMPACTION_SETTINGS.keepRecentTokens
MIN_TAIL_TOKENS = 256  # hard floor; below this, nothing to gain
MAX_SUMMARY_CHARS = 16_000  # OpenClaw safety rail only; view cap is _summary_budget * 4
IMAGE_BLOCK_TOKENS = 2000  # OpenClaw compaction.ts:278 (Hermes default is 1500)
AUDIO_BLOCK_TOKENS = 2000
# Hermes ContextCompressor._PRUNE_MIN_CHARS
# https://github.com/NousResearch/hermes-agent/blob/v2026.9.7/agent/context_compressor.py#L661
PRUNE_MIN_CHARS = 200
MAX_TOOL_CALL_ARGS_CHARS = 4000  # serialize_for_summary assistant tool_calls cap
DOCUMENT_MARKERS = ("[DOCUMENT CONTENT]", "[END DOCUMENT]")
_SUMMARY_TRUNCATION_MARKER = "\n\n[Compaction summary truncated to fit budget]"

# Process death is display-only (issue #570). Compact-and-retry on a dead
# llama-server is worse than today's overflow sentence.
_PROCESS_DEATH_MARKERS = (
    "llama-server process has terminated",
    "0xc0000005",
)

# Case-insensitive substrings from OpenClaw ASSISTANT_OVERFLOW_PATTERNS
# (packages/ai/src/utils/overflow.ts:44-72). Groq TPM-413 is excluded via
# _NON_OVERFLOW_MARKERS (OpenClaw NON_OVERFLOW_PATTERNS :146-150).
_OVERFLOW_MARKERS = (
    "request_too_large",
    "context length exceeded",
    "context_length_exceeded",
    "prompt is too long",
    "prompt too long",
    "input exceeds the maximum number of tokens",
    "exceeds the maximum number of tokens allowed",
    "exceeds the maximum number of input tokens",
    "input is too long for requested model",
    "input is too long for the model",
    "exceeds the context window",
    "exceeds the available context size",
    "maximum context length",
    "is longer than the model's context length",
    "reduce the length of the messages",
    "truncating input prompt",
    "prompt overflow",
    "maximum prompt length",
    "context window exceeds limit",
    "413 status code (no body)",
    "tokens in request more than max tokens allowed",
    "prompt exceeds max",
    "too large for model with",
    "exceeds the limit of",
    "greater than the context length",
    "exceeded model token limit",
    "model_context_window_exceeded",
    "too many tokens",
)

_NON_OVERFLOW_MARKERS = (
    "rate limit",
    "too many requests",
    "tokens per minute",
    "tpm",
)

# Adapted from Nous Research Hermes Agent 0.21.1 (MIT).
# estimate_tokens_rough / _CJK_DENSE_RE.
# https://github.com/NousResearch/hermes-agent/blob/v2026.9.7/agent/model_metadata.py#L1957-L1983
# Copyright (c) 2025 Nous Research. MIT License.
# Byte-counting (not chars) is the corrective for non-CJK, non-ASCII text:
# Cyrillic/Greek/Arabic are 2 bytes/char so count ~chars/2, matching real BPE
# cost where chars/4 under-counted ~2x (Hermes docstring :1970-1975).
_CJK_DENSE_RE = re.compile(
    "[\u1100-\u11ff\u2e80-\u9fff\ua960-\ua97f\uac00-\ud7af\uf900-\ufaff\uff00-\uffef]"
)

SUMMARIZATION_SYSTEM_PROMPT = (
    "You are a conversation compaction assistant for LibreOffice Writer, "
    "Calc, and Draw. Produce a structured summary the next model turn can "
    "continue from. Do not invent facts, dates, sheet names, or cell ranges. "
    "Do not include the live document snapshot — it is rebuilt every send."
)

# OpenClaw SUMMARIZATION_PROMPT headings (compaction.ts:498-529), office-flavored.
# Completed Actions [tool: name] fragment from Hermes _summary_template_sections
# (context_compressor.py:3420-3427). Not the full Hermes template.
# https://github.com/NousResearch/hermes-agent/blob/v2026.9.7/agent/context_compressor.py#L3420-L3427
# Temporal anchoring is appended at call time so a clock failure can omit it.
SUMMARIZATION_PROMPT = """\
Summarize the conversation inside <conversation> for a later model turn.

Use these headings (omit a heading if empty):

# Goal
What the user is trying to accomplish in Writer / Calc / Draw.

# Constraints / Preferences
Style, tone, must-keep wording, locale, and any explicit do-nots.

# Progress / Completed Actions
Number completed tool actions as:
1. [tool: name] Brief past-tense description
Preserve sheet names, cell ranges, heading text, and object names exactly.

# Key Decisions
Choices the user or assistant already made.

# Open Questions
Unresolved asks. Do not restate finished work as if it still needs doing.

# Files and Artifacts
Paths, sheet names, named ranges, bookmarks, slide titles.
"""

# OpenClaw UPDATE_SUMMARIZATION_PROMPT (compaction.ts:531-568).
UPDATE_SUMMARIZATION_PROMPT = """\
Update the previous summary using only the new turns in <conversation>.
Keep facts from <previous-summary> unless the new turns supersede them.
Do not re-summarize already-summarized turns. Use the same headings.
Number new completed tool actions as [tool: name] under Progress.
"""


@dataclass(frozen=True)
class CompactionState:
    summary: str
    first_kept_index: int  # into session.messages; always >= 1
    tokens_before: int
    window: int
    stubbed_tool_call_ids: tuple[Any, ...] = ()  # view-only tail-pressure stubs


@dataclass(frozen=True)
class CompactResult:
    compacted: bool
    reason: str  # below_threshold | nothing_to_compact | no_window | ok | failed | aborted | disabled
    tokens_before: int | None = None
    tokens_after: int | None = None


def gen_reserve(window: int) -> int:
    """Generation tokens the compacted view must leave free.

    Independent of ``chat_max_tokens`` (which would zero a 4k remainder).
    WA-only: a 4k-viable analog of Hermes's 85% small-window cap, not a
    Hermes function. Ollama's OpenAI-compatible endpoint silently clips
    over-window prompts, so exact-fill of ``n_ctx`` never reaches overflow
    retry. 4096 → 256; 8192+ → 512.
    """
    return min(512, max(256, window // 16))


def compaction_ratio(window: int) -> float:
    """Window-tiered trigger.

    Algorithm from Hermes ``ContextCompressor._effective_threshold_percent``
    (``context_compressor.py:2235-2239``). WA drops the 64k
    ``MINIMUM_CONTEXT_LENGTH`` floor and adds a 70% tier for ``W <= 8192``.
    https://github.com/NousResearch/hermes-agent/blob/v2026.9.7/agent/context_compressor.py#L2235-L2239
    """
    if window >= SMALL_CTX_WINDOW_LIMIT:
        return COMPACTION_RATIO_LARGE
    if window <= 8192:
        return COMPACTION_RATIO_SMALL
    return COMPACTION_RATIO_DEFAULT


def estimate_tokens_rough(text: Any) -> int:
    """CJK=1 + UTF-8 bytes/4. Near-copy of Hermes ``estimate_tokens_rough``.

    https://github.com/NousResearch/hermes-agent/blob/v2026.9.7/agent/model_metadata.py#L1966-L1983
    """
    if not text:
        return 0
    text = str(text)
    if text.isascii():
        return (len(text) + 3) // 4
    stripped = _CJK_DENSE_RE.sub("", text)
    dense = len(text) - len(stripped)
    return dense + ((len(stripped.encode("utf-8", "replace")) + 3) // 4)


def flatten_content(content: Any) -> tuple[str, int, int]:
    """Return (text, image_count, audio_count). Never str() a content list."""
    if content is None:
        return "", 0, 0
    if isinstance(content, str):
        return content, 0, 0
    if not isinstance(content, list):
        return "", 0, 0
    texts = []
    n_img = n_aud = 0
    for part in content:
        if not isinstance(part, dict):
            continue
        kind = part.get("type")
        if kind == "text" and isinstance(part.get("text"), str):
            texts.append(part["text"])
        elif kind == "image_url":
            n_img += 1
        elif kind == "input_audio":
            n_aud += 1
    return " ".join(texts), n_img, n_aud


def estimate_message_tokens(msg: dict[str, Any]) -> int:
    text, n_img, n_aud = flatten_content(msg.get("content"))
    tokens = estimate_tokens_rough(text)
    for tc in msg.get("tool_calls") or []:
        fn = (tc.get("function") or {}) if isinstance(tc, dict) else {}
        tokens += estimate_tokens_rough(str(fn.get("name") or ""))
        tokens += estimate_tokens_rough(str(fn.get("arguments") or ""))
    tokens += n_img * IMAGE_BLOCK_TOKENS
    tokens += n_aud * AUDIO_BLOCK_TOKENS
    return max(1, tokens)


def estimate_tokens(messages: list[dict[str, Any]]) -> int:
    return sum(estimate_message_tokens(m) for m in messages)


def tool_schema_tokens(tools: Any) -> int:
    if not tools:
        return 0
    blob = json.dumps(tools, ensure_ascii=False, separators=(",", ":"))
    return estimate_tokens_rough(blob)


def prompt_tokens(messages: list[dict[str, Any]], tools: Any) -> int:
    """Messages plus tool schemas.

    Same idea as Hermes ``estimate_request_tokens_rough``
    (``model_metadata.py:2155-2167``): count the request, not just the
    transcript. WA ``json.dumps``s the schema; Hermes sums cached field
    lengths.
    https://github.com/NousResearch/hermes-agent/blob/v2026.9.7/agent/model_metadata.py#L2155-L2167
    """
    return estimate_tokens(messages) + tool_schema_tokens(tools)


def _summary_budget(window: int) -> int:
    """Token allowance for the summary pair, summarizer max_tokens, and char cap."""
    return min(1024, max(256, window // 8))


def summary_char_cap(window: int) -> int:
    return min(MAX_SUMMARY_CHARS, _summary_budget(window) * CHARS_PER_TOKEN)


def cap_summary(summary: str, max_chars: int) -> str:
    if len(summary) <= max_chars:
        return summary
    keep = max(0, max_chars - len(_SUMMARY_TRUNCATION_MARKER))
    return summary[:keep] + _SUMMARY_TRUNCATION_MARKER


def should_compact(tokens: int, window: int | None, enabled: bool = True) -> bool:
    if not enabled or window is None or window <= 0:
        return False
    return tokens >= int(window * compaction_ratio(window))


def keep_recent_tokens(window: int, system_tokens: int, tool_tokens: int, force: bool = False) -> int | None:
    reserve = gen_reserve(window)
    remainder = window - system_tokens - tool_tokens - _summary_budget(window) - reserve
    if remainder < MIN_TAIL_TOKENS:
        return None  # document+system+tools already fill the window
    desired = min(KEEP_RECENT_CAP, max(KEEP_RECENT_FLOOR, window * 3 // 10))
    clamped = min(desired, remainder)  # MAY be below KEEP_RECENT_FLOOR
    if force:
        # Halve the already-clamped value so remainder-bound 4k actually shrinks.
        return max(MIN_TAIL_TOKENS, clamped // 2)
    return clamped


def resolve_context_window(client: Any, model_id: str | None = None) -> int | None:
    """Ollama: live ``num_ctx`` only. Else cached /v1/models, then catalog. None → skip compact."""
    model_id = str(model_id or (client.config or {}).get("model") or "").strip() or None
    if not model_id:
        return None
    try:
        provider = client._get_provider()
    except Exception:
        provider = None
    if provider == "ollama":
        # Issue #570: trained context_length is not the runtime window.
        # Missing num_ctx → None; do not fall back to v1 cache or catalog.
        try:
            endpoint = client._endpoint()
        except Exception:
            return None
        num_ctx = query_ollama_runtime_num_ctx(endpoint, model_id)
        if isinstance(num_ctx, int) and num_ctx > 0:
            return num_ctx
        return None
    try:
        endpoint = client._endpoint()
    except Exception:
        endpoint = None
    if endpoint:
        # Harvest-only: Settings/sidebar already fetched. Do not GET /v1/models here.
        live = cached_v1_context_tokens(endpoint, model_id, provider)
        if isinstance(live, int) and live > 0:
            return live
    for row in DEFAULT_MODELS:
        rid = resolve_model_id(row, provider)
        matched = rid == model_id
        if (
            not matched
            and provider == "openrouter"
            and rid
            and openrouter_model_ids_equivalent(rid, model_id)
        ):
            matched = True
        if not matched:
            continue
        cl = row.get("context_length")
        if isinstance(cl, int) and cl > 0:
            return cl
    # Custom OpenAI-compatible endpoints (including writeragent-mock) are
    # provider ``custom``, so the provider-keyed walk misses them. Match any
    # catalog id. Ollama already returned above — never this fallback.
    for row in DEFAULT_MODELS:
        ids = row.get("ids")
        if not isinstance(ids, dict) or model_id not in ids.values():
            continue
        cl = row.get("context_length")
        if isinstance(cl, int) and cl > 0:
            return cl
    return None


def _is_cut_point(msg: dict[str, Any]) -> bool:
    return msg.get("role") in ("user", "assistant")  # not "tool", not "system"


def find_cut_index(messages: list[dict[str, Any]], keep_tokens: int, start_index: int = 1) -> int | None:
    """First index of a verbatim tail whose token sum is <= keep_tokens.

    Ceiling walk (not OpenClaw's floor). Newest-first accumulate is the
    same direction as Hermes ``_walk_tail_budget``
    (``context_compressor.py:2589-2608``); Hermes has a ``min_tail``
    message-count floor, WA is a pure token ceiling.
    https://github.com/NousResearch/hermes-agent/blob/v2026.9.7/agent/context_compressor.py#L2589-L2608
    start_index is 1 (skip DOCUMENT CONTENT) or the previous
    first_kept_index. None if nothing to compact or if even the last
    user turn cannot fit in keep_tokens.
    """
    n = len(messages)
    if keep_tokens <= 0 or n <= start_index:
        return None
    cut_points = [i for i in range(start_index, n) if _is_cut_point(messages[i])]
    if not cut_points:
        return None
    accumulated = 0
    earliest = n  # tail would be messages[earliest:]
    for i in range(n - 1, start_index - 1, -1):
        t = estimate_message_tokens(messages[i])
        if accumulated + t > keep_tokens:
            break
        accumulated += t
        earliest = i
    if earliest >= n:
        return None  # last message alone exceeds keep
    # Snap FORWARD to a legal cut (user/assistant). Forward-only shrinks the
    # tail, so it cannot exceed keep. If earliest is a tool message, the
    # assistant+tools block goes to the summarized side together.
    candidates = [cp for cp in cut_points if cp >= earliest]
    if not candidates:
        return None
    cut_index = candidates[0]
    if cut_index <= start_index:
        return None  # entire unsummarized range already fits — nothing to compact
    return cut_index


def _is_real_user(msg: dict[str, Any]) -> bool:
    """Non-empty user text. Simpler analog of Hermes ``_is_actionable_user_turn``.

    https://github.com/NousResearch/hermes-agent/blob/v2026.9.7/agent/context_compressor.py#L3660-L3670
    """
    if msg.get("role") != "user":
        return False
    text = flatten_content(msg.get("content"))[0].strip()
    return bool(text)
    # Dummy compaction ack is view-only and never appears in session.messages.


def ensure_last_user_in_tail(messages: list[dict[str, Any]], cut: int, start_index: int, keep_tokens: int) -> int | None:
    """Pull cut back so the last real user is in the tail (Hermes #10896).

    Algorithm from Hermes ``_ensure_last_user_message_in_tail``
    (``context_compressor.py:3977-4003``). Hermes lets this win over the
    token budget; WA re-checks ``keep`` and returns None.
    https://github.com/NousResearch/hermes-agent/blob/v2026.9.7/agent/context_compressor.py#L3977-L4003
    https://github.com/NousResearch/hermes-agent/issues/10896
    A user message is already a clean boundary — do not backward-align
    into the preceding assistant+tools group (Hermes :3986-3988 / #22566).
    """
    last = None
    for i in range(len(messages) - 1, start_index - 1, -1):
        if _is_real_user(messages[i]):
            last = i
            break
    if last is None or last >= cut:
        return cut
    new_cut = last
    if estimate_tokens(messages[new_cut:]) > keep_tokens:
        return None
    return new_cut


def _tool_name_for(messages: list[dict[str, Any]], idx: int) -> str:
    """Best-effort name from the preceding assistant tool_calls matching tool_call_id."""
    call_id = messages[idx].get("tool_call_id")
    for j in range(idx - 1, -1, -1):
        tcs = messages[j].get("tool_calls") or []
        for tc in tcs:
            if not isinstance(tc, dict):
                continue
            if call_id and tc.get("id") == call_id:
                fn = tc.get("function") or {}
                return str(fn.get("name") or "tool")
        if messages[j].get("role") == "assistant":
            break
    return "tool"


def newest_assistant_tool_span(messages: list[dict[str, Any]], start_index: int) -> tuple[int, int] | None:
    """(asst_idx, end_idx exclusive) of the newest assistant+tool_calls group.

    Skips a trailing user so the last user stays in the tail. None if there
    is no such group.
    """
    n = len(messages)
    i = n - 1
    if i >= start_index and _is_real_user(messages[i]):
        i -= 1
    end = i + 1
    while i >= start_index and messages[i].get("role") == "tool":
        i -= 1
    if i < start_index or messages[i].get("role") != "assistant":
        return None
    if not (messages[i].get("tool_calls") or []):
        return None
    return i, end


def pressure_stub_newest_tool_group(messages: list[dict[str, Any]], keep_tokens: int, start_index: int = 1) -> tuple[list[dict[str, Any]], tuple[Any, ...]]:
    """Copy messages; stub newest-group role=tool bodies > PRUNE_MIN_CHARS.

    Algorithm from Hermes ``_pressure_demote_tail``
    (``context_compressor.py:2687-2735``). WA is newest-group-only and
    view-only; stub format follows the generic
    ``_summarize_tool_result`` fallback ``[{name}] ({N} chars result)``
    (:1391-1392), not the per-tool ``_sum_terminal`` dispatch.
    https://github.com/NousResearch/hermes-agent/blob/v2026.9.7/agent/context_compressor.py#L2687-L2735
    Largest first, newest last-resort until the #10896 tail fits keep:
    last real user immediately before the group + the group, or the
    group alone if there is no such user.

    Must not no-op when group <= keep but last_user + group > keep
    (typical #10896 overflow). Last user is never stubbed.

    Returns ``(work, stubbed_tool_call_ids)``. Does not mutate the input.
    """
    work = list(messages)
    span = newest_assistant_tool_span(work, start_index)
    if span is None:
        return work, ()
    asst_i, end_i = span
    tail_i = asst_i
    if asst_i - 1 >= start_index and _is_real_user(work[asst_i - 1]):
        tail_i = asst_i - 1

    def tail_tokens():
        return sum(estimate_message_tokens(work[j]) for j in range(tail_i, end_i))

    if tail_tokens() <= keep_tokens:
        return work, ()
    tool_idxs = [j for j in range(asst_i + 1, end_i) if work[j].get("role") == "tool"]
    tool_idxs.sort(key=lambda j: len(flatten_content(work[j].get("content"))[0]), reverse=True)
    stubbed = []
    for j in tool_idxs:
        text = flatten_content(work[j].get("content"))[0]
        if len(text) <= PRUNE_MIN_CHARS:
            continue
        copied = dict(work[j])
        copied["content"] = "[%s] (%d chars)" % (_tool_name_for(messages, j), len(text))
        work[j] = copied
        cid = copied.get("tool_call_id")
        if cid:
            stubbed.append(cid)
        if tail_tokens() <= keep_tokens:
            break
    return work, tuple(stubbed)


def _format_tool_calls(msg: dict[str, Any]) -> str:
    parts = []
    budget = MAX_TOOL_CALL_ARGS_CHARS
    for tc in msg.get("tool_calls") or []:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") or {}
        name = str(fn.get("name") or "tool")
        args = str(fn.get("arguments") or "")
        if len(args) > budget:
            args = args[:budget] + "…"
        budget = max(0, budget - len(args))
        parts.append("%s(%s)" % (name, args))
    return " ".join(parts)


def _strip_document_span(text: str) -> str:
    if DOCUMENT_MARKERS[0] not in text:
        return text
    start = text.find(DOCUMENT_MARKERS[0])
    end = text.find(DOCUMENT_MARKERS[1])
    if end >= 0:
        return (text[:start] + text[end + len(DOCUMENT_MARKERS[1]):]).strip()
    return text[:start].strip()


def serialize_for_summary(messages: list[dict[str, Any]]) -> str:
    """ROLE: text lines. Stub role=tool bodies > PRUNE_MIN_CHARS. Session unchanged.

    Prune idea from Hermes ``_PRUNE_MIN_CHARS`` / old-tool stub
    (``context_compressor.py:661``). Not ``_prune_old_tool_results`` or
    SessionDB archive.
    https://github.com/NousResearch/hermes-agent/blob/v2026.9.7/agent/context_compressor.py#L661
    """
    lines = []
    for i, msg in enumerate(messages):
        role = msg.get("role") or ""
        text, n_img, n_aud = flatten_content(msg.get("content"))
        text = _strip_document_span(text)
        if role == "assistant":
            calls = _format_tool_calls(msg)
            if calls:
                text = (text + " " + calls).strip() if text else calls
        if role == "tool" and len(text) > PRUNE_MIN_CHARS:
            text = "[%s] (%d chars)" % (_tool_name_for(messages, i), len(text))
        if n_img:
            text = (text + " [image data omitted from summary input]").strip()
        if n_aud:
            text = (text + " [audio omitted]").strip()
        lines.append("%s: %s" % (role.upper(), text))
    return "\n".join(lines)


def summary_pair(summary: str) -> list[dict[str, Any]]:
    return [
        {"role": "user", "content": "[CONVERSATION SUMMARY]\n" + summary + "\n[END SUMMARY]"},
        {"role": "assistant", "content": "Acknowledged. I will continue from the summary above."},
    ]


def sanitize_tool_pairs(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop orphan role=tool; strip dangling tool_calls (keep assistant text if any).

    Also drops tool_calls with empty/missing ``function.name`` (and their matching
    tool results), so stream-split phantoms cannot poison the next API round.

    Same job as Hermes ``_sanitize_tool_pairs``
    (``context_compressor.py:3846-3884``); not a port of that method.
    https://github.com/NousResearch/hermes-agent/blob/v2026.9.7/agent/context_compressor.py#L3846-L3884
    """
    if not messages:
        return []
    out = []
    i = 0
    n = len(messages)
    while i < n:
        msg = messages[i]
        if msg.get("role") == "assistant" and (msg.get("tool_calls") or []):
            j = i + 1
            tools = []
            while j < n and messages[j].get("role") == "tool":
                tools.append(messages[j])
                j += 1
            have = {t.get("tool_call_id") for t in tools}
            kept_calls = [
                tc
                for tc in (msg.get("tool_calls") or [])
                if isinstance(tc, dict)
                and tc.get("id") in have
                and (tc.get("function") or {}).get("name")
            ]
            asst = dict(msg)
            if kept_calls:
                asst["tool_calls"] = kept_calls
            else:
                asst.pop("tool_calls", None)
            text = flatten_content(asst.get("content"))[0]
            if kept_calls or text.strip():
                out.append(asst)
            kept_ids = {tc.get("id") for tc in kept_calls}
            for tool_msg in tools:
                if tool_msg.get("tool_call_id") in kept_ids:
                    out.append(tool_msg)
            i = j
            continue
        if msg.get("role") == "tool":
            i += 1
            continue
        out.append(msg)
        i += 1
    return out


def _apply_tail_stubs(messages: list[dict[str, Any]], first_kept_index: int, stubbed_ids: Any) -> list[dict[str, Any]]:
    if not stubbed_ids:
        return list(messages[first_kept_index:])
    ids = set(stubbed_ids)
    tail = []
    for i in range(first_kept_index, len(messages)):
        msg = messages[i]
        if msg.get("role") == "tool" and msg.get("tool_call_id") in ids:
            text = flatten_content(msg.get("content"))[0]
            copied = dict(msg)
            copied["content"] = "[%s] (%d chars)" % (_tool_name_for(messages, i), len(text))
            tail.append(copied)
        else:
            tail.append(msg)
    return tail


def messages_for_llm(session: Any, tools: Any = None) -> list[dict[str, Any]]:
    """Live messages[0] + summary pair + tail (view stubs). Does not mutate session."""
    del tools  # accepted so callers share the prompt_tokens signature
    messages = getattr(session, "messages", None) or []
    if not messages:
        return []
    state = getattr(session, "compaction", None)
    if state is None:
        return sanitize_tool_pairs(list(messages))
    tail = _apply_tail_stubs(messages, state.first_kept_index, state.stubbed_tool_call_ids)
    view = [messages[0]] + summary_pair(state.summary) + tail
    return sanitize_tool_pairs(view)


def is_process_death_error(text: str | None) -> bool:
    lower = (text or "").lower()
    return any(m in lower for m in _PROCESS_DEATH_MARKERS)


def is_context_overflow_error(text: str | None) -> bool:
    """Prompt-too-large only. Process death and 429/TPM are NOT overflow."""
    if is_process_death_error(text):
        return False
    lower = (text or "").lower()
    if any(m in lower for m in _NON_OVERFLOW_MARKERS):
        return False
    if any(m in lower for m in _OVERFLOW_MARKERS):
        return True
    if "llama.cpp" in lower and "overflow" in lower:
        return True
    return False


def should_retry_overflow(attempts: int, compact_reason: str | None, tokens_before: int | None = None, tokens_after: int | None = None) -> bool:
    """Cap retries and skip when compact did not shrink enough.

    5% gate from Hermes ``TurnOverflow.compress_scored_by_tokens``
    (``turn_overflow.py:179-208``, ``new_tokens < original * 0.95`` at :203).
    https://github.com/NousResearch/hermes-agent/blob/v2026.9.7/agent/turn_overflow.py#L179-L208
    """
    if attempts >= MAX_OVERFLOW_COMPACTION_ATTEMPTS:
        return False
    if compact_reason in ("nothing_to_compact", "no_window", "failed", "aborted", "disabled"):
        return False
    if (
        tokens_before is not None
        and tokens_after is not None
        and tokens_before > 0
        and tokens_after >= tokens_before * MIN_SHRINK_RATIO
    ):
        return False  # Hermes compress_scored_by_tokens :203 is after < before * 0.95
    return True


def _today_for_prompt():
    """YYYY-MM-DD; '' on clock failure.

    Same idea as Hermes ``_today_for_prompt``
    (``context_compressor.py:1584-1594``). Hermes uses ``hermes_time``;
    WA uses ``datetime.date.today()``.
    https://github.com/NousResearch/hermes-agent/blob/v2026.9.7/agent/context_compressor.py#L1584-L1594
    """
    try:
        return datetime.date.today().isoformat()
    except Exception:
        return ""


def _temporal_anchoring_rule():
    """Dated past-tense rule for the summarizer. Near-copy of Hermes.

    Adapted from ``ContextCompressor._temporal_anchoring_rule``
    (``context_compressor.py:3392-3405``).
    https://github.com/NousResearch/hermes-agent/blob/v2026.9.7/agent/context_compressor.py#L3392-L3405
    Copyright (c) 2025 Nous Research. MIT License.
    """
    today = _today_for_prompt()
    if not today:
        return ""
    return (
        "\nTEMPORAL ANCHORING: The current date is %s. When an "
        "action has already been carried out, phrase it as a completed, "
        "dated, past-tense fact rather than an open instruction. For "
        "example, rewrite \"email John about the proposal\" as \"Sent the "
        "proposal email to John on %s.\" Never leave a finished "
        "action worded as if it still needs doing, and never invent a date "
        "for work that has not happened yet.\n" % (today, today)
    )


def _choose_cut(messages: list[dict[str, Any]], keep: int, prev_kept: int) -> tuple[list[dict[str, Any]], int | None, tuple[Any, ...]]:
    """Ceiling walk, optional tail-pressure stub, then last-user snap.

    Returns ``(work, new_cut, stubbed_ids)`` or ``(work, None, stubbed_ids)``.
    """
    work, stubbed_ids = messages, ()
    new_cut = find_cut_index(work, keep, start_index=prev_kept)
    if new_cut is None:
        work, stubbed_ids = pressure_stub_newest_tool_group(messages, keep, prev_kept)
        new_cut = find_cut_index(work, keep, start_index=prev_kept)
    if new_cut is None or new_cut <= prev_kept:
        return work, None, stubbed_ids
    new_cut = ensure_last_user_in_tail(work, new_cut, prev_kept, keep)
    if new_cut is None or new_cut <= prev_kept:
        # #10896: group fit keep so the walk succeeded; user+group did not.
        # Helper only ran above when find_cut_index was None. Stub now, re-walk,
        # re-snap. pressure_stub fits last_user+group (not group alone).
        if stubbed_ids:
            return work, None, stubbed_ids
        work, stubbed_ids = pressure_stub_newest_tool_group(messages, keep, prev_kept)
        new_cut = find_cut_index(work, keep, start_index=prev_kept)
        if new_cut is None or new_cut <= prev_kept:
            return work, None, stubbed_ids
        new_cut = ensure_last_user_in_tail(work, new_cut, prev_kept, keep)
        if new_cut is None or new_cut <= prev_kept:
            return work, None, stubbed_ids
    return work, new_cut, stubbed_ids


def compact_session(
    session: Any,
    client: Any,
    *,
    window: int | None,
    tools: Any = None,
    max_tokens: int | None = None,
    stop_checker: Callable[[], bool] | None = None,
    force: bool = False,
    status_callback: Callable[[str], None] | None = None,
    enabled: bool | None = None,
) -> CompactResult:
    """May call LlmClient (blocking). Must run off the UI thread.

    Must already be inside ``llm_request_lane``; must not take the lane itself.
    Do not read ``client.config["chat_max_tokens"]`` — that key is not on
    ``LlmClient.config``. ``enabled=False`` returns ``disabled`` without I/O.
    """
    if enabled is None:
        enabled = get_config_bool_safe("chat_compaction_enabled")
    if not enabled:
        return CompactResult(False, "disabled")

    messages = session.messages
    if not messages:
        return CompactResult(False, "nothing_to_compact")

    view = messages_for_llm(session, tools)
    before = prompt_tokens(view, tools)
    if window is None or window <= 0:
        # No denominator → no remaining-budget clamp. Skip proactive *and* force.
        return CompactResult(False, "no_window", before, before)

    if not force and not should_compact(before, window, True):
        return CompactResult(False, "below_threshold", before, before)

    system_tokens = estimate_message_tokens(messages[0])
    tool_tokens = tool_schema_tokens(tools)
    keep = keep_recent_tokens(window, system_tokens, tool_tokens, force=force)
    if keep is None:
        return CompactResult(False, "nothing_to_compact", before, before)

    state = getattr(session, "compaction", None)
    prev_kept = state.first_kept_index if state is not None else 1
    _work, new_cut, stubbed_ids = _choose_cut(messages, keep, prev_kept)
    if new_cut is None or new_cut <= prev_kept:
        return CompactResult(False, "nothing_to_compact", before, before)

    # NEVER messages[0]; originals (stubs are view-only / serialize-only).
    slice_to_summarize = messages[prev_kept:new_cut]
    text = serialize_for_summary(slice_to_summarize)

    if status_callback:
        status_callback("Compacting conversation...")

    if stop_checker and stop_checker():
        return CompactResult(False, "aborted", before, before)

    temporal = _temporal_anchoring_rule()
    if state is not None and state.summary:
        user_body = (
            "<previous-summary>\n" + state.summary + "\n</previous-summary>\n\n"
            "<conversation>\n" + text + "\n</conversation>\n\n"
            + UPDATE_SUMMARIZATION_PROMPT + temporal
        )
    else:
        user_body = (
            "<conversation>\n" + text + "\n</conversation>\n\n"
            + SUMMARIZATION_PROMPT + temporal
        )

    budget = _summary_budget(window)
    max_out = budget
    if max_tokens is not None and max_tokens > 0:
        max_out = min(max_out, max_tokens)
    try:
        result = client.request_with_tools(
            [
                {"role": "system", "content": SUMMARIZATION_SYSTEM_PROMPT},
                {"role": "user", "content": user_body},
            ],
            max_tokens=max_out,
            tools=None,
            stream=False,
            stop_checker=stop_checker,
            prepend_dev_build_system_prefix=False,
        )
    except Exception:
        log.exception("Compaction failed")
        return CompactResult(False, "failed", before, before)

    if stop_checker and stop_checker():
        return CompactResult(False, "aborted", before, before)

    summary = (result or {}).get("content") or ""
    if not summary.strip():
        return CompactResult(False, "failed", before, before)
    summary = cap_summary(summary, summary_char_cap(window))
    # Pair (user summary + dummy ack) must itself fit in _summary_budget.
    while estimate_tokens(summary_pair(summary)) > budget and len(summary) > 32:
        summary = cap_summary(summary, max(32, len(summary) * 3 // 4))

    prev_state = state
    session.compaction = CompactionState(
        summary=summary,
        first_kept_index=new_cut,  # monotonic: new_cut > prev_kept
        tokens_before=before,
        window=window,
        stubbed_tool_call_ids=stubbed_ids,
    )
    after = prompt_tokens(messages_for_llm(session, tools), tools)
    reserve = gen_reserve(window)
    if after > window - reserve:
        session.compaction = prev_state  # revert; do not ship a too-large view
        return CompactResult(False, "failed", before, after)
    log.info(
        "Compaction ok window=%s tokens_before=%s tokens_after=%s cut=%s prev_kept=%s",
        window,
        before,
        after,
        new_cut,
        prev_kept,
    )
    if status_callback:
        status_callback("Thinking...")
    return CompactResult(True, "ok", before, after)

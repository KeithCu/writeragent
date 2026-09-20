"""Unit tests for plugin.chatbot.compaction (PR1; no soffice, no ChatSession)."""

from __future__ import annotations

import ast
import inspect
import math
import sys
import types
from pathlib import Path

import pytest

from plugin.chatbot import compaction as C


class DummySession:
    def __init__(self, messages):
        self.messages = messages
        self.compaction = None


class GuardConfig(dict):
    """Fails if compact_session reads chat_max_tokens off the client dict."""

    def __getitem__(self, key):
        if key == "chat_max_tokens":
            raise AssertionError("must not read client.config['chat_max_tokens']")
        return super().__getitem__(key)

    def get(self, key, default=None):
        if key == "chat_max_tokens":
            raise AssertionError("must not read client.config.get('chat_max_tokens')")
        return super().get(key, default)


class DummyClient:
    def __init__(
        self,
        *,
        provider="openrouter",
        endpoint="https://openrouter.ai/api/v1",
        model="openai/gpt-oss-120b",
        content="compacted summary",
        error=None,
    ):
        self.config = GuardConfig(model=model, endpoint=endpoint)
        self._provider = provider
        self._endpoint_url = endpoint
        self._content = content
        self._error = error
        self.calls = []

    def _get_provider(self):
        return self._provider

    def _endpoint(self):
        return self._endpoint_url

    def request_with_tools(self, messages, **kwargs):
        self.calls.append({"messages": messages, **kwargs})
        if self._error is not None:
            raise self._error
        return {"content": self._content}


def _ascii(n):
    """ASCII text that estimate_tokens_rough counts as exactly n tokens."""
    return "x" * (n * 4)


def _ascii_total(tokens, prefix="", suffix=""):
    """ASCII ``prefix+body+suffix`` whose rough token count is exactly ``tokens``."""
    body_len = tokens * 4 - len(prefix) - len(suffix)
    if body_len < 0:
        raise AssertionError("wrapper longer than token budget")
    return prefix + ("x" * body_len) + suffix


def _msg(role, tokens=None, text=None, **extra):
    body = text if text is not None else _ascii(tokens if tokens is not None else 1)
    out = {"role": role, "content": body}
    out.update(extra)
    return out


def _system_doc(tokens=2000):
    # Exact token count so keep_recent_tokens inside compact_session matches the plan tables.
    prefix = "base\n\n[DOCUMENT CONTENT]\n"
    suffix = "\n[END DOCUMENT]"
    return {"role": "system", "content": _ascii_total(tokens, prefix, suffix)}


def _tool(call_id, tokens=None, text=None, name="read_range"):
    return _msg("tool", tokens=tokens, text=text, tool_call_id=call_id, name=name)


def _assistant_tools(*calls):
    tool_calls = []
    for item in calls:
        cid, name, args = item if len(item) == 3 else (item[0], item[1], "{}")
        tool_calls.append({"id": cid, "type": "function", "function": {"name": name, "arguments": args}})
    return {"role": "assistant", "content": "", "tool_calls": tool_calls}


def _summary_for_pair_tokens(target):
    lo, hi = 1, target * 8
    found = "s"
    while lo <= hi:
        mid = (lo + hi) // 2
        cand = "s" * mid
        got = C.estimate_tokens(C.summary_pair(cand))
        if got == target:
            return cand
        if got < target:
            lo = mid + 1
            found = cand
        else:
            hi = mid - 1
    return found


def _history_3x1000(system_tokens=2000):
    return [
        _system_doc(system_tokens),
        _msg("user", 1000),
        _msg("assistant", 1000),
        _msg("user", 1000),
    ]


# --- 1. Threshold math (tiered) ------------------------------------------------


def test_compaction_ratio_tiers():
    assert C.compaction_ratio(4096) == 0.70
    assert C.compaction_ratio(8192) == 0.70
    assert C.compaction_ratio(8193) == 0.75
    assert C.compaction_ratio(131072) == 0.75
    assert C.compaction_ratio(511999) == 0.75
    assert C.compaction_ratio(512000) == 0.50


def test_should_compact_tiered_thresholds():
    assert C.should_compact(5733, 8192) is False
    assert C.should_compact(5734, 8192) is True
    assert int(8192 * 0.70) == 5734
    assert C.should_compact(98303, 131072) is False
    assert C.should_compact(98304, 131072) is True
    assert C.should_compact(255999, 512000) is False
    assert C.should_compact(256000, 512000) is True
    assert C.should_compact(100, None) is False
    assert C.should_compact(100, 0) is False
    assert C.should_compact(100, -1) is False
    assert C.should_compact(5734, 8192, enabled=False) is False


def test_compact_session_enabled_false_no_client_io():
    client = DummyClient()
    session = DummySession(_history_3x1000())
    result = C.compact_session(session, client, window=4096, enabled=False)
    assert result.reason == "disabled"
    assert result.compacted is False
    assert client.calls == []
    assert session.compaction is None


# --- 2. Fit guarantee ----------------------------------------------------------


def test_gen_reserve_and_keep_recent_pinned():
    assert C.gen_reserve(4096) == 256
    assert C.gen_reserve(8192) == 512
    assert C.keep_recent_tokens(4096, 2000, 0) == 1328
    assert C.keep_recent_tokens(4096, 2000, 0, force=True) == 664
    assert C.keep_recent_tokens(4096, 2000, 800) == 528
    assert C.keep_recent_tokens(4096, 3500, 0) is None
    assert C.keep_recent_tokens(8192, 2000, 0) == 2457
    assert C.keep_recent_tokens(131072, 2000, 0) == 20000


def test_fit_guarantee_ceiling_walk_4k():
    session = DummySession(_history_3x1000())
    client = DummyClient(content=_summary_for_pair_tokens(C._summary_budget(4096)))
    result = C.compact_session(session, client, window=4096, enabled=True)
    assert result.compacted is True
    view = C.messages_for_llm(session)
    after = C.prompt_tokens(view, None)
    assert after <= 4096 - C.gen_reserve(4096)
    tail = session.messages[session.compaction.first_kept_index :]
    assert C.estimate_tokens(tail) <= 1328
    assert C.estimate_tokens(tail) <= 2000  # floor-walk would keep ~2000


def test_exact_fill_of_window_minus_reserve_is_success():
    window = 4096
    reserve = C.gen_reserve(window)
    budget = C._summary_budget(window)
    keep = C.keep_recent_tokens(window, 2000, 0)
    assert keep == 1328
    summary = _summary_for_pair_tokens(budget)
    assert C.estimate_tokens(C.summary_pair(summary)) == budget
    session = DummySession(
        [
            _system_doc(2000),
            _msg("user", 1000),
            _msg("assistant", 1000),
            _msg("user", keep),
        ]
    )
    client = DummyClient(content=summary)
    result = C.compact_session(session, client, window=window, enabled=True)
    assert result.compacted is True
    assert result.reason == "ok"
    assert result.tokens_after == 2000 + budget + keep  # 3840
    assert result.tokens_after == window - reserve
    assert session.compaction is not None


def test_after_equals_window_is_not_success(monkeypatch):
    session = DummySession(_history_3x1000())
    client = DummyClient()
    real = C.prompt_tokens
    calls = {"n": 0}

    def _pt(messages, tools=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return real(messages, tools)
        return 4096

    monkeypatch.setattr(C, "prompt_tokens", _pt)
    prev = session.compaction
    result = C.compact_session(session, client, window=4096, enabled=True)
    assert result.reason == "failed"
    assert result.compacted is False
    assert result.tokens_after == 4096
    assert session.compaction is prev


def test_applied_view_over_reserve_reverts(monkeypatch):
    session = DummySession(_history_3x1000())
    client = DummyClient()
    real = C.prompt_tokens
    calls = {"n": 0}

    def _pt(messages, tools=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return real(messages, tools)
        return 4096 - C.gen_reserve(4096) + 1

    monkeypatch.setattr(C, "prompt_tokens", _pt)
    result = C.compact_session(session, client, window=4096, enabled=True)
    assert result.reason == "failed"
    assert session.compaction is None


# --- 3 / 21. Estimator ---------------------------------------------------------


def test_estimate_tokens_rough_ascii_cjk_cyrillic():
    assert C.estimate_tokens_rough("hello") == 2
    assert C.estimate_tokens_rough("你好世界") == 4
    cyr = "привет"
    cyr_tokens = C.estimate_tokens_rough(cyr)
    chars_over_4 = math.ceil(len(cyr) / 4)
    assert cyr_tokens > chars_over_4
    arabic = "مرحبا"
    assert C.estimate_tokens_rough(arabic) > math.ceil(len(arabic) / 4)
    # ASCII fast path: no encode needed; still correct.
    assert C.estimate_tokens_rough("abcd") == 1


# --- 4. Document exclusion -----------------------------------------------------


def test_document_content_never_in_summary_or_replaced_in_view():
    live = _system_doc(2000)
    live["content"] = "LIVE PROMPT\n\n[DOCUMENT CONTENT]\nsecret-doc\n[END DOCUMENT]"
    session = DummySession(
        [live, _msg("user", 1000), _msg("assistant", 1000), _msg("user", 1000)]
    )
    client = DummyClient()
    C.compact_session(session, client, window=4096, enabled=True)
    assert client.calls
    user_body = client.calls[0]["messages"][1]["content"]
    assert "[DOCUMENT CONTENT]" not in user_body
    assert "secret-doc" not in user_body
    view = C.messages_for_llm(session)
    assert view[0] is live or view[0]["content"] == live["content"]
    assert "LIVE PROMPT" in view[0]["content"]
    assert "[DOCUMENT CONTENT]" in view[0]["content"]
    # Defensive strip if a caller ever passed index 0.
    assert "secret-doc" not in C.serialize_for_summary([live])
    assert "[DOCUMENT CONTENT]" not in C.serialize_for_summary(session.messages[1:])


# --- 5. Tool-pair integrity ----------------------------------------------------


def test_find_cut_snaps_forward_off_tool_and_keeps_pairs():
    messages = [
        _system_doc(50),
        _msg("user", 20),
        _assistant_tools(("t1", "read_range"), ("t2", "write_range")),
        _tool("t1", 40),
        _tool("t2", 40),
        _msg("user", 20),
    ]
    # Land the ceiling walk on a role=tool (keep just the last user + last tool).
    last_user = C.estimate_message_tokens(messages[-1])
    last_tool = C.estimate_message_tokens(messages[-2])
    keep = last_user + last_tool
    cut = C.find_cut_index(messages, keep, start_index=1)
    assert cut is not None
    assert messages[cut]["role"] != "tool"
    # The assistant+both tools are together: either all in tail or all before cut.
    asst_i = 2
    if cut <= asst_i:
        assert cut <= asst_i
    else:
        assert cut >= 5


def test_sanitize_drops_orphan_tool_and_dangling_tool_calls():
    orphan = [_msg("user", 4), _tool("missing", 8)]
    cleaned = C.sanitize_tool_pairs(orphan)
    assert all(m.get("role") != "tool" for m in cleaned)

    dangling = [
        _msg("user", 4),
        {
            "role": "assistant",
            "content": "hello",
            "tool_calls": [{"id": "x", "type": "function", "function": {"name": "f", "arguments": "{}"}}],
        },
        _msg("user", 4),
    ]
    cleaned = C.sanitize_tool_pairs(dangling)
    asst = [m for m in cleaned if m.get("role") == "assistant"][0]
    assert "tool_calls" not in asst or not asst.get("tool_calls")
    assert asst["content"] == "hello"


def test_sanitize_drops_empty_name_tool_call_and_matching_result():
    """Stream-split phantom (empty name/id) must not stay paired into the next round."""
    messages = [
        _msg("user", 4),
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "good",
                    "type": "function",
                    "function": {"name": "read_cell_range", "arguments": '{"range":"A1"}'},
                },
                {
                    "id": "",
                    "type": "function",
                    "function": {"name": "", "arguments": '"}'},
                },
            ],
        },
        {"role": "tool", "tool_call_id": "good", "content": "ok"},
        {"role": "tool", "tool_call_id": "", "content": "UNKNOWN_TOOL"},
    ]
    cleaned = C.sanitize_tool_pairs(messages)
    asst = [m for m in cleaned if m.get("role") == "assistant"][0]
    assert len(asst["tool_calls"]) == 1
    assert asst["tool_calls"][0]["id"] == "good"
    assert asst["tool_calls"][0]["function"]["name"] == "read_cell_range"
    tools = [m for m in cleaned if m.get("role") == "tool"]
    assert len(tools) == 1
    assert tools[0]["tool_call_id"] == "good"


def test_last_user_snap_does_not_backward_align_into_tool_group():
    messages = [
        _system_doc(10),
        _msg("user", 10),
        _assistant_tools(("t1", "read_range")),
        _tool("t1", 10),
        _msg("user", 10),
    ]
    cut = 4  # already the last user
    assert C.ensure_last_user_in_tail(messages, cut, 1, 10_000) == 4


# --- 6. Image + audio ----------------------------------------------------------


def test_image_and_audio_charged_2000_not_base64_length():
    blob = "A" * 20000
    img = {
        "role": "user",
        "content": [
            {"type": "text", "text": "see"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + blob}},
        ],
    }
    wav_small = {
        "role": "user",
        "content": [{"type": "input_audio", "input_audio": {"data": "U" * 1000, "format": "wav"}}],
    }
    wav_big = {
        "role": "user",
        "content": [{"type": "input_audio", "input_audio": {"data": "U" * 80000, "format": "wav"}}],
    }
    assert C.estimate_message_tokens(img) == C.estimate_tokens_rough("see") + 2000
    assert C.estimate_message_tokens(wav_small) == 2000
    assert C.estimate_message_tokens(wav_big) == 2000
    ser = C.serialize_for_summary([img, wav_big])
    assert "image data omitted from summary input" in ser
    assert "[audio omitted]" in ser
    assert blob not in ser
    assert "U" * 100 not in ser


# --- 7. Failure leaves history intact ------------------------------------------


def test_failure_leaves_history_intact():
    messages = _history_3x1000()
    snapshot = [dict(m) for m in messages]
    session = DummySession(messages)
    client = DummyClient(error=RuntimeError("summarizer down"))
    result = C.compact_session(session, client, window=4096, enabled=True)
    assert result.reason == "failed"
    assert session.compaction is None
    assert session.messages == messages
    assert session.messages == snapshot


# --- 8. Overflow vs death ------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "prompt is too long",
        "request_too_large",
        "is longer than the model's context length",
        "reduce the length of the messages",
        "exceeds the maximum number of tokens allowed",
        "truncating input prompt",
        "maximum prompt length",
        "input is too long for requested model",
        "context window exceeds limit",
        "413 status code (no body)",
        "tokens in request more than max tokens allowed",
        "Prompt exceeds max length",
        "too large for model with 32000 maximum context length",
        "exceeds the limit of",
        "greater than the context length",
    ],
)
def test_is_context_overflow_error_true(text):
    assert C.is_context_overflow_error(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "llama-server process has terminated",
        "0xc0000005",
        "Rate limited (429)",
        "TPM",
        "too many requests",
    ],
)
def test_is_context_overflow_error_false(text):
    assert C.is_context_overflow_error(text) is False


def test_should_retry_overflow_reasons_and_attempts():
    assert C.should_retry_overflow(0, "ok") is True
    assert C.should_retry_overflow(1, "below_threshold") is True
    assert C.should_retry_overflow(2, "ok") is True
    assert C.should_retry_overflow(3, "ok") is False
    for reason in ("nothing_to_compact", "no_window", "failed", "aborted", "disabled"):
        assert C.should_retry_overflow(0, reason) is False


def test_compact_session_no_window_even_with_force():
    session = DummySession(_history_3x1000())
    client = DummyClient()
    result = C.compact_session(session, client, window=None, force=True, enabled=True)
    assert result.reason == "no_window"
    assert client.calls == []


# --- 9. Nothing to compact -----------------------------------------------------


def test_nothing_to_compact_system_plus_one_user():
    session = DummySession([_system_doc(2000), _msg("user", 100)])
    client = DummyClient()
    result = C.compact_session(session, client, window=4096, force=True, enabled=True)
    assert result.reason == "nothing_to_compact"
    assert C.find_cut_index(session.messages, 1328, start_index=1) is None


# --- 10. View does not mutate --------------------------------------------------


def test_view_does_not_mutate_session_messages():
    tool_body = _ascii(2500)
    messages = [
        _system_doc(2000),
        _msg("user", 1000),
        _msg("assistant", 1000),
        _msg("user", 40),
        _assistant_tools(("t1", "read_range")),
        _tool("t1", text=tool_body),
    ]
    session = DummySession(messages)
    snapshot = [dict(m) for m in messages]
    client = DummyClient()
    result = C.compact_session(session, client, window=4096, enabled=True)
    assert result.compacted is True
    assert len(session.messages) == len(snapshot)
    assert session.messages[-1]["content"] == tool_body
    view = C.messages_for_llm(session)
    dummy = [m for m in view if "[CONVERSATION SUMMARY]" in str(m.get("content"))]
    assert dummy
    assert all("[CONVERSATION SUMMARY]" not in str(m.get("content")) for m in session.messages)
    assert all("Acknowledged. I will continue" not in str(m.get("content")) for m in session.messages)


# --- 12 / 16. Window resolver --------------------------------------------------


def test_ollama_live_num_ctx_no_catalog_fallback(monkeypatch):
    seen = []

    def _query(endpoint, model_id):
        seen.append((endpoint, model_id))
        return 4096

    monkeypatch.setattr(C, "query_ollama_runtime_num_ctx", _query)
    client = DummyClient(provider="ollama", endpoint="http://localhost:11434", model="llama3")
    assert C.resolve_context_window(client) == 4096
    assert seen == [("http://localhost:11434", "llama3")]

    monkeypatch.setattr(C, "query_ollama_runtime_num_ctx", lambda *a, **k: None)
    assert C.resolve_context_window(client) is None  # not 256k, not catalog


def test_non_ollama_does_not_query_ollama(monkeypatch):
    monkeypatch.setattr(
        C,
        "query_ollama_runtime_num_ctx",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("/api/show must not run")),
    )
    client = DummyClient(provider="openrouter", model="openai/gpt-oss-120b")
    assert C.resolve_context_window(client) == 131072


def test_openrouter_nitro_equivalence_not_raw_ids():
    # Catalog stores openai/gpt-oss-120b:nitro; caller has the unsuffixed id.
    client = DummyClient(provider="openrouter", model="openai/gpt-oss-120b")
    assert C.resolve_context_window(client) == 131072
    unknown = DummyClient(provider="openrouter", model="totally-unknown/model")
    assert C.resolve_context_window(unknown) is None


def test_resolve_context_window_uses_client_config_model():
    client = DummyClient(provider="openrouter", model="openai/gpt-oss-120b")
    assert C.resolve_context_window(client) == 131072


def test_resolve_context_window_openrouter_free_catalog():
    """OpenRouter lists the free router at 200k (hop may be smaller)."""
    client = DummyClient(provider="openrouter", model="openrouter/free")
    assert C.resolve_context_window(client) == 200000


def test_resolve_context_window_writeragent_mock():
    """Mock soak id is not a hosted provider; catalog + any-id fallback."""
    client = DummyClient(
        provider="custom",
        model="writeragent-mock",
        endpoint="http://127.0.0.1:18766",
    )
    assert C.resolve_context_window(client) == 32768
    assert C.resolve_context_window(client) != 256000


def test_resolve_context_window_slash_lru_unknown_is_none():
    """Slash LRU ids are not in DEFAULT_MODELS — compact returns no_window, no retry."""
    client = DummyClient(
        provider="custom",
        model="mock-bravo",
        endpoint="http://127.0.0.1:18766",
    )
    assert C.resolve_context_window(client) is None


def _clear_v1_context_cache_keys(substr):
    import plugin.framework.client.model_fetcher as mf

    for store in (mf._model_fetch_cache, mf._model_fetch_image_cache, mf._model_context_cache):
        for key in [k for k in store if substr in k]:
            store.pop(key, None)


def test_resolve_context_window_prefers_cached_live_over_catalog():
    """Settings/sidebar harvest wins over stale DEFAULT_MODELS (Together MiniMax)."""
    from unittest.mock import patch

    from plugin.framework.client import model_fetcher as mf

    endpoint = "http://127.0.0.1:58921"
    _clear_v1_context_cache_keys("58921")
    payload = [{"id": "MiniMaxAI/MiniMax-M3", "context_length": 524288}]
    try:
        with patch("plugin.framework.client.requests.sync_request", return_value=payload):
            mf.fetch_available_models(endpoint)
        client = DummyClient(
            provider="together",
            endpoint=endpoint,
            model="MiniMaxAI/MiniMax-M3",
        )
        assert C.resolve_context_window(client) == 524288
    finally:
        _clear_v1_context_cache_keys("58921")


def test_resolve_context_window_openrouter_nitro_uses_cached_base():
    from unittest.mock import patch

    from plugin.framework.client import model_fetcher as mf

    endpoint = "http://127.0.0.1:58922"
    _clear_v1_context_cache_keys("58922")
    payload = {"data": [{"id": "openai/gpt-oss-120b", "context_length": 99999}]}
    try:
        with patch("plugin.framework.client.requests.sync_request", return_value=payload):
            mf.fetch_available_models(endpoint)
        client = DummyClient(
            provider="openrouter",
            endpoint=endpoint,
            model="openai/gpt-oss-120b:nitro",
        )
        assert C.resolve_context_window(client) == 99999
    finally:
        _clear_v1_context_cache_keys("58922")


def test_ollama_ignores_v1_context_cache(monkeypatch):
    from unittest.mock import patch

    from plugin.framework.client import model_fetcher as mf

    endpoint = "http://127.0.0.1:58924"
    _clear_v1_context_cache_keys("58924")
    payload = {"data": [{"id": "llama3", "context_length": 32768}]}
    try:
        with patch("plugin.framework.client.requests.sync_request", return_value=payload):
            mf.fetch_available_models(endpoint)
        monkeypatch.setattr(C, "query_ollama_runtime_num_ctx", lambda *a, **k: None)
        client = DummyClient(provider="ollama", endpoint=endpoint, model="llama3")
        assert C.resolve_context_window(client) is None
    finally:
        _clear_v1_context_cache_keys("58924")


def test_resolve_context_window_does_not_fetch_v1_models():
    from unittest.mock import patch

    client = DummyClient(
        provider="groq",
        endpoint="http://127.0.0.1:58925",
        model="not-in-catalog/model",
    )
    with patch("plugin.framework.client.requests.sync_request") as mock_sync:
        assert C.resolve_context_window(client) is None
        mock_sync.assert_not_called()


def test_compact_session_max_tokens_never_reads_chat_max_tokens():
    session = DummySession(_history_3x1000())
    client = DummyClient()
    result = C.compact_session(session, client, window=4096, max_tokens=512, enabled=True)
    assert result.reason in ("ok", "nothing_to_compact", "failed")
    if client.calls:
        assert client.calls[0]["max_tokens"] == 512
        assert client.calls[0]["stream"] is False
        assert client.calls[0]["tools"] is None
        assert client.calls[0]["prepend_dev_build_system_prefix"] is False


# --- 13. UPDATE path -----------------------------------------------------------


def test_update_path_slices_only_new_unsummarized_turns():
    session = DummySession(_history_3x1000())
    client = DummyClient(content="first-summary")
    r1 = C.compact_session(session, client, window=4096, enabled=True)
    assert r1.compacted is True
    k = session.compaction.first_kept_index
    first_body = client.calls[0]["messages"][1]["content"]
    assert "<previous-summary>" not in first_body
    session.messages.append(_msg("user", 900))
    session.messages.append(_msg("assistant", 900))
    session.messages.append(_msg("user", 400))
    client._content = "second-summary"
    r2 = C.compact_session(session, client, window=4096, force=True, enabled=True)
    assert r2.compacted is True
    assert session.compaction.first_kept_index > k
    update_body = client.calls[1]["messages"][1]["content"]
    assert "<previous-summary>" in update_body
    assert "first-summary" in update_body
    assert "[DOCUMENT CONTENT]" not in update_body
    # Already-summarized prefix is not re-sent: first user turn of the original
    # history lived at index 1 and was in the first slice.
    assert C.DOCUMENT_MARKERS[0] not in update_body


# --- 14. No panel / lane import ------------------------------------------------


def test_module_does_not_import_panel_or_lane():
    src = Path(C.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name != "uno"
                assert "panel" not in alias.name.split(".")
                assert "tool_loop" not in alias.name.split(".")
                assert alias.name != "llm_request_lane"
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            parts = mod.split(".")
            assert "panel" not in parts
            assert "tool_loop" not in parts
            assert "uno" not in parts
            assert "llm_request_lane" not in parts
            assert all(alias.name != "llm_request_lane" for alias in node.names)
        elif isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            assert name != "llm_request_lane"
    assert "llm_request_lane" not in inspect.signature(C.compact_session).parameters
    # Docstrings may mention the lane (must not take it). Do not import panel.
    assert "plugin.chatbot.panel" not in sys.modules or C.__name__ == "plugin.chatbot.compaction"


# --- 15. find_cut_index ceiling ------------------------------------------------


def test_find_cut_index_ceiling_and_oversized_last_user():
    messages = [
        _system_doc(10),
        _msg("user", 1000),
        _msg("assistant", 1000),
        _msg("user", 1000),
    ]
    cut = C.find_cut_index(messages, 1328, start_index=1)
    assert cut is not None
    assert C.estimate_tokens(messages[cut:]) <= 1328
    assert C.estimate_tokens(messages[cut:]) != 2000
    huge = [_system_doc(10), _msg("user", 2000)]
    assert C.find_cut_index(huge, 1328, start_index=1) is None


# --- 17. Last-user snap --------------------------------------------------------


def test_ensure_last_user_in_tail_snap_and_none():
    messages = [
        _system_doc(10),
        _msg("user", 100),
        _assistant_tools(("t1", "read_range")),
        _tool("t1", 200),
    ]
    # Cut at assistant; user+tail still fits.
    assert C.ensure_last_user_in_tail(messages, 2, 1, 500) == 1
    # Same snap would exceed keep.
    assert C.ensure_last_user_in_tail(messages, 2, 1, 250) is None
    # Already in tail.
    assert C.ensure_last_user_in_tail(messages, 1, 1, 500) == 1
    blank = [
        _system_doc(10),
        _msg("user", 40),
        _assistant_tools(("t1", "read_range")),
        _tool("t1", 20),
        _msg("user", text="   "),
    ]
    last = None
    for i in range(len(blank) - 1, 0, -1):
        if C._is_real_user(blank[i]):
            last = i
            break
    assert last == 1


# --- 18. Tool-stub serialize ---------------------------------------------------


def test_serialize_stubs_tool_bodies_over_200_and_formats_tool_calls():
    body_201 = "a" * 201
    body_200 = "b" * 200
    messages = [
        _assistant_tools(("c1", "read_file", '{"path":"/tmp/x"}')),
        _tool("c1", text=body_201),
        _tool("c2", text=body_200),
    ]
    # Name for c2 has no matching assistant; generic "tool".
    text = C.serialize_for_summary(messages)
    assert "[read_file] (201 chars)" in text
    assert body_201 not in text
    assert body_200 in text
    assert "ASSISTANT: read_file(" in text
    assert messages[1]["content"] == body_201  # session/input unchanged

    empty_asst = {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {"id": "z", "type": "function", "function": {"name": "read_file", "arguments": "x" * 5000}}
        ],
    }
    ser = C.serialize_for_summary([empty_asst])
    assert ser.startswith("ASSISTANT: ")
    assert ser != "ASSISTANT: "
    assert "read_file(" in ser
    assert "…" in ser
    assert len(ser) < 5000 + 50


# --- 19. Temporal anchoring ----------------------------------------------------


def test_temporal_anchoring_present_and_omitted_on_clock_failure(monkeypatch):
    session = DummySession(_history_3x1000())
    client = DummyClient()

    class _Date:
        @staticmethod
        def today():
            return types.SimpleNamespace(isoformat=lambda: "2026-09-10")

    monkeypatch.setattr(C.datetime, "date", _Date)
    C.compact_session(session, client, window=4096, enabled=True)
    body = client.calls[0]["messages"][1]["content"]
    assert "TEMPORAL ANCHORING" in body
    assert "2026-09-10" in body

    class _BadDate:
        @staticmethod
        def today():
            raise OSError("clock")

    session2 = DummySession(_history_3x1000())
    client2 = DummyClient()
    monkeypatch.setattr(C.datetime, "date", _BadDate)
    result = C.compact_session(session2, client2, window=4096, enabled=True)
    assert result.compacted is True
    body2 = client2.calls[0]["messages"][1]["content"]
    assert "TEMPORAL ANCHORING" not in body2


# --- 20. Negative 85% on 4k ----------------------------------------------------


def test_negative_85_percent_on_4k():
    assert C.compaction_ratio(4096) == 0.70
    assert C.compaction_ratio(4096) != 0.85
    hermes_85_trigger = int(4096 * 0.85)
    assert hermes_85_trigger == 3481
    assert 4096 - 3481 == 615  # too small for a tool round
    assert 4096 - int(4096 * 0.70) == 1229


# --- 22. 5% shrink gate --------------------------------------------------------


def test_five_percent_shrink_gate():
    # Hermes turn_overflow.compress_scored_by_tokens :203 is new_tokens < original * 0.95.
    # Exact 5% is not a shrink.
    assert C.should_retry_overflow(0, "ok", 1000, 950) is False
    assert C.should_retry_overflow(0, "ok", 1000, 949) is True
    assert C.should_retry_overflow(0, "ok", 1000, 960) is False
    assert C.should_retry_overflow(0, "ok", 1000, 951) is False


# --- 23. Tail-pressure stub (4k fat newest tool) --------------------------------


def test_tail_pressure_stub_fat_newest_tool():
    tool_body = _ascii(2500)
    last_user = _msg("user", 40)
    messages = [
        _system_doc(2000),
        _msg("user", 1000),
        _msg("assistant", 1000),
        last_user,
        _assistant_tools(("t1", "read_range")),
        _tool("t1", text=tool_body),
    ]
    session = DummySession(messages)
    client = DummyClient()
    result = C.compact_session(session, client, window=4096, enabled=True)
    assert result.compacted is True
    assert result.reason == "ok"
    assert result.tokens_after <= 4096 - C.gen_reserve(4096)
    assert session.messages[-1]["content"] == tool_body
    view = C.messages_for_llm(session)
    assert C.prompt_tokens(view, None) <= 3840
    # Last user stays in the tail; the fat tool is stubbed only in the view.
    tail = view[3:]  # after system + summary pair
    assert any(m.get("content") == last_user["content"] for m in tail)
    stubbed = [m for m in view if m.get("role") == "tool"]
    assert stubbed
    assert stubbed[0]["content"] != tool_body
    assert "chars)" in stubbed[0]["content"]
    assert session.compaction.stubbed_tool_call_ids


# --- 24. enabled=False ---------------------------------------------------------


def test_compact_session_enabled_false_alias():
    session = DummySession(_history_3x1000())
    client = DummyClient()
    result = C.compact_session(session, client, window=4096, force=True, enabled=False)
    assert result == C.CompactResult(False, "disabled")
    assert client.calls == []
    assert session.compaction is None


# --- 25. Last-user snap then tail-pressure (#10896) ----------------------------


def test_last_user_snap_then_tail_pressure():
    # group < keep 1328 so find_cut_index succeeds; user+group > keep so snap
    # would be None without stub. ~400 user + ~1000 tool.
    last_user = _msg("user", 400)
    tool_body = _ascii(1000)
    messages = [
        _system_doc(2000),
        _msg("user", 1000),
        _msg("assistant", 1000),
        last_user,
        _assistant_tools(("t1", "read_range")),
        _tool("t1", text=tool_body),
    ]
    keep = C.keep_recent_tokens(4096, 2000, 0)
    assert keep == 1328
    group = messages[-2:]
    assert C.estimate_tokens(group) < keep
    assert C.estimate_tokens([last_user] + group) > keep
    session = DummySession(messages)
    client = DummyClient()
    result = C.compact_session(session, client, window=4096, enabled=True)
    assert result.compacted is True, result
    assert result.tokens_after <= 3840
    assert session.messages[-1]["content"] == tool_body
    view = C.messages_for_llm(session)
    assert any(m.get("content") == last_user["content"] for m in view)
    view_tools = [m for m in view if m.get("role") == "tool"]
    assert view_tools and view_tools[0]["content"] != tool_body


# --- extras: status / abort / below_threshold ----------------------------------


def test_status_callback_only_when_summarizer_runs():
    statuses = []
    small = DummySession([_system_doc(100), _msg("user", 10)])
    client = DummyClient()
    C.compact_session(
        small, client, window=4096, enabled=True, status_callback=statuses.append
    )
    assert statuses == []

    session = DummySession(_history_3x1000())
    C.compact_session(
        session, client, window=4096, enabled=True, status_callback=statuses.append
    )
    assert statuses[0] == "Compacting conversation..."
    assert statuses[-1] == "Thinking..."


def test_stop_checker_aborts_without_mutating():
    session = DummySession(_history_3x1000())
    client = DummyClient()
    result = C.compact_session(
        session, client, window=4096, enabled=True, stop_checker=lambda: True
    )
    assert result.reason == "aborted"
    assert session.compaction is None
    assert client.calls == []


def test_below_threshold_skips_http():
    session = DummySession([_system_doc(100), _msg("user", 20), _msg("assistant", 20)])
    client = DummyClient()
    result = C.compact_session(session, client, window=4096, enabled=True)
    assert result.reason == "below_threshold"
    assert client.calls == []


def test_tool_schema_tokens_in_numerator():
    tools = [{"type": "function", "function": {"name": "x", "parameters": {"type": "object"}}}]
    msgs = [_msg("user", 4)]
    assert C.prompt_tokens(msgs, tools) == C.estimate_tokens(msgs) + C.tool_schema_tokens(tools)
    assert C.tool_schema_tokens(None) == 0
    assert C.tool_schema_tokens([]) == 0


# --- Coverage hardening & edge case tests --------------------------------------


def test_empty_or_whitespace_summary_fails():
    session1 = DummySession(_history_3x1000())
    client1 = DummyClient(content="")
    result1 = C.compact_session(session1, client1, window=4096, enabled=True)
    assert result1.reason == "failed"
    assert result1.compacted is False
    assert session1.compaction is None

    session2 = DummySession(_history_3x1000())
    client2 = DummyClient(content="   \n\t  ")
    result2 = C.compact_session(session2, client2, window=4096, enabled=True)
    assert result2.reason == "failed"
    assert result2.compacted is False
    assert session2.compaction is None


def test_summary_truncation_while_loop_and_cap_summary():
    # cap_summary unit checks
    assert C.cap_summary("short", 100) == "short"
    capped = C.cap_summary("a" * 200, 100)
    assert len(capped) <= 100
    assert capped.endswith(C._SUMMARY_TRUNCATION_MARKER)

    # Client returns an oversized summary (~2000 tokens) exceeding _summary_budget(4096)=512
    session = DummySession(_history_3x1000())
    client = DummyClient(content="Summary paragraph.\n" + ("detail info " * 600))
    result = C.compact_session(session, client, window=4096, enabled=True)
    assert result.compacted is True
    assert result.reason == "ok"
    assert session.compaction is not None
    assert C._SUMMARY_TRUNCATION_MARKER in session.compaction.summary
    assert C.estimate_tokens(C.summary_pair(session.compaction.summary)) <= C._summary_budget(4096)


def test_stop_checker_after_http_returns_aborts():
    session = DummySession(_history_3x1000())
    client = DummyClient(content="valid summary")
    calls = {"n": 0}

    def _checker():
        calls["n"] += 1
        # False on first call (preflight check), True on second call (after client.request_with_tools)
        return calls["n"] > 1

    result = C.compact_session(session, client, window=4096, enabled=True, stop_checker=_checker)
    assert result.reason == "aborted"
    assert result.compacted is False
    assert session.compaction is None
    assert len(client.calls) == 1


def test_multi_round_tool_sequence_cut():
    # User turn followed by sequential tool iterations (round 1 then round 2)
    messages = [
        _system_doc(50),
        _msg("user", 20),
        _assistant_tools(("t1", "tool_1")),
        _tool("t1", 30),
        _assistant_tools(("t2", "tool_2")),
        _tool("t2", 30),
        _msg("assistant", 20),
    ]
    # Ceiling walk trying to keep tail tokens
    keep = C.estimate_message_tokens(messages[-1]) + C.estimate_message_tokens(messages[-2])
    cut = C.find_cut_index(messages, keep, start_index=1)
    assert cut is not None
    # Must snap forward off tool result (cannot be at index 3 or 5)
    assert messages[cut]["role"] in ("user", "assistant")


def test_flatten_content_and_tool_name_fallbacks():
    assert C.flatten_content(None) == ("", 0, 0)
    assert C.flatten_content(12345) == ("", 0, 0)
    mixed_content = [
        {"type": "text", "text": "hello"},
        "stray_string",
        None,
        {"type": "unknown_type"},
        {"type": "text", "text": "world"},
    ]
    text, n_img, n_aud = C.flatten_content(mixed_content)
    assert text == "hello world"
    assert n_img == 0
    assert n_aud == 0

    # _tool_name_for fallback when no matching tool_call_id
    msgs = [_msg("user", 10), _tool("unmatched_id", 20)]
    assert C._tool_name_for(msgs, 1) == "tool"

    # _tool_name_for fallback when preceding assistant has no tool_calls
    msgs_no_tcs = [{"role": "assistant", "content": "no tools"}, _tool("t1", 20)]
    assert C._tool_name_for(msgs_no_tcs, 1) == "tool"


def test_llama_cpp_overflow_fallback_phrasing():
    assert C.is_context_overflow_error("error: llama.cpp context buffer overflow occurred") is True
    assert C.is_context_overflow_error("llama.cpp internal error: prompt overflow") is True


def test_newest_assistant_tool_span_empty_tool_calls_and_no_assistant():
    msgs_no_tcs = [_system_doc(10), _msg("user", 10), {"role": "assistant", "content": "plain text"}]
    assert C.newest_assistant_tool_span(msgs_no_tcs, 1) is None

    msgs_no_asst = [_system_doc(10), _msg("user", 10)]
    assert C.newest_assistant_tool_span(msgs_no_asst, 1) is None


def test_messages_for_llm_empty_returns_empty():
    assert C.messages_for_llm(DummySession([])) == []
    assert C.messages_for_llm(None) == []


def test_resolve_context_window_provider_exceptions():
    class ExceptionClient:
        config = {"model": "openai/gpt-oss-120b"}

        def _get_provider(self):
            raise RuntimeError("provider unavailable")

    # Should not crash on provider exception; falls back to catalog matching
    assert C.resolve_context_window(ExceptionClient()) == 131072

    class BadEndpointClient:
        config = {"model": "llama3"}

        def _get_provider(self):
            return "ollama"

        def _endpoint(self):
            raise RuntimeError("endpoint failed")

    assert C.resolve_context_window(BadEndpointClient()) is None


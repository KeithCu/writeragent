# WriterAgent - combined tests for web research functionality

import json
import os
from pathlib import Path
from typing import Any

from plugin.chatbot.web_research import (
    _norm_research_query,
    WebResearchToolCallingAgent,
)
from plugin.contrib.smolagents.memory import ToolCall
from plugin.contrib.smolagents.agents import ToolCallingAgent
from plugin.contrib.smolagents.models import (
    ChatMessage,
    MessageRole,
)
from unittest.mock import MagicMock, patch

import pytest

def _research_config_side_effect(cfg_int, *, cache_enabled=True, cache_max_mb=50):
    def side_effect(key: str):
        if key == "chatbot.web_research_cache_enabled":
            return cache_enabled
        if key == "chatbot.web_cache_max_mb":
            return cache_max_mb
        if key == "chatbot.web_research_browser":
            return "off"
        if key == "chatbot.prompt_for_web_research":
            return False
        if key == "chat_max_tokens":
            return 2048
        return cfg_int(key)
    return side_effect


def test_norm_research_query_collapses_whitespace_and_case():
    assert _norm_research_query("  Best  Pizza \n") == _norm_research_query("best pizza")
    assert _norm_research_query("") == ""


def test_cache_hit_does_not_stream_chat_append_callback():
    """Cache notice is shown once via tool result / delegate line, not chat_append during execute."""
    from plugin.chatbot.web_research import WebResearchTool
    from plugin.tests.testing_utils import MockContext
    from plugin.contrib.smolagents.default_tools import _web_cache_set

    ctx = MagicMock()
    ctx.ctx = MockContext()
    chat_lines: list[str] = []
    ctx.chat_append_callback = chat_lines.append

    import tempfile
    with tempfile.TemporaryDirectory() as td:
        db_file = os.path.join(td, "writeragent_web_cache.db")
        _web_cache_set(db_file, "research", "caching unique", "Cached Answer Content", 50 * 1024 * 1024)

        def _cfg_int(key):
            if key == "chatbot.web_cache_validity_days":
                return 30
            if key == "chatbot.web_research_cache_jaccard_percent":
                return 40
            if key == "chatbot.web_research_cache_min_overlap":
                return 8
            return 50

        with patch("plugin.framework.config.user_config_dir", return_value=td), \
             patch("plugin.framework.config.get_config", side_effect=_research_config_side_effect(_cfg_int)), \
             patch("plugin.framework.client.response_normalizers.should_prepend_dev_llm_system_prefix", return_value=False), \
             patch("plugin.chatbot.web_research_cache.resolve_research_locale", return_value=("en_US", "english")):
            tool = WebResearchTool()
            res = tool.execute(ctx, query="Search for caching test unique info")
            assert res.get("research_cache_event") == "hit"
            assert chat_lines == []

# =============================================================================
# Web research query override tests (from test_web_research_query_override.py)
# =============================================================================


def test_web_search_query_from_dict():
    captured: list[str] = []
    step = ToolCall(name="web_search", arguments={"query": "hello"}, id="t1")
    _run_web_search_tool_call_handler(
        step=step,
        approval_callback=lambda q, tool, args: (captured.append(q), (True, None))[1],
    )
    assert captured == ["hello"]

    captured.clear()
    step_empty = ToolCall(name="web_search", arguments={}, id="t1")
    _run_web_search_tool_call_handler(
        step=step_empty,
        approval_callback=lambda q, tool, args: (captured.append(q), (True, None))[1],
    )
    assert captured == [""]


def test_web_search_query_from_json_string():
    captured: list[str] = []
    step = ToolCall(name="web_search", arguments='{"query": "from json"}', id="t1")
    _run_web_search_tool_call_handler(
        step=step,
        approval_callback=lambda q, tool, args: (captured.append(q), (True, None))[1],
    )
    assert captured == ["from json"]


def test_web_search_query_from_invalid_string():
    captured: list[str] = []
    step = ToolCall(name="web_search", arguments="not json", id="t1")
    _run_web_search_tool_call_handler(
        step=step,
        approval_callback=lambda q, tool, args: (captured.append(q), (True, None))[1],
    )
    assert captured == [""]

    captured.clear()
    step_none = ToolCall(name="web_search", arguments=None, id="t1")
    _run_web_search_tool_call_handler(
        step=step_none,
        approval_callback=lambda q, tool, args: (captured.append(q), (True, None))[1],
    )
    assert captured == [""]


def test_apply_override_dict_mutable():
    original = {"query": "old", "x": 1}
    step = ToolCall(name="web_search", arguments=original, id="t1")
    _run_web_search_tool_call_handler(
        step=step,
        approval_callback=lambda q, tool, args: (True, "new"),
    )
    assert step.arguments is original
    assert step.arguments == {"query": "new", "x": 1}


@pytest.mark.parametrize(
    "arguments, value, value_2",
    [
        pytest.param('{"query": "old"}', "edited", "edited", id="test_apply_override_json_string"),
        pytest.param("<<<", "only-override", "only-override", id="test_apply_override_invalid_json_string"),
    ],
)
def test_apply_override_json_string(arguments, value, value_2):
    step = ToolCall(name="web_search", arguments=arguments, id="t1")
    _run_web_search_tool_call_handler(
        step=step,
        approval_callback=lambda q, tool, args: (True, value),
    )
    assert step.arguments == {"query": value_2}


def test_apply_override_json_string_keeps_recency():
    step = ToolCall(name="web_search", arguments='{"query": "old", "recency": "week"}', id="t1")
    _run_web_search_tool_call_handler(
        step=step,
        approval_callback=lambda q, tool, args: (True, "edited"),
    )
    assert step.arguments == {"query": "edited", "recency": "week"}


def test_apply_override_non_dict_non_string():
    step = ToolCall(name="web_search", arguments=["list"], id="t1")
    _run_web_search_tool_call_handler(
        step=step,
        approval_callback=lambda q, tool, args: (True, "fallback"),
    )
    assert step.arguments == {"query": "fallback"}


# =============================================================================
# Web search approval chat preview tests
# =============================================================================


def _run_web_search_tool_call_handler(
    *,
    outer_query: str = "user query",
    search_query: str = "engine query",
    approval_callback=None,
    chat_lines: list[str] | None = None,
    step: Any = None,
):
    """Invoke only the web_search branch of _run_web_agent's tool_call_handler."""
    from plugin.chatbot.web_research import WebAgentRunParams, _run_web_agent

    captured_chat = chat_lines if chat_lines is not None else []
    handler_results: list[Any] = []

    def _chat_append(text: str) -> None:
        captured_chat.append(text)

    params = WebAgentRunParams(
        smol_model=MagicMock(),
        max_steps=5,
        cache_path=None,
        cache_max_mb=0,
        cache_max_age_days=30,
        browser=None,
        status_callback=None,
        append_thinking_callback=None,
        approval_callback=approval_callback,
        chat_append_callback=_chat_append,
        outer_query=outer_query,
    )

    def fake_execute_safe(agent, task, tool_call_handler=None, **kwargs):
        if tool_call_handler:
            call_step = step if step is not None else ToolCall(name="web_search", arguments={"query": search_query}, id="t1")
            handler_results.append(tool_call_handler(call_step))
        return "done"

    with patch("plugin.chatbot.smol_agent.SmolAgentExecutor") as mock_exe_cls, \
         patch("plugin.chatbot.web_research.WebResearchToolCallingAgent"), \
         patch("plugin.chatbot.smol_examples.get_examples_block", return_value=""), \
         patch("plugin.contrib.smolagents.default_tools.DuckDuckGoSearchTool"), \
         patch("plugin.contrib.smolagents.default_tools.VisitWebpageTool"):
        mock_exe_cls.return_value.execute_safe.side_effect = fake_execute_safe
        _run_web_agent(MagicMock(), outer_query, None, params)

    return captured_chat, handler_results


def test_web_search_preview_shown_before_approval_returns():
    chat_lines: list[str] = []
    approval_called_after_chat: list[bool] = []

    def approval_cb(query_for_engine, tool, args):
        approval_called_after_chat.append(len(chat_lines) > 0 and query_for_engine in chat_lines[0])
        return True, None

    chat_lines, _ = _run_web_search_tool_call_handler(
        outer_query="What is Python?",
        search_query="latest Python release",
        approval_callback=approval_cb,
        chat_lines=chat_lines,
    )
    assert approval_called_after_chat == [True]
    assert len(chat_lines) == 1
    assert "latest Python release" in chat_lines[0]
    assert "Tool: web_search" in chat_lines[0]


def test_web_search_reject_keeps_preview_in_chat():
    chat_lines: list[str] = []

    def approval_cb(query_for_engine, tool, args):
        return False, None

    chat_lines, results = _run_web_search_tool_call_handler(
        search_query="blocked query",
        approval_callback=approval_cb,
        chat_lines=chat_lines,
    )
    assert len(chat_lines) == 1
    assert "blocked query" in chat_lines[0]
    assert results and results[0].get("code") == "USER_STOPPED"


def test_web_search_change_appends_second_preview():
    chat_lines: list[str] = []

    def approval_cb(query_for_engine, tool, args):
        return True, "edited query"

    chat_lines, _ = _run_web_search_tool_call_handler(
        search_query="original query",
        approval_callback=approval_cb,
        chat_lines=chat_lines,
    )
    assert len(chat_lines) == 2
    assert "original query" in chat_lines[0]
    assert "edited query" in chat_lines[1]


def test_web_search_no_prompt_dedup_when_matches_outer_query():
    chat_lines, _ = _run_web_search_tool_call_handler(
        outer_query="best pizza madison",
        search_query="best pizza madison",
        chat_lines=[],
    )
    assert chat_lines == []


def test_visit_handler_leaves_page_store_empty():
    from plugin.chatbot.web_research import WebAgentRunParams, _run_web_agent
    from plugin.contrib.smolagents.memory import ToolCall

    params = WebAgentRunParams(
        smol_model=MagicMock(),
        max_steps=5,
        cache_path=None,
        cache_max_mb=0,
        cache_max_age_days=30,
        browser=None,
        status_callback=None,
        append_thinking_callback=None,
        approval_callback=None,
        chat_append_callback=None,
        outer_query="topic",
    )

    def fake_execute_safe(agent, task, tool_call_handler=None, **kwargs):
        step = ToolCall(name="visit_webpage", arguments={"urls": ["https://example.com/a/"]}, id="v1")
        assert tool_call_handler is not None
        assert tool_call_handler(step) is None
        return "done"

    with patch("plugin.chatbot.smol_agent.SmolAgentExecutor") as mock_exe_cls, \
         patch("plugin.chatbot.web_research.WebResearchToolCallingAgent"), \
         patch("plugin.chatbot.smol_examples.get_examples_block", return_value=""), \
         patch("plugin.contrib.smolagents.default_tools.DuckDuckGoSearchTool"), \
         patch("plugin.contrib.smolagents.default_tools.VisitWebpageTool"):
        mock_exe_cls.return_value.execute_safe.side_effect = fake_execute_safe
        _run_web_agent(MagicMock(), "topic", None, params)

    assert params.pages.get("https://example.com/a") is None


def test_web_research_agent_accepts_urls_array_when_parallel_flag_off(monkeypatch):
    from plugin.chatbot import web_pages
    from plugin.chatbot.web_pages import PageStore, PagedVisitTool
    from plugin.chatbot.web_research import WebResearchToolCallingAgent

    monkeypatch.setattr(web_pages, "PARALLEL_WEB_FETCH", False)

    inner = MagicMock()
    inner.forward.side_effect = lambda u: f"Body for {u}"
    store = PageStore()
    visit_tool = PagedVisitTool(inner, store)
    assert list(visit_tool.inputs) == ["url"]

    agent = WebResearchToolCallingAgent(tools=[visit_tool], model=MagicMock())

    single_out = agent.execute_tool_call("visit_webpage", {"url": "https://single.test/1"})
    assert "Body for https://single.test/1" in single_out
    assert store.get("https://single.test/1") is not None

    multi_out = agent.execute_tool_call("visit_webpage", {"urls": ["https://a.test/1", "https://b.test/2"]})
    assert "Body for https://a.test/1" in multi_out
    assert "Body for https://b.test/2" in multi_out
    assert store.get("https://a.test/1") is not None
    assert store.get("https://b.test/2") is not None

    agent.state["target_urls"] = ["https://state.test/1"]
    state_out = agent.execute_tool_call("visit_webpage", {"urls": "target_urls"})
    assert "Body for https://state.test/1" in state_out

    from plugin.contrib.smolagents.utils import AgentToolExecutionError

    store.put = MagicMock(side_effect=RuntimeError("boom"))
    with pytest.raises(AgentToolExecutionError):
        agent.execute_tool_call("visit_webpage", {"urls": ["https://err.test/1"]})


@pytest.mark.parametrize(
    ("url", "expected"),
    [("https://example.com/a/", "Read: example.com..."), ("https://www.bbc.co.uk/news", "Read: bbc.co.uk...")],
)
def test_web_research_single_url_status_callback(monkeypatch, url, expected):
    from plugin.chatbot import web_pages
    from plugin.chatbot.web_research import WebAgentRunParams, _run_web_agent
    from plugin.contrib.smolagents.memory import ToolCall

    monkeypatch.setattr(web_pages, "PARALLEL_WEB_FETCH", False)
    statuses: list[str] = []
    params = WebAgentRunParams(
        smol_model=MagicMock(),
        max_steps=4,
        cancel=None,
        cache_path="",
        cache_max_mb=0,
        cache_max_age_days=30,
        browser=None,
        status_callback=statuses.append,
        append_thinking_callback=None,
        approval_callback=None,
        chat_append_callback=None,
        outer_query="topic",
    )

    def fake_execute_safe(agent, task, tool_call_handler=None, **kwargs):
        step = ToolCall(name="visit_webpage", arguments={"url": url}, id="v1")
        assert tool_call_handler is not None
        assert tool_call_handler(step) is None
        return "done"

    with patch("plugin.chatbot.smol_agent.SmolAgentExecutor") as mock_exe_cls, \
         patch("plugin.chatbot.web_research.WebResearchToolCallingAgent"), \
         patch("plugin.chatbot.smol_examples.get_examples_block", return_value=""), \
         patch("plugin.contrib.smolagents.default_tools.DuckDuckGoSearchTool"), \
         patch("plugin.contrib.smolagents.default_tools.VisitWebpageTool"):
        mock_exe_cls.return_value.execute_safe.side_effect = fake_execute_safe
        _run_web_agent(MagicMock(), "topic", None, params)

    assert expected in statuses


def test_find_all_pages_status_is_not_blank():
    from plugin.chatbot.web_research import WebAgentRunParams, _run_web_agent
    from plugin.contrib.smolagents.memory import ToolCall

    statuses: list[str] = []
    params = WebAgentRunParams(
        smol_model=MagicMock(),
        max_steps=4,
        cancel=None,
        cache_path="",
        cache_max_mb=0,
        cache_max_age_days=30,
        browser=None,
        status_callback=statuses.append,
        append_thinking_callback=None,
        approval_callback=None,
        chat_append_callback=None,
        outer_query="topic",
    )

    def fake_execute_safe(agent, task, tool_call_handler=None, **kwargs):
        assert tool_call_handler is not None
        for idx, args in enumerate(
            [
                {"text": "x"},
                {"text": "x", "url": None},
                {"text": "x", "url": "  "},
                {"text": "x", "url": "https://example.com/a"},
            ]
        ):
            assert tool_call_handler(ToolCall(name="find_in_page", arguments=args, id=f"f{idx}")) is None
        return "done"

    with patch("plugin.chatbot.smol_agent.SmolAgentExecutor") as mock_exe_cls, \
         patch("plugin.chatbot.web_research.WebResearchToolCallingAgent"), \
         patch("plugin.chatbot.smol_examples.get_examples_block", return_value=""), \
         patch("plugin.contrib.smolagents.default_tools.DuckDuckGoSearchTool"), \
         patch("plugin.contrib.smolagents.default_tools.VisitWebpageTool"):
        mock_exe_cls.return_value.execute_safe.side_effect = fake_execute_safe
        _run_web_agent(MagicMock(), "topic", None, params)

    assert len(statuses) == 4
    assert statuses[0] == statuses[1] == statuses[2]
    assert statuses[0] != "Read: ..."
    assert statuses[3] == "Read: example.com..."


def test_cdp_visit_uses_a_private_target(monkeypatch):

    from plugin.chatbot.web_research import CdpPageDriver, VisitWebpageBrowserTool

    calls: list[tuple[str, str | None]] = []
    ids = iter(["tab-a", "tab-b"])

    def fake_cdp(method, params=None, target_id=None, **kwargs):
        calls.append((method, target_id if method != "Target.closeTarget" else (params or {}).get("targetId")))
        if method == "Target.createTarget":
            return json.dumps({"success": True, "result": {"targetId": next(ids)}})
        if method in ("Page.navigate", "Runtime.evaluate", "Target.closeTarget"):
            if method == "Runtime.evaluate":
                return json.dumps({"success": True, "result": {"result": {"value": "body text"}}})
            return json.dumps({"success": True})
        raise AssertionError(method)

    monkeypatch.setattr("plugin.contrib.cdp.browser_cdp_tool.browser_cdp", fake_cdp)
    monkeypatch.setattr("time.sleep", lambda _seconds: None)
    assert "body text" in VisitWebpageBrowserTool(CdpPageDriver(), stop_checker=None).forward("https://a.test")
    assert "body text" in VisitWebpageBrowserTool(CdpPageDriver(), stop_checker=None).forward("https://b.test")
    assert [method for method, _target in calls if method == "Target.getTargets"] == []
    assert [target for method, target in calls if method == "Page.navigate"] == ["tab-a", "tab-b"]
    assert [target for method, target in calls if method == "Target.closeTarget"] == ["tab-a", "tab-b"]


@pytest.fixture
def _isolated_browser_refcount(monkeypatch, tmp_path):
    from plugin.chatbot import web_research as wr

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    wr._browser_refcount.reset_for_tests()
    try:
        yield tmp_path
    finally:
        wr._browser_refcount.reset_for_tests()


def test_cdp_cleanup_waits_until_visit_leaves(_isolated_browser_refcount):
    import threading

    from plugin.chatbot import web_research as wr

    wr._browser_refcount.run_enter("chrome")
    started = threading.Event()
    release = threading.Event()
    cleaned = threading.Event()

    def visit() -> None:
        assert wr._browser_refcount.visit_enter()
        started.set()
        assert release.wait(2)
        wr._browser_refcount.visit_leave()

    thread = threading.Thread(target=visit)
    finisher = None
    try:
        thread.start()
        assert started.wait(1)
        with patch("plugin.contrib.cdp.browser_cdp_tool.cleanup_local_browser", side_effect=cleaned.set):
            finisher = threading.Thread(target=wr._browser_refcount.finish)
            finisher.start()
            assert not cleaned.wait(0.15)
            release.set()
            finisher.join(2)
            thread.join(2)
        assert cleaned.is_set()
        assert wr._browser_refcount.closing is False
        assert wr._browser_refcount.visits_inflight == 0
    finally:
        release.set()
        thread.join(1)
        if finisher is not None:
            finisher.join(1)


def test_one_research_run_finishing_leaves_the_shared_browser(_isolated_browser_refcount):
    import threading

    from plugin.chatbot import web_research as wr

    started = threading.Event()
    release = threading.Event()
    cleaned = threading.Event()

    def visit() -> None:
        assert wr._browser_refcount.visit_enter()
        started.set()
        assert release.wait(2)
        wr._browser_refcount.visit_leave()

    thread = threading.Thread(target=visit)
    finisher = None
    try:
        wr._browser_refcount.run_enter("chrome")
        wr._browser_refcount.run_enter("chrome")
        thread.start()
        assert started.wait(1)
        with patch("plugin.contrib.cdp.browser_cdp_tool.cleanup_local_browser", side_effect=cleaned.set):
            # The other run still holds the browser, so this finish must not
            # wait for the in-flight visit or kill the browser.
            wr._browser_refcount.finish()
            assert not cleaned.is_set()
            assert wr._browser_refcount.closing is False
            assert wr._browser_refcount.visit_enter()
            wr._browser_refcount.visit_leave()
            finisher = threading.Thread(target=wr._browser_refcount.finish)
            finisher.start()
            assert not cleaned.wait(0.15)
            release.set()
            finisher.join(2)
            thread.join(2)
        assert cleaned.is_set()
        assert wr._browser_refcount.closing is False
        assert wr._browser_refcount.runs == 0
        assert wr._browser_refcount.visits_inflight == 0
    finally:
        release.set()
        thread.join(1)
        if finisher is not None:
            finisher.join(1)


def test_cdp_launch_failure_releases_the_run(monkeypatch, _isolated_browser_refcount):
    from plugin.chatbot import web_research as wr

    mode = {"fail": True}
    launched: list[str] = []

    def fake_url(browser_type: str) -> str:
        launched.append(browser_type)
        if mode["fail"]:
            raise RuntimeError("no browser")
        return "ws://shared"

    monkeypatch.setattr("plugin.contrib.cdp.browser_cdp_tool.get_local_cdp_url", fake_url)
    with patch("plugin.contrib.cdp.browser_cdp_tool.cleanup_local_browser") as cleanup:
        with pytest.raises(RuntimeError, match="no browser"):
            wr._begin_shared_browser("chrome")
        # The only run failed to launch. Teardown still runs, and the count is released.
        cleanup.assert_called_once()
    assert wr._browser_refcount.runs == 0

    mode["fail"] = False
    with patch("plugin.contrib.cdp.browser_cdp_tool.cleanup_local_browser") as cleanup:
        assert isinstance(wr._begin_shared_browser("chrome"), wr.CdpPageDriver)
        mode["fail"] = True
        with pytest.raises(RuntimeError, match="no browser"):
            wr._begin_shared_browser("chrome")
        # The first run still holds the browser, so the failed launch must not kill it.
        cleanup.assert_not_called()
        assert wr._browser_refcount.runs == 1
        wr._browser_refcount.finish()
        cleanup.assert_called_once()
    assert wr._browser_refcount.runs == 0
    assert wr._browser_refcount.closing is False
    assert launched == ["chrome", "chrome", "chrome"]


def test_firefox_begin_dispatches_to_bidi_and_finish_closes_it(_isolated_browser_refcount):
    from plugin.chatbot import web_research as wr

    fake_client = MagicMock()
    with (
        patch("plugin.chatbot.web_research_firefox.begin_firefox_bidi", return_value=fake_client) as mock_begin,
        patch("plugin.chatbot.web_research_firefox.close_firefox_bidi") as mock_close_bidi,
        patch("plugin.contrib.cdp.browser_cdp_tool.cleanup_local_browser") as mock_cleanup,
    ):
        assert wr._begin_shared_browser("firefox") is fake_client
        mock_begin.assert_called_once_with()
        wr._browser_refcount.finish()
        mock_close_bidi.assert_called_once_with()
        mock_cleanup.assert_called_once_with()


@pytest.mark.parametrize("browser_type", ["chrome", "firefox"])
def test_second_process_is_refused_while_profile_is_held(browser_type, _isolated_browser_refcount):
    from plugin.chatbot import web_research as wr

    with (
        patch("plugin.contrib.cdp.browser_cdp_tool.get_local_cdp_url", return_value="ws://shared") as mock_cdp,
        patch("plugin.chatbot.web_research_firefox.begin_firefox_bidi", return_value=MagicMock()) as mock_bidi,
        patch("plugin.contrib.cdp.browser_cdp_tool.cleanup_local_browser") as mock_cleanup,
    ):
        with wr._profile_lock(browser_type):
            with pytest.raises(RuntimeError, match=f"the {browser_type} research profile is in use"):
                wr._begin_shared_browser(browser_type)
            mock_cdp.assert_not_called()
            mock_bidi.assert_not_called()
            mock_cleanup.assert_not_called()
            assert wr._browser_refcount.runs == 0

        wr._begin_shared_browser(browser_type)
        assert wr._browser_refcount.runs == 1
        wr._browser_refcount.finish()


def test_profile_lock_is_held_until_the_browser_is_closed(_isolated_browser_refcount):
    from plugin.chatbot import web_research as wr

    wr._browser_refcount.run_enter("chrome")
    wr._browser_refcount.run_enter("chrome")
    with pytest.raises(RuntimeError, match="the chrome research profile is in use"):
        with wr._profile_lock("chrome"):
            pass

    wr._browser_refcount.finish()
    with pytest.raises(RuntimeError, match="the chrome research profile is in use"):
        with wr._profile_lock("chrome"):
            pass

    checked_during_cleanup = False

    def check_locked_during_cleanup():
        nonlocal checked_during_cleanup
        with pytest.raises(RuntimeError, match="the chrome research profile is in use"):
            with wr._profile_lock("chrome"):
                pass
        checked_during_cleanup = True

    with patch("plugin.contrib.cdp.browser_cdp_tool.cleanup_local_browser", side_effect=check_locked_during_cleanup):
        wr._browser_refcount.finish()
    assert checked_during_cleanup is True

    with wr._profile_lock("chrome"):
        pass

    with (
        patch("plugin.contrib.cdp.browser_cdp_tool.get_local_cdp_url", side_effect=RuntimeError("boom")),
        patch("plugin.contrib.cdp.browser_cdp_tool.cleanup_local_browser"),
    ):
        with pytest.raises(RuntimeError, match="boom"):
            wr._begin_shared_browser("chrome")
    with wr._profile_lock("chrome"):
        pass


def test_other_browser_type_refused_while_one_is_in_use(_isolated_browser_refcount):
    from plugin.chatbot import web_research as wr
    from plugin.chatbot import web_research_firefox as wrf
    from plugin.contrib.cdp import browser_cdp_tool

    fake_client = MagicMock()
    with (
        patch("plugin.contrib.cdp.browser_cdp_tool.get_local_cdp_url", return_value="ws://shared") as mock_cdp,
        patch("plugin.chatbot.web_research_firefox.begin_firefox_bidi", return_value=fake_client) as mock_bidi,
        patch("plugin.chatbot.web_research_firefox.close_firefox_bidi") as mock_close_bidi,
        patch("plugin.contrib.cdp.browser_cdp_tool.cleanup_local_browser") as mock_cleanup,
    ):
        assert isinstance(wr._begin_shared_browser("chrome"), wr.CdpPageDriver)
        for other in ("brave", "firefox"):
            with pytest.raises(RuntimeError, match="another local browser is already in use for web research \\(chrome\\)"):
                wr._begin_shared_browser(other)
        mock_cdp.assert_called_once_with("chrome")
        mock_bidi.assert_not_called()

        wr._browser_refcount.finish()
        assert wr._begin_shared_browser("firefox") is fake_client
        wr._browser_refcount.finish()

        # Dead Firefox client held with a live local handle: chrome is refused and does not kill it.
        mock_cleanup.reset_mock()
        mock_close_bidi.reset_mock()
        closed_client = MagicMock()
        closed_client.closed = True
        live_handle = MagicMock()
        live_handle.is_alive.return_value = True
        wrf._CLIENT = closed_client
        browser_cdp_tool._LOCAL_BROWSER = live_handle
        try:
            wr._browser_refcount.run_enter("firefox")
            with pytest.raises(RuntimeError, match="another local browser is already in use for web research \\(firefox\\)"):
                wr._begin_shared_browser("chrome")
            live_handle.kill_tree.assert_not_called()
            mock_cleanup.assert_not_called()
            mock_close_bidi.assert_not_called()
            wr._browser_refcount.finish()
        finally:
            wrf._CLIENT = None
            browser_cdp_tool._LOCAL_BROWSER = None


@pytest.mark.parametrize(
    "exc, expect_warning",
    [
        (ImportError("No module named 'websockets'"), True),
        (RuntimeError("Firefox refused"), False),
    ],
)
def test_firefox_import_error_warns_chat_while_runtime_error_is_silent(exc, expect_warning):
    from plugin.chatbot.web_research import WebResearchTool
    from plugin.tests.testing_utils import MockContext

    ctx = MagicMock()
    ctx.ctx = MockContext()
    ctx.doc = None
    ctx.doc_type = "writer"
    ctx.status_callback = None
    ctx.append_thinking_callback = None
    ctx.approval_callback = None
    chat_lines: list[str] = []
    ctx.chat_append_callback = chat_lines.append
    ctx.send_cancellation = None

    def _cfg(key: str):
        if key == "chatbot.web_research_browser":
            return "firefox"
        if key == "chatbot.max_tool_rounds":
            return 2
        if key == "chat_max_tokens":
            return 2048
        return 0

    with (
        patch("plugin.framework.config.get_config", side_effect=_cfg),
        patch("plugin.framework.config.get_api_config", return_value={}),
        patch("plugin.chatbot.web_research_cache.resolve_research_locale", return_value=("en_US", "english")),
        patch("plugin.chatbot.web_research.resolve_context_window", return_value=32_000),
        patch("plugin.chatbot.web_research._begin_shared_browser", side_effect=exc),
        patch("plugin.chatbot.web_research._run_web_agent", return_value="ok"),
    ):
        WebResearchTool().execute(ctx, query="test")

    if expect_warning:
        assert len(chat_lines) == 1
        assert "make vendor" in chat_lines[0]
    else:
        assert chat_lines == []


def test_web_search_prompt_shows_preview_even_when_matches_outer_query():
    chat_lines, _ = _run_web_search_tool_call_handler(
        outer_query="best pizza madison",
        search_query="best pizza madison",
        approval_callback=lambda q, tool, args: (True, None),
        chat_lines=[],
    )
    assert len(chat_lines) == 1
    assert "best pizza madison" in chat_lines[0]


# =============================================================================
# Web research step budget tests (from test_web_research_step_budget.py)
# =============================================================================


def test_web_research_augment_messages_includes_used_and_remaining():
    model = MagicMock()
    agent = WebResearchToolCallingAgent(tools=[], model=model, max_steps=10)
    agent.step_number = 3

    # Test case 1: Appending when last message is NOT a user message
    base = [ChatMessage(role=MessageRole.SYSTEM, content="sys")]
    out = agent.augment_messages_for_step(base)
    assert len(out) == 2
    last = out[-1]
    assert last.role == MessageRole.USER
    text = str(last.content)
    assert "2 step(s) used" in text
    assert "8 step(s) remaining" in text
    assert "maximum 10" in text

    # Test case 2: Merging when last message IS a user message
    base2 = [ChatMessage(role=MessageRole.USER, content="prior")]
    out2 = agent.augment_messages_for_step(base2)
    assert len(out2) == 1
    text2 = str(out2[0].content)
    assert "2 step(s) used" in text2
    assert "prior" in text2


def test_tool_calling_agent_default_augment_is_identity():
    model = MagicMock()
    agent = ToolCallingAgent(tools=[], model=model, max_steps=5)
    base = [ChatMessage(role=MessageRole.SYSTEM, content=[{"type": "text", "text": "sys"}])]
    assert agent.augment_messages_for_step(base) is base


def test_web_research_unique_words_key():
    from plugin.chatbot.web_research import _cache_key_inputs

    q = "Please find the best pizza restaurants in Madison Heights, Michigan (MI)!"
    key, emb = _cache_key_inputs(q)
    # Fluff words like please, find, the, best, in are stripped; content words stay sorted unique.
    assert key == "heights madison michigan pizza restaurants"
    assert emb == "pizza restaurants madison heights michigan"
    q2 = "Research a concise report on the best pizza in Madison Heights"
    key2, emb2 = _cache_key_inputs(q2)
    assert key2 == "heights madison pizza"
    assert emb2 == "pizza madison heights"


def test_web_research_caching_logic(tmp_path):
    from plugin.chatbot.web_research import WebResearchTool
    from plugin.tests.testing_utils import MockContext

    ctx = MagicMock()
    ctx.ctx = MockContext()

    db_file = str(tmp_path / "writeragent_web_cache.db")

    # Set up config patches
    def _cfg_int(key):
        if key == "chatbot.web_cache_validity_days":
            return 30
        if key == "chatbot.web_research_cache_jaccard_percent":
            return 40
        if key == "chatbot.web_research_cache_min_overlap":
            return 8
        return 50

    with patch("plugin.framework.config.user_config_dir", return_value=str(tmp_path)), \
         patch("plugin.framework.config.get_api_config", return_value={}), \
         patch("plugin.framework.config.get_config", side_effect=_research_config_side_effect(_cfg_int)), \
         patch("plugin.framework.client.response_normalizers.should_prepend_dev_llm_system_prefix", return_value=False), \
         patch("plugin.chatbot.web_research_cache.resolve_research_locale", return_value=("en_US", "english")):

        # Pre-populate the cache using _web_cache_set
        from plugin.contrib.smolagents.default_tools import _web_cache_set
        _web_cache_set(db_file, "research", "caching unique", "Cached Answer Content", 50 * 1024 * 1024)

        tool = WebResearchTool()
        # Query that normalizes to "caching unique" ("test" is an English stop word)
        res = tool.execute(ctx, query="Search for caching test unique info")
        assert res["status"] == "ok"
        assert res["result"] == "Cached Answer Content"
        assert res.get("research_cache_event") == "hit"
        assert res.get("research_cache_key") == "caching unique"
        from plugin.chatbot.web_research_chat import web_research_cache_chat_text

        cache_chat = web_research_cache_chat_text(res)
        assert "Research cache hit" in cache_chat
        assert "caching unique" in cache_chat


def test_web_research_cache_lookup_uses_embedding_threshold(tmp_path):
    from plugin.chatbot.web_research import WebResearchTool
    from plugin.tests.testing_utils import MockContext

    ctx = MagicMock()
    ctx.ctx = MockContext()
    db_file = str(tmp_path / "writeragent_web_cache.db")
    db_file_path = Path(db_file)
    db_file_path.write_bytes(b"")
    captured: dict[str, object] = {}

    def _cfg_int(key):
        if key == "chatbot.web_cache_validity_days":
            return 30
        if key == "chatbot.web_research_cache_jaccard_percent":
            return 60
        if key == "chatbot.web_research_cache_embedding_percent":
            return 75
        if key == "chatbot.web_research_cache_min_overlap":
            return 8
        return 50

    def fake_lookup(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return ("hit_embedding", "english|cached similar", 0.78, "Cached Answer Content")

    with patch("plugin.framework.config.user_config_dir", return_value=str(tmp_path)), \
         patch("plugin.framework.config.get_api_config", return_value={}), \
         patch("plugin.framework.config.get_config", side_effect=_research_config_side_effect(_cfg_int)), \
         patch("plugin.framework.client.response_normalizers.should_prepend_dev_llm_system_prefix", return_value=False), \
         patch("plugin.chatbot.web_research_cache.resolve_research_locale", return_value=("en_US", "english")), \
         patch("plugin.chatbot.web_research_cache.enqueue_research_cache_embedding_backfill"), \
         patch("plugin.chatbot.web_research_cache.lookup_research_cache", side_effect=fake_lookup):
        res = WebResearchTool().execute(ctx, query="Search for caching test unique info")

    assert res["status"] == "ok"
    assert res["research_cache_event"] == "hit_embedding"
    assert captured["kwargs"]["embedding_percent"] == 75
    assert captured["kwargs"]["embedding_text"] == "caching unique"
    assert captured["args"][4] == 60


def test_web_research_caching_write(tmp_path):
    from plugin.chatbot.web_research import WebResearchTool
    from plugin.tests.testing_utils import MockContext
    from plugin.contrib.smolagents.default_tools import _web_cache_get

    ctx = MagicMock()
    ctx.ctx = MockContext()
    setattr(ctx.ctx, "getServiceManager", MagicMock())

    db_file = str(tmp_path / "writeragent_web_cache.db")

    # Ensure empty db exists by writing something and checking
    from plugin.contrib.smolagents.default_tools import _web_cache_set
    _web_cache_set(db_file, "research", "dummy", "val", 1024 * 1024)

    def _cfg_int(key):
        if key == "chatbot.web_cache_validity_days":
            return 30
        if key == "chatbot.web_research_cache_jaccard_percent":
            return 40
        if key == "chatbot.web_research_cache_min_overlap":
            return 8
        return 50

    with patch("plugin.framework.config.user_config_dir", return_value=str(tmp_path)), \
         patch("plugin.framework.config.get_api_config", return_value={}), \
         patch("plugin.framework.config.get_config", side_effect=_research_config_side_effect(_cfg_int)), \
         patch("plugin.framework.client.response_normalizers.should_prepend_dev_llm_system_prefix", return_value=False), \
         patch("plugin.framework.config.get_api_config", return_value={}), \
         patch("plugin.chatbot.web_research_cache.resolve_research_locale", return_value=("en_US", "english")), \
         patch("plugin.chatbot.smol_agent.SmolAgentExecutor") as mock_exec:

        mock_exec.return_value.execute_safe.return_value = "Live Searched Output"

        tool = WebResearchTool()
        res = tool.execute(ctx, query="Execute a new search query")
        assert res["status"] == "ok"
        assert res["result"] == "Live Searched Output"

        # Check database directly (new writes use lang-prefixed keys)
        cached = _web_cache_get(db_file, "research", "english|execute", max_age_days=30)
        assert cached == "Live Searched Output"
        assert res.get("research_cache_event") == "saved"
        assert res.get("research_cache_key") == "execute"
        from plugin.chatbot.web_research_chat import web_research_cache_chat_text

        cache_chat = web_research_cache_chat_text(res)
        assert "Research cache saved" in cache_chat
        assert "execute" in cache_chat


@pytest.mark.parametrize("agent_result", [None, "   "])
def test_web_research_empty_agent_result_is_an_error(tmp_path, agent_result):
    from plugin.chatbot.web_research import WebResearchTool
    from plugin.tests.testing_utils import MockContext

    ctx = MagicMock()
    ctx.ctx = MockContext()
    setattr(ctx.ctx, "getServiceManager", MagicMock())

    def _cfg_int(key):
        if key == "chatbot.web_cache_validity_days":
            return 30
        if key == "chatbot.web_research_cache_jaccard_percent":
            return 40
        if key == "chatbot.web_research_cache_min_overlap":
            return 8
        return 50

    with patch("plugin.framework.config.user_config_dir", return_value=str(tmp_path)), \
         patch("plugin.framework.config.get_api_config", return_value={}), \
         patch("plugin.framework.config.get_config", side_effect=_research_config_side_effect(_cfg_int)), \
         patch("plugin.framework.client.response_normalizers.should_prepend_dev_llm_system_prefix", return_value=False), \
         patch("plugin.chatbot.web_research_cache.resolve_research_locale", return_value=("en_US", "english")), \
         patch("plugin.chatbot.web_research._write_research_cache") as mock_write_cache, \
         patch("plugin.chatbot.smol_agent.SmolAgentExecutor") as mock_exec:

        mock_exec.return_value.execute_safe.return_value = agent_result

        tool = WebResearchTool()
        res = tool.execute(ctx, query="Execute a new search query")
        assert res["status"] == "error"
        assert res["code"] == "WEB_RESEARCH_EMPTY_RESULT"
        mock_write_cache.assert_not_called()


def test_deep_and_shallow_execute_do_not_share_cache_rows(tmp_path):
    """A shallow row must not satisfy deep=True, and a deep row must not satisfy ordinary research."""
    from plugin.chatbot.web_research import WebResearchTool, _cache_key_inputs
    from plugin.tests.testing_utils import MockContext
    from plugin.contrib.smolagents.default_tools import _web_cache_get, _web_cache_set

    ctx = MagicMock()
    ctx.ctx = MockContext()
    setattr(ctx.ctx, "getServiceManager", MagicMock())
    db_file = str(tmp_path / "writeragent_web_cache.db")
    query = "Execute a new search query"
    word_key, _ = _cache_key_inputs(query, snowball_lang="english")
    assert word_key == "execute"
    shallow_key = "english|execute"
    deep_key = "deep|english|execute"

    def _cfg_int(key):
        if key == "chatbot.web_cache_validity_days":
            return 30
        if key == "chatbot.web_research_cache_jaccard_percent":
            return 40
        # Overlap 1 would fuzzy-match the same word across modes if mode were ignored.
        if key == "chatbot.web_research_cache_min_overlap":
            return 1
        if key == "chatbot.web_research_cache_embedding_percent":
            return 75
        return 50

    _web_cache_set(db_file, "research", word_key, "BARE SHALLOW", 50 * 1024 * 1024)
    _web_cache_set(db_file, "research", shallow_key, "PREFIXED SHALLOW", 50 * 1024 * 1024)

    with patch("plugin.framework.config.user_config_dir", return_value=str(tmp_path)), \
         patch("plugin.framework.config.get_api_config", return_value={}), \
         patch("plugin.framework.config.get_config", side_effect=_research_config_side_effect(_cfg_int)), \
         patch("plugin.chatbot.web_research_cache.resolve_research_locale", return_value=("en_US", "english")), \
         patch("plugin.chatbot.web_research_cache._research_cache_embedding_configured", return_value=False), \
         patch("plugin.chatbot.web_research._run_deep_web_research", return_value=("DEEP REPORT", query)) as mock_deep:
        deep_res = WebResearchTool().execute(ctx, query=query, deep=True)

    assert deep_res["status"] == "ok"
    assert deep_res["result"] == "DEEP REPORT"
    mock_deep.assert_called_once()
    assert _web_cache_get(db_file, "research", deep_key, max_age_days=30) == "DEEP REPORT"
    assert _web_cache_get(db_file, "research", word_key, max_age_days=30) == "BARE SHALLOW"
    assert _web_cache_get(db_file, "research", shallow_key, max_age_days=30) == "PREFIXED SHALLOW"

    with patch("plugin.framework.config.user_config_dir", return_value=str(tmp_path)), \
         patch("plugin.framework.config.get_api_config", return_value={}), \
         patch("plugin.framework.config.get_config", side_effect=_research_config_side_effect(_cfg_int)), \
         patch("plugin.chatbot.web_research_cache.resolve_research_locale", return_value=("en_US", "english")), \
         patch("plugin.chatbot.web_research_cache._research_cache_embedding_configured", return_value=False), \
         patch("plugin.chatbot.smol_agent.SmolAgentExecutor") as mock_exec:
        mock_exec.return_value.execute_safe.return_value = "LIVE SHALLOW"
        shallow_res = WebResearchTool().execute(ctx, query=query)

    # Bare key is still preferred for shallow exact lookup, not the deep row.
    assert shallow_res["result"] == "BARE SHALLOW"
    assert _web_cache_get(db_file, "research", deep_key, max_age_days=30) == "DEEP REPORT"


def test_write_research_cache_enqueues_embedding_backfill(tmp_path):
    from plugin.chatbot.web_research import _write_research_cache
    from plugin.contrib.smolagents.default_tools import _web_cache_get

    ctx = MagicMock()
    ctx.ctx = object()
    db_file = str(tmp_path / "writeragent_web_cache.db")

    with patch("plugin.chatbot.web_research_cache.enqueue_research_cache_embedding_backfill") as enqueue_backfill, \
         patch("plugin.chatbot.web_research_cache.enqueue_research_cache_embedding_for_row") as enqueue_row:
        fields = _write_research_cache(ctx, db_file, "execute", "Live Searched Output", 50, 30, "english", embedding_text="execute ordered")

    assert fields["research_cache_event"] == "saved"
    assert _web_cache_get(db_file, "research", "english|execute", max_age_days=30) == "Live Searched Output"
    enqueue_row.assert_called_once_with(ctx.ctx, db_file, "english|execute", "execute ordered")
    enqueue_backfill.assert_called_once_with(ctx.ctx, db_file, 30)


def test_web_research_caching_disabled_bypasses_cache(tmp_path):
    from plugin.chatbot.web_research import WebResearchTool
    from plugin.tests.testing_utils import MockContext
    from plugin.contrib.smolagents.default_tools import _web_cache_set

    ctx = MagicMock()
    ctx.ctx = MockContext()
    setattr(ctx.ctx, "getServiceManager", MagicMock())

    db_file = str(tmp_path / "writeragent_web_cache.db")
    _web_cache_set(db_file, "research", "caching unique", "Cached Answer Content", 50 * 1024 * 1024)

    def _cfg_int(key):
        if key == "chatbot.web_cache_validity_days":
            return 30
        if key == "chatbot.web_research_cache_jaccard_percent":
            return 40
        if key == "chatbot.web_research_cache_min_overlap":
            return 8
        return 50

    with patch("plugin.framework.config.user_config_dir", return_value=str(tmp_path)), \
         patch("plugin.framework.config.get_config", side_effect=_research_config_side_effect(_cfg_int, cache_enabled=False)), \
         patch("plugin.framework.client.response_normalizers.should_prepend_dev_llm_system_prefix", return_value=False), \
         patch("plugin.framework.config.get_api_config", return_value={}), \
         patch("plugin.chatbot.smol_agent.SmolAgentExecutor") as mock_exec:

        mock_exec.return_value.execute_safe.return_value = "Live Searched Output"

        tool = WebResearchTool()
        res = tool.execute(ctx, query="Search for caching test unique info")
        assert res["status"] == "ok"
        assert res["result"] == "Live Searched Output"
        mock_exec.return_value.execute_safe.assert_called_once()


def test_web_research_cache_max_zero_does_not_serve_a_stale_report(tmp_path):
    """web_cache_max_mb 0 disables report reads, not only the later write."""
    from plugin.chatbot.web_research import WebResearchTool
    from plugin.tests.testing_utils import MockContext
    from plugin.contrib.smolagents.default_tools import _web_cache_get, _web_cache_set

    ctx = MagicMock()
    ctx.ctx = MockContext()
    setattr(ctx.ctx, "getServiceManager", MagicMock())

    db_file = str(tmp_path / "writeragent_web_cache.db")
    _web_cache_set(db_file, "research", "caching unique", "Cached Answer Content", 50 * 1024 * 1024)

    def _cfg_int(key):
        if key == "chatbot.web_cache_max_mb":
            return 0
        if key == "chatbot.web_cache_validity_days":
            return 30
        if key == "chatbot.web_research_cache_jaccard_percent":
            return 40
        if key == "chatbot.web_research_cache_min_overlap":
            return 8
        return 50

    with patch("plugin.framework.config.user_config_dir", return_value=str(tmp_path)), \
         patch("plugin.framework.config.get_api_config", return_value={}), \
         patch("plugin.framework.config.get_config", side_effect=_research_config_side_effect(_cfg_int, cache_max_mb=0)), \
         patch("plugin.chatbot.web_research_cache.resolve_research_locale", return_value=("en_US", "english")), \
         patch("plugin.chatbot.web_research_cache.lookup_research_cache") as lookup, \
         patch("plugin.chatbot.smol_agent.SmolAgentExecutor") as mock_exec:

        mock_exec.return_value.execute_safe.return_value = "Live Searched Output"
        res = WebResearchTool().execute(ctx, query="Search for caching test unique info")

    assert res["status"] == "ok"
    assert res["result"] == "Live Searched Output"
    assert res.get("research_cache_event") is None
    lookup.assert_not_called()
    mock_exec.return_value.execute_safe.assert_called_once()
    assert _web_cache_get(db_file, "research", "caching unique", max_age_days=30) == "Cached Answer Content"
    assert _web_cache_get(db_file, "research", "english|caching unique", max_age_days=30) is None


# =============================================================================
# Fake urllib response for VisitWebpageTool tests
# =============================================================================


class _FakeUrlopenHeaders:
    def get(self, name, default=None):
        if str(name).lower() == "content-type":
            return "text/html; charset=utf-8"
        return default

    def get_content_charset(self):
        return "utf-8"


class _FakeUrlopenResponse:
    def __init__(self, body: bytes):
        self._body = body
        self._offset = 0
        self.headers = _FakeUrlopenHeaders()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, n: int | None = None):
        if n is None:
            chunk = self._body[self._offset :]
            self._offset = len(self._body)
            return chunk
        chunk = self._body[self._offset : self._offset + n]
        self._offset += n
        return chunk


def test_visit_webpage_rejects_non_http_urls(tmp_path):
    from plugin.contrib.smolagents.default_tools import VisitWebpageTool
    tool = VisitWebpageTool(cache_max_age_days=30, cache_path=str(tmp_path / "writeragent_web_cache.db"), cache_max_mb=10)
    assert "Error fetching the webpage" in tool.forward("file:///etc/passwd")
    assert "Error fetching the webpage" in tool.forward("chrome://version")
    assert "Error fetching the webpage" in tool.forward("javascript:alert(1)")

def test_cdp_visit_rejects_non_http_urls():
    from plugin.chatbot.web_research import CdpPageDriver, VisitWebpageBrowserTool
    tool = VisitWebpageBrowserTool(CdpPageDriver())
    assert "Error visiting webpage via browser: URL must use http or https scheme." in tool.forward("file:///etc/passwd")
    assert "Error visiting webpage via browser: URL must use http or https scheme." in tool.forward("chrome://version")
    assert "Error visiting webpage via browser: URL must use http or https scheme." in tool.forward("javascript:alert(1)")

def test_visit_webpage_does_not_cache_fetch_errors(tmp_path):
    """Transient fetch failures must not poison the page cache."""
    from plugin.contrib.smolagents.default_tools import VisitWebpageTool, _web_cache_get

    db_file = str(tmp_path / "writeragent_web_cache.db")
    url = "https://example.test/gone"

    with patch("urllib.request.urlopen", side_effect=OSError("timed out")):
        tool = VisitWebpageTool(cache_max_age_days=30, cache_path=db_file, cache_max_mb=10)
        msg = tool.forward(url)

    assert "Error fetching the webpage" in msg
    assert _web_cache_get(db_file, "page", url, max_age_days=30) is None


def test_visit_webpage_still_caches_successful_fetch(tmp_path):
    from plugin.contrib.smolagents.default_tools import VisitWebpageTool, _web_cache_get

    db_file = str(tmp_path / "writeragent_web_cache.db")
    url = "https://example.test/ok"
    html = b"<html><body><p>Hello cached page</p></body></html>"

    with patch("urllib.request.urlopen", return_value=_FakeUrlopenResponse(html)):
        tool = VisitWebpageTool(cache_max_age_days=30, cache_path=db_file, cache_max_mb=10)
        result = tool.forward(url)

    assert "Hello cached page" in result
    cached = _web_cache_get(db_file, "page", url, max_age_days=30)
    assert cached is not None
    assert "Hello cached page" in cached


def test_get_research_completion_instruction():
    """Verify document-type-tailored research completion instructions."""
    from plugin.framework.prompts import (
        get_research_completion_instruction,
        RESEARCH_COMPLETION_INSTRUCTION_WRITER,
        RESEARCH_COMPLETION_INSTRUCTION_CALC,
        RESEARCH_COMPLETION_INSTRUCTION_DRAW,
    )

    assert get_research_completion_instruction("writer") == RESEARCH_COMPLETION_INSTRUCTION_WRITER
    assert get_research_completion_instruction("calc") == RESEARCH_COMPLETION_INSTRUCTION_CALC
    assert get_research_completion_instruction("draw") == RESEARCH_COMPLETION_INSTRUCTION_DRAW
    assert get_research_completion_instruction("impress") == RESEARCH_COMPLETION_INSTRUCTION_DRAW
    assert get_research_completion_instruction(None) == RESEARCH_COMPLETION_INSTRUCTION_WRITER


def test_web_research_tool_includes_instruction_in_result(tmp_path):
    """WebResearchTool.execute must include an instruction field tailored to doc_type."""
    from plugin.chatbot.web_research import WebResearchTool
    from plugin.framework.prompts import RESEARCH_COMPLETION_INSTRUCTION_CALC
    from plugin.tests.testing_utils import MockContext

    ctx = MagicMock()
    ctx.ctx = MockContext()
    ctx.doc_type = "calc"

    with patch("plugin.framework.config.user_config_dir", return_value=str(tmp_path)), \
         patch("plugin.framework.config.get_api_config", return_value={}), \
         patch("plugin.framework.config.get_config", return_value=30), \
         patch("plugin.chatbot.web_research_cache.resolve_research_locale", return_value=("en_US", "english")), \
         patch("plugin.chatbot.web_research._run_web_agent", return_value="Live research result text"):

        tool = WebResearchTool()
        res = tool.execute(ctx, query="Calculate revenue trends")
        assert res["status"] == "ok"
        assert res["result"] == "Live research result text"
        assert res["instruction"] == RESEARCH_COMPLETION_INSTRUCTION_CALC


@pytest.mark.parametrize("status", ["ok", "error"])
def test_uncacheable_deep_notes_are_returned_and_not_stored(tmp_path, status):
    from plugin.chatbot.web_research import WebResearchTool
    from plugin.contrib.smolagents.default_tools import _web_cache_list_keys
    from plugin.tests.testing_utils import MockContext

    notes = {
        "status": status,
        "message": "Web research completed.",
        "result": "Research notes (automatic synthesis failed) for: topic\n\nFinding X",
        "cacheable": False,
    }
    ctx = MagicMock()
    ctx.ctx = MockContext()
    ctx.doc = None
    ctx.doc_type = "writer"
    ctx.status_callback = None
    ctx.append_thinking_callback = None
    ctx.approval_callback = None
    ctx.chat_append_callback = None
    ctx.stop_checker = None
    ctx.send_cancellation = None

    def _cfg_int(key):
        if key == "chatbot.web_cache_validity_days":
            return 30
        return 50

    with patch("plugin.framework.config.user_config_dir", return_value=str(tmp_path)), \
         patch("plugin.framework.config.get_api_config", return_value={}), \
         patch("plugin.framework.config.get_config", side_effect=_research_config_side_effect(_cfg_int)), \
         patch("plugin.chatbot.web_research_cache.resolve_research_locale", return_value=("en_US", "english")), \
         patch("plugin.framework.client.llm_client.LlmClient", return_value=MagicMock()), \
         patch("plugin.chatbot.smol_agent.WriterAgentSmolModel", return_value=MagicMock()), \
         patch("plugin.chatbot.web_research._run_deep_web_research", return_value=(dict(notes), "topic ratings")):
        res = WebResearchTool().execute(ctx, query="topic ratings", deep=True)

    assert res["status"] == status
    assert "Finding X" in res["result"]
    # A cacheable flag must not leak to the model even on non-ok payloads.
    assert "cacheable" not in res
    db_file = str(tmp_path / "writeragent_web_cache.db")
    assert _web_cache_list_keys(db_file, "research", 30) == []

def test_chromium_cdp_enabled():
    from plugin.chatbot.web_research import WebResearchTool
    from plugin.tests.testing_utils import MockContext
    from unittest.mock import MagicMock

    ctx = MagicMock()
    ctx.ctx = MockContext()
    ctx.doc = None
    ctx.stop_checker = None

    def mock_get_config(key):
        if key == "chatbot.web_research_browser":
            return "chromium"
        if key == "chatbot.max_tool_rounds":
            return 1
        if key == "chat_max_tokens":
            return 2048
        if key == "chatbot.web_research_cache_enabled":
            return False
        if key == "chatbot.web_cache_max_mb":
            return 0
        if key == "chatbot.prompt_for_web_research":
            return False
        return 0

    import plugin.chatbot.web_research as wr

    fake_driver = MagicMock()

    with patch("plugin.framework.config.get_config", side_effect=mock_get_config), \
         patch("plugin.chatbot.web_research._begin_shared_browser", return_value=fake_driver) as mock_begin, \
         patch.object(wr._browser_refcount, "finish") as mock_finish, \
         patch("plugin.chatbot.web_research._run_web_agent", return_value={"status": "ok", "result": "done"}) as mock_run, \
         patch("plugin.framework.config.get_api_config", return_value={}), \
         patch("plugin.framework.client.llm_client.LlmClient"):

         WebResearchTool().execute(ctx, query="test query")

         mock_begin.assert_called_once_with("chromium")
         mock_finish.assert_called_once()

         agent_params = mock_run.call_args.args[3]
         assert agent_params.browser is fake_driver

def test_cdp_connection_leak_early_exception():
    from plugin.chatbot.web_research import WebResearchTool
    from plugin.tests.testing_utils import MockContext
    from unittest.mock import MagicMock

    ctx = MagicMock()
    ctx.ctx = MockContext()
    ctx.doc = None
    ctx.stop_checker = None

    def mock_get_config(key):
        if key == "chatbot.web_research_browser":
            return "chrome"
        if key == "chatbot.max_tool_rounds":
            return 1
        if key == "chat_max_tokens":
            return 2048
        if key == "chatbot.web_research_cache_enabled":
            return False
        if key == "chatbot.web_cache_max_mb":
            return 0
        if key == "chatbot.prompt_for_web_research":
            return False
        return 0

    class FakeException(Exception):
        pass

    import plugin.chatbot.web_research as wr

    with patch("plugin.framework.config.get_config", side_effect=mock_get_config), \
         patch("plugin.chatbot.web_research._begin_shared_browser", return_value=MagicMock()) as mock_begin, \
         patch.object(wr._browser_refcount, "finish") as mock_finish, \
         patch("plugin.framework.config.get_api_config", return_value={}), \
         patch("plugin.chatbot.web_research._run_web_agent", side_effect=FakeException("Early crash")), \
         patch("plugin.framework.client.llm_client.LlmClient"):

         with pytest.raises(FakeException):
             WebResearchTool().execute(ctx, query="test query")

         mock_begin.assert_called_once_with("chrome")
         # Important: Even though the exception was raised during LlmClient setup, finish must be called
         mock_finish.assert_called_once()


def test_web_research_uses_ctx_approval_callback_as_given():
    from plugin.chatbot.web_research import WebResearchTool

    ctx = MagicMock()
    ctx.ctx = MagicMock()
    ctx.send_cancellation = None
    cb = MagicMock(return_value=(True, None))
    ctx.approval_callback = cb
    captured = {}

    def _fake_run(_ctx, _q, _hist, agent_params, **_kwargs):
        captured["approval_callback"] = agent_params.approval_callback
        return "ok"

    def _cfg(key):
        if key == "chatbot.prompt_for_web_research":
            return False
        if key == "chatbot.max_tool_rounds":
            return 1
        if key == "chat_max_tokens":
            return 2048
        return 0

    with (
        patch("plugin.framework.config.get_config", side_effect=_cfg),
        patch("plugin.framework.config.get_api_config", return_value={}),
        patch("plugin.chatbot.web_research._run_web_agent", side_effect=_fake_run),
        patch("plugin.framework.client.llm_client.LlmClient"),
    ):
        res = WebResearchTool().execute(ctx, query="test query")
    assert res["status"] == "ok"
    assert captured["approval_callback"] is cb


@pytest.mark.parametrize("doc_type", ["writer", "calc", "draw", "impress"])
def test_web_research_is_off_main_chat_and_mcp_lists(doc_type: str) -> None:
    """WebResearchTool must stay off main chat and all MCP lists.

    Re-basing the class onto ToolBase in cbb0f07b6 silently reset the tier
    it had inherited to ``core``; setting ``tier = "unlisted"`` on the
    class prevents a future base change from putting it back on any chat or
    MCP list.
    """
    from plugin.app import get_tools
    from plugin.framework.doc_type import uno_services_for_doc_type_label
    from plugin.mcp_server.mcp_protocol import _exclude_tiers

    reg = get_tools()
    services = uno_services_for_doc_type_label(doc_type)
    chat_names = {s["function"]["name"] for s in reg.get_schemas("openai", doc_type=doc_type)}
    assert "web_research" not in chat_names
    assert "delegate_to_subagent" in chat_names

    tool = reg.get("web_research")
    assert tool is not None

    for mode in ("delegate", "direct_discovery", "direct_flat"):
        schemas = reg.get_schemas(
            "mcp",
            doc_type=doc_type,
            uno_services_supported=services,
            exclude_tiers=_exclude_tiers(mode, listing=True),
        )
        mcp_names = {s["name"] for s in schemas}
        assert "web_research" not in mcp_names
        assert "delegate_to_subagent" in mcp_names
        assert "web_research" not in json.dumps(schemas)
        assert tool.tier in _exclude_tiers(mode, listing=False)


def test_web_research_still_runs_through_delegate_sidebar_and_wa_proxy() -> None:
    from plugin.app import get_tools
    from plugin.chatbot.chat_sidebar_mode import CHAT_MODE_WEB_RESEARCH, require_sidebar_mode
    from plugin.chatbot.web_research import WebResearchTool
    from plugin.doc.specialized_base import DelegateToSubagent
    from plugin.framework.tool import tool_supports_document
    from tests.harness.strip_bundle import is_release_build

    with patch.object(WebResearchTool, "execute", return_value={"status": "ok", "result": "R"}) as mock_exec:
        res = DelegateToSubagent().execute(MagicMock(), domain="web_research", task="pizza")
    assert res == {"status": "ok", "result": "R"}
    mock_exec.assert_called_once()
    assert mock_exec.call_args.kwargs == {"query": "pizza"}

    reg = get_tools()
    mode_tool_name = require_sidebar_mode(CHAT_MODE_WEB_RESEARCH).tool_name
    assert mode_tool_name is not None
    mode_tool = reg.get(mode_tool_name)
    assert isinstance(mode_tool, WebResearchTool)
    for dt in ("writer", "calc", "draw", "impress"):
        assert tool_supports_document(mode_tool, doc_type=dt, uno_services_supported=None)

    if not is_release_build():
        from scripts.generate_tool_proxies import domain_tools

        core_proxy_tools = domain_tools(
            reg.get_tools(filter_doc_type=False, exclude_tiers=frozenset({"specialized_control"}))
        )["core"]
        assert "web_research" in core_proxy_tools


def test_web_agent_tools_read_stored_parts_beyond_old_cap():
    import re
    from dataclasses import replace
    from plugin.chatbot.web_research import WebAgentRunParams, _run_web_agent

    url = "https://example.test/long-article"
    body_paras = "".join(f"<p>Paragraph {i:04d}: " + ("word " * 18) + "</p>\n" for i in range(1500))
    html = f"<html><body>{body_paras}<p>TAILMARKER</p></body></html>".encode("utf-8")
    assert len(html) > 140_000

    params = WebAgentRunParams(
        smol_model=MagicMock(),
        max_steps=5,
        cache_path=None,
        cache_max_mb=0,
        cache_max_age_days=30,
        browser=None,
        status_callback=None,
        append_thinking_callback=None,
        approval_callback=None,
        chat_append_callback=None,
        outer_query="find tailmarker",
    )

    captured_tools: list[dict[str, Any]] = []
    find_results: list[str] = []
    read_results: list[str] = []

    def capture_agent(*args, tools=None, **kwargs):
        captured_tools.append({t.name: t for t in (tools or [])})
        return MagicMock()

    def first_execute(agent, task, **kwargs):
        tools_by_name = captured_tools[-1]
        visit_out = tools_by_name["visit_webpage"].forward([url])
        assert "Part 1 of " in visit_out
        find_out = tools_by_name["find_in_page"].forward("tailmarker", url)
        find_results.append(find_out)
        return "done"

    def second_execute(agent, task, **kwargs):
        tools_by_name = captured_tools[-1]
        read_results.append(tools_by_name["read_page"].forward(url, 1))
        return "done"

    with patch("urllib.request.urlopen", return_value=_FakeUrlopenResponse(html)) as mock_open, \
         patch("plugin.chatbot.web_research.WebResearchToolCallingAgent", side_effect=capture_agent), \
         patch("plugin.chatbot.smol_examples.get_examples_block", return_value=""), \
         patch("plugin.chatbot.smol_agent.SmolAgentExecutor") as mock_exe_cls:
        mock_exe_cls.return_value.execute_safe.side_effect = first_execute
        _run_web_agent(MagicMock(), "find tailmarker", None, params)
        mock_open.assert_called_once()

        mock_exe_cls.return_value.execute_safe.side_effect = second_execute
        _run_web_agent(MagicMock(), "read part 1", None, replace(params, smol_model=MagicMock()))
        mock_open.assert_called_once()

    assert find_results
    match = re.search(r"Part (\d+):", find_results[0])
    assert match is not None and int(match.group(1)) > 7
    assert "TAILMARKER" in find_results[0]
    assert read_results and "Part 1 of " in read_results[0]


def test_cdp_visit_keeps_text_up_to_page_cap(monkeypatch):
    from plugin.chatbot.web_pages import PAGE_TEXT_MAX_CHARS
    from plugin.chatbot.web_research import CdpPageDriver, VisitWebpageBrowserTool

    long_value = "A" * 250_000
    responses = iter([
        json.dumps({"success": True, "result": {"targetId": "tab-cap"}}),
        json.dumps({"success": True}),
        json.dumps({"success": True, "result": {"result": {"type": "string", "value": long_value}}}),
        json.dumps({"success": True}),
    ])
    monkeypatch.setattr("plugin.contrib.cdp.browser_cdp_tool.browser_cdp", lambda *a, **kw: next(responses))
    monkeypatch.setattr("time.sleep", lambda _s: None)

    out = VisitWebpageBrowserTool(CdpPageDriver()).forward("https://example.test/huge")
    assert out.startswith("A" * PAGE_TEXT_MAX_CHARS)
    assert f"stay below {PAGE_TEXT_MAX_CHARS} characters" in out


def _execute_web_research_with_window(
    api_config: dict[str, Any],
    resolved_window: int | None,
    *,
    deep: bool = False,
    browser: str = "off",
):
    from plugin.chatbot.web_research import WebResearchTool
    from plugin.tests.testing_utils import MockContext

    ctx = MagicMock()
    ctx.ctx = MockContext()
    ctx.doc = None
    ctx.doc_type = "writer"
    ctx.status_callback = None
    ctx.append_thinking_callback = None
    ctx.approval_callback = None
    ctx.chat_append_callback = None
    ctx.send_cancellation = None

    captured: dict[str, Any] = {}

    def _fake_shallow(_ctx, _q, _hist, agent_params, **_kwargs):
        captured["pages"] = agent_params.pages
        captured["api"] = agent_params.smol_model.api
        return "ok"

    def _fake_deep(_ctx, query_str, agent_params):
        captured["pages"] = agent_params.pages
        captured["api"] = agent_params.smol_model.api
        return "deep ok", query_str

    def _cfg(key: str):
        if key == "chatbot.max_tool_rounds":
            return 5
        if key == "chat_max_tokens":
            return 2048
        if key == "chatbot.web_research_browser":
            return browser
        return 0

    with (
        patch("plugin.framework.config.get_config", side_effect=_cfg),
        patch("plugin.framework.config.get_api_config", return_value=api_config),
        patch("plugin.chatbot.web_research_cache.resolve_research_locale", return_value=("en_US", "english")),
        patch("plugin.chatbot.web_research.resolve_context_window", return_value=resolved_window) as mock_resolve,
        patch("plugin.chatbot.web_research._begin_shared_browser") as mock_cdp,
        patch("plugin.chatbot.web_research._run_web_agent", side_effect=_fake_shallow) as mock_shallow,
        patch("plugin.chatbot.web_research._run_deep_web_research", side_effect=_fake_deep) as mock_deep,
    ):
        res = WebResearchTool().execute(ctx, query="test sizing", deep=deep)
    return res, captured, mock_resolve, mock_cdp, mock_shallow, mock_deep


@pytest.mark.parametrize(
    "api_config, resolved_window, expected_budget, expected_part",
    [
        pytest.param({"endpoint": "http://localhost:11434", "model": "gemma4"}, 8_192, 4_096, 2_048, id="ollama-8k"),
        pytest.param({"endpoint": "https://openrouter.ai/api", "model": "anthropic/claude-opus-4"}, 200_000, 48_000, 24_000, id="opus-200k"),
        pytest.param({"endpoint": "https://custom.example/v1", "model": "custom-model"}, None, 12_000, 6_000, id="custom-unknown"),
        pytest.param({"endpoint": "http://localhost:11434", "model": "x", "is_openrouter": True}, None, 12_000, 6_000, id="explicit-openrouter-proxy"),
    ],
)
def test_web_research_sizes_page_store_from_context_window(api_config, resolved_window, expected_budget, expected_part):
    res, captured, mock_resolve, _cdp, _shallow, _deep = _execute_web_research_with_window(api_config, resolved_window)
    assert res["status"] == "ok"
    mock_resolve.assert_called_once_with(captured["api"])
    assert captured["pages"].budget_chars == expected_budget
    assert captured["pages"].part_chars == expected_part


@pytest.mark.parametrize("deep", [False, True])
@pytest.mark.parametrize(
    "resolved_window, expected_label, expect_assumed",
    [
        pytest.param(2_048, "2K", False, id="known-2k"),
        pytest.param(4_096, "4K", False, id="known-4k"),
        pytest.param(None, "4K", True, id="ollama-unknown"),
    ],
)
def test_web_research_rejects_too_small_context_window(deep, resolved_window, expected_label, expect_assumed):
    res, _captured, mock_resolve, mock_cdp, mock_shallow, mock_deep = _execute_web_research_with_window(
        {"endpoint": "http://localhost:11434", "model": "gemma4"},
        resolved_window,
        deep=deep,
        browser="chrome",
    )
    assert res["status"] == "error"
    assert res["code"] == "WEB_RESEARCH_CONTEXT_TOO_SMALL"
    assert "num_ctx" in res["message"]
    assert expected_label in res["message"]
    assert "8K" in res["message"]
    if expect_assumed:
        assert "assumed" in res["message"]
        assert "PARAMETER num_ctx 8192" in res["message"]
        assert "ollama create" in res["message"]
        assert "Test Connection" in res["message"]
        assert "OLLAMA_CONTEXT_LENGTH" not in res["message"]
    else:
        assert "assumed" not in res["message"]
    mock_resolve.assert_called_once()
    mock_cdp.assert_not_called()
    mock_shallow.assert_not_called()
    mock_deep.assert_not_called()


def test_deep_web_research_uses_sized_page_store():
    res, captured, mock_resolve, _cdp, _shallow, _deep = _execute_web_research_with_window(
        {"endpoint": "https://api.example/v1", "model": "m"},
        32_768,
        deep=True,
    )
    assert res["status"] == "ok"
    mock_resolve.assert_called_once()
    assert captured["pages"].budget_chars == 16_384
    assert captured["pages"].part_chars == 8_192


def test_web_research_prompt_overhead_fits_within_minimum_window():
    """Keep the measured prompt + examples + wire tool-schema overhead well below MIN_WEB_RESEARCH_CONTEXT_TOKENS."""
    from plugin.chatbot.web_pages import MIN_WEB_RESEARCH_CONTEXT_TOKENS
    from plugin.chatbot.web_research import WebAgentRunParams, _run_web_agent
    from plugin.contrib.smolagents.models import get_tool_json_schema

    params = WebAgentRunParams(
        smol_model=MagicMock(),
        max_steps=25,
        cache_path=None,
        cache_max_mb=0,
        cache_max_age_days=30,
        browser=None,
        status_callback=None,
        append_thinking_callback=None,
        approval_callback=None,
        chat_append_callback=None,
        outer_query="topic",
    )
    captured_agent: list[Any] = []

    def _capture(agent, _task, **_kwargs):
        captured_agent.append(agent)
        return "done"

    with patch("plugin.chatbot.smol_agent.SmolAgentExecutor") as mock_exe_cls:
        mock_exe_cls.return_value.execute_safe.side_effect = _capture
        _run_web_agent(MagicMock(), "topic", None, params)

    assert len(captured_agent) == 1
    agent = captured_agent[0]
    wire_tools = json.dumps([get_tool_json_schema(t) for t in agent.tools.values()])
    overhead_tokens = (len(agent.system_prompt) + len(wire_tools)) // 4
    assert 1_000 < overhead_tokens < MIN_WEB_RESEARCH_CONTEXT_TOKENS // 3


def test_run_web_agent_prints_nothing(capsys):
    from plugin.chatbot.web_research import WebAgentRunParams, _run_web_agent
    from plugin.contrib.smolagents.models import (
        ChatMessage,
        ChatMessageToolCall,
        ChatMessageToolCallFunction,
        MessageRole,
    )

    smol_model = MagicMock()
    smol_model.generate.side_effect = [
        ChatMessage(
            role=MessageRole.ASSISTANT,
            content="",
            tool_calls=[
                ChatMessageToolCall(
                    function=ChatMessageToolCallFunction(name="final_answer", arguments={"answer": "web-answer"}),
                    id="call_1",
                    type="function",
                )
            ],
        )
    ]
    params = WebAgentRunParams(
        smol_model=smol_model,
        max_steps=5,
        cache_path=None,
        cache_max_mb=0,
        cache_max_age_days=30,
        browser=None,
        status_callback=None,
        append_thinking_callback=None,
        approval_callback=None,
        chat_append_callback=None,
        outer_query="secret-query",
    )
    ctx = MagicMock()
    ctx.send_cancellation = None

    result = _run_web_agent(ctx, "secret-query", None, params)
    assert result == "web-answer"
    assert capsys.readouterr().out == ""


def test_web_research_agent_keeps_partial_observations_and_step_budget():
    from plugin.contrib.smolagents.models import ChatMessageToolCall, ChatMessageToolCallFunction
    from plugin.contrib.smolagents.monitoring import LogLevel
    from plugin.contrib.smolagents.tools import Tool

    class _SearchTool(Tool):
        name = "search_web"
        description = "Search."
        inputs = {"q": {"type": "string", "description": "query"}}
        output_type = "string"

        def forward(self, q: str) -> str:
            return f"results for {q}"

    class _FinalTool(Tool):
        name = "final_answer"
        description = "Finish."
        inputs = {"answer": {"type": "string", "description": "answer"}}
        output_type = "string"

        def forward(self, answer: str) -> str:
            return answer

    seen: list[str] = []

    def _gen(messages, **kwargs):
        del kwargs
        text = "\n".join(
            "\n".join(str(p.get("text", p)) if isinstance(p, dict) else str(p) for p in m.content)
            if isinstance(m.content, list)
            else str(m.content or "")
            for m in messages
        )
        seen.append(text)
        if len(seen) == 1:
            return ChatMessage(
                role=MessageRole.ASSISTANT,
                content="",
                tool_calls=[
                    ChatMessageToolCall(
                        function=ChatMessageToolCallFunction(name="search_web", arguments={"q": "alpha"}),
                        id="c1",
                        type="function",
                    ),
                    ChatMessageToolCall(
                        function=ChatMessageToolCallFunction(name="no_such_tool", arguments={}),
                        id="c2",
                        type="function",
                    ),
                ],
            )
        return ChatMessage(
            role=MessageRole.ASSISTANT,
            content="",
            tool_calls=[
                ChatMessageToolCall(
                    function=ChatMessageToolCallFunction(name="final_answer", arguments={"answer": "ok"}),
                    id="c3",
                    type="function",
                )
            ],
        )

    model = MagicMock()
    model.generate.side_effect = _gen
    agent = WebResearchToolCallingAgent(
        tools=[_SearchTool(), _FinalTool()],
        model=model,
        max_steps=4,
        final_answer_tool_name="final_answer",
        verbosity_level=LogLevel.OFF,
    )

    assert agent.run("q") == "ok"
    assert "results for alpha" in seen[1]
    assert "1 step(s) used" in seen[1]

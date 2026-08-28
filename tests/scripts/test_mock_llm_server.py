# Tests for scripts/mock_llm_server.py

from __future__ import annotations

import json
import threading
from http.server import ThreadingHTTPServer
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from scripts.mock_llm_server import (
    DEFAULT_TRANSCRIPT,
    MOCK_MODEL_ID,
    MOCK_STT_MODEL_ID,
    RAMBLE_PARTS,
    Completion,
    MockLLMConfig,
    _TurnState,
    completion_tool_calls,
    decide_completion,
    detect_scenario,
    iter_sse_payloads,
    make_handler_class,
    models_list_body,
    sync_response_body,
)


def _tools(*names: str) -> list[dict[str, Any]]:
    return [{"type": "function", "function": {"name": n, "parameters": {"type": "object", "properties": {}}}} for n in names]


def test_models_list_includes_mock_id():
    body = models_list_body()
    ids = [row["id"] for row in body["data"]]
    assert MOCK_MODEL_ID in ids
    assert MOCK_STT_MODEL_ID in ids
    chat = next(row for row in body["data"] if row["id"] == MOCK_MODEL_ID)
    assert "audio" in chat["architecture"]["input_modalities"]


def test_chit_chat_html():
    out = decide_completion(
        {"messages": [{"role": "user", "content": "hello there"}], "tools": _tools("web_research", "apply_document_content")},
        MockLLMConfig(delay_ms=0),
        _TurnState(),
    )
    assert out.tool_name is None
    assert out.content is not None
    assert "<p>" in out.content
    assert "hello there" in out.content or "hello" in out.content


def test_research_keyword_calls_web_research():
    out = decide_completion(
        {
            "messages": [{"role": "user", "content": "look up the latest Python release"}],
            "tools": _tools("web_research"),
        },
        MockLLMConfig(delay_ms=0),
    )
    assert out.tool_name == "web_research"
    assert out.tool_args and "latest Python" in out.tool_args["query"]


def test_tool_result_becomes_html_summary():
    out = decide_completion(
        {
            "messages": [
                {"role": "user", "content": "look up cats"},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "c1",
                            "type": "function",
                            "function": {"name": "web_research", "arguments": '{"query":"cats"}'},
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "c1", "content": "Findings\n- Cats are mammals"},
            ],
            "tools": _tools("web_research"),
        },
        MockLLMConfig(delay_ms=0),
    )
    assert out.tool_name is None
    assert out.content is not None
    assert "<p>" in out.content
    assert "Cats are mammals" in out.content


def test_smol_offline_final_answer_plain():
    out = decide_completion(
        {
            "messages": [{"role": "user", "content": "### CURRENT QUERY:\nPython 3.13"}],
            "tools": _tools("web_search", "visit_webpage", "final_answer"),
        },
        MockLLMConfig(offline=True),
    )
    assert out.tool_name == "final_answer"
    answer = (out.tool_args or {}).get("answer") or ""
    assert "<p>" not in answer
    assert "Python 3.13" in answer
    assert "- " in answer
    assert "Step budget" not in answer


def test_smol_offline_ignores_step_budget_banner():
    """Live smolagents prefixes each turn with a step-budget user blob (Packet E1)."""
    out = decide_completion(
        {
            "messages": [
                {
                    "role": "system",
                    "content": 'Example Action:\n{"name": "web_search", "arguments": "Population Guangzhou"}',
                },
                {
                    "role": "user",
                    "content": (
                        "Step budget: 0 step(s) used, 15 step(s) remaining (maximum 15). "
                        "You are on step 1 of 15.\nNew task:\n### CONVERSATION HISTORY:\nNone\n\n"
                        "### CURRENT QUERY:\nlook up latest Python"
                    ),
                },
            ],
            "tools": _tools("web_search", "visit_webpage", "final_answer"),
        },
        MockLLMConfig(offline=True),
    )
    assert out.tool_name == "final_answer"
    answer = (out.tool_args or {}).get("answer") or ""
    assert "look up latest Python" in answer
    assert "Step budget" not in answer


def test_smol_online_sequence():
    tools = _tools("web_search", "visit_webpage", "final_answer")
    cfg = MockLLMConfig(offline=False)
    first = decide_completion({"messages": [{"role": "user", "content": "q"}], "tools": tools}, cfg)
    assert first.tool_name == "web_search"
    second = decide_completion(
        {
            "messages": [
                {"role": "user", "content": "q"},
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "c1",
                            "type": "function",
                            "function": {"name": "web_search", "arguments": '{"query":"q"}'},
                        }
                    ],
                },
                {"role": "tool", "content": "1. https://example.com/a Title"},
            ],
            "tools": tools,
        },
        cfg,
    )
    assert second.tool_name == "visit_webpage"
    assert (second.tool_args or {}).get("url", "").startswith("http")
    third = decide_completion(
        {
            "messages": [
                {"role": "user", "content": "q"},
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "c1",
                            "type": "function",
                            "function": {"name": "web_search", "arguments": "{}"},
                        }
                    ],
                },
                {"role": "tool", "content": "hits"},
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "c2",
                            "type": "function",
                            "function": {"name": "visit_webpage", "arguments": "{}"},
                        }
                    ],
                },
                {"role": "tool", "content": "page body"},
            ],
            "tools": tools,
        },
        cfg,
    )
    assert third.tool_name == "final_answer"


def test_smol_action_in_content_advances_search_then_visit():
    """smolagents memory is Action JSON in user content, not assistant.tool_calls (Packet E2)."""
    tools = _tools("web_search", "visit_webpage", "final_answer")
    cfg = MockLLMConfig(offline=False)
    system = 'Example Action:\n{"name": "web_search", "arguments": "Population Guangzhou"}'
    task = (
        "Step budget: 0 step(s) used, 15 remaining.\nNew task:\n"
        "### CURRENT QUERY:\nlook up latest Python"
    )
    first = decide_completion(
        {"messages": [{"role": "system", "content": system}, {"role": "user", "content": task}], "tools": tools},
        cfg,
    )
    assert first.tool_name == "web_search"
    assert (first.tool_args or {}).get("query") == "look up latest Python"

    obs = (
        "Step budget: 1 step(s) used, 14 remaining.\n"
        'Action:\n{"name": "web_search", "arguments": {"query": "look up latest Python"}}\n'
        "Observation:\n<h2>Search Results</h2>"
        "<a href='https://www.python.org/downloads/'>Download Python</a>"
    )
    second = decide_completion(
        {
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": task},
                {"role": "user", "content": obs},
            ],
            "tools": tools,
        },
        cfg,
    )
    assert second.tool_name == "visit_webpage"
    assert (second.tool_args or {}).get("url") == "https://www.python.org/downloads/"

    visited = (
        obs
        + '\nAction:\n{"name": "visit_webpage", "arguments": {"url": "https://www.python.org/downloads/"}}\n'
        "Observation:\nPython 3.14 notes"
    )
    third = decide_completion(
        {
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": task},
                {"role": "user", "content": visited},
            ],
            "tools": tools,
        },
        cfg,
    )
    assert third.tool_name == "final_answer"
    assert "look up latest Python" in ((third.tool_args or {}).get("answer") or "")
    assert "Step budget" not in ((third.tool_args or {}).get("answer") or "")


def test_sync_tool_call_arguments_are_json_string():
    body = sync_response_body(
        Completion(tool_name="web_research", tool_args={"query": "x"}, finish_reason="tool_calls"),
        "m",
    )
    args = body["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"]
    assert isinstance(args, str)
    assert json.loads(args)["query"] == "x"


def _serve(config: MockLLMConfig):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler_class(config))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address[:2]
    yield f"http://{host}:{port}"
    httpd.shutdown()
    thread.join(timeout=2)


@pytest.fixture
def mock_http():
    yield from _serve(MockLLMConfig(delay_ms=0, offline=True))


@pytest.fixture
def mock_http_fail():
    bases = {}
    servers = []
    for status, fail in ((500, "http500"), (429, "http429")):
        config = MockLLMConfig(delay_ms=0, fail=fail)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler_class(config))
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        host, port = httpd.server_address[:2]
        bases[status] = f"http://{host}:{port}"
        servers.append((httpd, thread))
    yield bases
    for httpd, thread in servers:
        httpd.shutdown()
        thread.join(timeout=2)


@pytest.fixture
def mock_http_hang():
    yield from _serve(MockLLMConfig(delay_ms=0, fail="hang", fail_after_chunks=3))


@pytest.fixture
def mock_http_comments():
    yield from _serve(MockLLMConfig(delay_ms=0, sse_comments=True))


def _post_json(url: str, payload: dict[str, Any]) -> Any:
    req = Request(url, data=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json"}, method="POST")
    with urlopen(req, timeout=5) as resp:
        return resp.read().decode("utf-8"), resp.headers.get_content_type()


def test_http_models_and_health(mock_http):
    with urlopen(mock_http + "/health", timeout=5) as resp:
        assert resp.read() == b"ok"
    with urlopen(mock_http + "/v1/models", timeout=5) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    assert data["data"][0]["id"] == MOCK_MODEL_ID


def test_http_stream_chit_chat(mock_http):
    raw, ctype = _post_json(
        mock_http + "/v1/chat/completions",
        {
            "model": MOCK_MODEL_ID,
            "stream": True,
            "messages": [{"role": "user", "content": "hello mock"}],
            "tools": _tools("web_research"),
        },
    )
    assert "text/event-stream" in ctype or "event-stream" in ctype or "<p>" in raw
    assert "[DONE]" in raw
    assert "<p>" in raw


def test_http_stream_web_research_tool(mock_http):
    raw, unused_ctype = _post_json(
        mock_http + "/v1/chat/completions",
        {
            "model": MOCK_MODEL_ID,
            "stream": True,
            "messages": [{"role": "user", "content": "look up pandas"}],
            "tools": _tools("web_research"),
        },
    )
    assert unused_ctype is not None
    assert "web_research" in raw
    assert "tool_calls" in raw


def test_http_sync_offline_final_answer(mock_http):
    raw, unused_ctype = _post_json(
        mock_http + "/v1/chat/completions",
        {
            "model": MOCK_MODEL_ID,
            "stream": False,
            "messages": [{"role": "user", "content": "query"}],
            "tools": _tools("web_search", "final_answer"),
        },
    )
    assert unused_ctype is not None
    body = json.loads(raw)
    tc = body["choices"][0]["message"]["tool_calls"][0]
    assert tc["function"]["name"] == "final_answer"
    args = json.loads(tc["function"]["arguments"])
    assert "<p>" not in args["answer"]


def test_http_404(mock_http):
    with pytest.raises(HTTPError) as err:
        urlopen(mock_http + "/nope", timeout=5)
    assert err.value.code == 404


def test_comment_with_document_text_calls_add_comment():
    doc_system_msg = (
        "You are WriterAgent.\n\n"
        "[DOCUMENT CONTENT]\n"
        "Document length: 30 characters.\n\n"
        "[DOCUMENT START]\n"
        "Welcome to the document test.\n"
        "[END DOCUMENT]"
    )
    out = decide_completion(
        {
            "messages": [
                {"role": "system", "content": doc_system_msg},
                {"role": "user", "content": "Please add a comment to this document"},
            ],
            "tools": _tools("add_comment", "apply_document_content"),
        },
        MockLLMConfig(delay_ms=0),
    )
    assert out.tool_name == "add_comment"
    assert out.tool_args is not None
    assert out.tool_args["search"] == "Welcome"
    assert "Mock comment" in out.tool_args["content"]


def test_comment_with_empty_document_calls_apply_document_content():
    empty_system_msg = (
        "You are WriterAgent.\n\n"
        "[DOCUMENT CONTENT]\n"
        "[DOCUMENT START]\n\n"
        "[END DOCUMENT]"
    )
    out = decide_completion(
        {
            "messages": [
                {"role": "system", "content": empty_system_msg},
                {"role": "user", "content": "insert a comment"},
            ],
            "tools": _tools("add_comment", "apply_document_content"),
        },
        MockLLMConfig(delay_ms=0),
    )
    assert out.tool_name == "apply_document_content"
    assert out.tool_args is not None
    assert out.tool_args["target"] == "beginning"
    assert len(out.tool_args["content"]) > 0


def test_comment_after_apply_content_step():
    out = decide_completion(
        {
            "messages": [
                {"role": "user", "content": "insert a comment"},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "c1",
                            "type": "function",
                            "function": {
                                "name": "apply_document_content",
                                "arguments": '{"target":"beginning","content":["<p>Hello world</p>"]}',
                            },
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "c1", "content": '{"status": "ok", "inserted": true}'},
            ],
            "tools": _tools("add_comment", "apply_document_content"),
        },
        MockLLMConfig(delay_ms=0),
    )
    assert out.tool_name == "add_comment"
    assert out.tool_args is not None
    assert out.tool_args["search"] == "Hello"


def test_comment_after_add_comment_step():
    out = decide_completion(
        {
            "messages": [
                {"role": "user", "content": "insert a comment"},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "c2",
                            "type": "function",
                            "function": {
                                "name": "add_comment",
                                "arguments": '{"search":"Hello","content":"Mock comment"}',
                            },
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "c2", "content": '{"status": "ok", "comment_added": true}'},
            ],
            "tools": _tools("add_comment", "apply_document_content"),
        },
        MockLLMConfig(delay_ms=0),
    )
    assert out.tool_name is None
    assert out.content is not None
    assert "Comment" in out.content
    assert out.finish_reason == "stop"


def test_detect_scenario_phrases_and_force():
    assert detect_scenario("please keep talking") == "ramble"
    assert detect_scenario("say nothing now") == "empty"
    assert detect_scenario("hello", forced="flood") == "flood"
    assert detect_scenario("hello") == ""


def test_ramble_and_empty_and_flood():
    cfg = MockLLMConfig(delay_ms=0)
    ramble = decide_completion(
        {"messages": [{"role": "user", "content": "keep talking"}], "tools": _tools("web_research")},
        cfg,
    )
    assert ramble.ramble_parts == RAMBLE_PARTS
    assert ramble.content and ramble.content.count("word") >= 50
    empty = decide_completion(
        {"messages": [{"role": "user", "content": "say nothing"}], "tools": _tools("web_research")},
        cfg,
    )
    assert empty.content is None
    assert empty.finish_reason == "length"
    assert not completion_tool_calls(empty)
    flood = decide_completion(
        {"messages": [{"role": "user", "content": "fill the sidebar"}], "tools": _tools("web_research")},
        cfg,
    )
    assert flood.content and flood.content.count("<p>") >= 40
    assert "<table>" in flood.content


def test_think_modes():
    cfg = MockLLMConfig(delay_ms=0)
    think = decide_completion(
        {"messages": [{"role": "user", "content": "think out loud"}], "tools": _tools("web_research")},
        cfg,
    )
    assert think.reasoning
    assert think.reasoning_mode == "reasoning"
    assert "<p>" in (think.content or "")
    tags = decide_completion(
        {"messages": [{"role": "user", "content": "think tags please"}], "tools": _tools("web_research")},
        cfg,
    )
    assert tags.reasoning_mode == "think_tags"
    assert "<think>" in (tags.content or "")
    details = decide_completion(
        {"messages": [{"role": "user", "content": "reasoning details"}], "tools": _tools("web_research")},
        cfg,
    )
    assert details.reasoning_mode == "details"
    body = sync_response_body(details, "m")
    msg = body["choices"][0]["message"]
    assert "reasoning_content" in msg
    assert msg["reasoning_details"][0]["type"] == "reasoning.text"


def test_delegate_when_advertised_else_html():
    cfg = MockLLMConfig(delay_ms=0)
    hit = decide_completion(
        {
            "messages": [{"role": "user", "content": "outline this document"}],
            "tools": _tools("delegate_to_specialized_writer_toolset", "web_research"),
        },
        cfg,
    )
    assert hit.tool_name == "delegate_to_specialized_writer_toolset"
    assert hit.tool_args is not None
    assert hit.tool_args["domain"] == "document_research"
    miss = decide_completion(
        {"messages": [{"role": "user", "content": "outline this document"}], "tools": _tools("web_research")},
        cfg,
    )
    assert miss.tool_name is None
    assert miss.content and "<p>" in miss.content


def test_specialized_inner_tree_then_final_answer():
    tools = _tools("get_document_tree", "final_answer")
    cfg = MockLLMConfig(delay_ms=0)
    first = decide_completion({"messages": [{"role": "user", "content": "q"}], "tools": tools}, cfg)
    assert first.tool_name == "get_document_tree"
    second = decide_completion(
        {
            "messages": [
                {"role": "user", "content": "q"},
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "c1",
                            "type": "function",
                            "function": {"name": "get_document_tree", "arguments": "{}"},
                        }
                    ],
                },
                {"role": "tool", "content": '{"headings": []}'},
            ],
            "tools": tools,
        },
        cfg,
    )
    assert second.tool_name == "final_answer"
    assert "outline" in ((second.tool_args or {}).get("answer") or "").lower()


def test_specialized_inner_without_tree_finishes_immediately():
    """Live document_research inner HTTP has specialized_workflow_finished, often no tree tool (Packet E7)."""
    tools = _tools("search_nearby_files", "specialized_workflow_finished")
    out = decide_completion(
        {"messages": [{"role": "user", "content": "outline this"}], "tools": tools},
        MockLLMConfig(delay_ms=0),
    )
    assert out.tool_name == "specialized_workflow_finished"
    assert "outline" in ((out.tool_args or {}).get("answer") or "").lower()
    assert out.content is None


def test_mutate_wrapup_is_not_research_wording():
    out = decide_completion(
        {
            "messages": [
                {"role": "user", "content": "insert filler"},
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "c1",
                            "type": "function",
                            "function": {"name": "apply_document_content", "arguments": "{}"},
                        }
                    ],
                },
                {"role": "tool", "content": '{"status": "ok", "message": "Inserted content at end."}'},
            ],
            "tools": _tools("apply_document_content", "web_research"),
        },
        MockLLMConfig(delay_ms=0),
    )
    assert out.tool_name is None
    assert out.content is not None
    assert "Inserted content" in out.content
    assert "I looked that up" not in out.content


def test_parallel_two_core_tools():
    out = decide_completion(
        {
            "messages": [{"role": "user", "content": "run two tools please"}],
            "tools": _tools("search_in_document", "get_document_tree", "web_research"),
        },
        MockLLMConfig(delay_ms=0),
    )
    names = [n for n, _a in completion_tool_calls(out)]
    assert names == ["search_in_document", "get_document_tree"]
    body = sync_response_body(out, "m")
    tcs = body["choices"][0]["message"]["tool_calls"]
    assert len(tcs) == 2
    chunks = list(iter_sse_payloads(out, "m"))
    indexes = set()
    for obj in chunks:
        for tc in obj["choices"][0]["delta"].get("tool_calls") or []:
            if "index" in tc:
                indexes.add(tc["index"])
    assert indexes == {0, 1}


def test_parallel_missing_tools_falls_back_html():
    out = decide_completion(
        {
            "messages": [{"role": "user", "content": "in parallel"}],
            "tools": _tools("web_research"),
        },
        MockLLMConfig(delay_ms=0),
    )
    assert out.tool_name is None
    assert out.content and "<p>" in out.content


def test_mutate_and_calc_draw_thin_tools():
    cfg = MockLLMConfig(delay_ms=0)
    mut = decide_completion(
        {
            "messages": [{"role": "user", "content": "insert filler"}],
            "tools": _tools("apply_document_content"),
        },
        cfg,
    )
    assert mut.tool_name == "apply_document_content"
    assert mut.tool_args and mut.tool_args["target"] == "end"
    sheets = decide_completion(
        {"messages": [{"role": "user", "content": "list sheets"}], "tools": _tools("list_sheets")},
        cfg,
    )
    assert sheets.tool_name == "list_sheets"
    pages = decide_completion(
        {"messages": [{"role": "user", "content": "list pages"}], "tools": _tools("list_pages")},
        cfg,
    )
    assert pages.tool_name == "list_pages"


def test_fail_and_hang_completions():
    cfg = MockLLMConfig(delay_ms=0)
    boom = decide_completion(
        {"messages": [{"role": "user", "content": "crash the stream"}], "tools": _tools("web_research")},
        cfg,
    )
    assert boom.http_error == 500
    limited = decide_completion(
        {"messages": [{"role": "user", "content": "rate limit me"}], "tools": _tools("web_research")},
        cfg,
    )
    assert limited.http_error == 429
    hung = decide_completion(
        {"messages": [{"role": "user", "content": "hang the stream"}], "tools": _tools("web_research")},
        cfg,
    )
    assert hung.hang is True


def test_forced_scenario_overrides_phrase():
    out = decide_completion(
        {"messages": [{"role": "user", "content": "look up cats"}], "tools": _tools("web_research")},
        MockLLMConfig(delay_ms=0, scenario="empty"),
    )
    assert out.finish_reason == "length"
    assert out.tool_name is None


def test_http_500_and_429(mock_http_fail):
    from urllib.error import HTTPError
    from urllib.request import Request, urlopen
    import json as json_mod

    def post(base: str):
        req = Request(
            base + "/v1/chat/completions",
            data=json_mod.dumps({"model": MOCK_MODEL_ID, "messages": [{"role": "user", "content": "hi"}]}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urlopen(req, timeout=5)

    with pytest.raises(HTTPError) as err500:
        post(mock_http_fail[500])
    assert err500.value.code == 500
    with pytest.raises(HTTPError) as err429:
        post(mock_http_fail[429])
    assert err429.value.code == 429


def test_http_hang_stream_incomplete(mock_http_hang):
    from urllib.request import Request, urlopen
    import json as json_mod

    req = Request(
        mock_http_hang + "/v1/chat/completions",
        data=json_mod.dumps(
            {
                "model": MOCK_MODEL_ID,
                "stream": True,
                "messages": [{"role": "user", "content": "hello"}],
            }
        ).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(req, timeout=2) as resp:
        raw = resp.read().decode("utf-8")
    assert "[DONE]" not in raw
    assert raw.count("data:") >= 1


def test_http_ramble_many_chunks(mock_http):
    raw, unused_ctype = _post_json(
        mock_http + "/v1/chat/completions",
        {
            "model": MOCK_MODEL_ID,
            "stream": True,
            "messages": [{"role": "user", "content": "keep talking"}],
            "tools": _tools("web_research"),
        },
    )
    assert unused_ctype is not None
    assert raw.count("data:") > 50
    assert "[DONE]" in raw


def test_http_sse_comments(mock_http_comments):
    raw, unused_ctype = _post_json(
        mock_http_comments + "/v1/chat/completions",
        {
            "model": MOCK_MODEL_ID,
            "stream": True,
            "messages": [{"role": "user", "content": "hello mock"}],
        },
    )
    assert unused_ctype is not None
    assert ": ping" in raw
    assert "[DONE]" in raw
    assert "<p>" in raw


def test_iter_sse_reasoning_split():
    chunks = list(
        iter_sse_payloads(
            Completion(content="Hi.", reasoning="One two three four five six", reasoning_mode="reasoning"),
            "m",
        )
    )
    reasoning_bits = [c["choices"][0]["delta"].get("reasoning") for c in chunks if c["choices"][0]["delta"].get("reasoning")]
    assert len(reasoning_bits) >= 2


def _tiny_wav_bytes() -> bytes:
    import io
    import wave

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(b"\x00\x00" * 1600)  # 0.1s silence
    return buf.getvalue()


def _audio_user(text: str = "", wav_b64: str | None = None) -> dict[str, Any]:
    import base64

    parts: list[dict[str, Any]] = []
    if text:
        parts.append({"type": "text", "text": text})
    data = wav_b64 if wav_b64 is not None else base64.b64encode(_tiny_wav_bytes()).decode("ascii")
    parts.append({"type": "input_audio", "input_audio": {"data": data, "format": "wav"}})
    return {"role": "user", "content": parts}


def test_native_audio_html_contains_transcript():
    import base64

    b64 = base64.b64encode(_tiny_wav_bytes()).decode("ascii")
    out = decide_completion(
        {
            "messages": [_audio_user("please listen", wav_b64=b64)],
            "tools": _tools("web_research"),
        },
        MockLLMConfig(delay_ms=0),
    )
    assert out.tool_name is None
    assert out.content is not None
    assert "<p>" in out.content
    assert DEFAULT_TRANSCRIPT in out.content
    assert "please listen" in out.content
    assert "~0.1s" in out.content


def test_audio_only_not_empty_chat():
    out = decide_completion(
        {"messages": [_audio_user("")], "tools": _tools("web_research")},
        MockLLMConfig(delay_ms=0),
    )
    assert out.content is not None
    assert DEFAULT_TRANSCRIPT in out.content
    assert "<p>" in out.content


def test_stt_prompt_plain_transcript():
    out = decide_completion(
        {
            "messages": [
                _audio_user("Transcribe this audio exactly. Output ONLY the transcript. No preamble, no markers.")
            ]
        },
        MockLLMConfig(delay_ms=0, transcript="Custom line."),
    )
    assert out.content == "Custom line."
    assert "<p>" not in (out.content or "")


def test_audio_phrase_still_research():
    out = decide_completion(
        {
            "messages": [_audio_user("look up pandas")],
            "tools": _tools("web_research"),
        },
        MockLLMConfig(delay_ms=0),
    )
    assert out.tool_name == "web_research"


def test_http_transcriptions_json(mock_http):
    import base64

    b64 = base64.b64encode(_tiny_wav_bytes()).decode("ascii")
    raw, unused_ctype = _post_json(
        mock_http + "/v1/audio/transcriptions",
        {"model": MOCK_STT_MODEL_ID, "input_audio": {"data": b64, "format": "wav"}},
    )
    assert unused_ctype is not None
    body = json.loads(raw)
    assert body["text"] == DEFAULT_TRANSCRIPT


def test_http_transcriptions_multipart(mock_http):
    wav = _tiny_wav_bytes()
    boundary = "Boundary-testmock"
    body = b"\r\n".join(
        [
            f"--{boundary}".encode(),
            b'Content-Disposition: form-data; name="file"; filename="clip.wav"',
            b"Content-Type: audio/wav",
            b"",
            wav,
            f"--{boundary}".encode(),
            b'Content-Disposition: form-data; name="model"',
            b"",
            MOCK_STT_MODEL_ID.encode(),
            f"--{boundary}--".encode(),
            b"",
        ]
    )
    req = Request(
        mock_http + "/v1/audio/transcriptions",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    with urlopen(req, timeout=5) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    assert data["text"] == DEFAULT_TRANSCRIPT


def test_http_models_lists_stt_id(mock_http):
    with urlopen(mock_http + "/v1/models", timeout=5) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    ids = [row["id"] for row in data["data"]]
    assert MOCK_STT_MODEL_ID in ids


# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for llm_chat_eval inner delegate normalization and transcript capture."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parents[2]
_PO = _REPO / "scripts" / "prompt_optimization"
for p in (str(_REPO), str(_PO)):
    if p not in sys.path:
        sys.path.insert(0, p)

import llm_chat_eval  # noqa: E402
from plugin.doc.specialized_catalog import NO_EDIT_TOOL_NOTE  # noqa: E402


class _FakeInnerClient:
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self._responses = list(responses)
        self.calls: list[list[dict[str, Any]]] = []

    def request_with_tools(
        self,
        messages: list[dict[str, Any]],
        **kwargs: Any,
    ) -> dict[str, Any]:
        del kwargs
        self.calls.append(list(messages))
        return self._responses.pop(0)


def _tc(name: str, args: dict[str, Any], tid: str = "call_1") -> dict[str, Any]:
    return {
        "id": tid,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


def test_inner_delegate_preserves_finish_answer_and_no_edit_note(monkeypatch: Any) -> None:
    """H3: specialized_workflow_finished.answer and NO_EDIT_TOOL_NOTE reach the outer agent."""
    from eval_worlds import CalcWorld

    fake_inner = _FakeInnerClient(
        [
            {
                "content": "",
                "reasoning": "Single-column tool cannot do two keys at once.",
                "tool_calls": [
                    _tc(
                        "specialized_workflow_finished",
                        {"answer": "Cannot perform a two-key sort in one call."},
                    )
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            }
        ]
    )
    monkeypatch.setattr(
        llm_chat_eval,
        "_build_api_config",
        lambda **kw: {"model": kw["model"]},
    )
    monkeypatch.setattr(llm_chat_eval, "LlmClient", lambda cfg, ctx: fake_inner)
    monkeypatch.setattr(
        llm_chat_eval,
        "_eval_tools",
        lambda **kw: [],
    )

    state = CalcWorld("Product\tRevenue\nWidget\t1200")
    capture: dict[str, Any] = {}
    out = json.loads(
        llm_chat_eval._run_specialized_inner(
            kind="calc",
            domain="ranges",
            task="Sort by Revenue desc then Product asc",
            state=state,
            client=None,
            endpoint="http://unused",
            api_key="",
            model="test-model",
            backend="string",
            max_tokens=256,
            verbose=False,
            student="llm",
            usage_acc={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            trace=[],
            _capture=capture,
        )
    )
    assert out["status"] == "ok"
    assert out["finished"] is True
    assert out["answer"] == "Cannot perform a two-key sort in one call."
    assert out["note"] == NO_EDIT_TOOL_NOTE
    assert len(capture["inner_runs"]) == 1
    inner_run = capture["inner_runs"][0]
    assert inner_run["rounds"][0]["reasoning"] == "Single-column tool cannot do two keys at once."
    assert any(m["role"] == "assistant" for m in inner_run["messages"])


def test_inner_delegate_omits_no_edit_note_after_mutating_tool(monkeypatch: Any) -> None:
    """H3: once a mutating tool runs, NO_EDIT_TOOL_NOTE is not attached."""
    from eval_worlds import CalcWorld

    fake_inner = _FakeInnerClient(
        [
            {
                "content": "Sorting now.",
                "tool_calls": [
                    _tc(
                        "calc_sort_range",
                        {"range": ["A1:B3"], "sort_column": 1, "ascending": False, "has_header": True},
                        tid="c1",
                    )
                ],
                "usage": {},
            },
            {
                "content": "",
                "tool_calls": [
                    _tc("specialized_workflow_finished", {"answer": "Sorted A1:B3."}, tid="c2")
                ],
                "usage": {},
            },
        ]
    )
    monkeypatch.setattr(llm_chat_eval, "_build_api_config", lambda **kw: {"model": kw["model"]})
    monkeypatch.setattr(llm_chat_eval, "LlmClient", lambda cfg, ctx: fake_inner)
    monkeypatch.setattr(llm_chat_eval, "_eval_tools", lambda **kw: [])

    state = CalcWorld("Product\tRevenue\nWidget\t1200\nTool\t2100")
    out = json.loads(
        llm_chat_eval._run_specialized_inner(
            kind="calc",
            domain="ranges",
            task="Sort by Revenue desc",
            state=state,
            client=None,
            endpoint="http://unused",
            api_key="",
            model="test-model",
            backend="string",
            max_tokens=256,
            verbose=False,
            student="llm",
            usage_acc={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            trace=[],
        )
    )
    assert out["status"] == "ok"
    assert out["answer"] == "Sorted A1:B3."
    assert "note" not in out


def test_transcript_includes_inner_assistant_messages(tmp_path: Path, monkeypatch: Any) -> None:
    """Transcript JSON records inner_runs with assistant messages and reasoning."""
    outer_responses = [
        {
            "content": "Delegating sort.",
            "reasoning": "Outer reasoning",
            "tool_calls": [
                _tc(
                    "delegate_to_specialized_calc_toolset",
                    {"domain": "ranges", "task": "Sort A1:B3"},
                    tid="outer_1",
                )
            ],
            "usage": {},
        },
        {
            "content": "Done.",
            "tool_calls": None,
            "usage": {},
        },
    ]
    inner_responses = [
        {
            "content": "Let me sort that.",
            "reasoning": "Inner reasoning step",
            "tool_calls": [
                _tc(
                    "calc_sort_range",
                    {"range": ["A1:B3"], "sort_column": 1, "ascending": False, "has_header": True},
                    tid="inner_1",
                )
            ],
            "usage": {},
        },
        {
            "content": "",
            "tool_calls": [
                _tc("specialized_workflow_finished", {"answer": "Sorted."}, tid="inner_2")
            ],
            "usage": {},
        },
    ]
    clients = [_FakeInnerClient(outer_responses), _FakeInnerClient(inner_responses)]
    monkeypatch.setattr(llm_chat_eval, "_build_api_config", lambda **kw: {"model": kw["model"]})
    monkeypatch.setattr(llm_chat_eval, "LlmClient", lambda cfg, ctx: clients.pop(0))
    monkeypatch.setattr(llm_chat_eval, "_eval_tools", lambda **kw: [])

    llm_chat_eval.set_transcript_dir(str(tmp_path))
    try:
        _final, _usage, err, _trace = llm_chat_eval.run_llm_chat_eval(
            system_prompt="Test prompt",
            document_content="Product\tRevenue\nWidget\t1200\nTool\t2100",
            user_question="Sort by Revenue",
            endpoint="http://unused",
            api_key="",
            model="test/model",
            task_id="data_sorting",
        )
    finally:
        llm_chat_eval.set_transcript_dir(None)

    assert err is None
    data = json.loads((tmp_path / "test_model" / "data_sorting.json").read_text(encoding="utf-8"))
    assert len(data["inner_runs"]) == 1
    run = data["inner_runs"][0]
    assert run["domain"] == "ranges"
    asst_msgs = [m for m in run["messages"] if m["role"] == "assistant"]
    assert len(asst_msgs) == 2
    assert asst_msgs[0]["content"] == "Let me sort that."
    assert run["rounds"][0]["reasoning"] == "Inner reasoning step"


def test_eval_nudge_fires_once_on_edit_intent_zero_tools(monkeypatch) -> None:
    from plugin.chatbot.tool_loop_state import EDIT_NUDGE_MESSAGE

    class _SeqClient:
        def __init__(self, responses):
            self._responses = list(responses)
            self.seen_messages = []

        def request_with_tools(self, messages, **_kwargs):
            self.seen_messages.append([dict(m) for m in messages])
            return self._responses.pop(0)

    apply_schema = [{"type": "function", "function": {"name": "writer_apply_content"}}]

    # Case 1: zero-tool edit reply gets nudged once and then applies the edit.
    client1 = _SeqClient([
        {"content": "<p>Rewritten paragraph.</p>", "tool_calls": None, "usage": {}},
        {
            "content": "",
            "tool_calls": [_tc("writer_apply_content", {"target": "full_document", "content": ["<p>Rewritten paragraph.</p>"]})],
            "usage": {},
        },
        {"content": "<p>Applied.</p>", "tool_calls": None, "usage": {}},
    ])
    monkeypatch.setattr(llm_chat_eval, "_build_api_config", lambda **kw: {"model": kw["model"]})
    monkeypatch.setattr(llm_chat_eval, "LlmClient", lambda cfg, ctx: client1)
    monkeypatch.setattr(llm_chat_eval, "_eval_tools", lambda **kw: apply_schema)

    final, _usage, err, trace = llm_chat_eval.run_llm_chat_eval(
        system_prompt="Sys",
        document_content="Original paragraph.",
        user_question="Rewrite this paragraph to be concise.",
        endpoint="http://unused",
        api_key="",
        model="test/model",
        task_id="logical_rewriting",
    )
    assert err is None
    assert len(client1.seen_messages) == 3
    assert client1.seen_messages[1][-1] == {"role": "user", "content": EDIT_NUDGE_MESSAGE}
    assert any(t["name"] == "writer_apply_content" for t in trace)
    assert "Rewritten paragraph." in final

    # Case 2: if the model still answers in chat after the nudge, it stops (no loop).
    client2 = _SeqClient([
        {"content": "<p>First chat.</p>", "tool_calls": None, "usage": {}},
        {"content": "<p>Second chat.</p>", "tool_calls": None, "usage": {}},
    ])
    monkeypatch.setattr(llm_chat_eval, "LlmClient", lambda cfg, ctx: client2)
    _final2, _usage2, err2, trace2 = llm_chat_eval.run_llm_chat_eval(
        system_prompt="Sys",
        document_content="Original paragraph.",
        user_question="Rewrite this paragraph to be concise.",
        endpoint="http://unused",
        api_key="",
        model="test/model",
        task_id="logical_rewriting",
    )
    assert err2 is None
    assert len(client2.seen_messages) == 2
    assert trace2 == []


def test_eval_nudge_skipped_on_plain_question_and_after_mutation(monkeypatch) -> None:
    class _SeqClient:
        def __init__(self, responses):
            self._responses = list(responses)
            self.calls = 0

        def request_with_tools(self, messages, **_kwargs):
            self.calls += 1
            return self._responses.pop(0)

    apply_schema = [{"type": "function", "function": {"name": "writer_apply_content"}}]
    monkeypatch.setattr(llm_chat_eval, "_build_api_config", lambda **kw: {"model": kw["model"]})
    monkeypatch.setattr(llm_chat_eval, "_eval_tools", lambda **kw: apply_schema)

    # Plain question: 1 call, no nudge.
    q_client = _SeqClient([{"content": "<p>It is about releases.</p>", "tool_calls": None, "usage": {}}])
    monkeypatch.setattr(llm_chat_eval, "LlmClient", lambda cfg, ctx: q_client)
    _f1, _u1, err1, _t1 = llm_chat_eval.run_llm_chat_eval(
        system_prompt="Sys",
        document_content="Release notes.",
        user_question="What is this document about?",
        endpoint="http://unused",
        api_key="",
        model="test/model",
        task_id="logical_rewriting",
    )
    assert err1 is None
    assert q_client.calls == 1

    # After mutating tool already ran: 2 calls total (tool call + final chat), no nudge.
    m_client = _SeqClient([
        {
            "content": "",
            "tool_calls": [_tc("writer_apply_content", {"target": "full_document", "content": ["<p>Updated.</p>"]})],
            "usage": {},
        },
        {"content": "<p>Done.</p>", "tool_calls": None, "usage": {}},
    ])
    monkeypatch.setattr(llm_chat_eval, "LlmClient", lambda cfg, ctx: m_client)
    _f2, _u2, err2, _t2 = llm_chat_eval.run_llm_chat_eval(
        system_prompt="Sys",
        document_content="Original.",
        user_question="Rewrite this paragraph to be concise.",
        endpoint="http://unused",
        api_key="",
        model="test/model",
        task_id="logical_rewriting",
    )
    assert err2 is None
    assert m_client.calls == 2

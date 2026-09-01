# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""
LlmClient + multi-round tool loop for prompt_optimization benchmarks.

Mirrors sidebar chat semantics (sync ``request_with_tools``) without DSPy ReAct.
"""
from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path
from typing import Any, Literal

from plugin.framework.errors import safe_json_loads
from plugin.framework.config import normalize_endpoint_url
from plugin.framework.client.llm_client import LlmClient

# PyUNO: `com.sun.star` imports inside plugin.writer require uno first.
# String/scripted eval must still run before `make ensure-uno` if uno is absent.
try:
    import uno as _uno  # noqa: F401
except ImportError:
    _uno = None

_SCRIPTS_PO = Path(__file__).resolve().parent
_REPO = _SCRIPTS_PO.parent.parent
for _p in (_REPO, _SCRIPTS_PO):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from dataset import task_kind
from eval_catalog import build_eval_tool_schemas
from eval_worlds import CalcWorld, DrawWorld, WriterWorld
from string_eval_tools import dispatch_string_tool


class _EvalMockContext:
    """Stand-in for UNO context when constructing ``LlmClient`` outside LibreOffice."""

    def __init__(self) -> None:
        self.mock_values: dict[str, Any] = {}

    def getValueByName(self, name: str) -> Any:
        return self.mock_values.get(name)

BackendKind = Literal["string", "lo"]


def _trace_entry(name: str, raw_args: str, result: str) -> dict[str, Any]:
    parsed: dict[str, Any] = {}
    try:
        loaded = json.loads(result) if result else {}
        if isinstance(loaded, dict):
            parsed = loaded
    except json.JSONDecodeError:
        parsed = {}
    return {
        "name": name,
        "arguments": raw_args,
        "result_status": str(parsed.get("status") or ""),
        "result_chars": len(result or ""),
        "error_code": str(parsed.get("code") or ""),
    }


def _build_api_config(
    *,
    endpoint: str,
    api_key: str,
    model: str,
    max_tool_rounds: int,
    request_timeout: int = 120,
) -> dict[str, Any]:
    ep = normalize_endpoint_url(endpoint)
    return {
        "endpoint": ep,
        "api_key": api_key,
        "model": model,
        "is_openwebui": False,
        "is_openrouter": "openrouter.ai" in ep.lower(),
        "is_together": "together.xyz" in ep.lower(),
        "request_timeout": request_timeout,
        "chat_max_tool_rounds": max_tool_rounds,
    }


def _merge_usage(acc: dict[str, int], usage: dict[str, Any] | None) -> None:
    if not usage:
        return
    pt = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
    ct = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
    tt = int(usage.get("total_tokens") or 0)
    if tt == 0 and (pt or ct):
        tt = pt + ct
    acc["prompt_tokens"] = acc.get("prompt_tokens", 0) + pt
    acc["completion_tokens"] = acc.get("completion_tokens", 0) + ct
    acc["total_tokens"] = acc.get("total_tokens", 0) + tt


def _dispatch_lo_tool(name: str, raw_args: str, *, verbose: bool) -> str:
    import tools_lo as tl

    args = safe_json_loads(raw_args)
    if not isinstance(args, dict):
        args = {}
    return tl.execute_lo_tool(name, args, verbose=verbose)


def run_llm_chat_eval(
    *,
    system_prompt: str,
    document_content: str,
    user_question: str,
    endpoint: str,
    api_key: str,
    model: str,
    backend: BackendKind = "string",
    max_tool_rounds: int = 25,
    max_tokens: int = 8192,
    bust_cache: bool = False,
    verbose: bool = False,
    student: Literal["llm", "scripted"] = "llm",
    task_id: str = "",
) -> tuple[str, dict[str, int], str | None, list[dict[str, Any]]]:
    """
    Run one eval example: multi-round tool loop.

    Returns ``(final_document, usage, error, trace)``. Trace entries are
    ``{name, arguments, result_status, result_chars, error_code}``.
    """
    kind = task_kind(task_id)
    tools = build_eval_tool_schemas(kind=kind)

    instruction = system_prompt
    if bust_cache:
        instruction = f"{instruction}\n\n[Eval: {uuid.uuid4().hex[:8]}]"

    if kind == "draw":
        state: WriterWorld | DrawWorld | CalcWorld = DrawWorld()
    elif kind == "calc":
        state = CalcWorld(document_content)
    else:
        state = WriterWorld(document_content)
    user_body = (
        f"[DOCUMENT CONTENT]\n{document_content}\n[END DOCUMENT]\n\n{user_question}"
    )
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": instruction},
        {"role": "user", "content": user_body},
    ]

    usage_acc: dict[str, int] = {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
    }
    err: str | None = None
    trace: list[dict[str, Any]] = []

    if student == "scripted":
        from scripted_student import ScriptedStudent

        client: Any = ScriptedStudent(task_id)
    else:
        cfg = _build_api_config(
            endpoint=endpoint,
            api_key=api_key,
            model=model,
            max_tool_rounds=max_tool_rounds,
        )
        client = LlmClient(cfg, _EvalMockContext())

    if backend == "lo":
        import tools_lo as tl

        tl.prepare_example(kind, document_content)

    rounds = max(1, int(max_tool_rounds))
    try:
        for round_i in range(rounds):
            resp = client.request_with_tools(
                messages,
                max_tokens=max_tokens,
                tools=tools,
                stream=False,
                model=model,
            )
            _merge_usage(usage_acc, resp.get("usage"))

            content = (resp.get("content") or "") or ""
            tool_calls = resp.get("tool_calls")
            if verbose:
                n_tc = len(tool_calls) if tool_calls else 0
                print(
                    f"  [LlmChat] round={round_i + 1} content_len={len(content)} "
                    f"tool_calls={n_tc} usage={resp.get('usage')!r}",
                    flush=True,
                )

            asst_msg: dict[str, Any] = {"role": "assistant", "content": content}
            if tool_calls:
                asst_msg["tool_calls"] = tool_calls
            messages.append(asst_msg)

            if not tool_calls:
                break

            for tc in tool_calls:
                tid = tc.get("id") or ""
                fn = (tc.get("function") or {}) if isinstance(tc, dict) else {}
                name = fn.get("name", "") if isinstance(fn, dict) else ""
                raw_args = fn.get("arguments", "") if isinstance(fn, dict) else ""
                if isinstance(name, str) and name:
                    if backend == "string":
                        if verbose:
                            print(
                                f"  [Tool] {name} args={raw_args[:500]!r}"
                                f"{'...' if len(raw_args or '') > 500 else ''}",
                                flush=True,
                            )
                        result = dispatch_string_tool(state, name, raw_args or "{}")
                        trace.append(_trace_entry(name, raw_args or "{}", result))
                        if verbose:
                            rp = result if len(result) <= 400 else result[:400] + "..."
                            print(f"  [Tool->] {rp!r}", flush=True)
                    else:
                        result = _dispatch_lo_tool(name, raw_args or "{}", verbose=verbose)
                        trace.append(_trace_entry(name, raw_args or "{}", result))
                else:
                    result = json.dumps(
                        {"status": "error", "message": "Missing tool name"}
                    )
                    trace.append(_trace_entry(name, raw_args or "{}", result))
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tid,
                        "content": result,
                    }
                )

    except Exception as e:
        err = str(e)
        return "", usage_acc, err, trace

    if backend == "lo":
        import tools_lo as tl

        final = tl.get_eval_export(kind) or ""
    else:
        if isinstance(state, DrawWorld):
            tree_res = state.get_draw_tree()
            final = json.dumps(tree_res, indent=2)
        elif isinstance(state, CalcWorld):
            final = json.dumps(state.snapshot(), indent=2)
        else:
            final = state.get_html()

    return final, usage_acc, err, trace

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
from eval_catalog import apply_schema_patches, build_eval_tool_schemas
from eval_worlds import CalcWorld, DrawWorld, WriterWorld
from string_eval_tools import dispatch_string_tool

DELEGATE_TOOL_NAMES = frozenset(
    {
        "delegate_to_specialized_writer_toolset",
        "delegate_to_specialized_calc_toolset",
        "delegate_to_specialized_draw_toolset",
    }
)
SPECIALIZED_FINISH = "specialized_workflow_finished"
INNER_MAX_ROUNDS = 12


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


def _parse_tool_call(tc: Any) -> tuple[str, str, str]:
    fn = (tc.get("function") or {}) if isinstance(tc, dict) else {}
    name = fn.get("name", "") if isinstance(fn, dict) else ""
    raw_args = fn.get("arguments", "") if isinstance(fn, dict) else ""
    tid = (tc.get("id") or "") if isinstance(tc, dict) else ""
    return str(name or ""), str(raw_args or "{}"), str(tid)


def _dispatch_world_tool(
    state: WriterWorld | DrawWorld | CalcWorld,
    name: str,
    raw_args: str,
    *,
    backend: BackendKind,
    verbose: bool,
) -> str:
    if name == SPECIALIZED_FINISH:
        args = safe_json_loads(raw_args)
        if not isinstance(args, dict):
            args = {}
        return json.dumps(
            {
                "status": "ok",
                "finished": True,
                "answer": args.get("answer"),
                "message": "Specialized task complete. Normal toolset restored.",
            },
            ensure_ascii=False,
        )
    if backend == "string":
        if verbose:
            print(
                f"  [Tool] {name} args={raw_args[:500]!r}"
                f"{'...' if len(raw_args or '') > 500 else ''}",
                flush=True,
            )
        result = dispatch_string_tool(state, name, raw_args or "{}")
        if verbose:
            rp = result if len(result) <= 400 else result[:400] + "..."
            print(f"  [Tool->] {rp!r}", flush=True)
        return result
    return _dispatch_lo_tool(name, raw_args or "{}", verbose=verbose)


def _eval_tools(
    *,
    kind: str,
    active_domain: str | None = None,
    schema_patches: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Same catalog as run_eval_multi, optionally with a MIPRO slice patched in."""
    tools = build_eval_tool_schemas(kind=kind, active_domain=active_domain)
    return apply_schema_patches(tools, schema_patches)


def _run_specialized_inner(
    *,
    kind: str,
    domain: str,
    task: str,
    state: WriterWorld | DrawWorld | CalcWorld,
    client: Any,
    endpoint: str,
    api_key: str,
    model: str,
    backend: BackendKind,
    max_tokens: int,
    verbose: bool,
    student: Literal["llm", "scripted"],
    usage_acc: dict[str, int],
    trace: list[dict[str, Any]],
    schema_patches: dict[str, dict[str, Any]] | None = None,
) -> str:
    """Bounded inner LlmClient loop (not SmolAgents) on the same world."""
    domain = str(domain or "").strip()
    task = str(task or "").strip()
    if not domain:
        return json.dumps(
            {"status": "error", "message": "domain is required for specialized delegation."}
        )
    if not task:
        return json.dumps(
            {"status": "error", "message": "task is required for specialized delegation."}
        )
    tools = _eval_tools(kind=kind, active_domain=domain, schema_patches=schema_patches)
    if student == "scripted":
        inner_client = client
        messages: list[dict[str, Any]] = []
    else:
        cfg = _build_api_config(
            endpoint=endpoint,
            api_key=api_key,
            model=model,
            max_tool_rounds=INNER_MAX_ROUNDS,
        )
        inner_client = LlmClient(cfg, _EvalMockContext())
        messages = [
            {
                "role": "system",
                "content": (
                    f"You are a specialized {kind} task executor for domain '{domain}'. "
                    "Use the provided tools to complete the task. "
                    f"Call {SPECIALIZED_FINISH} when done."
                ),
            },
            {"role": "user", "content": task},
        ]

    last_content = ""
    finished = False
    for round_i in range(INNER_MAX_ROUNDS):
        resp = inner_client.request_with_tools(
            messages,
            max_tokens=max_tokens,
            tools=tools,
            stream=False,
            model=model,
        )
        _merge_usage(usage_acc, resp.get("usage"))
        content = (resp.get("content") or "") or ""
        last_content = content
        tool_calls = resp.get("tool_calls")
        if verbose:
            n_tc = len(tool_calls) if tool_calls else 0
            print(
                f"  [Specialized {domain}] round={round_i + 1} "
                f"content_len={len(content)} tool_calls={n_tc}",
                flush=True,
            )
        asst_msg: dict[str, Any] = {"role": "assistant", "content": content}
        if tool_calls:
            asst_msg["tool_calls"] = tool_calls
        messages.append(asst_msg)
        if not tool_calls:
            break
        stop_inner = False
        for tc in tool_calls:
            name, raw_args, tid = _parse_tool_call(tc)
            if name in DELEGATE_TOOL_NAMES:
                result = json.dumps(
                    {
                        "status": "error",
                        "code": "unsupported_in_eval",
                        "message": "Nested specialized delegation is not allowed.",
                    }
                )
            else:
                result = _dispatch_world_tool(
                    state, name, raw_args, backend=backend, verbose=verbose
                )
            entry = _trace_entry(name, raw_args, result)
            entry["domain"] = domain
            entry["nested"] = True
            trace.append(entry)
            messages.append(
                {"role": "tool", "tool_call_id": tid, "content": result}
            )
            if name == SPECIALIZED_FINISH:
                finished = True
                stop_inner = True
        if stop_inner:
            break

    answer = last_content
    if not answer and finished:
        answer = "Specialized task complete."
    return json.dumps(
        {
            "status": "ok",
            "domain": domain,
            "finished": finished,
            "message": answer or "Specialized task finished.",
        },
        ensure_ascii=False,
    )


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
    tools: list[dict[str, Any]] | None = None,
    schema_patches: dict[str, dict[str, Any]] | None = None,
) -> tuple[str, dict[str, int], str | None, list[dict[str, Any]]]:
    """
    Run one eval example: multi-round tool loop.

    Returns ``(final_document, usage, error, trace)``. Trace entries are
    ``{name, arguments, result_status, result_chars, error_code}``.

    ``schema_patches`` rewrites advertised tool / param descriptions for
    this run (outer + specialized inner). Same dispatch as production eval.
    """
    kind = task_kind(task_id)
    if tools is None:
        tools = _eval_tools(kind=kind, schema_patches=schema_patches)
    elif schema_patches:
        tools = apply_schema_patches(tools, schema_patches)

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
    last_had_tools = False
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
                last_had_tools = False
                if round_i == 0 and not content.strip() and student != "scripted":
                    err = "empty model response"
                break

            last_had_tools = True
            for tc in tool_calls:
                name, raw_args, tid = _parse_tool_call(tc)
                if not name:
                    result = json.dumps(
                        {"status": "error", "message": "Missing tool name"}
                    )
                    trace.append(_trace_entry(name, raw_args, result))
                elif name in DELEGATE_TOOL_NAMES:
                    args = safe_json_loads(raw_args)
                    if not isinstance(args, dict):
                        args = {}
                    result = _run_specialized_inner(
                        kind=kind,
                        domain=str(args.get("domain") or ""),
                        task=str(args.get("task") or ""),
                        state=state,
                        client=client,
                        endpoint=endpoint,
                        api_key=api_key,
                        model=model,
                        backend=backend,
                        max_tokens=max_tokens,
                        verbose=verbose,
                        student=student,
                        usage_acc=usage_acc,
                        trace=trace,
                        schema_patches=schema_patches,
                    )
                    trace.append(_trace_entry(name, raw_args, result))
                else:
                    result = _dispatch_world_tool(
                        state, name, raw_args, backend=backend, verbose=verbose
                    )
                    trace.append(_trace_entry(name, raw_args, result))
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tid,
                        "content": result,
                    }
                )
        else:
            if last_had_tools:
                err = "max_tool_rounds exceeded"
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

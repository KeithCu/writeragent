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
from types import SimpleNamespace
from typing import Any, Literal

from plugin.framework.errors import safe_json_loads
from plugin.framework.utils import normalize_endpoint_url
from plugin.framework.schema_convert import to_openai_schema
from plugin.modules.http.client import LlmClient
from plugin.modules.writer.content import ApplyDocumentContent, GetDocumentContent

_SCRIPTS_PO = Path(__file__).resolve().parent
_REPO = _SCRIPTS_PO.parent.parent
for _p in (_REPO, _SCRIPTS_PO):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from string_eval_tools import StringDocState, dispatch_string_tool


class _EvalMockContext:
    """Stand-in for UNO context when constructing ``LlmClient`` outside LibreOffice."""

    def __init__(self) -> None:
        self.mock_values: dict[str, Any] = {}

    def getValueByName(self, name: str) -> Any:
        return self.mock_values.get(name)

BackendKind = Literal["string", "lo"]

_FIND_TEXT_SCHEMA = SimpleNamespace(
    name="find_text",
    description=(
        "Find text in the document. Returns JSON with status and ranges "
        "(start, end, text) in document character offsets."
    ),
    parameters={
        "type": "object",
        "properties": {
            "search": {"type": "string", "description": "Text to find."},
            "start": {
                "type": "integer",
                "description": "Character offset to start searching from.",
            },
            "limit": {
                "type": "integer",
                "description": "Max number of matches to return.",
            },
            "case_sensitive": {
                "type": "boolean",
                "description": "Case-sensitive match (default true).",
            },
        },
        "required": ["search"],
    },
)


def build_eval_tool_schemas() -> list[dict[str, Any]]:
    """OpenAI function schemas for the three eval tools (matches production tool names)."""
    g = GetDocumentContent()
    a = ApplyDocumentContent()
    return [
        to_openai_schema(g),
        to_openai_schema(a),
        to_openai_schema(_FIND_TEXT_SCHEMA),
    ]


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
    if verbose:
        print(f"  [Tool] {name} {args}", flush=True)
    if name == "get_document_content":
        ac = dict(args)
        scope = ac.pop("scope", "full")
        max_chars = ac.pop("max_chars", None)
        start = ac.pop("start", None)
        end = ac.pop("end", None)
        out = tl.get_document_content(scope, max_chars, start, end, **ac)
    elif name == "apply_document_content":
        ac = dict(args)
        content = str(ac.pop("content", "") or "")
        old_content = str(ac.pop("old_content") or "")
        all_matches = bool(ac.pop("all_matches", False))
        out = tl.apply_document_content(content, old_content, all_matches, **ac)
    elif name == "find_text":
        out = tl.find_text(
            str(args.get("search", "")),
            int(args.get("start", 0)),
            args.get("limit"),
            bool(args.get("case_sensitive", True)),
        )
    else:
        out = json.dumps({"status": "error", "message": f"Unknown tool: {name}"})
    if verbose:
        print(f"  [Tool->] {out[:500]!r}{'...' if len(out) > 500 else ''}", flush=True)
    return out


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
) -> tuple[str, dict[str, int], str | None]:
    """
    Run one eval example: multi-round tool loop, return (final_html, usage, error).

    ``final_html`` is the document after tool calls: in-memory HTML for ``string``,
    or Writer-exported HTML for ``lo`` (via ``get_content_as_html``).
    """
    cfg = _build_api_config(
        endpoint=endpoint,
        api_key=api_key,
        model=model,
        max_tool_rounds=max_tool_rounds,
    )
    client = LlmClient(cfg, _EvalMockContext())
    tools = build_eval_tool_schemas()

    instruction = system_prompt
    if bust_cache:
        instruction = f"{instruction}\n\n[Eval: {uuid.uuid4().hex[:8]}]"

    state = StringDocState(document_content)
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

    if backend == "lo":
        import tools_lo as tl

        tl.set_document(document_content)

    rounds = max(1, int(max_tool_rounds))
    try:
        for _round in range(rounds):
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
                    f"  [LlmChat] round={_round + 1} content_len={len(content)} "
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
                        if verbose:
                            rp = result if len(result) <= 400 else result[:400] + "..."
                            print(f"  [Tool->] {rp!r}", flush=True)
                    else:
                        result = _dispatch_lo_tool(name, raw_args or "{}", verbose=verbose)
                else:
                    result = json.dumps(
                        {"status": "error", "message": "Missing tool name"}
                    )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tid,
                        "content": result,
                    }
                )

    except Exception as e:
        err = str(e)
        return "", usage_acc, err

    if backend == "lo":
        import tools_lo as tl

        final = tl.get_content_as_html() or ""
    else:
        final = state.get_html()

    return final, usage_acc, err

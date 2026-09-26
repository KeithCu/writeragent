# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Host-side handlers for venv → LibreOffice tool RPC.

The wire format is the existing Pickle5 frame already used by ppt-master:
``{"type": "tool_call", "id": ..., "tool": ..., "args": {...}}``. The child
writes it on stdout; ``PythonWorkerManager`` replies on stdin. No extra
protocol is added here.

``python_tool_domain`` is host-only (never sent to the child):
- ``None`` — Run Python Script / chat: every registered tool except recursion.
- ``""`` — ``=PY()`` recalc: tool RPC is disabled (formula evaluation must
  stay side-effect free).
- a domain name — allow only that domain's proxies plus ``list_open_documents``.
- several names separated by commas — union of those domains. The inner agent
  from ``delegate_tool_domains`` passes the delegated list this way so one
  script can call each domain's ``writeragent_api`` tools.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from plugin.scripting.ipc import pack_pickle_frame

log = logging.getLogger(__name__)

# ``run_venv_python_script`` would re-enter the same warm worker while
# ``_io_lock`` is held and deadlock the pipe.
_BLOCKED_FROM_VENV = frozenset({"run_venv_python_script"})

# Host sentinel: disable venv → LO tool RPC (Calc ``=PY()`` recalc).
TOOL_RPC_DISABLED = ""

# ``get_active_document_type()`` always needs this, even in a scoped domain.
_ALWAYS_ALLOWED = frozenset({"list_open_documents"})

# String fetch of user/document scripts — not document mutation; allowed during =PY().
_NAMED_SCRIPT_TOOLS = frozenset({"get_named_python_script", "list_named_python_scripts"})


def domain_proxy_tool_names(python_tool_domain: str) -> frozenset[str] | None:
    """Proxy tool names for one specialized domain, without ``list_open_documents``.

    ``None`` means ``writeragent_api`` is not in this build (LibrePy). An empty
    set means the name is not a ``DOMAIN_TOOLS`` key. Specialized ``shapes``
    maps to key ``shape`` (same singularization ``generate_tool_proxies`` uses
    for ``footnotes`` → ``footnote`` and ``indexes`` → ``index``).
    """
    try:
        from plugin.scripting.writeragent_api import DOMAIN_TOOLS
    except ImportError:
        return None

    names = DOMAIN_TOOLS.get(python_tool_domain)
    if names is None:
        if python_tool_domain == "indexes":
            singular = "index"
        elif python_tool_domain.endswith("s") and python_tool_domain not in ("images", "styles", "forms"):
            singular = python_tool_domain[:-1]
        else:
            singular = python_tool_domain
        names = DOMAIN_TOOLS.get(singular)
    return frozenset(names or ())


def resolve_allowed_tools(python_tool_domain: str | None) -> frozenset[str] | None:
    """Return an allowlist, ``None`` (unrestricted minus blocked), or empty (disabled).

    A comma-separated list is the union of each domain (inner
    ``delegate_tool_domains``). Whitespace around commas is ignored.
    """
    if python_tool_domain is None:
        return None
    if python_tool_domain == TOOL_RPC_DISABLED:
        return frozenset()

    parts = [part.strip() for part in python_tool_domain.split(",") if part.strip()]
    if not parts:
        return frozenset()

    allowed: set[str] = set()
    for part in parts:
        names = domain_proxy_tool_names(part)
        if names is None:
            # LibrePy omits the generated proxy; there is nothing to allowlist.
            return frozenset()
        allowed |= names
    return frozenset(allowed) | _ALWAYS_ALLOWED


def execute_tool(
    tool_name: str,
    args: dict[str, Any] | None = None,
    *,
    caller: str = "script",
    allowed_tools: frozenset[str] | None = None,
) -> Any:
    """Dispatch a registered WriterAgent tool on the LO main thread (UNO-safe)."""
    if tool_name in _BLOCKED_FROM_VENV:
        raise RuntimeError(
            f"Tool {tool_name!r} cannot run from a venv script (it would re-enter the worker)."
        )
    if tool_name in _NAMED_SCRIPT_TOOLS:
        payload = args if isinstance(args, dict) else {}
        from plugin.framework.queue_executor import execute_on_main_thread

        return execute_on_main_thread(lambda: _execute_named_script_tool(tool_name, payload))
    if allowed_tools is not None and tool_name not in allowed_tools:
        if not allowed_tools:
            raise RuntimeError(
                "Document tool RPC is disabled during =PY() recalculation. "
                "Use Run Python Script… to call writeragent tools."
            )
        raise RuntimeError(
            f"Tool {tool_name!r} is not available in this Python tool domain."
        )

    payload = args if isinstance(args, dict) else {}

    def _run() -> Any:
        try:
            from plugin.doc.doc_type import is_calc, is_draw, is_writer
            from plugin.framework.tool import ToolContext
            from plugin.framework.uno_context import get_active_document, get_ctx
            from plugin.main import get_tools
        except ImportError as exc:
            raise RuntimeError(
                "Document tool RPC is not available in this extension build."
            ) from exc

        uno_ctx = get_ctx()
        doc = get_active_document(uno_ctx)
        if not doc:
            raise RuntimeError("No active document found to run tool")
        if is_calc(doc):
            doc_type = "calc"
        elif is_writer(doc):
            doc_type = "writer"
        elif is_draw(doc):
            doc_type = "draw"
        else:
            doc_type = ""
        registry = get_tools()
        tctx = ToolContext(
            doc=doc,
            ctx=uno_ctx,
            doc_type=doc_type,
            services=registry._services,
            caller=caller,
        )
        return registry.execute(tool_name, tctx, **payload)

    from plugin.framework.queue_executor import execute_on_main_thread

    return execute_on_main_thread(_run)


def _execute_named_script_tool(tool_name: str, payload: dict[str, Any]) -> Any:
    from plugin.framework.uno_context import get_active_document, get_ctx
    from plugin.scripting.document_scripts import get_document_scripts, get_user_scripts
    from plugin.scripting.named_scripts import (
        GET_NAMED_PYTHON_SCRIPT,
        LIST_NAMED_PYTHON_SCRIPTS,
        ORIGIN_USER,
        host_get_named_python_script,
        host_list_named_python_scripts,
    )

    user_scripts = get_user_scripts()
    uno_ctx = get_ctx()
    doc = get_active_document(uno_ctx) if uno_ctx is not None else None
    document_scripts = get_document_scripts(doc) if doc is not None else {}
    if tool_name == LIST_NAMED_PYTHON_SCRIPTS:
        return host_list_named_python_scripts(user_scripts=user_scripts, document_scripts=document_scripts)
    if tool_name == GET_NAMED_PYTHON_SCRIPT:
        name = str(payload.get("name") or "")
        origin = str(payload.get("origin") or ORIGIN_USER)
        known = payload.get("known_hash")
        known_hash = known if isinstance(known, str) else None
        return host_get_named_python_script(
            name=name,
            origin=origin,
            known_hash=known_hash,
            user_scripts=user_scripts,
            document_scripts=document_scripts,
        )
    raise RuntimeError(f"Unknown named-script tool {tool_name!r}")


def handle_tool_call_frame(
    response: dict[str, Any],
    *,
    stdin_write: Callable[[bytes], None],
    allowed_tools: frozenset[str] | None = None,
    caller: str = "script",
) -> bool:
    """Handle a worker ``tool_call`` frame. Returns True if the host should keep reading."""
    if not isinstance(response, dict) or response.get("type") != "tool_call":
        return False

    tool_name = response.get("tool")
    if not isinstance(tool_name, str):
        raise RuntimeError(f"Invalid tool_call: {tool_name!r}")
    args = response.get("args") or {}
    call_id = response.get("id")
    try:
        res = execute_tool(
            tool_name,
            args if isinstance(args, dict) else {},
            caller=caller,
            allowed_tools=allowed_tools,
        )
        tool_response = {"status": "ok", "id": call_id, "result": res}
    except Exception as exc:
        log.exception("venv tool_call %s failed", tool_name)
        tool_response = {"status": "error", "id": call_id, "message": str(exc)}
    stdin_write(pack_pickle_frame(tool_response))
    return True

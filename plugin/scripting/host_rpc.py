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
  from ``delegate_tool_domains`` passes the delegated list plus ``core``
  (``inner_script_tool_domain``) so one script can call those domains and
  ``DOMAIN_TOOLS['core']``. ``run_venv_python_script`` is removed from every
  allowlist so a script cannot re-enter the worker.
"""

from __future__ import annotations

import inspect
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


def _domain_tools_map() -> dict[str, list[str]] | None:
    """``DOMAIN_TOOLS`` or ``None`` when this build has no generated proxy (LibrePy)."""
    try:
        from plugin.scripting.writeragent_api import DOMAIN_TOOLS
    except ImportError:
        return None
    return DOMAIN_TOOLS


def domain_proxy_namespace(python_tool_domain: str, tools: dict[str, list[str]]) -> str | None:
    """``DOMAIN_TOOLS`` key for one specialized domain name.

    Same singularization as ``generate_tool_proxies`` (``shapes`` → ``shape``,
    ``footnotes`` → ``footnote``, ``indexes`` → ``index``). ``None`` when
    *python_tool_domain* is not a key.
    """
    if python_tool_domain in tools:
        return python_tool_domain
    if python_tool_domain == "indexes":
        singular = "index"
    elif python_tool_domain.endswith("s") and python_tool_domain not in ("images", "styles", "forms"):
        singular = python_tool_domain[:-1]
    else:
        singular = python_tool_domain
    if singular in tools:
        return singular
    return None


def domain_proxy_tool_names(python_tool_domain: str) -> frozenset[str] | None:
    """Proxy tool names for one specialized domain, without ``list_open_documents``.

    ``None`` means ``writeragent_api`` is not in this build (LibrePy). An empty
    set means the name is not a ``DOMAIN_TOOLS`` key. Specialized ``shapes``
    maps to key ``shape`` (same singularization ``generate_tool_proxies`` uses
    for ``footnotes`` → ``footnote`` and ``indexes`` → ``index``).
    """
    tools = _domain_tools_map()
    if tools is None:
        return None
    key = domain_proxy_namespace(python_tool_domain, tools)
    if key is None:
        return frozenset()
    return frozenset(tools.get(key) or ())


def inner_script_tool_domain(domains: list[str]) -> str:
    """Allowlist key for the inner ``delegate_tool_domains`` script.

    Delegated specialized names, then ``core``. ``core`` is not a specialized
    domain the outer agent passes; the script may still call
    ``DOMAIN_TOOLS['core']`` (``list_open_documents``, ``undo``, and the rest).
    ``run_venv_python_script`` is not in ``core``. A bare domain string
    (``"writer"``, ``"shapes"``) does not gain ``core`` — only this inner path
    appends it — and ``None`` / ``""`` are unchanged.
    """
    parts: list[str] = []
    seen: set[str] = set()
    for name in domains:
        if name and name not in seen and name != "core":
            seen.add(name)
            parts.append(name)
    parts.append("core")
    return ",".join(parts)


def resolve_allowed_tools(python_tool_domain: str | None) -> frozenset[str] | None:
    """Return an allowlist, ``None`` (unrestricted minus blocked), or empty (disabled).

    A comma-separated list is the union of each domain (inner
    ``delegate_tool_domains`` passes delegated names plus ``core``).
    Whitespace around commas is ignored. ``run_venv_python_script`` is always
    removed: allowing it would re-enter the warm worker.
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
    # Blocked even when a domain entry lists it (``DOMAIN_TOOLS['python']``).
    # ``execute_tool`` rejects it too; keeping it off the set matches the catalog.
    return (frozenset(allowed) | _ALWAYS_ALLOWED) - _BLOCKED_FROM_VENV


def _method_code(method: Any) -> Any:
    """Code object for a proxy function or bound method.

    Newer CPython exposes ``__code__`` on bound methods. Older LibreOffice
    Pythons only have it on ``__func__``.
    """
    code = getattr(method, "__code__", None)
    if code is not None:
        return code
    func = getattr(method, "__func__", None)
    if func is None:
        return None
    return getattr(func, "__code__", None)


def _rpc_tool_name(method: Any, known: frozenset[str]) -> str | None:
    """Tool name passed to ``_rpc_call`` inside a generated proxy method.

    ``CALL_KW`` stores it as a string const. ``CALL_FUNCTION_EX`` (many
    kwargs, as on ``shape.upsert``) stores it as a one-element tuple. Parameter
    names are skipped when they collide with a tool name. The allowlist uses
    that tool name; the catalog shows the Python method (``wa.shape.upsert``).
    """
    code = _method_code(method)
    if code is None:
        return None
    found: list[str] = []
    for const in code.co_consts:
        if isinstance(const, str) and const in known:
            found.append(const)
        elif (
            isinstance(const, tuple)
            and len(const) == 1
            and isinstance(const[0], str)
            and const[0] in known
        ):
            found.append(const[0])
    if not found:
        return None
    locals_ = set(code.co_varnames)
    for name in found:
        if name not in locals_:
            return name
    return found[0]


def _proxy_methods_by_tool(tools: dict[str, list[str]]) -> dict[str, Any] | None:
    """Map each ``DOMAIN_TOOLS`` name to the generated proxy method.

    ``None`` when ``writeragent_api`` is not in this build (LibrePy). The
    import sits in this function, so it must be guarded the same way as
    ``_domain_tools_map``: LibrePy ships ``host_rpc`` and omits the proxy.
    """
    try:
        import plugin.scripting.writeragent_api as api
    except ImportError:
        return None

    known = frozenset(name for names in tools.values() for name in names)
    found: dict[str, Any] = {}
    for namespace in tools:
        proxy = getattr(api, namespace, None)
        if proxy is None:
            continue
        for attr in dir(proxy):
            if attr.startswith("_"):
                continue
            method = getattr(proxy, attr, None)
            if not callable(method):
                continue
            tool_name = _rpc_tool_name(method, known)
            if tool_name and tool_name not in found:
                found[tool_name] = method
    return found


def _format_proxy_call(namespace: str, method: Any) -> str:
    """``wa.shape.upsert(action, *, ...)`` without the return annotation."""
    sig = inspect.signature(method)
    if sig.return_annotation is not inspect.Signature.empty:
        sig = sig.replace(return_annotation=inspect.Signature.empty)
    return f"wa.{namespace}.{method.__name__}{sig}"


def _catalog_lead(namespaces: list[str]) -> str:
    examples: list[str] = []
    if "core" in namespaces:
        examples.append("wa.core.list_open_documents()")
    if "shape" in namespaces:
        examples.append("wa.shape.upsert(...)")
    example = ""
    if examples:
        example = " For example " + " and ".join(examples) + "."
    return (
        "run_venv_python_script has access to the following APIs you can call from within it. "
        "Only the Python script may call these wa.* functions; they are not additional LLM tool names. "
        "Inside the script, import writeragent as wa and call the listed APIs."
        f"{example} "
        "When the work is bulk or scripted, do that domain work in one run_venv_python_script."
    )


def format_script_api_catalog(domains: list[str]) -> str:
    """Script-callable API catalog: ``core`` plus each delegated domain.

    Each entry is the ``wa.<namespace>.<method>(...)`` call and the full
    generated method docstring (description and Args). That docstring is the
    text ``scripts/generate_tool_proxies.py`` ``_method_doc_lines`` wrote into
    ``writeragent_api``. The venv sandbox blocks ``inspect``, ``dir``, and
    ``__doc__``, so the inner agent cannot read this off the proxy itself.
    Empty when the proxy module is not shipped (LibrePy).
    """
    tools = _domain_tools_map()
    if tools is None:
        return ""
    methods = _proxy_methods_by_tool(tools)
    if not methods:
        # LibrePy, or proxies that did not load. Omit the catalog.
        return ""
    namespaces: list[str] = []
    seen: set[str] = set()
    for name in ("core", *domains):
        key = domain_proxy_namespace(name, tools)
        if not key or key in seen:
            continue
        seen.add(key)
        namespaces.append(key)
    blocks: list[str] = [_catalog_lead(namespaces), ""]
    any_entry = False
    for namespace in namespaces:
        entries: list[str] = []
        for tool_name in tools.get(namespace) or ():
            if tool_name in _BLOCKED_FROM_VENV:
                continue
            method = methods.get(tool_name)
            if method is None:
                log.warning("script API catalog: no proxy method for %s", tool_name)
                continue
            # Verbatim generated docstring (description + Args), not a summary.
            doc = inspect.cleandoc(getattr(method, "__doc__", None) or "")
            call = _format_proxy_call(namespace, method)
            entries.append(f"{call}\n{doc}" if doc else call)
        if not entries:
            continue
        any_entry = True
        blocks.append(f"{namespace}:")
        blocks.append("\n\n".join(entries))
        blocks.append("")
    if not any_entry:
        return ""
    return "\n".join(blocks).rstrip() + "\n"


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

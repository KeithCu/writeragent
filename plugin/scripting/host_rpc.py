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
- ``""`` — ``=PY()`` recalc: tool RPC is disabled, including named-script
  fetch (formula evaluation must stay side-effect free and must not read
  stored script source).
- a domain name — allow only that domain's proxies plus ``document_list_open``.
  If ``writeragent_api`` is absent (LibrePy), that string stays unrestricted
  (``None``). An empty allowlist would disable every tool RPC.
- several names separated by commas — union of those domains. The inner agent
  from ``python_delegate_domains`` passes the delegated list plus ``core``
  (``inner_script_tool_domain``) so one script can call those domains and
  ``DOMAIN_TOOLS['core']``. ``python_run_script`` is removed from every
  allowlist so a script cannot re-enter the worker.
"""

from __future__ import annotations

import difflib
import functools
import inspect
import logging
from typing import TYPE_CHECKING, Any, Callable

from plugin.framework.errors import resolve_exception_message
from plugin.framework.ipc import DEFAULT_MAX_PAYLOAD_BYTES, IpcFrameError, pack_pickle_frame
from plugin.framework.queue_executor import main
from plugin.framework.send_cancellation import SendCancellation, is_cancelled
from plugin.framework.thread_guard import in_sync_host_dispatch
from plugin.framework.uno_context import get_active_document, get_ctx

if TYPE_CHECKING:
    from types import ModuleType

log = logging.getLogger(__name__)

# ``python_run_script`` would re-enter the same warm worker while the
# warm-child pipe lock is held and deadlock the pipe.
_BLOCKED_FROM_VENV = frozenset({"python_run_script"})

# Host sentinel: disable venv → LO tool RPC (Calc ``=PY()`` recalc).
TOOL_RPC_DISABLED = ""

# ``get_active_document_type()`` always needs this, even in a scoped domain.
_ALWAYS_ALLOWED = frozenset({"document_list_open"})

# String fetch of user/document scripts — not document mutation.
# Refused when the allowlist is empty (=PY() recalc). Still dispatched when
# RPC is unrestricted or the allowlist is a non-empty domain: these names are
# not in DOMAIN_TOOLS, so a domain list would otherwise hide wa.scripts / wa.doc.
_NAMED_SCRIPT_TOOLS = frozenset({"get_named_python_script", "list_named_python_scripts"})

# Read-only API help lookup for wa.help() in sandboxed scripts.
WRITERAGENT_HELP_TOOL = "writeragent_help"


def _api_module() -> ModuleType | None:
    """The generated ``writeragent_api`` proxy module, or ``None`` when this build omits it.

    LibrePy ships ``host_rpc`` without the proxy. Callers treat ``None`` as
    no domain scoping and no script API catalog.
    """
    try:
        import plugin.scripting.writeragent_api as api
    except ImportError:
        return None
    return api


# Domains that pull tools from another specialized domain via required_core_tools.
_DOMAIN_EXTRA_TOOLS: dict[str, frozenset[str]] = {
    "structural": frozenset({"section_list"}),
}


def _domain_names(domain_tools: dict[str, list[str]], domain: str) -> frozenset[str]:
    names = domain_tools.get(domain)
    if names is None:
        log.warning("Python tool domain %r is not a DOMAIN_TOOLS key", domain)
        return frozenset()
    return frozenset(names) | _DOMAIN_EXTRA_TOOLS.get(domain, frozenset())


def domain_proxy_tool_names(python_tool_domain: str) -> frozenset[str] | None:
    """Proxy tool names for one specialized domain, without ``document_list_open``.

    ``None`` means ``writeragent_api`` is not in this build (LibrePy). An empty
    set means the name is not a ``DOMAIN_TOOLS`` key.
    """
    api = _api_module()
    return None if api is None else _domain_names(api.DOMAIN_TOOLS, python_tool_domain)


def inner_script_tool_domain(domains: list[str]) -> str:
    """Allowlist key for the inner ``python_delegate_domains`` script.

    Delegated specialized names, then ``core``. ``core`` is not a specialized
    domain the outer agent passes; the script may still call
    ``DOMAIN_TOOLS['core']`` (``document_list_open``, ``document_undo``, and the rest).
    ``python_run_script`` is not in ``core``. A bare domain string
    (``"writer"``, ``"shapes"``) does not gain ``core`` — only this inner path
    appends it — and ``None`` / ``""`` are unchanged.
    """
    parts = [name for name in dict.fromkeys(domains) if name and name != "core"]
    parts.append("core")
    return ",".join(parts)


def resolve_allowed_tools(python_tool_domain: str | None) -> frozenset[str] | None:
    """Return an allowlist, ``None`` (unrestricted minus blocked), or empty (disabled).

    A comma-separated list is the union of each domain (inner
    ``python_delegate_domains`` passes delegated names plus ``core``).
    Whitespace around commas is ignored. ``python_run_script`` is always
    removed: allowing it would re-enter the warm worker.

    When this build has no ``writeragent_api`` (LibrePy), a domain string is
    unrestricted, same as ``None``; an empty allowlist there would refuse every
    document tool with the =PY() disabled message. A name that is not a
    ``DOMAIN_TOOLS`` key adds nothing, so it stays limited to
    ``document_list_open``.
    """
    # Broad catalog access is intentional; re-entry guard in venv_worker covers deadlock;
    # do not narrow the catalog without a product ask.
    if python_tool_domain is None:
        return None
    if python_tool_domain == TOOL_RPC_DISABLED:
        return frozenset()

    parts = [part.strip() for part in python_tool_domain.split(",") if part.strip()]
    if not parts:
        return frozenset()

    api = _api_module()
    if api is None:
        return None

    allowed: set[str] = set()
    for part in parts:
        allowed |= _domain_names(api.DOMAIN_TOOLS, part)
    # Blocked even when a domain entry lists it (``DOMAIN_TOOLS['python']``).
    # ``execute_tool`` rejects it too; keeping it off the set matches the catalog.
    return (frozenset(allowed) | _ALWAYS_ALLOWED) - _BLOCKED_FROM_VENV


def _proxy_methods_by_tool(api: ModuleType) -> dict[str, tuple[str, Any]]:
    """Map each ``TOOL_METHODS`` tool name to ``(wa_namespace, proxy_method)``."""
    found: dict[str, tuple[str, Any]] = {}
    for tool_name, (namespace, method_name) in api.TOOL_METHODS.items():
        proxy = getattr(api, namespace, None)
        method = getattr(proxy, method_name, None)
        if callable(method):
            found[tool_name] = (namespace, method)
    return found


# Dunder proxy methods in COLLECTION_NAMESPACES are displayed as Python container
# syntax in the script catalog and wa.help() instead of wa.<ns>.__iter__().
_CONTAINER_FORMS: dict[str, str] = {
    "__iter__": "for item in wa.{ns}",
    "__getitem__": "wa.{ns}[{key}]",
    "__delitem__": "del wa.{ns}[{key}]",
}


def _format_proxy_call(namespace: str, method: Any) -> str:
    """``wa.shape.upsert(action, *, ...)`` without the return annotation.

    *namespace* is the ``wa`` attribute from ``TOOL_METHODS``. ``method.__name__``
    is already keyword-escaped.
    """
    sig = inspect.signature(method)
    form = _CONTAINER_FORMS.get(method.__name__)
    if form is not None:
        return form.format(ns=namespace, key=next(iter(sig.parameters), ""))
    if sig.return_annotation is not inspect.Signature.empty:
        sig = sig.replace(return_annotation=inspect.Signature.empty)
    return f"wa.{namespace}.{method.__name__}{sig}"


def _format_proxy_entry(namespace: str, method: Any) -> str:
    """Format one ``wa.<namespace>.<method>(...)`` signature plus docstring."""
    doc = inspect.cleandoc(getattr(method, "__doc__", None) or "")
    call = _format_proxy_call(namespace, method)
    return f"{call}\n{doc}" if doc else call


def _catalog_lead(domains: list[str]) -> str:
    examples: list[str] = []
    if "core" in domains:
        examples.append("wa.document.list_open()")
    if "shapes" in domains:
        examples.append("wa.shape.upsert(...)")
    example = ""
    if examples:
        example = " For example " + " and ".join(examples) + "."
    return (
        "python_run_script has access to the following APIs you can call from within it. "
        "Only the Python script may call these wa.* functions; they are not additional LLM tool names. "
        "Inside the script, import writeragent as wa and call the listed APIs. "
        "Each call returns a dict whose keys also read as attributes (result.text or result['text']); "
        "a tool error raises RuntimeError with .code; results carry no ok status key."
        f"{example} "
        "Call wa.help() to list namespaces or wa.help('namespace.method') for signatures and arguments. "
        "When the work is bulk or scripted, do that domain work in one python_run_script."
    )


@functools.lru_cache(maxsize=16)
def _format_script_api_catalog_cached(domains_tuple: tuple[str, ...]) -> str:
    api = _api_module()
    if api is None:
        # LibrePy: no proxy, so no catalog.
        return ""
    tools: dict[str, list[str]] = api.DOMAIN_TOOLS
    methods = _proxy_methods_by_tool(api)
    if not methods:
        return ""
    domains: list[str] = []
    seen: set[str] = set()
    for name in ("core", *domains_tuple):
        if name not in tools or name in seen:
            continue
        seen.add(name)
        domains.append(name)
    blocks: list[str] = []
    for domain in domains:
        entries: list[str] = []
        for tool_name in tools.get(domain) or ():
            if tool_name in _BLOCKED_FROM_VENV:
                continue
            entry = methods.get(tool_name)
            if entry is None:
                log.warning("script API catalog: no proxy method for %s", tool_name)
                continue
            entries.append(_format_proxy_entry(*entry))
        if entries:
            blocks.append(f"{domain}:")
            blocks.append("\n\n".join(entries))
            blocks.append("")
    if not blocks:
        return ""
    blocks = [_catalog_lead(domains), "", *blocks]
    return "\n".join(blocks).rstrip() + "\n"


def format_script_api_help(
    name: str | None = None,
    *,
    allowed_tools: frozenset[str] | None = None,
) -> str:
    """Return generated signature/docstring help for ``wa.help()``.

    Shares ``_format_proxy_entry`` with ``format_script_api_catalog`` so the
    runtime help output cannot drift from the prompt catalog.
    """
    api = _api_module()
    methods = _proxy_methods_by_tool(api) if api is not None else {}
    if not methods:
        return "writeragent API proxies are not available in this build."

    by_qualified: dict[str, tuple[str, Any]] = {}
    by_tool: dict[str, tuple[str, Any]] = {}
    ns_methods: dict[str, list[str]] = {}
    for tool_name, (ns, method) in methods.items():
        if tool_name in _BLOCKED_FROM_VENV:
            continue
        if allowed_tools is not None and tool_name not in allowed_tools:
            continue
        by_tool[tool_name] = (ns, method)
        if method.__name__ in _CONTAINER_FORMS:
            ns_methods.setdefault(ns, []).append(f"{_format_proxy_call(ns, method)} ({tool_name})")
        else:
            by_qualified[f"{ns}.{method.__name__}"] = (ns, method)
            ns_methods.setdefault(ns, []).append(method.__name__)

    if name is None or not name.strip():
        if not ns_methods:
            return "No writeragent APIs are available in this Python tool domain."
        lines = [
            "Available wa.<namespace>.<method> APIs "
            "(call wa.help('namespace.method') or wa.help('tool_name') for details):"
        ]
        for ns in sorted(ns_methods):
            lines.append(f"{ns}: {', '.join(sorted(ns_methods[ns]))}")
        return "\n".join(lines)

    target = name.strip()
    if target.startswith("wa."):
        target = target[3:]
    elif target.startswith("writeragent."):
        target = target[12:]

    if target in by_qualified:
        ns, method = by_qualified[target]
        return _format_proxy_entry(ns, method)
    if target in by_tool:
        ns, method = by_tool[target]
        return _format_proxy_entry(ns, method)
    if target in ns_methods:
        method_list = ", ".join(sorted(ns_methods[target]))
        return f"{target}: {method_list}\nCall wa.help('{target}.<method>') for signature and arguments."

    candidates = sorted(set(by_qualified) | set(ns_methods))
    close = difflib.get_close_matches(target, candidates, n=5, cutoff=0.4)
    if close:
        return f"Unknown API {name!r}. Close matches: {', '.join(close)}. Call wa.help() to list all namespaces."
    return f"Unknown API {name!r}. No close matches found. Call wa.help() to list all namespaces."


def format_script_api_catalog(domains: list[str]) -> str:
    """Script-callable API catalog: ``core`` plus each delegated domain.

    Each entry is the ``wa.<namespace>.<method>(...)`` call and the full
    generated method docstring (description and Args). That docstring is the
    text ``scripts/generate_tool_proxies.py`` ``_method_doc_lines`` wrote into
    ``writeragent_api``. The venv sandbox blocks ``inspect``, ``dir``, and
    ``__doc__``, so the inner agent cannot read this off the proxy itself.
    Empty when the proxy module is not shipped (LibrePy).
    """
    return _format_script_api_catalog_cached(tuple(domains))


def _script_target_doc(uno_ctx: Any, script_session_id: str | None) -> Any:
    # A venv tool targets the document of its worker turn, not the focused
    # window. The request carries ppt_master:{url} (PPT-Master) or a host-only
    # doc: pin of ctx.doc (chat python_run_script), and
    # document_for_script_session matches it to the open component, so a turn
    # started on deck A still exports into A after the user switches windows.
    # The id comes from the turn, not from a process-global that another pool
    # (embeddings) would overwrite. The pin is not the worker namespace id.
    # Only when nothing matches does the focused document win.
    from plugin.scripting.session_manager import document_for_script_session

    doc = document_for_script_session(uno_ctx, script_session_id)
    if doc is None:
        doc = get_active_document(uno_ctx)
    return doc


def execute_tool(
    tool_name: str,
    args: dict[str, Any] | None = None,
    *,
    caller: str = "script",
    allowed_tools: frozenset[str] | None = None,
    script_session_id: str | None = None,
    send_cancellation: SendCancellation | None = None,
) -> Any:
    """Dispatch a registered WriterAgent tool on the LO main thread (UNO-safe)."""
    if tool_name in _BLOCKED_FROM_VENV:
        raise RuntimeError(
            f"Tool {tool_name!r} cannot run from a venv script."
        )
    # Named-script tools must hit this gate first. An empty allowlist means
    # =PY() recalc; dispatching them before it would let recalc return every
    # user script's source from get_named_python_script and
    # list_named_python_scripts. A non-empty domain allowlist still reaches
    # the named-script branch below.
    if allowed_tools is not None and not allowed_tools:
        raise RuntimeError(
            "Document tool RPC is disabled during =PY() recalculation. "
            "Use Run Python Script… to call writeragent tools."
        )
    payload = args or {}
    if tool_name == WRITERAGENT_HELP_TOOL:
        raw_name = payload.get("name")
        return format_script_api_help(
            raw_name if isinstance(raw_name, str) else None,
            allowed_tools=allowed_tools,
        )
    if tool_name in _NAMED_SCRIPT_TOOLS:
        # Inside a synchronous host dispatch off the main thread, the main thread
        # is waiting on this one, so main.call would deadlock (thread_guard.sync_host_dispatch).
        # =PY() recalc never reaches here: its allowlist is empty and refused above.
        # User scripts are a config dict. Document scripts need UNO.
        if in_sync_host_dispatch() and not main.is_current():
            return _dispatch_named_script(tool_name, payload, allow_document=False)

        return main.call(lambda: _dispatch_named_script(tool_name, payload, script_session_id=script_session_id))
    if allowed_tools is not None and tool_name not in allowed_tools:
        raise RuntimeError(
            f"Tool {tool_name!r} is not available in this Python tool domain."
        )

    def _run() -> Any:
        try:
            from plugin.framework.doc_type import doc_type_family_label, get_document_type
            from plugin.framework.tool import ToolContext
            from plugin.app import get_tools
        except ImportError as exc:
            raise RuntimeError(
                "Document tool RPC is not available in this extension build."
            ) from exc

        uno_ctx = get_ctx()
        doc = _script_target_doc(uno_ctx, script_session_id)
        if not doc:
            raise RuntimeError("No active document found to run tool")
        family = doc_type_family_label(get_document_type(doc))
        registry = get_tools()
        # Copy send_cancellation onto ToolContext so script-started HTTP tools honor Stop instead of finishing the request after cancellation.
        tctx = ToolContext(
            doc=doc,
            ctx=uno_ctx,
            doc_type="" if family == "unknown" else family,
            services=registry.services,
            caller=caller,
            send_cancellation=send_cancellation,
        )

        tool = registry.get(tool_name)
        is_async = tool is not None and tool.is_async()
        return registry, tctx, is_async

    # We keep two hops; collapsing resolve+execute into one hop changes queue semantics/races.
    # Known window: the doc in tctx is resolved in the first hop and used in the second,
    # so it can be closed in between and the tool then sees a dead UNO proxy
    # (DisposedException). That is the same risk as a doc closing mid-tool, so we accept
    # it rather than merge hops; the tool error path must report it cleanly.
    registry, tctx, is_async = main.call(_run)

    if is_async:
        return registry.execute(tool_name, tctx, **payload)
    return main.call(lambda: registry.execute(tool_name, tctx, **payload))


def _document_script_store(script_session_id: str | None) -> dict[str, str]:
    from plugin.scripting.document_scripts import get_document_scripts

    uno_ctx = get_ctx()
    doc = _script_target_doc(uno_ctx, script_session_id)
    if doc is None:
        return {}
    return get_document_scripts(doc)


def _dispatch_named_script(
    tool_name: str,
    payload: dict[str, Any],
    *,
    script_session_id: str | None = None,
    allow_document: bool = True,
) -> Any:
    """Dispatch get/list named scripts on the main thread, or off-main during =PY().

    *allow_document* is False off the main thread: document scripts need UNO,
    so only user origin is allowed there.
    """
    from plugin.scripting.domain_registry import SCRIPT_ORIGIN_DOCUMENT, SCRIPT_ORIGIN_USER
    from plugin.scripting.document_scripts import get_user_scripts
    from plugin.scripting.named_scripts import (
        GET_NAMED_PYTHON_SCRIPT,
        LIST_NAMED_PYTHON_SCRIPTS,
        host_get_named_python_script,
        host_list_named_python_scripts,
    )

    if tool_name == LIST_NAMED_PYTHON_SCRIPTS:
        user_scripts = get_user_scripts()
        document_scripts = _document_script_store(script_session_id) if allow_document else {}
        return host_list_named_python_scripts(
            user_scripts=user_scripts, document_scripts=document_scripts
        )
    if tool_name != GET_NAMED_PYTHON_SCRIPT:
        raise RuntimeError(f"Unknown named-script tool {tool_name!r}")

    name = str(payload.get("name") or "")
    origin = str(payload.get("origin") or SCRIPT_ORIGIN_USER)
    known = payload.get("known_hash")
    known_hash = known if isinstance(known, str) else None

    if not allow_document and origin != SCRIPT_ORIGIN_USER:
        raise RuntimeError(
            "Document scripts cannot be loaded during =PY() recalculation off the UI thread. "
            "Use a user script (wa.scripts) or Run Python Script."
        )
    user_scripts = get_user_scripts() if origin in (SCRIPT_ORIGIN_USER, SCRIPT_ORIGIN_DOCUMENT) else {}
    document_scripts = _document_script_store(script_session_id) if origin == SCRIPT_ORIGIN_DOCUMENT else {}
    return host_get_named_python_script(
        name=name, origin=origin, known_hash=known_hash, user_scripts=user_scripts, document_scripts=document_scripts
    )


def handle_tool_call_frame(
    response: dict[str, Any],
    *,
    stdin_write: Callable[[bytes], None],
    allowed_tools: frozenset[str] | None = None,
    caller: str = "script",
    script_session_id: str | None = None,
    cancel: SendCancellation | None = None,
) -> bool:
    """Handle a worker ``tool_call`` frame. Returns True if the host should keep reading."""
    if response.get("type") != "tool_call":
        return False

    tool_name = response.get("tool")
    call_id = response.get("id")

    def _error_frame(message: str, code: str | None = None) -> dict[str, Any]:
        frame: dict[str, Any] = {"status": "error", "id": call_id, "message": message}
        if code:
            frame["code"] = code
        return frame

    def _reply(
        payload: dict[str, Any],
        *,
        failure_log: str = "venv tool_call reply failed (worker pipe closed)",
    ) -> None:
        try:
            frame = pack_pickle_frame(payload, max_payload_bytes=DEFAULT_MAX_PAYLOAD_BYTES)
        except IpcFrameError as exc:
            # Oversized tool results become error frames so the host does not kill the shared worker parsing a huge pickle.
            frame = pack_pickle_frame(_error_frame(str(exc)), max_payload_bytes=DEFAULT_MAX_PAYLOAD_BYTES)
        except Exception as exc:
            # Unpicklable tool result. Fall back to sending an error frame instead of hanging.
            frame = pack_pickle_frame(
                _error_frame(f"Result not serializable: {exc}"), max_payload_bytes=DEFAULT_MAX_PAYLOAD_BYTES
            )

        # A broken worker pipe makes stdin_write raise (ValueError when writing to
        # a closed file, or OSError). Log it and do not let it escape: an escaping
        # error would abort the Stop path, so the child would never get
        # USER_STOPPED and could keep calling tools after the sidebar went idle.
        try:
            stdin_write(frame)
        except (OSError, ValueError):
            # ValueError can be raised when writing to a closed file in Python.
            log.warning(failure_log, exc_info=True)

    if not isinstance(tool_name, str):
        _reply(_error_frame(f"Invalid tool_call: {tool_name!r}"))
        return True
    args = response.get("args")
    if args is not None and not isinstance(args, dict):
        _reply(_error_frame("args must be a dictionary"))
        return True
    # Stop is checked once before the venv turn; without this check the child
    # could keep calling host tools, including export, after the sidebar went
    # idle. The same scope the LLM frame receives refuses the call and tells
    # the child to end with USER_STOPPED.
    if is_cancelled(cancel):
        _reply(
            _error_frame("Stopped by user.", code="USER_STOPPED"),
            failure_log="venv tool_call USER_STOPPED reply failed (worker pipe closed)",
        )
        return True

    try:
        res = execute_tool(
            tool_name,
            args,
            caller=caller,
            allowed_tools=allowed_tools,
            script_session_id=script_session_id,
            send_cancellation=cancel,
        )
        tool_response = {"status": "ok", "id": call_id, "result": res}
    except Exception as exc:
        log.exception("venv tool_call %s failed", tool_name)
        # Copy code onto the response when the exception has one
        # (ToolExecutionError code="USER_STOPPED", domain codes); a payload of
        # only status and message would hide that code from the child worker.
        err_code = getattr(exc, "code", None)
        # resolve_exception_message, not str(exc): this runs on the pipe holder (see errors._exception_text).
        tool_response = _error_frame(resolve_exception_message(exc), code=str(err_code) if err_code else None)
    _reply(tool_response)
    return True

# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Named My Scripts / This Document libraries for venv code (``wa.scripts`` / ``wa.doc``).

Library bodies are fetched over the existing ``tool_call`` pipe (not
``run_venv_python_script``). Defs are eval'd into a private namespace on the
current ``LocalPythonExecutor`` so later calls do not re-fetch the source.
Across runs the cache lives on that executor — document-keyed shared kernel
when session mode is shared.
"""

from __future__ import annotations

import ast
import hashlib
import keyword
import logging
import os
import re
import sys
from contextvars import ContextVar
from types import SimpleNamespace
from typing import Any

log = logging.getLogger(__name__)

from plugin.scripting.domain_registry import SCRIPT_ORIGIN_DOCUMENT, SCRIPT_ORIGIN_USER

ORIGIN_USER = SCRIPT_ORIGIN_USER
ORIGIN_DOCUMENT = SCRIPT_ORIGIN_DOCUMENT

_NAMED_SCRIPT_MAX_BYTES = 200_000
_IDENT_NON_ALNUM = re.compile(r"[^0-9A-Za-z_]+")
_IDENT_MULTI_US = re.compile(r"_+")

# Bind-thread executor for this execute. Off-thread timeout evaluation has no
# ContextVar; that path uses the ScriptLibrary stored on the executor.
_current_executor: ContextVar[Any] = ContextVar("named_scripts_executor", default=None)

GET_NAMED_PYTHON_SCRIPT = "get_named_python_script"
LIST_NAMED_PYTHON_SCRIPTS = "list_named_python_scripts"


def python_identifier_from_script_name(name: str) -> str:
    """Turn a picker title into a Python identifier (tweak here, not call sites)."""
    raw = (name or "").strip()
    ident = _IDENT_NON_ALNUM.sub("_", raw)
    ident = _IDENT_MULTI_US.sub("_", ident).strip("_")
    if not ident:
        ident = "_script"
    if ident[0].isdigit():
        ident = f"_{ident}"
    if keyword.iskeyword(ident):
        ident = f"{ident}_"
    if not ident.isidentifier():
        ident = "_script"
    return ident


def script_body_hash(code: str) -> str:
    return hashlib.sha256((code or "").encode("utf-8")).hexdigest()


# Bodies of these nodes do not run while the library statement is loading.
_NESTED_SCOPES = (ast.Lambda, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def _import_time_has(node: ast.AST, kinds: tuple[type[ast.AST], ...]) -> bool:
    """True when *node* contains *kinds* outside nested function/class/lambda bodies.

    ``ast.walk`` used to enter ``lambda x: transform(x)`` and reject a binding
    that does not call ``transform`` until the lambda runs.
    """
    pending: list[ast.AST] = [node]
    while pending:
        current = pending.pop()
        if isinstance(current, _NESTED_SCOPES):
            continue
        if isinstance(current, kinds):
            return True
        pending.extend(ast.iter_child_nodes(current))
    return False


def _expr_has_call(node: ast.AST) -> bool:
    """True when *node* contains a call that would run while the library loads."""
    return _import_time_has(node, (ast.Call, ast.Await))


def _expr_has_namedexpr(node: ast.AST) -> bool:
    """True when *node* binds a name with ``:=`` at library-load time."""
    return _import_time_has(node, (ast.NamedExpr,))


def _function_import_time_call_lines(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[int]:
    """Lines evaluated when a function definition is reached.

    Function bodies wait until the function is called, but decorators, default
    argument values, and annotations run when the definition is reached.
    Reject calls in decorator_list, args.defaults, args.kw_defaults, and
    argument/return annotations.
    """
    lines: list[int] = []

    def _note(expr: ast.AST | None) -> None:
        if expr is None or not _expr_has_call(expr):
            return
        lineno = getattr(expr, "lineno", getattr(node, "lineno", 0))
        if lineno not in lines:
            lines.append(lineno)

    for dec in node.decorator_list:
        _note(dec)
    for default in node.args.defaults:
        _note(default)
    for kw_default in node.args.kw_defaults:
        if kw_default is not None:
            _note(kw_default)
    if node.returns is not None:
        _note(node.returns)
    all_args = (
        node.args.posonlyargs
        + node.args.args
        + node.args.kwonlyargs
        + ([node.args.vararg] if node.args.vararg else [])
        + ([node.args.kwarg] if node.args.kwarg else [])
    )
    for arg in all_args:
        if arg.annotation is not None:
            _note(arg.annotation)
    return lines


def _class_import_time_call_lines(node: ast.ClassDef) -> list[int]:
    """Lines ``evaluate_class_def`` would execute while the library loads.

    Class decorators and non-assign class-body statements run at load time.
    Scanning only bases, keywords, and assignments missed those calls.
    Scan decorator_list, bases, keywords, method definitions, and assignments,
    and reject other class-body statements that contain calls.
    """
    lines: list[int] = []

    def _note(expr: ast.AST | None) -> None:
        if expr is None or not _expr_has_call(expr):
            return
        lineno = getattr(expr, "lineno", getattr(node, "lineno", 0))
        if lineno not in lines:
            lines.append(lineno)

    for dec in node.decorator_list:
        _note(dec)
    for base in node.bases:
        _note(base)
    for kw in node.keywords:
        _note(kw.value)
    for stmt in node.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for ln in _function_import_time_call_lines(stmt):
                if ln not in lines:
                    lines.append(ln)
        elif isinstance(stmt, ast.ClassDef):
            for ln in _class_import_time_call_lines(stmt):
                if ln not in lines:
                    lines.append(ln)
        elif isinstance(stmt, ast.Assign):
            _note(stmt.value)
        elif isinstance(stmt, ast.AnnAssign):
            _note(stmt.value)
            _note(stmt.annotation)
        elif isinstance(stmt, ast.Pass):
            continue
        elif isinstance(stmt, ast.Expr):
            if isinstance(stmt.value, ast.Constant) and isinstance(stmt.value.value, str):
                continue
            lineno = getattr(stmt, "lineno", getattr(node, "lineno", 0))
            if lineno not in lines:
                lines.append(lineno)
        else:
            lineno = getattr(stmt, "lineno", getattr(node, "lineno", 0))
            if lineno not in lines:
                lines.append(lineno)
    return lines


def extract_library_source(code: str) -> str:
    """Keep defs, classes, imports, and name assignments. Drop module-level calls."""
    try:
        tree = ast.parse(code or "")
    except SyntaxError as exc:
        raise ValueError(f"Named script is not valid Python: {exc}") from exc

    keep: list[ast.stmt] = []
    dropped: list[int] = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            call_lines = _class_import_time_call_lines(node)
            if call_lines:
                dropped.extend(call_lines)
            else:
                keep.append(node)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            call_lines = _function_import_time_call_lines(node)
            if call_lines:
                dropped.extend(call_lines)
            else:
                keep.append(node)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            keep.append(node)
        elif isinstance(node, ast.Assign) and all(isinstance(t, ast.Name) for t in node.targets):
            # Name assignments stay, including ``SCALE = FACTOR * 2``. Only
            # constant assigns used to be kept, so the name vanished with no error.
            # A call in the value still runs at import (``x = wa.writer...()``),
            # which mutated the document when Run Python Script left tool RPC open.
            if node.value is not None and _expr_has_call(node.value):
                dropped.append(getattr(node, "lineno", 0))
            else:
                keep.append(node)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
            if _expr_has_call(node.value):
                dropped.append(getattr(node, "lineno", 0))
            else:
                keep.append(node)
        elif isinstance(node, ast.Expr):
            # Module-level calls are not library definitions. They are omitted.
            # A walrus here binds a name. Dropping the Expr used to discard
            # that binding with no error, so the library loaded without it.
            if _expr_has_namedexpr(node):
                dropped.append(getattr(node, "lineno", 0))
            continue
        elif isinstance(node, ast.Pass):
            continue
        else:
            dropped.append(getattr(node, "lineno", 0))
    if dropped:
        lines = ", ".join(str(n) for n in sorted(set(dropped)))
        raise ValueError(f"Named script has statements that are not library definitions (lines {lines})")
    if not keep:
        return ""
    return ast.unparse(ast.Module(body=keep, type_ignores=[]))


def _rpc_named(tool_name: str, **kwargs: Any) -> Any:
    kwargs = {k: v for k, v in kwargs.items() if v is not None}
    # Same fail-closed path as writeragent_api._rpc_call. The ImportError
    # branch below would otherwise call exchange_tool_call with no host.
    if os.environ.get("WRITERAGENT_COMPUTE_WORKER") == "1":
        raise RuntimeError("WriterAgent document tools are not available in the Python compute service.")
    if os.environ.get("WRITERAGENT_IS_WORKER") == "1":
        # Catch ImportError only around the import. Wrapping _rpc_call too
        # turned an ImportError raised during the call into a second
        # exchange_tool_call, so the RPC was sent twice.
        try:
            from plugin.scripting.writeragent_api import _rpc_call
        except ImportError:
            # LibrePy omits writeragent_api. Same locked, id-checked pipe as _rpc_call.
            from plugin.scripting.ipc import exchange_tool_call

            return exchange_tool_call(tool_name, kwargs)
        return _rpc_call(tool_name, **kwargs)

    from plugin.scripting.host_rpc import execute_tool

    return execute_tool(tool_name, kwargs, caller="script")


def _executor_cache(executor: Any) -> dict[tuple[str, str], tuple[str, Any]]:
    cache = getattr(executor, "_named_script_cache", None)
    if cache is None:
        cache = {}
        executor._named_script_cache = cache
    return cache


def _checked_keys(executor: Any) -> set[tuple[str, str]]:
    checked = getattr(executor, "_named_script_checked", None)
    if checked is None:
        checked = set()
        executor._named_script_checked = checked
    return checked


def _eval_library(executor: Any, source: str, ident: str) -> SimpleNamespace:
    from plugin.contrib.smolagents.local_python_executor import evaluate_python_code

    extracted = extract_library_source(source)
    state: dict[str, Any] = {"__name__": ident}
    if extracted.strip():
        # evaluate_function_def writes each def into custom_tools. Passing the
        # executor dict made those names callable in later runs on this
        # executor. The copy still sees helpers that already exist.
        shared_tools = executor.custom_tools if isinstance(executor.custom_tools, dict) else {}
        evaluate_python_code(
            extracted,
            static_tools=executor.static_tools or {},
            custom_tools=dict(shared_tools),
            state=state,
            authorized_imports=executor.authorized_imports,
            max_print_outputs_length=executor.max_print_outputs_length,
            timeout_seconds=executor.timeout_seconds,
        )
    skip = {"__name__", "_print_outputs", "_operations_count"}
    ns = SimpleNamespace()
    for key, val in state.items():
        if key in skip:
            continue
        setattr(ns, key, val)
    return ns


def load_named_script(origin: str, name: str, executor: Any | None = None) -> Any:
    """Return the library namespace for *name*, using the current executor cache."""
    executor = executor if executor is not None else _current_executor.get()
    if executor is None:
        raise RuntimeError("Named scripts are only available while a Python script is running.")
    if not isinstance(name, str) or not name.strip():
        raise AttributeError("Named script title must be a non-empty string")
    name = name.strip()
    cache = _executor_cache(executor)
    checked = _checked_keys(executor)
    key = (origin, name)
    # Once per execute (Isolated Run or Shared cell): hash-check. Same execute
    # later (Helpers.add then Helpers.mul) stays local.
    if key in cache and key in checked:
        return cache[key][1]
    known_hash = cache[key][0] if key in cache else None
    payload = _rpc_named(
        GET_NAMED_PYTHON_SCRIPT,
        name=name,
        origin=origin,
        known_hash=known_hash,
    )
    if not isinstance(payload, dict):
        raise RuntimeError(f"Invalid named-script payload for {name!r}")
    if payload.get("unchanged") and key in cache:
        checked.add(key)
        return cache[key][1]
    code = payload.get("code")
    if not isinstance(code, str):
        raise AttributeError(f"No {origin} script named {name!r}")
    if len(code.encode("utf-8")) > _NAMED_SCRIPT_MAX_BYTES:
        raise RuntimeError(f"Named script {name!r} is too large to import as a library")
    body_hash = str(payload.get("hash") or script_body_hash(code))
    ident = python_identifier_from_script_name(name)
    ns = _eval_library(executor, code, ident)
    cache[key] = (body_hash, ns)
    checked.add(key)
    return ns


class ScriptLibrary:
    """``wa.scripts`` (My Scripts) or ``wa.doc`` (This Document)."""

    _origin: str

    def __init__(self, origin: str) -> None:
        self._origin = origin
        self._executor: Any | None = None

    def _resolve_executor(self) -> Any:
        """Executor for this lookup.

        One module-level ScriptLibrary stored ``_executor``, and
        ``bind_named_scripts_executor`` overwrote it. ``__getattr__`` used
        that field and ignored the ContextVar, so the next run stole lookups
        that still held the old library object. ``writeragent`` is one
        process-global module. Each executor keeps its own ScriptLibrary. On
        the bind thread the ContextVar wins. Off that thread (timeout
        fallback) the library's own executor is used, because the ContextVar
        does not follow the thread.
        """
        current = _current_executor.get()
        if current is not None:
            return current
        return self._executor

    def _names(self) -> list[str]:
        executor = self._resolve_executor()
        listing_cache = getattr(executor, "_named_script_listing", None) if executor is not None else None
        if listing_cache is None:
            listing = _rpc_named(LIST_NAMED_PYTHON_SCRIPTS)
            listing_cache = listing if isinstance(listing, dict) else {}
            if executor is not None:
                executor._named_script_listing = listing_cache
        raw = listing_cache.get(self._origin) or []
        if not isinstance(raw, list):
            return []
        return [str(n) for n in raw if isinstance(n, str)]

    def _ident_map_now(self) -> dict[str, list[str]]:
        mapping: dict[str, list[str]] = {}
        for title in self._names():
            ident = python_identifier_from_script_name(title)
            mapping.setdefault(ident, []).append(title)
        return mapping

    def __getattr__(self, item: str) -> Any:
        if item.startswith("_"):
            raise AttributeError(item)
        mapping = self._ident_map_now()
        titles = mapping.get(item) or []
        if not titles:
            raise AttributeError(f"No {self._origin} script sanitizes to {item!r}")
        if len(titles) > 1:
            raise AttributeError(
                f"Multiple {self._origin} scripts map to {item!r}: {titles!r}. "
                "Use wa.scripts[title] / wa.doc[title] with the stored name."
            )
        return load_named_script(self._origin, titles[0], executor=self._resolve_executor())

    def __getitem__(self, name: str) -> Any:
        return load_named_script(self._origin, name, executor=self._resolve_executor())

    def __dir__(self) -> list[str]:
        return sorted(set(super().__dir__()) | set(self._ident_map_now().keys()) | set(self._names()))

    def __contains__(self, item: object) -> bool:
        if not isinstance(item, str):
            return False
        return item in self._names() or item in self._ident_map_now()

    def __repr__(self) -> str:
        return f"ScriptLibrary(origin={self._origin!r})"


def _library_for_executor(executor: Any, origin: str) -> ScriptLibrary:
    """Return the ScriptLibrary bound to *executor*, creating it once."""
    attr = "_named_scripts_library" if origin == ORIGIN_USER else "_named_doc_library"
    existing = getattr(executor, attr, None)
    if isinstance(existing, ScriptLibrary) and existing._origin == origin and existing._executor is executor:
        return existing
    lib = ScriptLibrary(origin)
    lib._executor = executor
    setattr(executor, attr, lib)
    return lib


def attach_named_script_libraries(executor: Any | None = None) -> None:
    """Bind ``writeragent.scripts`` / ``writeragent.doc`` on the alias module.

    LibrePy omits ``writeragent_api``; ``import writeragent`` is
    ``writeragent_namespace``. Attach there first so Run Python Script still
    sees ``wa.scripts`` / ``wa.doc``.
    """
    mods: list[Any] = []
    try:
        from plugin.scripting import writeragent_namespace

        mods.append(writeragent_namespace)
    except ImportError:
        log.debug("named_scripts: writeragent_namespace missing", exc_info=True)
    try:
        from plugin.scripting import writeragent_api

        mods.append(writeragent_api)
    except ImportError:
        log.debug("named_scripts: writeragent_api missing (LibrePy)", exc_info=True)
    alias = sys.modules.get("writeragent")
    if alias is not None and alias not in mods:
        mods.append(alias)
    if not mods:
        return
    # Bind this execute's libraries onto the executor and point the alias
    # module at those objects. Do not retarget a library another run still
    # holds: one module-level ScriptLibrary meant a second document's run
    # redirected the first run's object.
    if executor is None:
        scripts = ScriptLibrary(ORIGIN_USER)
        doc = ScriptLibrary(ORIGIN_DOCUMENT)
    else:
        scripts = _library_for_executor(executor, ORIGIN_USER)
        doc = _library_for_executor(executor, ORIGIN_DOCUMENT)
    for mod in mods:
        mod.scripts = scripts
        mod.doc = doc


def bind_named_scripts_executor(executor: Any) -> Any:
    """New execute: re-check hashes; keep module cache on the shared executor.

    Returns the ContextVar token so callers can reset it on completion.

    Set the ContextVar only after attach succeeds. Attach stamps the executor
    onto the library and does not read the var. Setting the var first meant a
    raise in attach never reached ``_run_on_executor``'s finally, so the var
    stayed bound to this executor on the thread.
    """
    executor._named_script_checked = set()
    executor._named_script_listing = None
    attach_named_script_libraries(executor)
    return _current_executor.set(executor)


def reset_named_scripts_executor(token: Any) -> None:
    """Reset the ContextVar token from bind_named_scripts_executor.

    Reset the ContextVar with the token from bind. Leaving it set leaked the
    executor to later code on the same thread.
    """
    if token is not None:
        try:
            _current_executor.reset(token)
        except Exception as e:
            log.debug("Failed to reset named_scripts_executor ContextVar: %s", e)


def host_list_named_python_scripts(*, user_scripts: dict[str, str], document_scripts: dict[str, str]) -> dict[str, list[str]]:
    from plugin.scripting.document_scripts import picker_document_scripts

    return {
        ORIGIN_USER: sorted(user_scripts.keys()),
        ORIGIN_DOCUMENT: sorted(picker_document_scripts(document_scripts)),
    }


def host_get_named_python_script(
    *,
    name: str,
    origin: str,
    known_hash: str | None,
    user_scripts: dict[str, str],
    document_scripts: dict[str, str],
) -> dict[str, Any]:
    if origin not in (ORIGIN_USER, ORIGIN_DOCUMENT):
        raise RuntimeError(f"Unknown named-script origin {origin!r}")
    if origin == ORIGIN_DOCUMENT:
        from plugin.scripting.document_scripts import is_calc_init_script_name

        # INIT is the workbook init script, not a wa.doc library.
        if is_calc_init_script_name(name):
            raise RuntimeError(f"No {origin} script named {name!r}")
    store = user_scripts if origin == ORIGIN_USER else document_scripts
    code = store.get(name)
    if not isinstance(code, str):
        raise RuntimeError(f"No {origin} script named {name!r}")
    if len(code.encode("utf-8")) > _NAMED_SCRIPT_MAX_BYTES:
        raise RuntimeError(f"Named script {name!r} is too large to import as a library")
    digest = script_body_hash(code)
    if known_hash and known_hash == digest:
        return {"unchanged": True, "hash": digest, "name": name, "origin": origin}
    return {"unchanged": False, "hash": digest, "name": name, "origin": origin, "code": code}

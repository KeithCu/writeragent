# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Venv worker sandbox: path setup for vendored smolagents + LocalPythonExecutor.

Used by worker_harness.py (venv child adds repo root to sys.path for ``plugin.*`` imports).
Import policy is default-allow plus overrides against a short blocklist—no find_spec pre-checks.

Trusted host helpers (vision, embeddings, …) use ``run_trusted_action`` via the worker
harness / ``trusted_action_registry`` — not string stubs through this sandbox.
"""

from __future__ import annotations

import ast
import copy
from collections import OrderedDict
from dataclasses import dataclass
import hashlib
import logging
import math
import os
import sys
import threading
import time
import types
from collections.abc import Callable
from contextvars import ContextVar
from typing import Any

log = logging.getLogger(__name__)

# ``LocalPythonExecutor.timeout()`` needs ``int | None`` (at least 1, or None when spent).
# ``child_runtime.Deadline.run_for()`` returns a float; ceil with a one-second floor matches alarm semantics.
_EXECUTOR_ALARM_MIN_SEC = 1.0

from plugin.contrib.smolagents.local_python_executor import (
    ExecutionTimeoutError,
    InterpreterError,
    LocalPythonExecutor,
    SANDBOX_POLICY_CONTEXT,
    finalize_thread_pool_timeout,
    get_file_os_proxy,
    print_outputs_from_state,
)
from plugin.scripting.session_key import (
    SessionKey,
    SessionKind,
)
from plugin.scripting.payload_codec import (
    ForceBinary,
    describe_wire_value,
    find_image_payloads,
    is_split_grid,
)
from plugin.scripting.venv.to_wire import _figure_to_image_payload, to_wire
from plugin.scripting.venv import optional_import
from plugin.scripting.config_limits import python_exec_timeout_default
from plugin.framework.ipc import UserStopped
from plugin.framework.constants import auto_imports
from plugin.scripting.venv.results import error_result
from plugin.scripting.sandbox import (
    SANDBOX_BLOCKED_MODULES,
    SANDBOX_EXTRA_MODULES,
    SandboxPolicy,
    build_sandbox_policy,
    install_alias_authorizer,
    parse_extra_modules,
    policy_allowlist,
    policy_blocked_imports,
    policy_relaxed_roots,
)

install_alias_authorizer()

_MAX_ISOLATED_INIT_SNAPSHOTS = 32


@dataclass
class SessionRecord:
    executor: LocalPythonExecutor | None = None
    init_script_hash: str | None = None
    seeded_digest: str | None = None


class SessionStore:
    """Persistent sandbox executors and init metadata keyed by session wire id."""

    def __init__(self) -> None:
        # Lock order: this lock before DuckDB duckdb_sql._SESSION_LOCK.
        self._lock: threading.Lock = threading.Lock()
        self._records: dict[SessionKey, SessionRecord] = {}
        self._isolated_init_lru: OrderedDict[SessionKey, None] = OrderedDict()

    @staticmethod
    def _policy_current(rec: SessionRecord | None, policy: SandboxPolicy) -> bool:
        # No cached executor means nothing was built under another policy.
        if rec is None or rec.executor is None:
            return True
        return rec.executor._sandbox_policy == policy

    def _drop_if_policy_mismatch_unlocked(
        self, key: SessionKey, policy: SandboxPolicy
    ) -> None:
        if not self._policy_current(self._records.get(key), policy):
            self._drop_unlocked(key)

    def _maybe_drop_record(self, key: SessionKey) -> None:
        rec = self._records.get(key)
        if rec is None:
            return
        if (
            rec.executor is None
            and rec.init_script_hash is None
            and rec.seeded_digest is None
        ):
            self._records.pop(key, None)

    def _forget_cell_unlocked(self, cell_key: SessionKey) -> None:
        cell_rec = self._records.get(cell_key)
        if cell_rec is not None:
            cell_rec.executor = None
            cell_rec.seeded_digest = None
            self._maybe_drop_record(cell_key)

    def executor(self, session_id: str) -> LocalPythonExecutor | None:
        with self._lock:
            rec = self._records.get(SessionKey.parse(session_id))
            return rec.executor if rec else None

    def seeded_digest(self, session_id: str) -> str | None:
        with self._lock:
            rec = self._records.get(SessionKey.parse(session_id))
            return rec.seeded_digest if rec else None

    def init_is_current(
        self,
        init_session_id: str,
        script_digest: str,
        policy: SandboxPolicy,
    ) -> bool:
        """Return True when init already ran for *script_digest* (refresh isolated LRU)."""
        with self._lock:
            key = SessionKey.parse(init_session_id)
            prior = self._records.get(key)
            prior_hash = prior.init_script_hash if prior else None
            cached_exec = prior.executor if prior else None
            hash_stale = prior_hash is not None and prior_hash != script_digest
            if hash_stale or not self._policy_current(prior, policy):
                self._clear_init_unlocked(key)
                return False
            if prior_hash == script_digest and cached_exec is not None:
                if key.kind is SessionKind.ISOLATED:
                    self._note_isolated_access_unlocked(key)
                return True
            return False

    def commit_init(self, init_session_id: str, script_digest: str) -> None:
        with self._lock:
            key = SessionKey.parse(init_session_id)
            rec = self._records.setdefault(key, SessionRecord())
            rec.init_script_hash = script_digest
            if key.kind is SessionKind.ISOLATED:
                self._note_isolated_access_unlocked(key)

    def abort_init(self, init_session_id: str) -> None:
        with self._lock:
            self._clear_init_unlocked(SessionKey.parse(init_session_id))

    def get_or_create(
        self,
        session_id: str,
        *,
        policy: SandboxPolicy,
    ) -> tuple[LocalPythonExecutor, bool]:
        with self._lock:
            key = SessionKey.parse(session_id)
            self._drop_if_policy_mismatch_unlocked(key, policy)
            rec = self._records.get(key)
            executor = rec.executor if rec else None

            created = False
            if executor is None:
                executor = _new_executor(policy=policy)
                executor._sandbox_policy = policy
                if rec is None:
                    rec = SessionRecord()
                    self._records[key] = rec
                rec.executor = executor
                created = True
            return executor, created

    def set_seeded_digest(self, session_id: str, digest: str) -> None:
        with self._lock:
            key = SessionKey.parse(session_id)
            rec = self._records.setdefault(key, SessionRecord())
            rec.seeded_digest = digest

    def _note_isolated_access_unlocked(self, key: SessionKey) -> None:
        self._isolated_init_lru[key] = None
        self._isolated_init_lru.move_to_end(key)
        while len(self._isolated_init_lru) > _MAX_ISOLATED_INIT_SNAPSHOTS:
            # Take the least recently used slot out first, then drop what it
            # cached through the same path as an explicit reset (executor,
            # init digest, DuckDB catalog and the cell companion).
            oldest_key = self._isolated_init_lru.popitem(last=False)[0]
            self._clear_init_unlocked(oldest_key)

    def _clear_init_unlocked(self, key: SessionKey) -> None:
        cell_sid = key.without_init().wire() if key.init else None
        rec = self._records.get(key)
        if rec is not None:
            rec.executor = None
            rec.init_script_hash = None
            if key.kind is SessionKind.ISOLATED:
                self._isolated_init_lru.pop(key, None)
            self._maybe_drop_record(key)
        _reset_session_duckdb(key.wire())
        if cell_sid:
            self._forget_cell_unlocked(key.without_init())
            # Init-hash change drops the workbook kernel; DuckDB tables must go too.
            _reset_session_duckdb(cell_sid)

    def _drop_unlocked(self, key: SessionKey) -> None:
        # Desktop workbooks use calc:… and compute uses the raw Online session id;
        # both store the init executor at {id}:init. Reset drops both.
        if key.init:
            self._clear_init_unlocked(key)
        else:
            self._forget_cell_unlocked(key)
            self._clear_init_unlocked(key.with_init_suffix())

    def reset(self, session_id: str) -> dict[str, Any]:
        """Drop cached executors for *session_id* and reset DuckDB tables.

        Also clears the ``{id}:init`` companion when *session_id* is a cell id,
        and clears the cell session companion when *session_id* is an init id.
        ``_clear_init_unlocked`` closes DuckDB for this id and for the
        :init companion. Do not close again after the lock.
        """
        # The harness and the compute child refuse a blank id before calling.
        with self._lock:
            self._drop_unlocked(SessionKey.parse(session_id))
        return {"status": "ok"}

    def drop_if_policy_mismatch(self, session_id: str, policy: SandboxPolicy) -> None:
        with self._lock:
            self._drop_if_policy_mismatch_unlocked(
                SessionKey.parse(session_id), policy
            )


SESSIONS = SessionStore()


# Cell / RPS session for the current execute. Isolated runs leave this None so
# DuckDB and similar caches stay per-request. Init-only ids are not stored here
# (``calc:…:init`` would otherwise leak a catalog across Isolated cells).
_CURRENT_SANDBOX_SESSION: ContextVar[str | None] = ContextVar(
    "sandbox_session_id", default=None
)
_CURRENT_SANDBOX_SCOPED_DIR: ContextVar[str | None] = ContextVar(
    "sandbox_scoped_dir", default=None
)


def current_sandbox_scoped_dir() -> str | None:
    """Host document folder for this sandboxed execute, or ``None``.

    Set from the ``bindings`` argument, not from ``executor.state``.
    A cell assignment to ``scoped_dir`` must not retarget the DuckDB jail.
    """
    return _CURRENT_SANDBOX_SCOPED_DIR.get()


# Distinct from the session id: isolated executes set the id to None, and host
# callers never enter run_sandboxed_code. DuckDB uses this to refuse a cell
# that names another workbook's catalog.
_SANDBOX_EXECUTE: ContextVar[bool] = ContextVar("sandbox_execute", default=False)


def current_sandbox_session_id() -> str | None:
    """Workbook session id for this sandboxed execute, or ``None`` (isolated)."""
    return _CURRENT_SANDBOX_SESSION.get()


def sandbox_execute_active() -> bool:
    """True while ``run_sandboxed_code`` is on this thread (including isolated)."""
    return _SANDBOX_EXECUTE.get()


def _install_timeout_context_pool() -> None:
    """Copy sandbox ContextVars onto the SIGALRM fallback thread.

    ``local_python_executor.timeout`` runs the cell on a ``ThreadPoolExecutor``
    worker when SIGALRM cannot be installed (Windows, or not the main thread).
    That worker starts with an empty context, so the session id and the
    execute flag would be the defaults and ``session_duckdb(other_id)`` would
    open another workbook.

    The vendored timeout looks up ``ThreadPoolExecutor`` on its module when
    the fallback runs. Submit through ``copy_context().run`` so the worker
    sees the session ``run_sandboxed_code`` just set. The vendored file stays
    unchanged; it must keep using that module global.

    That fallback thread cannot be killed. The _Ops deadline stops
    pure-Python AST execution on timeout, but a blocked C call in the thread
    still runs until it returns; host IPC kill remains the stop for a call that never returns.
    """
    import contextvars
    from concurrent.futures import Future, ThreadPoolExecutor

    from plugin.contrib.smolagents import local_python_executor as lpe

    class _ContextThreadPoolExecutor(ThreadPoolExecutor):
        def submit(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Future[Any]:
            ctx = contextvars.copy_context()
            return super().submit(ctx.run, fn, *args, **kwargs)

    # The vendored name is the stdlib class. This subclass is what timeout()
    # constructs on the SIGALRM fallback path. setattr: mypy rejects assigning
    # over a class object ("Cannot assign to a type").
    setattr(lpe, "ThreadPoolExecutor", _ContextThreadPoolExecutor)


_install_timeout_context_pool()


def _reset_session_duckdb(session_id: str) -> None:
    """Close the Phase D DuckDB catalog for *session_id* (LibrePy has no module)."""
    try:
        from plugin.scripting.venv.duckdb_sql import reset_session_duckdb
    except ImportError:
        return
    reset_session_duckdb(session_id)


def _host_scoped_dir(bindings: dict[str, Any] | None) -> str | None:
    if not isinstance(bindings, dict):
        return None
    raw_folder = bindings.get("scoped_dir")
    if isinstance(raw_folder, str) and raw_folder.strip():
        return raw_folder
    return None


def _inject_session_duckdb(
    executor: LocalPythonExecutor,
    host_scoped_dir: str | None,
) -> None:
    """Bind ``session_duckdb`` / ``run_sql`` / ``invalidate_session_tables`` when DuckDB helpers ship."""
    try:
        from plugin.scripting.venv.duckdb_sql import (
            invalidate_session_tables,
            query_folder_df,
            run_sql,
            session_duckdb,
        )
    except ImportError:
        return

    # Capture the host folder at inject time — after bindings, before user
    # code — and do not read executor.state["scoped_dir"] again. The cell can
    # assign scoped_dir before calling run_sql, and resolve_flat_file_path
    # would then accept files under that folder. run_sandboxed_code drops a
    # previous execute's scoped_dir before bindings, so this value is only the
    # folder this execute actually bound.

    def run_sql_bound(
        sql: str,
        con: Any | None = None,
        **kwargs: Any,
    ) -> Any:
        return run_sql(sql, con, **kwargs)

    def query_folder_df_bound(
        sql: str,
        files: list[str] | dict[str, str] | None = None,
        scoped_dir: str | None = None,
        *,
        preloaded: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Any:
        del scoped_dir
        return query_folder_df(
            sql,
            files=files,
            scoped_dir=host_scoped_dir,
            preloaded=preloaded,
            **kwargs,
        )

    helpers = {
        "session_duckdb": session_duckdb,
        "invalidate_session_tables": invalidate_session_tables,
        "run_sql": run_sql_bound,
        "query_folder_df": query_folder_df_bound,
    }
    executor.send_variables(helpers)
    executor.custom_tools.update(helpers)


_DUCKDB_HELPER_NAMES = ("session_duckdb", "invalidate_session_tables", "run_sql", "query_folder_df")
_CELL_OWNED_NAMES = (
    "result",
    "data",
    "ranges",
    "xl",
    "scoped_dir",
    *_DUCKDB_HELPER_NAMES,
)
_INIT_STATE_SKIP_KEYS = frozenset(
    {
        "__name__",
        # _snapshot_init_bindings already drops key.startswith("_"). These two
        # stay so a future filter change does not seed the framework counters.
        "_print_outputs",
        "_operations_count",
        *_CELL_OWNED_NAMES,
    }
)


class CellNamespace:
    """Namespace builder for sandboxed execution: fresh, seed, bind."""

    CELL_OWNED_NAMES: tuple[str, ...] = _CELL_OWNED_NAMES
    SKIP_KEYS: frozenset[str] = _INIT_STATE_SKIP_KEYS

    executor: LocalPythonExecutor

    def __init__(self, executor: LocalPythonExecutor) -> None:
        self.executor = executor

    @classmethod
    def fresh(
        cls,
        policy: SandboxPolicy,
    ) -> CellNamespace:
        return cls(_new_executor(policy=policy))

    def seed(self, init_session_id: str, *, copy: bool = False) -> None:
        _seed_executor_from_init(self.executor, init_session_id, copy_values=copy, skip_keys=self.SKIP_KEYS)

    def seed_shared(
        self,
        session_id: str,
        init_session_id: str,
        init_script_hash: str | None,
    ) -> None:
        """Copy init bindings into a shared executor once per init digest.

        ``send_variables`` is ``state.update``. Seeding on every cell overwrote
        names the cell had rebound (init ``FACTOR = 10``, cell ``FACTOR = 99``,
        the next cell saw 10). Init-script edits already drop the cell executor
        in ``SessionStore._clear_init_unlocked``, so a new digest seeds a fresh one.
        Isolated cells have no ``session_id`` and still seed on every run.
        """
        digest = init_script_hash or ""
        if SESSIONS.seeded_digest(session_id) == digest:
            return
        _seed_executor_from_init(self.executor, init_session_id)
        SESSIONS.set_seeded_digest(session_id, digest)

    def bind(
        self,
        *,
        code: str = "",
        data: Any = None,
        bindings: dict[str, Any] | None = None,
        scoped_dir: str | None,
    ) -> None:
        inject_auto_imports(self.executor, code)
        ranges = _inject_data(self.executor, data)
        _inject_excel_xl(self.executor, ranges)
        self.executor.state.pop("scoped_dir", None)
        _inject_bindings(self.executor, bindings)
        _inject_session_duckdb(self.executor, scoped_dir)


def _code_says_imported(code: str, module_name: str) -> bool:
    return f"import {module_name}" in code or f"from {module_name}" in code


def _auto_import_alias(module_name: str, import_stmt: str) -> str:
    if " as " in import_stmt:
        return import_stmt.rsplit(" as ", 1)[-1].strip()
    return module_name


def apply_auto_imports(code: str) -> tuple[str, int]:
    """Prepend imports from AUTO_IMPORTS if missing and available. Returns (new_code, lines_added)."""
    scanned = _scan_user_code(code)
    bound: set[str] = scanned[0] if scanned is not None else set()
    prepended_lines: list[str] = []
    for module_name, import_stmt in auto_imports().items():
        if _auto_import_alias(module_name, import_stmt) in bound:
            continue
        imported_already = (
            _code_says_imported(code, module_name)
            if scanned is None
            else _code_imports_module(scanned[1], module_name)
        )
        if not imported_already and optional_import.optional_module(module_name) is not None:
            prepended_lines.append(import_stmt)
    if not prepended_lines:
        return code, 0
    return "\n".join(prepended_lines) + "\n" + code, len(prepended_lines)


def _scan_user_code(code_str: str) -> tuple[set[str], set[str]] | None:
    """Module-scope bound names and imported module strings from one parse."""
    try:
        tree = ast.parse(code_str)
    except SyntaxError:
        return None
    bound: set[str] = set()
    imported: set[str] = set()

    def visit(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                bound.add(child.name)
                continue  # bodies run in their own namespace
            if isinstance(child, ast.Lambda):
                continue
            if isinstance(child, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
                # Only the iter runs on the module state (e.g. [x for x in (r := 5)]).
                for comp in child.generators:
                    visit(comp.iter)
                continue
            # ``result: int`` does not store when there is no value; do not
            # walk the annotation either (evaluate_annassign skips it).
            if isinstance(child, ast.AnnAssign) and child.value is None:
                continue
            if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Store):
                bound.add(child.id)
                continue
            if isinstance(child, ast.Import):
                for alias in child.names:
                    bound.add(alias.asname or alias.name.split(".")[0])
                    imported.add(alias.name)
                continue
            if isinstance(child, ast.ImportFrom):
                if child.module:
                    imported.add(child.module)
                for alias in child.names:
                    bound.add(alias.asname or alias.name)
                continue
            visit(child)

    visit(tree)
    return bound, imported


def _code_imports_module(imported: set[str], module_name: str) -> bool:
    prefix = module_name + "."
    return any(name == module_name or name.startswith(prefix) for name in imported)


def _module_binds_name(code: str, name: str) -> bool:
    """True when *code* stores *name* in the module state the executor keeps."""
    scanned = _scan_user_code(code)
    return scanned is not None and name in scanned[0]


def inject_auto_imports(executor: LocalPythonExecutor, code: str) -> None:
    """Inject auto imports into executor state if not already bound or imported in code."""
    scanned = _scan_user_code(code)
    bound_names: set[str] = scanned[0] if scanned is not None else set()
    imported: set[str] | None = None if scanned is None else scanned[1]
    bindings: dict[str, Any] = {}
    for module_name, import_stmt in auto_imports().items():
        alias = _auto_import_alias(module_name, import_stmt)
        if alias in bound_names or alias in executor.state:
            continue
        # pyplot costs seconds (font cache) on first import. Bind plt only when
        # the code names it or pyplot is already loaded.
        if module_name == "matplotlib.pyplot" and alias not in code and module_name not in sys.modules:
            continue
        # A cell that fails to parse never runs, but the executor is reused.
        already = (
            _code_says_imported(code, module_name)
            if imported is None
            else _code_imports_module(imported, module_name)
        )
        if not already:
            mod = optional_import.optional_module(module_name)
            if mod is not None:
                bindings[alias] = mod
    if bindings:
        executor.send_variables(bindings)


def serialize_result(
    obj: Any,
    *,
    force: ForceBinary = "auto",
    log_failure: bool = True,
) -> Any:
    """Convert numpy/pandas and containers to JSON-safe values (split_grid for large numeric/mixed arrays).

    DataFrames (and named Series) are returned as a dataframe envelope with 'columns' and 'data'
    (a split_grid envelope when large enough, else nested lists), so a large frame takes the
    binary grid fast path instead of a list of row dicts.
    """
    try:
        return to_wire(obj, target="pickle", force=force)
    except Exception:
        if log_failure:
            log.exception(
                "venv_sandbox serialize_result failed for value %s",
                describe_wire_value(obj),
            )
        raise


def _capture_open_figures_payload(*, fmt: str = "svg") -> tuple[dict[str, Any] | None, str]:
    """Return (image payload from open pyplot figures, optional stdout note)."""
    # No figure can be open before something imported pyplot; importing it
    # here would cost the multi-second font cache load on non-plot cells.
    if "matplotlib.pyplot" not in sys.modules:
        return None, ""
    plt_mod = optional_import.optional_module("matplotlib.pyplot")
    if plt_mod is None:
        return None, ""
    fignums = plt_mod.get_fignums()
    if not fignums:
        return None, ""

    note = ""
    try:
        figs = [plt_mod.figure(num) for num in fignums]
        if len(figs) > 1:
            items = [_figure_to_image_payload(fig, fmt=fmt) for fig in figs]
            payload = {
                "__wa_payload__": "multi_data",
                "items": items,
            }
            note = f"Captured {len(figs)} open figures.\n"
        else:
            payload = _figure_to_image_payload(figs[0], fmt=fmt)
        return payload, note
    finally:
        # Close even when rendering raises or figure(num) fails; a figure left
        # open would be what the next cell returns. A close failure must not
        # replace the payload or the original render error (same swallow as
        # _close_open_figures).
        try:
            plt_mod.close("all")
        except Exception:
            log.debug("failed to close pyplot figures", exc_info=True)


def _new_executor(
    policy: SandboxPolicy,
) -> LocalPythonExecutor:
    # open must be in static_tools; pass in additional_functions so send_tools keeps it.
    additional_functions = {"open": open} if policy.files else None
    # timeout_seconds uses the vendored default until _arm_alarm writes it before each run.
    executor = LocalPythonExecutor(
        additional_authorized_imports=policy_allowlist(policy),
        additional_functions=additional_functions,
    )
    # Upstream only merges BASE_PYTHON_TOOLS (sum, len, …) after send_tools(); without this,
    # static_tools stays None and builtins like sum() are rejected.
    executor.send_tools({})
    return executor


def _snapshot_init_bindings(
    init_session_id: str,
    skip_keys: frozenset[str] = _INIT_STATE_SKIP_KEYS,
) -> dict[str, Any]:
    """Copy user-visible names from the init executor (references, not deep copies)."""
    executor = SESSIONS.executor(init_session_id)
    if executor is None:
        return {}
    return {
        key: value
        for key, value in executor.state.items()
        if (
            isinstance(key, str)
            and key not in skip_keys
            and not key.startswith("_")
        )
    }


def _snapshot_init_custom_tools(init_session_id: str) -> dict[str, Any]:
    """Copy user-defined helper functions (custom tools) from the init executor."""
    executor = SESSIONS.executor(init_session_id)
    if executor is None:
        return {}
    return dict(executor.custom_tools)


def _copy_isolated_seed_value(value: Any) -> Any:
    """Duplicate an init binding so a cell cannot mutate that copied object.

    Cell 1 wrote ``items.append(...)`` without reassigning ``items``; that
    changed what every later isolated cell on that worker saw. Deepcopy gives
    each cell its own containers. Functions and modules stay shared: deepcopy
    rejects them, and a smolagents function closes over the init executor
    state. ``def add(x): items.append(x)`` therefore mutates the seed, and
    the next isolated cell sees that change. Lambdas and methods on
    init-defined classes do the same. Shared-kernel seeding does not use
    this — that workbook is one namespace.

    A lazy copy-on-demand or size guard would be too complex and prone to edge
    cases, so a simple deepcopy is used here on every cell execution for safety.
    """
    if callable(value) or isinstance(value, types.ModuleType):
        return value
    try:
        return copy.deepcopy(value)
    except Exception:
        return value


def _seed_executor_from_init(
    executor: LocalPythonExecutor,
    init_session_id: str,
    *,
    copy_values: bool = False,
    skip_keys: frozenset[str] = _INIT_STATE_SKIP_KEYS,
) -> None:
    bindings = _snapshot_init_bindings(init_session_id, skip_keys=skip_keys)
    if copy_values and bindings:
        bindings = {key: _copy_isolated_seed_value(value) for key, value in bindings.items()}
    if bindings:
        executor.send_variables(bindings)
    # custom_tools go to executor.custom_tools only, not into executor state,
    # so an init lambda in custom_tools does not become the next cell's printed result.
    custom_tools = _snapshot_init_custom_tools(init_session_id)
    if custom_tools:
        executor.custom_tools.update(custom_tools)


def _resolve_init_digest(init_script: str | None, init_script_hash: str | None) -> str:
    """Digest that decides whether the init session must re-run.

    A caller-supplied hash still wins, so the host and child stay on the
    same digest. When the hash is omitted, hash the stripped script — the
    same bytes ``document_scripts.init_script_hash`` hashes. An empty
    stand-in would match the next edit and ``_ensure_init_executed`` would
    return early, leaving the shared cell executor uncleared. Both the init
    map and the cell-seed map store this digest, so a later call that starts
    passing the host hash does not look like a change and reseed over a
    cell rebind.
    """
    provided = (init_script_hash or "").strip()
    if provided:
        return provided
    script = (init_script or "").strip()
    if not script:
        return ""
    return hashlib.sha256(script.encode("utf-8")).hexdigest()


def reset_sandbox_session(session_id: str) -> dict[str, Any]:
    """Forward to SessionStore.reset."""
    return SESSIONS.reset(session_id)


def _arm_alarm(executor: LocalPythonExecutor, deadline: float, timeout_sec: int) -> dict[str, Any] | None:
    left = deadline - time.monotonic()
    if left <= 0:
        return error_result(
            message=f"Code execution exceeded the maximum execution time of {timeout_sec} seconds"
        )
    executor.timeout_seconds = math.ceil(max(left, _EXECUTOR_ALARM_MIN_SEC))
    return None


def _ensure_init_executed(
    init_session_id: str,
    init_script: str,
    *,
    timeout_sec: int,
    deadline: float,
    init_script_hash: str | None = None,
    policy: SandboxPolicy,
) -> dict[str, Any] | None:
    """Run *init_script* once in the persistent init session. Returns error dict or None."""
    script = (init_script or "").strip()
    if not script:
        return None

    script_digest = init_script_hash or ""
    if SESSIONS.init_is_current(init_session_id, script_digest, policy):
        return None

    init_executor, _ = SESSIONS.get_or_create(init_session_id, policy=policy)
    # Init shares the request deadline with the cell. A full budget for each
    # could outlast the host's single read, and the host would then kill this
    # worker and every other workbook's session on it.
    spent = _arm_alarm(init_executor, deadline, timeout_sec)
    if spent is not None:
        return spent
    inject_auto_imports(init_executor, script)
    result = _run_on_executor(init_executor, script, result_optional=True)
    if result.get("status") != "ok":
        SESSIONS.abort_init(init_session_id)
        return result

    SESSIONS.commit_init(init_session_id, script_digest)
    return None


def _inject_excel_xl(executor: LocalPythonExecutor, ranges: tuple[Any, ...] | None = None) -> None:
    """Inject binding-only Excel ``xl()`` closed over *ranges* (may be empty)."""
    from plugin.scripting.excel_xl import make_xl

    executor.send_variables({"xl": make_xl(ranges)})


def _inject_data(executor: LocalPythonExecutor, data: Any | None) -> tuple[Any, ...]:
    """Inject ``ranges`` (always a list) and polymorphic ``data``.

    * One formula arg: ``data`` is that ``CalcRange``; ``ranges == [data]``.
    * Two or more: ``data`` is the same list object as ``ranges``.

    Returns the materialized ranges tuple (empty when *data* is None) so callers
    can bind Excel ``xl()`` to the same ranges.
    """
    if data is None:
        executor.send_variables({"data": None, "ranges": []})
        return ()
    from plugin.scripting.calc_range import materialize_inputs
    from plugin.scripting.payload_codec import describe_wire_value, is_calc_range_payload, is_multi_data, is_split_grid

    if is_split_grid(data) or is_calc_range_payload(data) or is_multi_data(data):
        log.debug("venv_sandbox injecting data %s", describe_wire_value(data))

    ranges = materialize_inputs(data)

    ranges_list = list(ranges)
    if len(ranges_list) == 1:
        data_var: Any = ranges_list[0]
    elif len(ranges_list) >= 2:
        # Same object so ``data is ranges`` under multi-range.
        data_var = ranges_list
    else:
        data_var = None
    variables: dict[str, Any] = {
        "data": data_var,
        "ranges": ranges_list,
    }
    executor.send_variables(variables)
    return ranges


def _inject_bindings(executor: LocalPythonExecutor, bindings: dict[str, Any] | None) -> None:
    """Inject host-provided named values (e.g. selected image bytes) into the sandbox namespace."""
    if not bindings:
        return
    executor.send_variables(dict(bindings))


_RESULT_MISSING = object()
# Intentional check-then-set with no lock: LibrePy/WriterAgent venv workers are
# one thread per process (execution is serialized; ``SessionStore._lock`` is for
# session state, not this). ``matplotlib.use("Agg")`` twice is harmless. Do not
# add a threading.Lock here unless the worker becomes multi-threaded; then this
# flag needs a lock (or to move under ``SessionStore._lock``).
_MPL_AGG_SET = False
_MPL_AGG_FAILED = False


def _ensure_mpl_agg() -> None:
    global _MPL_AGG_SET, _MPL_AGG_FAILED
    if _MPL_AGG_SET or _MPL_AGG_FAILED:
        return
    mpl = optional_import.optional_module("matplotlib")
    if mpl is not None and hasattr(mpl, "use"):
        try:
            mpl.use("Agg")
            _MPL_AGG_SET = True
        except Exception:
            # Remember the failed switch; retrying it on every execution hung.
            _MPL_AGG_FAILED = True


def _sync_custom_tools(
    executor: LocalPythonExecutor,
    skip_keys: frozenset[str] = _INIT_STATE_SKIP_KEYS,
) -> None:
    """Copy user callables into custom_tools for named-library eval.

    evaluate_name and evaluate_call read executor.state before custom_tools,
    so the cell finds a def without this copy. named_scripts._eval_library
    evaluates with a fresh state and a copy of custom_tools, so an init or
    earlier-cell helper is visible there only after this copy.

    Cell-owned names stay out. An init ``result = lambda: ...`` or
    ``def session_duckdb`` must not be seeded into the next cell.
    """
    for key, value in executor.state.items():
        if not isinstance(key, str) or key in skip_keys or key.startswith("_"):
            continue
        if callable(value) and key not in executor.custom_tools:
            executor.custom_tools[key] = value


def _is_non_value_result(obj: Any) -> bool:
    """True for a function or class the executor returns as the cell value.

    A ``def`` or ``class`` statement evaluates to the object it created.
    Those objects cannot cross the LibreOffice pickle boundary. Nulling
    them keeps a helper-only script (and an init session) successful.
    Instances stay values: a cell that ends on an instance still errors.
    Import statements already return None, so a module expression still
    errors on purpose.
    """
    return isinstance(
        obj,
        (
            types.FunctionType,
            types.BuiltinFunctionType,
            types.BuiltinMethodType,
            types.MethodType,
            type,
        ),
    )


def _is_mpl_artist_result(obj: Any) -> bool:
    """True for a pyplot artist or the list ``plt.plot`` returns."""
    artist_mod = sys.modules.get("matplotlib.artist")
    if artist_mod is None:
        return False
    artist = artist_mod.Artist
    if isinstance(obj, artist):
        return True
    return isinstance(obj, (list, tuple)) and bool(obj) and all(isinstance(item, artist) for item in obj)


def _close_open_figures() -> None:
    if "matplotlib.pyplot" not in sys.modules:
        return
    plt_mod = optional_import.optional_module("matplotlib.pyplot")
    if plt_mod is None:
        return
    try:
        if plt_mod.get_fignums():
            plt_mod.close("all")
    except Exception:
        log.debug("failed to close pyplot figures", exc_info=True)


def _serialize_cell_result(
    result: Any,
    *,
    force: ForceBinary = "auto",
    log_failure: bool = True,
) -> tuple[Any, str]:
    """Serialize *result*, swapping in open matplotlib figures when that is the output.

    A helper-only init script evaluates to the function, and plt.plot()
    evaluates to Line2D artists. Capture open figures before the pickle
    check rejects those objects, or the figures stay open and the next
    script returns that SVG. A class statement is the same kind of
    non-value as a function statement.

    A non-image value, including result = 42, is replaced when a figure is still open. That replacement stays.
    """
    if _is_non_value_result(result):
        result = None

    extra_stdout = ""
    if _is_mpl_artist_result(result):
        captured, note = _capture_open_figures_payload()
        if captured is None:
            serialized = serialize_result(result, force=force, log_failure=log_failure)
        else:
            serialized = captured
            extra_stdout = note
    else:
        serialized = serialize_result(result, force=force, log_failure=log_failure)
        if not find_image_payloads(serialized):
            captured, note = _capture_open_figures_payload()
            if captured is not None:
                serialized = captured
                extra_stdout = note
        else:
            _close_open_figures()
    return serialized, extra_stdout


def _fail_cell(
    executor: LocalPythonExecutor,
    prior_result: Any,
    message: str,
    *,
    code: str | None = None,
    stdout: str = "",
    include_traceback: bool = False,
) -> dict[str, Any]:
    """Restore the pre-cell ``result``, close figures, and return an error dict."""
    _restore_prior_result(executor, prior_result)
    _close_open_figures()
    return error_result(
        code, message, stdout=stdout, with_traceback=include_traceback
    )


def cell_rebound_result(code: str, prior: Any, current: Any) -> bool:
    """Whether the cell rebound ``result``, by identity change or module-scope store.

    A module-scope Store covers explicit assignments like ``result = 5`` or
    in-place ``result += [2]``. Identity covers writes the AST visitor does not
    see, such as ``except Exception as result``.
    """
    return current is not prior or _module_binds_name(code, "result")


def _run_on_executor(
    executor: LocalPythonExecutor,
    code: str,
    *,
    force: ForceBinary = "auto",
    result_optional: bool = False,
) -> dict[str, Any]:
    # Keep ``result`` in the namespace. Egress uses it when this cell stored
    # the name. Popping it after every cell would make ``result * 2`` in a
    # later cell a NameError, and a leftover binding would also be the egress
    # of a later last-expression cell. Identity misses interned singletons
    # (``result = 5`` then ``pass``) and in-place ``result += [2]`` (the same
    # list is stored again). A module-scope Store covers those. Identity still
    # covers writes the visitor does not see, such as ``except Exception as result``.
    # On failure, restore the pre-cell value.
    prior_result = executor.state.get("result", _RESULT_MISSING)
    token = None
    try:
        from plugin.scripting.named_scripts import (
            bind_named_scripts_executor,
            reset_named_scripts_executor,
        )

        token = bind_named_scripts_executor(executor)
        try:
            code_output = executor(code)
        finally:
            # Reset with the token from bind. A leftover ContextVar is the
            # previous cell's executor on this thread.
            reset_named_scripts_executor(token)
        _sync_custom_tools(executor)

        current = executor.state.get("result", _RESULT_MISSING)
        if current is not _RESULT_MISSING and cell_rebound_result(code, prior_result, current):
            result = current
        else:
            result = code_output.output

        try:
            serialized, extra_stdout = _serialize_cell_result(
                result, force=force, log_failure=not result_optional
            )
        except Exception:
            if not result_optional:
                raise
            # The init result is discarded; the executor has already stored the
            # script's names. Failing here would make _ensure_init_executed abort the
            # init session, and the first cell would lose helpers the script bound.
            log.debug(
                "dropping unpickleable init result %s.%s",
                type(result).__module__,
                type(result).__name__,
                exc_info=True,
            )
            # Unpickleable init results still close pyplot figures here so implicit capture on the next cell does not return a leftover plot.
            _close_open_figures()
            serialized, extra_stdout = None, ""

        if is_split_grid(serialized):
            log.debug("venv_sandbox worker result %s", describe_wire_value(serialized))
        stdout = (code_output.logs or "") + extra_stdout
        return {
            "status": "ok",
            "result": serialized,
            "stdout": stdout,
        }
    except UserStopped as e:
        # UserStopped is BaseException, so evaluate_try does not swallow it.
        # Wrapping host USER_STOPPED in RuntimeError lets the script keep
        # issuing wa.* calls after Stop.
        return _fail_cell(executor, prior_result, str(e) or "Stopped by user.", code="USER_STOPPED")
    except ExecutionTimeoutError as e:
        # ExecutionTimeoutError is BaseException; catch it before the broad
        # BaseException handler so timeout errors return without traceback.
        finalize_thread_pool_timeout()
        return _fail_cell(
            executor,
            prior_result,
            str(e),
            stdout=print_outputs_from_state(executor.state),
        )
    except InterpreterError as e:
        return _fail_cell(
            executor,
            prior_result,
            str(e),
            stdout=print_outputs_from_state(executor.state),
        )
    except Exception as e:
        # A pickle-boundary failure is raised after the executor returns, so
        # the cell's prints are already in _print_outputs; pass them as
        # stdout or they are lost.
        return _fail_cell(
            executor,
            prior_result,
            str(e),
            stdout=print_outputs_from_state(executor.state),
            include_traceback=True,
        )
    except BaseException as e:
        # SystemExit and KeyboardInterrupt are BaseException. The vendored
        # executor only catches Exception, so they would leave
        # run_sandboxed_code. After EXEC_STARTED a process death is not
        # replayed, and every shared session on that worker is dropped.
        # compute_service calls run_sandboxed_code directly, so this catch
        # is required even with the harness backstop. Return an error dict
        # and keep the executor. Re-raise GeneratorExit after the restore
        # so generator cleanup is unchanged.
        _restore_prior_result(executor, prior_result)
        _close_open_figures()
        if isinstance(e, GeneratorExit):
            raise
        # SystemExit and KeyboardInterrupt skip the executor's except Exception,
        # which is what copies prints, but the text is already in _print_outputs.
        return error_result(
            message=str(e),
            stdout=print_outputs_from_state(executor.state),
            with_traceback=True,
        )


def _restore_prior_result(executor: LocalPythonExecutor, prior_result: Any) -> None:
    """Drop a failed cell's partial ``result``; keep the last successful assignment."""
    if prior_result is _RESULT_MISSING:
        executor.state.pop("result", None)
    else:
        executor.state["result"] = prior_result


def run_sandboxed_code(
    code: str,
    data: Any | None = None,
    *,
    force: ForceBinary = "auto",
    bindings: dict[str, Any] | None = None,
    timeout_sec: int | None = None,
    session_id: str | None = None,
    init_script: str | None = None,
    init_session_id: str | None = None,
    init_script_hash: str | None = None,
    sandbox_policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run *code* in LocalPythonExecutor.

    Without *session_id*, each call uses a new namespace. With *session_id*, reuse one
    executor per id (shared kernel / workbook session).

    When *init_script* is set, it runs once in *init_session_id* (typically ``calc:…:init``).
    Isolated cell runs seed a fresh executor from a copy of that snapshot; shared kernel
    seeds the workbook session executor once, then reuses it for cell code.
    A missing *init_script_hash* is filled from the stripped script text in this process.
    """
    if timeout_sec is None:
        timeout_sec = python_exec_timeout_default()
    try:
        timeout_sec = int(timeout_sec)
    except (TypeError, ValueError, OverflowError):
        return error_result(message=f"Invalid timeout_sec: {timeout_sec!r}", stdout="")
    deadline = time.monotonic() + float(timeout_sec)

    if sandbox_policy is not None and not isinstance(sandbox_policy, dict):
        return error_result(message="sandbox_policy must be a dict", stdout="")
    policy_dict = sandbox_policy or {}
    try:
        raw_extra = policy_dict.get("extra_modules") or ""
        if not isinstance(raw_extra, str):
            return error_result(message="sandbox_policy.extra_modules must be a comma-separated string", stdout="")
        policy = build_sandbox_policy(
            network=bool(policy_dict.get("network", False)),
            files=bool(policy_dict.get("files", False)),
            os_=bool(policy_dict.get("os", False)),
            extra=parse_extra_modules(raw_extra),
        )
    except ValueError as e:
        return error_result(message=str(e), stdout="")

    # Force non-interactive backend so plt.show() doesn't block in the subprocess.
    # Before matplotlib is loaded, MPLBACKEND makes any later import (plt binding,
    # pandas .plot, seaborn) start on Agg without paying the import here.
    if "matplotlib" in sys.modules:
        _ensure_mpl_agg()
    else:
        os.environ["MPLBACKEND"] = "Agg"

    # Only the cell / RPS session_id is persistable. Isolated cells still have
    # init_session_id (calc:…:init); binding that would share DuckDB across cells.
    active_token = _SANDBOX_EXECUTE.set(True)
    token = _CURRENT_SANDBOX_SESSION.set(session_id)
    # session_duckdb().execute does not receive scoped_dir; the jail reads
    # this variable. It is set before _ensure_init_executed and _run_on_executor,
    # so the timeout fallback's copy_context() already carries it.
    host_folder = _host_scoped_dir(bindings)
    dir_token = _CURRENT_SANDBOX_SCOPED_DIR.set(host_folder)

    relaxed_roots = policy_relaxed_roots(policy)
    _, file_os_ids = get_file_os_proxy(files=policy.files)
    policy_token = SANDBOX_POLICY_CONTEXT.set((relaxed_roots, file_os_ids))
    blocked_token = SANDBOX_BLOCKED_MODULES.set(frozenset(policy_blocked_imports(policy)))
    extra_token = SANDBOX_EXTRA_MODULES.set(frozenset(policy_allowlist(policy)))

    try:
        init_sid = init_session_id if isinstance(init_session_id, str) and init_session_id.strip() else None
        init_digest = _resolve_init_digest(init_script, init_script_hash)

        if session_id:
            SESSIONS.drop_if_policy_mismatch(session_id, policy)

        if init_sid and (init_script or "").strip():
            init_err = _ensure_init_executed(
                init_sid,
                init_script or "",
                timeout_sec=timeout_sec,
                deadline=deadline,
                init_script_hash=init_digest,
                policy=policy,
            )
            if init_err is not None:
                return init_err

        if session_id:
            executor, created = SESSIONS.get_or_create(
                session_id, policy=policy
            )
            ns = CellNamespace(executor)
            if init_sid:
                ns.seed_shared(session_id, init_sid, init_digest)
        else:
            ns = CellNamespace.fresh(policy)
            executor = ns.executor
            created = False
            if init_sid:
                ns.seed(init_sid, copy=True)

        spent = _arm_alarm(executor, deadline, timeout_sec)
        if spent is not None:
            if session_id:
                spent["namespace_created"] = created
            return spent

        ns.bind(code=code, data=data, bindings=bindings, scoped_dir=host_folder)
        raw = _run_on_executor(executor, code, force=force)
        if session_id:
            raw["namespace_created"] = created
        return raw
    finally:
        SANDBOX_EXTRA_MODULES.reset(extra_token)
        SANDBOX_BLOCKED_MODULES.reset(blocked_token)
        SANDBOX_POLICY_CONTEXT.reset(policy_token)
        _CURRENT_SANDBOX_SCOPED_DIR.reset(dir_token)
        _CURRENT_SANDBOX_SESSION.reset(token)
        _SANDBOX_EXECUTE.reset(active_token)

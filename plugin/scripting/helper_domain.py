# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared host glue for trusted helper domains (script facts, templates, RPS outcomes, data binding).

No imports of domain modules here (avoids cycles).
"""

from __future__ import annotations

import ast
import logging
import re
import time
from typing import Any, Callable

from plugin.framework.deal_shim import deal
from plugin.framework.i18n import _

log = logging.getLogger("writeragent.scripting")


_UNRESOLVED: Any = object()

# Names the host injects into Run Python Script before exec.
_INJECTED_BINDING_NAMES: frozenset[str] = frozenset({"data", "text", "image", "document_context"})


def _literal_value(node: ast.AST) -> Any:
    """Best-effort static value for template-style literal AST nodes. Returns _UNRESOLVED for expressions that are not literals (a bare name that is not host-injected is not a literal)."""
    if isinstance(node, ast.Name):
        return node.id if node.id in _INJECTED_BINDING_NAMES else _UNRESOLVED
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Dict):
        out: dict[str, Any] = {}
        for key_node, value_node in zip(node.keys, node.values, strict=False):
            if key_node is None:
                continue
            key = _literal_value(key_node)
            if not isinstance(key, str):
                continue
            val = _literal_value(value_node)
            if val is _UNRESOLVED:
                continue
            out[key] = val
        return out
    if isinstance(node, ast.List | ast.Tuple):
        items = [_literal_value(elt) for elt in node.elts]
        if any(item is _UNRESOLVED for item in items):
            return _UNRESOLVED
        return tuple(items) if isinstance(node, ast.Tuple) else items
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        # Recurse so -(-5) and {"n": -1} both resolve. The old check only
        # accepted a top-level USub of a Constant, so nested unary and unary
        # plus became None inside otherwise-literal params.
        value = _literal_value(node.operand)
        if value is _UNRESOLVED or isinstance(value, bool) or not isinstance(value, int | float):
            return _UNRESOLVED
        if isinstance(node.op, ast.UAdd):
            return value
        return -value
    return _UNRESOLVED


def _bound_alias_names(aliases: list[ast.alias]) -> set[str]:
    return {alias.asname or alias.name for alias in aliases} - {"", "*"}


# Dispatch keys for a direct units call. Same names as the template
# positional_args and venv _dispatch_* lookups. ``from`` / ``to`` stay the
# short keys because ``from`` is not a keyword argument and _dispatch_convert
# reads them before from_unit / to_unit. parse_quantity, format_quantity, and
# check_dimensionality are keyword-only at runtime; a positional call
# TypeErrors if executed, and the spec still records the prefix. Keywords
# already stored win via setdefault. Helpers missing from this table keep
# kwargs only: describe_data(data) must not become {"quantity": "data"}.
_DIRECT_POSITIONAL_KEYS: dict[str, tuple[str, ...]] = {
    "convert_quantity": ("value", "from", "to"),
    "parse_quantity": ("quantity",),
    "format_quantity": ("magnitude", "units", "format_spec"),
    "check_dimensionality": ("quantity_a", "quantity_b"),
}


def _spec_from_direct_helper_call(node: ast.Call, helper: str) -> dict[str, Any]:
    """Literal params from ``helper(...)`` (units positional style and kwargs).

    The positional prefix table maps arguments for convert_quantity,
    parse_quantity, format_quantity, and check_dimensionality; keywords win.
    Applying that map to every helper turned describe_data(data) into
    {"quantity": "data"}.
    """
    params: dict[str, Any] = {}
    for kw in node.keywords:
        if kw.arg is None:
            val = _literal_value(kw.value)
            if isinstance(val, dict):
                params.update(val)
    for kw in node.keywords:
        if kw.arg is not None:
            val = _literal_value(kw.value)
            if val is not _UNRESOLVED:
                params[kw.arg] = val
    keys = _DIRECT_POSITIONAL_KEYS.get(helper)
    if keys and node.args:
        for key, arg in zip(keys, node.args):
            val = _literal_value(arg)
            if val is not _UNRESOLVED:
                params.setdefault(key, val)
    return {"helper": helper, "params": params}


_RUN_DATA_BINDING_CALLS: frozenset[str] = frozenset({
    "run_analysis",
    "run_viz",
    "run_quant",
    "run_optimize",
    "run_forecast",
})

# Both SQL templates call query_folder_sql. The sheet template passes
# {"data": data}. The folder template does not, and still shows the toolbar.
_SHEET_SQL_CALL = "query_folder_sql"

_IMPORT_DATA_BINDING: frozenset[str] = frozenset({
    "writeragent.scripting.analysis",
    "writeragent.scripting.viz",
    "writeragent.scripting.quant",
    "writeragent.scripting.optimize",
    "writeragent.scripting.forecast",
    "writeragent.scripting.duckdb_sql",
})


class ScriptFacts:
    """Parsed AST and metadata for user scripts, computed in a single pass."""

    code: str
    _tree: ast.AST | None
    _parse_error: bool
    _call_names: set[str]
    _call_nodes: list[ast.Call]
    _writeragent_imported: set[str]
    _imports: list[tuple[str | None, str]]
    _needs_data_binding: bool

    def __init__(self, code: str, tree: ast.AST | None, parse_error: bool) -> None:
        self.code = code
        self._tree = tree
        self._parse_error = parse_error
        self._call_names = set()
        self._call_nodes = []
        self._writeragent_imported = set()
        self._imports = []
        self._needs_data_binding = False
        if tree is not None:
            self._analyze(tree)
        elif parse_error and code:
            self._analyze_fallback(code)

    @classmethod
    def from_source(cls, code: str) -> ScriptFacts:
        if not code:
            return cls(code="", tree=None, parse_error=False)
        try:
            tree = ast.parse(code)
            return cls(code=code, tree=tree, parse_error=False)
        except (SyntaxError, TypeError, ValueError):
            return cls(code=code, tree=None, parse_error=True)

    def _analyze(self, tree: ast.AST) -> None:
        imported_data_binding_names: set[str] = set()
        data_binding_alias_modules: set[str] = set()
        data_binding_candidate_calls: list[ast.Call] = []

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self._imports.append((None, alias.name))
                    if alias.name in _IMPORT_DATA_BINDING:
                        data_binding_alias_modules.add(alias.asname or alias.name)
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                for alias in node.names:
                    self._imports.append((node.module, alias.name))
                if mod == "writeragent" or mod.startswith("writeragent."):
                    self._writeragent_imported.update(_bound_alias_names(node.names))
                if mod in _IMPORT_DATA_BINDING:
                    imported_data_binding_names.update(_bound_alias_names(node.names))
                elif mod == "writeragent.scripting":
                    for alias in node.names:
                        if f"writeragent.scripting.{alias.name}" in _IMPORT_DATA_BINDING:
                            data_binding_alias_modules.add(alias.asname or alias.name)
            elif isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name):
                    self._call_names.add(func.id)
                    self._call_nodes.append(node)
                    name: str | None = func.id
                elif isinstance(func, ast.Attribute):
                    name = func.attr
                else:
                    name = None
                if name in _RUN_DATA_BINDING_CALLS or name == _SHEET_SQL_CALL:
                    self._needs_data_binding = True
                else:
                    data_binding_candidate_calls.append(node)

        if not self._needs_data_binding:
            for node in data_binding_candidate_calls:
                func = node.func
                if isinstance(func, ast.Name) and func.id in imported_data_binding_names:
                    self._needs_data_binding = True
                    break
                if isinstance(func, ast.Attribute):
                    target = func.value
                    if isinstance(target, ast.Name) and target.id in data_binding_alias_modules:
                        self._needs_data_binding = True
                        break

    def _analyze_fallback(self, code: str) -> None:
        for name in (*_RUN_DATA_BINDING_CALLS, _SHEET_SQL_CALL):
            if re.search(rf"\b{re.escape(name)}\s*\(", code):
                self._needs_data_binding = True
                break
        if not self._needs_data_binding:
            self._needs_data_binding = any(module in code for module in _IMPORT_DATA_BINDING)

    def imports_module(self, module_name: str) -> bool:
        """True when the script imports *module_name* (import or from-import)."""
        if not self.code or not module_name:
            return False
        if self._parse_error:
            return module_name in self.code
        for mod, name in self._imports:
            if mod is None:
                if name == module_name or name.startswith(module_name + "."):
                    return True
            else:
                if mod == module_name:
                    return True
                if module_name.startswith(mod + "."):
                    sub = module_name[len(mod) + 1 :]
                    if name == sub:
                        return True
        return False

    def calls(self, name: str) -> bool:
        """True when the script contains a call to *name*."""
        if not self.code or not name:
            return False
        if self._parse_error:
            return f"{name}(" in self.code
        return name in self._call_names

    @deal.post(lambda result: result is None or isinstance(result, dict))
    def first_run_spec(self, run_name: str) -> dict[str, Any] | None:
        """Return the first positional spec dict from ``run_name({...}, ...)`` or a writeragent helper call."""
        if not self.code or self._parse_error:
            return None
        direct_spec: dict[str, Any] | None = None
        for node in self._call_nodes:
            func = node.func
            assert isinstance(func, ast.Name)
            if func.id == run_name:
                if not node.args:
                    continue
                spec = _literal_value(node.args[0])
                if isinstance(spec, dict):
                    return spec
                if isinstance(spec, str):
                    return {"helper": spec, "params": {}}
            elif (
                direct_spec is None
                and func.id in self._writeragent_imported
                and not func.id.startswith("run_")
            ):
                direct_spec = _spec_from_direct_helper_call(node, func.id)
        return direct_spec

    def needs_data_binding(self, *, doc: Any = None) -> bool:
        """True when code uses a trusted helper that may bind Calc sheet data."""
        if doc is not None:
            from plugin.framework.doc_type import is_calc

            if not is_calc(doc):
                return False
        return self._needs_data_binding


# --- Templates ---


def build_helper_script_template(
    *,
    helper: str,
    params: dict[str, Any],
    description: str,
    import_module: str,
    run_name: str | None = None,
    data_expr: str = "data",
    context_expr: str = "{}",
    extra_comment_lines: tuple[str, ...] = ("# Edit the call below, then Run.",),
    positional_args: tuple[str, ...] = (),
    leading_data: bool = False,
    invoke: str = "direct",
) -> str:
    """Build a Run Python Script template body."""
    if invoke == "runner":
        spec_obj: dict[str, Any] = {"helper": helper}
        if params:
            spec_obj["params"] = params
        imported = run_name
        call = f"{run_name}({repr(spec_obj)}, {data_expr}, {context_expr})"
    else:
        def _format_param_val(key: str, val: Any) -> str:
            # Bare data identifier only for the injected data argument (data, value, quantity).
            # Real string values like column="data" or dict keys must remain quoted.
            if key in ("data", "value", "quantity") and data_expr != "None" and val == "data":
                return data_expr
            return repr(val)

        parts: list[str] = []
        if leading_data:
            parts.append(data_expr)
        for key in positional_args:
            if key in params:
                parts.append(_format_param_val(key, params[key]))
        kw_parts = [
            f"{key}={_format_param_val(key, val)}"
            for key, val in params.items()
            if key not in positional_args
        ]
        imported = helper
        call = f"{helper}({', '.join([*parts, *kw_parts])})"

    body_lines = [
        f"# {description}",
        *extra_comment_lines,
        f"from {import_module} import {imported}\n",
        f"result = {call}",
        "",
    ]
    return "\n".join(body_lines)


# --- RPS timing / error outcomes ---


def format_elapsed_time(seconds: float) -> str:
    """Human-readable duration for RPS status lines.

    Round to two decimal places before the 60-second check. Checking the raw
    value formatted 59.999 as "60.00s" instead of "1m 0s".
    """
    rounded = round(seconds, 2)
    if rounded >= 60.0:
        minutes = int(rounded // 60)
        secs = int(rounded % 60)
        return f"{minutes}m {secs}s"
    if seconds >= 1.0:
        return f"{seconds:.2f}s"
    ms = seconds * 1000.0
    if ms < 1.0:
        return "<1 ms"
    return f"{int(ms)} ms"


def _elapsed_since(t0: float) -> float:
    """Seconds since *t0*, or 0 while CrossHair is loaded."""
    return time.perf_counter() - t0


def _exception_detail(error: BaseException) -> str:
    """Text for an insert-failure dialog.

    UNO often leaves ``str(exc)`` empty (or only a method name). An empty
    string rendered as "Failed to insert result:  (took ...)".
    """
    return str(error).strip() or repr(error).strip() or type(error).__name__


def rps_error_outcome(
    message: str,
    *,
    t0: float,
    traceback: str | None = None,
) -> dict[str, Any]:
    """Standard ``{ok: False, message}`` with elapsed time when not a timeout."""
    elapsed = _elapsed_since(t0)
    err_msg = str(message)
    if "timed out" not in err_msg.lower() and "timeout" not in err_msg.lower():
        err_msg = f"{err_msg} (took {format_elapsed_time(elapsed)})"
    out: dict[str, Any] = {"ok": False, "message": err_msg}
    if traceback is not None:
        out["traceback"] = traceback
    return out


def rps_insert_failed_outcome(error: BaseException, *, t0: float) -> dict[str, Any]:
    """Outcome when domain insert/egress fails after a successful helper run."""
    # UNO often sets str(exc) to the method name only (e.g. insertDocumentFromURL);
    # log type/str/repr + traceback so Arch debug.log matches the RPS dialog.
    # Prefer exc_info=error so we still get a stack if called outside an except.
    log.error(
        "rps_insert_failed_outcome: type=%s str=%r repr=%r",
        type(error).__name__,
        str(error),
        repr(error),
        exc_info=error,
    )

    return {
        "ok": False,
        "message": _("Failed to insert result: {error} (took {time})").format(
            error=_exception_detail(error),
            time=format_elapsed_time(_elapsed_since(t0)),
        ),
    }


def rps_ok_outcome(
    status_ok_text: str,
    *,
    result: Any,
    stdout: str | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "ok": True,
        "status_ok_text": status_ok_text,
        "result": result,
    }
    if stdout is not None:
        out["stdout"] = stdout
    return out


def preview_insert_ok_outcome(
    *,
    domain_label: str,
    helper: str,
    preview_text: str,
    t0: float,
    stdout: str | None,
    result: Any,
) -> dict[str, Any]:
    formatted_time = format_elapsed_time(_elapsed_since(t0))
    preview = preview_text[:80] + ("…" if len(preview_text) > 80 else "")
    status_ok = _("{domain} '{helper}' completed. Inserted: {preview} (took {time})").format(
        domain=domain_label,
        helper=helper,
        preview=preview,
        time=formatted_time,
    )
    return rps_ok_outcome(status_ok, result=result, stdout=stdout)


def plot_insert_ok_outcome(
    *,
    helper: str,
    title: str,
    t0: float,
    stdout: str | None,
    result: Any,
) -> dict[str, Any]:
    formatted_time = format_elapsed_time(_elapsed_since(t0))
    msg = _("Plot inserted ({title}). (took {time})").format(title=title, time=formatted_time)
    if helper:
        status_ok = _("Viz '{helper}' completed. {msg}").format(helper=helper, msg=msg)
    else:
        status_ok = msg
    return rps_ok_outcome(status_ok, result=result, stdout=stdout)


def run_domain(
    wiring_id: str,
    uno_ctx: Any,
    doc: Any,
    *,
    helper: str,
    **kwargs: Any,
) -> dict[str, Any]:
    """Execute trusted helper within *wiring_id* on the host."""
    from plugin.scripting.domain_registry import get_domain_wiring
    from plugin.scripting.helper_registry import names

    row = get_domain_wiring(wiring_id)
    if row.runner == "symbolic":
        from plugin.framework.errors import ToolExecutionError

        name = str(helper or "").strip()
        if not name:
            raise ToolExecutionError("helper is required", code="SYMBOLIC_ERROR")
        h_names = names("symbolic")
        if name not in h_names:
            raise ToolExecutionError(f"Unknown helper {name!r}", code="SYMBOLIC_ERROR")

        doc_type = kwargs.get("doc_type")
        if doc_type is None and doc is not None:
            from plugin.framework.doc_type import is_calc, is_writer

            if is_calc(doc):
                doc_type = "calc"
            elif is_writer(doc):
                doc_type = "writer"
        if doc_type not in ("calc", "writer"):
            raise ToolExecutionError("Symbolic helpers require a Writer or Calc document.", code="SYMBOLIC_ERROR")

        spec: dict[str, Any] = {"helper": name}
        params = kwargs.get("params")
        if isinstance(params, dict) and params:
            spec["params"] = params

        task_hint = kwargs.get("task_hint")
        from plugin.scripting.client import run_symbolic as client_run_symbolic

        return client_run_symbolic(
            uno_ctx,
            spec,
            None,
            context={"task_hint": str(task_hint)} if task_hint else None,
        )

    if row.runner == "sheet":
        import plugin.scripting.client as client

        rpc = row.rpc_domain or row.helper_domain or wiring_id
        client_run = getattr(client, f"run_{rpc}")
        h_names = names(row.helper_domain or wiring_id)
        return run_trusted_calc_data_helper(
            uno_ctx,
            doc,
            helper=helper,
            helper_names=h_names,
            error_code=row.error_code or "",
            empty_data_message=row.empty_data_message or "",
            client_run=client_run,
            require_calc_or_writer=row.require_calc_or_writer,
            force_queue=row.force_queue,
            **kwargs,
        )

    raise ValueError(f"No host runner configured for wiring id {wiring_id!r}")


def is_status_helper_result(
    value: Any,
    names: frozenset[str] | set[str],
    exact_error_codes: frozenset[str],
) -> bool:
    """True when *value* is a helper status dict for *names* or an exact error code.

    Helper names must match the domain set exactly; substring or fetch_* prefix matches are not accepted.
    """
    if not isinstance(value, dict) or "status" not in value:
        return False
    helper = value.get("helper")
    if isinstance(helper, str) and helper in names:
        return True
    if value.get("status") != "error":
        return False
    return str(value.get("code") or "") in exact_error_codes


def run_trusted_calc_data_helper(
    uno_ctx: Any,
    doc: Any,
    *,
    helper: str,
    helper_names: frozenset[str] | set[str],
    error_code: str,
    empty_data_message: str,
    client_run: Callable[..., dict[str, Any]],
    params: dict[str, Any] | None = None,
    data_range: str | None = None,
    data: Any = None,
    headers: bool = True,
    task_hint: str | None = None,
    require_calc_or_writer: bool = False,
    force_queue: bool = False,
) -> dict[str, Any]:
    """Fetch Calc data and run one trusted sheet helper. Forecast and optimize share this."""
    from plugin.calc.calc_addin_data import _resolve_python_data
    from plugin.framework.errors import ToolExecutionError

    name = str(helper or "").strip()
    if not name:
        raise ToolExecutionError("helper is required", code=error_code)
    if name not in helper_names:
        raise ToolExecutionError(f"Unknown helper {name!r}", code=error_code)

    dr = str(data_range).strip() if data_range else None
    if not dr and data is None:
        raise ToolExecutionError("Provide data_range or data", code=error_code)

    def _read_sheet() -> tuple[Any, str | None, dict[str, Any]]:
        # UNO only. Callers are async workers; the venv IPC must not sit in this hop.
        from plugin.framework.doc_type import is_calc, is_writer
        from plugin.framework.errors import reraise_if_disposed

        # Doc-type check lives inside _read_sheet after the main-thread hop because get_document_type must not run on worker threads.
        if require_calc_or_writer and not is_calc(doc) and not is_writer(doc):
            raise ToolExecutionError("Viz helpers require a Writer or Calc document.", code=error_code)

        py_data, err = _resolve_python_data(doc, data_range=dr, data=data)
        context: dict[str, Any] = {}
        try:
            from plugin.calc.bridge import CalcBridge

            context["sheet_name"] = CalcBridge(doc).get_active_sheet().getName()
        except Exception as e:
            # reraise_if_disposed propagates closed documents while a missing sheet name remains a benign probe failure.
            reraise_if_disposed(e)
            log.debug("Failed to resolve active sheet name: %s", e)
        if task_hint:
            context["task_hint"] = str(task_hint)
        if dr:
            context["range_a1"] = dr
        return py_data, err, context

    # Only the sheet read hops to the main thread; venv client_run stays on the worker so Calc does not freeze during IPC.
    from plugin.framework.queue_executor import main

    # force_queue marshals sheet reads through main.call on PyUNO Dummy-N threads that falsely report as main.
    if not force_queue and main.is_current():
        py_data, err, context = _read_sheet()
    else:
        py_data, err, context = main.call(_read_sheet)
    if err:
        raise ToolExecutionError(err, code=error_code)
    if py_data is None:
        raise ToolExecutionError(empty_data_message, code=error_code)

    spec: dict[str, Any] = {"helper": name, "headers": bool(headers)}
    if isinstance(params, dict) and params:
        spec["params"] = params

    return client_run(uno_ctx, spec, py_data, context=context or None)


def supports_calc_or_writer_manual(doc: Any) -> bool:
    """True when Run Python Script should expose helpers for Calc or Writer *doc*."""
    if doc is None:
        return False
    from plugin.framework.doc_type import is_calc, is_writer

    return is_writer(doc) or is_calc(doc)


def insert_helper_result_into_calc(
    doc: Any,
    uno_ctx: Any,
    result: dict[str, Any],
    *,
    domain_label: str,
    default_helper: str,
    failed_message: str,
    sheet_name: str | None = None,
    start_col: int | None = None,
    start_row: int | None = None,
) -> int:
    """Write formatted helper output at *sheet_name*/*start_col*/*start_row* (or the selection).

    *sheet_name* is the prefix from ``parse_output_anchor``. The shared
    tabular writer qualifies the anchor so ``CalcBridge.resolve`` opens
    that sheet. A missing name stays on the active sheet (Run Python Script
    selection, or an unqualified ``output_range``).
    """
    from plugin.calc.tabular_egress import (
        format_tabular_helper_for_calc,
        insert_tabular_result_into_calc,
    )

    grid = format_tabular_helper_for_calc(
        result,
        domain_label=domain_label,
        default_helper=default_helper,
        failed_message=failed_message,
    )
    return insert_tabular_result_into_calc(
        doc,
        uno_ctx,
        grid,
        sheet_name=sheet_name,
        start_col=start_col,
        start_row=start_row,
    )




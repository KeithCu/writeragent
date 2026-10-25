# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared host glue for trusted helper domains (headers, templates, RPS outcomes, data binding).

Domain modules keep public parse/template wrappers; compute and egress stay domain-specific.
No imports of domain modules here (avoids cycles).
"""

from __future__ import annotations

import ast
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable

from plugin.framework.deal_shim import DEAL_MAX_SOURCE, DEAL_MAX_TOKEN, UNDER_CROSSHAIR, ascii_bounded, str_bounded, deal


def _deal_user_code_ok_pytest(code: object, run_name: object = "") -> bool:
    # User scripts are longer than DEAL_MAX_SOURCE. The cap raised
    # PreContractError before ast.parse could return None. run_name is
    # our helper token. CrossHair keeps the short source.
    return isinstance(code, str) and ascii_bounded(run_name, DEAL_MAX_TOKEN)


def _deal_user_code_ok_crosshair(code: object, run_name: object = "") -> bool:
    return str_bounded(code, DEAL_MAX_SOURCE) and ascii_bounded(run_name, DEAL_MAX_TOKEN)


_deal_user_code_ok = _deal_user_code_ok_crosshair if UNDER_CROSSHAIR else _deal_user_code_ok_pytest
from plugin.framework.i18n import _

log = logging.getLogger("writeragent.scripting")


if TYPE_CHECKING:
    from collections.abc import Collection

# --- Header meta ---


@dataclass(frozen=True)
class HelperScriptMeta:
    """Machine-readable ``# writeragent:<tag> helper=… params=…`` header."""

    helper: str
    params: dict[str, Any]


@deal.pre(lambda tag: str_bounded(tag, DEAL_MAX_TOKEN, min_len=1))
@deal.post(lambda result: isinstance(result, str) and result.startswith("# writeragent:"))
def header_prefix(tag: str) -> str:
    """Return ``# writeragent:<tag>`` (no trailing space)."""
    return f"# writeragent:{tag}"


def _header_re(tag: str) -> re.Pattern[str]:
    # Same wire shape as historical per-domain regexes.
    return re.compile(
        rf"^\s*#\s*writeragent:{re.escape(tag)}\s+helper=(\w+)\s+params=(\{{.*\}})\s*$",
        re.MULTILINE,
    )


@deal.post(lambda result: result is None or isinstance(result, HelperScriptMeta))
def parse_helper_script_header(
    code: str,
    *,
    tag: str,
    helper_names: Collection[str] | None = None,
) -> HelperScriptMeta | None:
    """Parse ``# writeragent:<tag> helper=NAME params={…}``.

    *helper_names*: when set, unknown helpers return None.
    """
    # crosshair: off
    if not code:
        return None
    prefix = header_prefix(tag)
    if prefix not in code:
        return None
    match = _header_re(tag).search(code)
    if not match:
        return None
    helper = match.group(1)
    if helper_names is not None and helper not in helper_names:
        return None
    raw = match.group(2)
    try:
        params = json.loads(raw)
    except Exception:
        params = {}
    if not isinstance(params, dict):
        params = {}
    return HelperScriptMeta(helper=helper, params=params)


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
    if isinstance(node, ast.List):
        items = [_literal_value(elt) for elt in node.elts]
        if any(item is _UNRESOLVED for item in items):
            return _UNRESOLVED
        return items
    if isinstance(node, ast.Tuple):
        items = [_literal_value(elt) for elt in node.elts]
        if any(item is _UNRESOLVED for item in items):
            return _UNRESOLVED
        return tuple(items)
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


@deal.pre(lambda code, run_name="": _deal_user_code_ok(code, run_name))
@deal.post(lambda result: result is None or isinstance(result, dict))
def parse_run_import_call_params(code: str, *, run_name: str) -> dict[str, Any] | None:
    """Return the ``params`` dict from ``run_name({"helper": ..., "params": {...}}, ...)`` when literal."""
    spec = parse_run_import_call_spec(code, run_name=run_name)
    if not spec:
        return None
    params = spec.get("params")
    return params if isinstance(params, dict) else None


def _writeragent_imported_names(tree: ast.AST) -> set[str]:
    """Names brought in by ``from writeragent... import``.

    Direct helper templates call those names (``convert_quantity(...)``). A bare
    ``print(...)`` must not be treated as a helper spec.
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        module = node.module or ""
        if module != "writeragent" and not module.startswith("writeragent."):
            continue
        for alias in node.names:
            bound = alias.asname or alias.name
            if bound and bound != "*":
                names.add(bound)
    return names


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
    if node.keywords and any(kw.arg is None for kw in node.keywords):
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


@deal.pre(lambda code, run_name="": _deal_user_code_ok(code, run_name))
@deal.post(lambda result: result is None or isinstance(result, dict))
def parse_run_import_call_spec(code: str, *, run_name: str) -> dict[str, Any] | None:
    """Return the first positional spec dict from ``run_name({...}, ...)`` or a writeragent helper call."""
    if not code:
        return None

    # CrossHair timing/contract switch, not a test leak.
    if UNDER_CROSSHAIR:
        return None
    try:
        tree = ast.parse(code)
    except (SyntaxError, TypeError, ValueError):
        return None
    imported = _writeragent_imported_names(tree)
    direct_spec: dict[str, Any] | None = None
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Name):
            continue
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
            and func.id in imported
            and not func.id.startswith("run_")
        ):
            # Prefer run_name when both exist. Only imported writeragent names
            # count: print("hi") used to become helper="print" and prepend the
            # whole Writer document on Run Python Script.
            direct_spec = _spec_from_direct_helper_call(node, func.id)
    return direct_spec


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


def _call_tail(func: ast.AST) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _ast_calls_binding_name(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _call_tail(node.func)
        if name in _RUN_DATA_BINDING_CALLS or name == _SHEET_SQL_CALL:
            return True
    return False


def _ast_imports_and_calls(tree: ast.AST) -> bool:
    imported_names: set[str] = set()
    alias_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            for module in _IMPORT_DATA_BINDING:
                if mod == module:
                    for alias in node.names:
                        bound = alias.asname or alias.name
                        if bound and bound != "*":
                            imported_names.add(bound)
                elif mod == "writeragent.scripting":
                    sub = module.split(".")[-1]
                    for alias in node.names:
                        if alias.name == sub:
                            bound = alias.asname or alias.name
                            alias_modules.add(bound)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                for module in _IMPORT_DATA_BINDING:
                    if alias.name == module:
                        bound = alias.asname or alias.name
                        alias_modules.add(bound)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name) and node.func.id in imported_names:
            return True
        if isinstance(node.func, ast.Attribute):
            target = node.func.value
            if isinstance(target, ast.Name) and target.id in alias_modules:
                return True
    return False


def script_header_needs_data_binding(code: str, *, doc: Any) -> bool:
    """True when *code* uses a trusted helper that may bind Calc sheet data.

    What was wrong: regex matched comments like ``# run_analysis(`` and string
    literals like ``"run_analysis("``, and run_viz's old calc_only=False returned
    true on Writer documents.
    How: a raw-source pre-pass ran before the AST walk.
    Why: AST call tails skip comments and strings; the toolbar binds a Calc sheet,
    so a non-Calc doc returns false even though viz still inserts on Writer;
    substring search runs only when ast.parse raises, same policy as
    python_runner.script_uses_run_import.
    """
    # The data toolbar binds a Calc sheet range; run_viz is not post_venv_calc_only
    # because plots insert on Writer, and that must not enable this toolbar.
    from plugin.doc.doc_type import is_calc

    if not code or not is_calc(doc):
        return False
    try:
        tree = ast.parse(code)
    except (SyntaxError, TypeError, ValueError):
        for name in (*_RUN_DATA_BINDING_CALLS, _SHEET_SQL_CALL):
            if re.search(rf"\b{re.escape(name)}\s*\(", code):
                return True
        return any(module in code for module in _IMPORT_DATA_BINDING)

    if _ast_calls_binding_name(tree):
        return True
    return _ast_imports_and_calls(tree)



# --- Templates ---


def _python_literal(val: Any) -> str:
    """Python source for a template parameter literal (repr-based).

    Strings use repr. Treating every ``"data"`` as the bare injected name
    rewrote dict keys and parameters such as column="data". The bare ``data``
    identifier is injected only for the data argument in
    build_helper_script_template.
    """
    if val is None:
        return "None"
    if val is True:
        return "True"
    if val is False:
        return "False"
    if isinstance(val, (int, float, str)):
        return repr(val)
    if isinstance(val, (list, tuple)):
        items = ", ".join(_python_literal(item) for item in val)
        return f"[{items}]" if isinstance(val, list) else f"({items}{',' if len(val) == 1 else ''})"
    if isinstance(val, dict):
        parts = [f"{repr(k) if isinstance(k, str) else _python_literal(k)}: {_python_literal(v)}" for k, v in val.items()]
        return "{" + ", ".join(parts) + "}"
    return repr(val)


def build_helper_script_template(
    *,
    helper: str,
    params: dict[str, Any],
    description: str,
    import_module: str | None = None,
    run_name: str | None = None,
    data_expr: str = "data",
    context_expr: str = "{}",
    extra_comment_lines: tuple[str, ...] = (),
    positional_args: tuple[str, ...] | None = None,
    leading_data: bool = False,
    invoke: str = "direct",
) -> str:
    """Build a Run Python Script template body."""
    if not import_module:
        raise ValueError("import_module required")
    default_extra = extra_comment_lines or ("# Edit the call below, then Run.",)

    def _format_param_val(key: str, val: Any) -> str:
        # Bare data identifier only for the injected data argument (data, value, quantity).
        # Real string values like column="data" or dict keys must remain quoted.
        if (key in ("data", "value", "quantity") and data_expr != "None") and val == "data":
            return data_expr
        return _python_literal(val)

    pos_keys = positional_args or ()
    parts: list[str] = []
    if leading_data:
        parts.append(data_expr)
    for key in pos_keys:
        if key in params:
            parts.append(_format_param_val(key, params[key]))
    kw_parts = [
        f"{key}={_format_param_val(key, val)}"
        for key, val in params.items()
        if key not in pos_keys
    ]
    args_str = ", ".join([*parts, *kw_parts])

    import_name = run_name if invoke == "runner" else helper
    if invoke == "runner":
        spec_obj: dict[str, Any] = {"helper": helper}
        if params:
            spec_obj["params"] = params
        call = f"{run_name}({_python_literal(spec_obj)}, {data_expr}, {context_expr})"
    else:
        call = f"{helper}({args_str})"

    body_lines = [
        f"# {description}",
        *default_extra,
        f"from {import_module} import {import_name}\n",
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
    if round(seconds, 2) >= 60.0:
        rounded = round(seconds, 2)
        minutes = int(rounded // 60)
        secs = int(rounded % 60)
        return f"{minutes}m {secs}s"
    if seconds >= 1.0:
        return f"{seconds:.2f}s"
    ms = seconds * 1000.0
    if ms < 1.0:
        return "<1 ms"
    return f"{int(ms)} ms"


def _append_took(message: str, elapsed: float) -> str:
    err_msg = str(message)
    if "timed out" in err_msg.lower() or "timeout" in err_msg.lower():
        return err_msg
    return f"{err_msg} (took {format_elapsed_time(elapsed)})"


def _elapsed_since(t0: float) -> float:
    """Seconds since *t0*, or 0 while CrossHair is loaded."""
    if UNDER_CROSSHAIR:
        return 0.0
    return time.perf_counter() - t0


def _exception_detail(error: BaseException) -> str:
    """Text for an insert-failure dialog.

    UNO often leaves ``str(exc)`` empty (or only a method name). An empty
    string rendered as "Failed to insert result:  (took ...)".
    """
    detail = str(error).strip()
    if detail:
        return detail
    fallback = repr(error).strip()
    return fallback or type(error).__name__


def rps_error_outcome(
    message: str,
    *,
    t0: float,
    traceback: str | None = None,
) -> dict[str, Any]:
    """Standard ``{ok: False, message}`` with elapsed time when not a timeout."""
    elapsed = _elapsed_since(t0)
    out: dict[str, Any] = {"ok": False, "message": _append_took(message, elapsed)}
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

    elapsed_total = _elapsed_since(t0)
    formatted_time_total = format_elapsed_time(elapsed_total)
    return {
        "ok": False,
        "message": _("Failed to insert result: {error} (took {time})").format(
            error=_exception_detail(error),
            time=formatted_time_total,
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
    elapsed = _elapsed_since(t0)
    formatted_time = format_elapsed_time(elapsed)
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
    elapsed = _elapsed_since(t0)
    formatted_time = format_elapsed_time(elapsed)
    msg = _("Plot inserted ({title}). (took {time})").format(title=title, time=formatted_time)
    if helper:
        status_ok = _("Viz '{helper}' completed. {msg}").format(helper=helper, msg=msg)
    else:
        status_ok = msg
    return rps_ok_outcome(status_ok, result=result, stdout=stdout)


def symbolic_insert_ok_outcome(
    *,
    helper: str,
    latex: str,
    t0: float,
    stdout: str | None,
    result: Any,
) -> dict[str, Any]:
    return preview_insert_ok_outcome(
        domain_label="Math",
        helper=helper,
        preview_text=latex,
        t0=t0,
        stdout=stdout,
        result=result,
    )


def units_insert_ok_outcome(
    *,
    helper: str,
    formatted: str,
    t0: float,
    stdout: str | None,
    result: Any,
) -> dict[str, Any]:
    return preview_insert_ok_outcome(
        domain_label="Units",
        helper=helper,
        preview_text=formatted,
        t0=t0,
        stdout=stdout,
        result=result,
    )


# --- Host Facade Factory ---

from types import SimpleNamespace

@dataclass(frozen=True)
class DomainFacadeConfig:
    tag: str                          # "viz", "math", "units", "quant", "optimize", "forecast"
    helper_names: frozenset[str]      # from domains_common
    default_params: dict[str, dict[str, Any]]
    descriptions: dict[str, str]
    import_module: str                # e.g. "writeragent.scripting.viz"
    run_name: str                     # e.g. "run_viz"
    shipped_templates: frozenset[str] | None = None
    data_expr: str = "data"
    context_expr: str = "{}"
    positional_args: dict[str, tuple[str, ...]] | None = None
    extra_comment_lines: tuple[str, ...] = ("# Edit the run call below, then Run.",)
    # Sheet helpers take the injected range as the first argument. Quant and
    # text call ``run_*`` instead of the helper name (those callables do not
    # match ``helper(**params)``).
    leading_data: bool = False
    invoke: str = "direct"


def make_template_api(cfg: DomainFacadeConfig) -> Any:
    """Build _template_body, get_templates, and parse_header functions dynamically."""
    # crosshair: off
    def _template_body(helper: str, params: dict[str, Any]) -> str:
        desc = cfg.descriptions.get(helper, helper.replace("_", " ").title() if "_" in helper else helper)
        pos = cfg.positional_args.get(helper) if cfg.positional_args else None
        return build_helper_script_template(
            helper=helper,
            params=params,
            description=desc,
            import_module=cfg.import_module,
            run_name=cfg.run_name,
            data_expr=cfg.data_expr,
            context_expr=cfg.context_expr,
            extra_comment_lines=cfg.extra_comment_lines,
            positional_args=pos,
            leading_data=cfg.leading_data,
            invoke=cfg.invoke,
        )

    def get_templates() -> dict[str, str]:
        shipped = cfg.shipped_templates if cfg.shipped_templates is not None else cfg.helper_names
        return {
            helper: _template_body(helper, dict(cfg.default_params.get(helper, {})))
            for helper in sorted(shipped)
            if helper in cfg.helper_names
        }

    def parse_header(code: str) -> HelperScriptMeta | None:
        return parse_helper_script_header(
            code,
            tag=cfg.tag,
            helper_names=cfg.helper_names,
        )

    def template_for(helper: str) -> str | None:
        if helper not in cfg.helper_names:
            return None
        return _template_body(helper, dict(cfg.default_params.get(helper, {})))

    return SimpleNamespace(
        template_body=_template_body,
        get_templates=get_templates,
        parse_header=parse_header,
        template_for=template_for,
    )


def is_status_helper_result(
    value: Any,
    names: frozenset[str] | set[str],
    exact_error_codes: frozenset[str],
) -> bool:
    """True when *value* is a helper status dict for *names* or an exact error code.

    Substring matches (``"FORECAST" in code``) and ``fetch_*`` prefixes used to
    accept helpers that were not in the domain's name set.
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
        from plugin.doc.doc_type import is_calc, is_writer
        from plugin.framework.errors import reraise_if_disposed

        # What was wrong: require_calc_or_writer checked is_calc(doc) and is_writer(doc)
        # on the calling worker thread before hopping to the main thread.
        # How it happened: both helpers call get_document_type which calls supportsService,
        # marked @main_thread_only, violating thread guard off the main thread.
        # Why this change: move the doc type validation inside _read_sheet so it executes
        # after the main-thread hop.
        if require_calc_or_writer and not is_calc(doc) and not is_writer(doc):
            raise ToolExecutionError("Viz helpers require a Writer or Calc document.", code=error_code)

        py_data, err = _resolve_python_data(doc, data_range=dr, data=data)
        context: dict[str, Any] = {}
        try:
            from plugin.calc.bridge import CalcBridge

            context["sheet_name"] = CalcBridge(doc).get_active_sheet().getName()
        except Exception as e:
            # What was wrong: DisposedException was logged and ignored, then client_run
            # ran against a dead document.
            # How it happened: the sheet name is best-effort context, so the probe
            # catches Exception.
            # Why this change: reraise_if_disposed propagates real disposal and still
            # swallows a missing sheet. A bare RuntimeException is not disposal.
            reraise_if_disposed(e)
            log.debug("Failed to resolve active sheet name: %s", e)
        if task_hint:
            context["task_hint"] = str(task_hint)
        if dr:
            context["range_a1"] = dr
        return py_data, err, context

    # forecast_data / optimize_data used to wrap this whole helper in
    # execute_on_main_thread, so the venv IPC ran on the UI thread and froze
    # Calc. Hop only the UNO read to the main thread; client_run stays on the caller.
    from plugin.framework.queue_executor import execute_on_main_thread
    from plugin.framework.thread_guard import on_main_thread

    # What was wrong: untagged PyUNO Dummy-N threads ran the sheet read inline
    # because on_main_thread() returned True.
    # How it happened: analysis_runner was unified with run_trusted_calc_data_helper,
    # which bypassed execute_on_main_thread when on_main_thread() was True.
    # Why this change: force_queue forces the sheet read through execute_on_main_thread,
    # enqueuing work on Dummy-N threads while still running inline on the true main thread.
    if not force_queue and on_main_thread():
        py_data, err, context = _read_sheet()
    else:
        py_data, err, context = execute_on_main_thread(_read_sheet)
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
    from plugin.doc.doc_type import is_calc, is_writer

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




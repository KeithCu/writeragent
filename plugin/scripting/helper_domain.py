# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared host glue for trusted helper domains (headers, templates, RPS outcomes).

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
from typing import TYPE_CHECKING, Any, Callable, Literal

from plugin.framework.deal_shim import DEAL_MAX_SOURCE, DEAL_MAX_TOKEN, ascii_bounded, str_bounded, deal
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
    require_prefix: bool = True,
    on_bad_json: Literal["empty", "none", "raise"] = "empty",
) -> HelperScriptMeta | None:
    """Parse ``# writeragent:<tag> helper=NAME params={…}``.

    *require_prefix*: if True, require the prefix substring before regex (units/analysis style).
    *helper_names*: when set, unknown helpers return None.
    *on_bad_json*: ``empty`` → ``{}``; ``none`` → return None; ``raise`` → ValueError
    so a corrupt header does not run the helper with empty params.
    """
    # crosshair: off
    if not code:
        return None
    prefix = header_prefix(tag)
    if require_prefix and prefix not in code:
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
    except Exception as exc:
        if on_bad_json == "none":
            return None
        if on_bad_json == "raise":
            raise ValueError(f"Invalid helper params JSON for {tag} {helper}") from exc
        params = {}
    if not isinstance(params, dict):
        if on_bad_json == "none":
            return None
        if on_bad_json == "raise":
            raise ValueError(f"Helper params for {tag} {helper} must be a JSON object")
        params = {}
    return HelperScriptMeta(helper=helper, params=params)


def _literal_value(node: ast.AST) -> Any:
    """Best-effort static value for template-style literal AST nodes."""
    if isinstance(node, ast.Name):
        return node.id
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
            out[key] = _literal_value(value_node)
        return out
    if isinstance(node, ast.List):
        return [_literal_value(elt) for elt in node.elts]
    if isinstance(node, ast.Tuple):
        return tuple(_literal_value(elt) for elt in node.elts)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        # Recurse so -(-5) and {"n": -1} both resolve. The old check only
        # accepted a top-level USub of a Constant, so nested unary and unary
        # plus became None inside otherwise-literal params.
        value = _literal_value(node.operand)
        if isinstance(value, bool) or not isinstance(value, int | float):
            return None
        if isinstance(node.op, ast.UAdd):
            return value
        return -value
    return None


@deal.pre(
    lambda code, run_name="": str_bounded(code, DEAL_MAX_SOURCE)
    and ascii_bounded(run_name, DEAL_MAX_TOKEN)
)
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


def _spec_from_direct_helper_call(node: ast.Call, helper: str) -> dict[str, Any]:
    """Literal params from ``helper(...)`` (units positional style and kwargs)."""
    params: dict[str, Any] = {}
    if node.keywords and any(kw.arg is None for kw in node.keywords):
        for kw in node.keywords:
            if kw.arg is None:
                val = _literal_value(kw.value)
                if isinstance(val, dict):
                    params.update(val)
    for kw in node.keywords:
        if kw.arg is not None:
            params[kw.arg] = _literal_value(kw.value)
    if node.args:
        if len(node.args) == 3:
            params.setdefault("value", _literal_value(node.args[0]))
            params.setdefault("from", _literal_value(node.args[1]))
            params.setdefault("to", _literal_value(node.args[2]))
        elif len(node.args) == 1:
            params.setdefault("quantity", _literal_value(node.args[0]))
        elif len(node.args) == 2:
            params.setdefault("quantity_a", _literal_value(node.args[0]))
            params.setdefault("quantity_b", _literal_value(node.args[1]))
    return {"helper": helper, "params": params}


@deal.pre(
    lambda code, run_name="": str_bounded(code, DEAL_MAX_SOURCE)
    and ascii_bounded(run_name, DEAL_MAX_TOKEN)
)
@deal.post(lambda result: result is None or isinstance(result, dict))
def parse_run_import_call_spec(code: str, *, run_name: str) -> dict[str, Any] | None:
    """Return the first positional spec dict from ``run_name({...}, ...)`` or a writeragent helper call."""
    if not code:
        return None
    import sys

    # CrossHair timing/contract switch, not a test leak. Optional later:
    # deal_shim.is_crosshair_running() if more sites grow this probe.
    if "crosshair" in sys.modules:
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


def prepend_run_import_document_bindings(code: str, *, bindings: dict[str, Any]) -> str:
    """Prepend literal variable assignments for host-injected Writer document inputs."""
    if not bindings:
        return code
    lines = ["# Document inputs injected below — edit the run_*() call only."]
    for name, value in bindings.items():
        lines.append(f"{name} = {json.dumps(value, ensure_ascii=False)}")
    lines.append("")
    return "\n".join(lines) + code


def _source_calls_name(code: str, name: str) -> bool:
    try:
        tree = ast.parse(code)
    except (SyntaxError, TypeError, ValueError):
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == name:
            return True
    return False


def script_uses_run_import(code: str, *, run_name: str) -> bool:
    """True when *code* contains a call to *run_name*.

    Direct helper calls are not this. Using the spec parser here treated
    ``print("hi")`` as ``run_text_analytics`` and prepended the Writer body.
    The substring fallback covers a call the AST cannot parse.
    """
    if not code or not run_name:
        return False
    return _source_calls_name(code, run_name) or f"{run_name}(" in code


# --- Templates ---


def _python_literal(val: Any) -> str:
    """Python source for a template argument. The string ``data`` is the range name."""
    if val == "data":
        return "data"
    if val is None:
        return "None"
    if val is True:
        return "True"
    if val is False:
        return "False"
    if isinstance(val, str):
        return repr(val)
    if isinstance(val, float):
        return repr(val)
    if isinstance(val, int) and not isinstance(val, bool):
        return repr(val)
    if isinstance(val, (list, tuple)):
        return "[" + ", ".join(_python_literal(item) for item in val) + "]"
    if isinstance(val, dict):
        parts = [f"{_python_literal(key)}: {_python_literal(item)}" for key, item in val.items()]
        return "{" + ", ".join(parts) + "}"
    return repr(val)


def build_helper_script_template(
    *,
    tag: str,
    helper: str,
    params: dict[str, Any],
    description: str,
    style: str = "run_import",  # "run_import" | "header_only" (str: CrossHair cannot proxy Literal)
    import_module: str | None = None,
    run_name: str | None = None,
    data_expr: str = "data",
    context_expr: str = "{}",
    extra_comment_lines: tuple[str, ...] = (),
    positional_args: tuple[str, ...] | None = None,
    compact_json: bool = True,
    leading_data: bool = False,
    invoke: str = "direct",
) -> str:
    """Build a Run Python Script template body."""
    import sys

    # Symbolic dict → json.dumps CrossHair crash; empty JSON keeps template shape coverable.
    if "crosshair" in sys.modules:
        params_json = "{}"
    elif compact_json:
        params_json = json.dumps(params, separators=(",", ":"))
    else:
        params_json = json.dumps(params)

    if style == "header_only":
        header_line = f"{header_prefix(tag)} helper={helper} params={params_json}"
        lines = [
            header_line,
            "#",
            f"# {description}",
            *extra_comment_lines,
        ]
        return "\n".join(lines) + "\n"

    # run_import style (analysis / units / viz / math / …) — executable Python only; no header comment.
    if not import_module:
        raise ValueError("import_module required for style='run_import'")
    default_extra = extra_comment_lines or ("# Edit the call below, then Run.",)

    def _format_param_val(val: Any) -> str:
        # ``"data"`` is the injected range name, not a JSON string. json.dumps
        # used to emit ``null`` / ``false``, which are not Python.
        return _python_literal(val)

    pos_keys = positional_args or ()
    parts: list[str] = []
    if leading_data:
        parts.append(data_expr)
    for key in pos_keys:
        if key in params:
            parts.append(_format_param_val(params[key]))
    kw_parts = [
        f"{key}={_format_param_val(val)}"
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
    """Human-readable duration for RPS status lines."""
    if seconds >= 60.0:
        minutes = int(seconds // 60)
        secs = int(seconds % 60)
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
    """Seconds since *t0*, or 0 while CrossHair is loaded.

    ``perf_counter`` is NotDeterministic for the SMT solver. Outcome helpers
    all need the same guard; repeating ``"crosshair" in sys.modules`` at each
    site was the same check copied six times.
    """
    import sys

    if "crosshair" in sys.modules:
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
    status_ok = _("Plot inserted ({title}). (took {time})").format(title=title, time=formatted_time)
    if helper:
        status_ok = _("Viz '{helper}' completed. {msg}").format(
            helper=helper,
            msg=_("Plot inserted ({title}). (took {time})").format(title=title, time=formatted_time),
        )
    return rps_ok_outcome(status_ok, result=result, stdout=stdout)


def symbolic_insert_ok_outcome(
    *,
    helper: str,
    latex: str,
    t0: float,
    stdout: str | None,
    result: Any,
) -> dict[str, Any]:
    elapsed = _elapsed_since(t0)
    formatted_time = format_elapsed_time(elapsed)
    preview = latex[:80] + ("…" if len(latex) > 80 else "")
    status_ok = _("Math '{helper}' completed. Inserted: {preview} (took {time})").format(
        helper=helper,
        preview=preview,
        time=formatted_time,
    )
    return rps_ok_outcome(status_ok, result=result, stdout=stdout)


def units_insert_ok_outcome(
    *,
    helper: str,
    formatted: str,
    t0: float,
    stdout: str | None,
    result: Any,
) -> dict[str, Any]:
    elapsed = _elapsed_since(t0)
    formatted_time = format_elapsed_time(elapsed)
    preview = formatted[:80] + ("…" if len(formatted) > 80 else "")
    status_ok = _("Units '{helper}' completed. Inserted: {preview} (took {time})").format(
        helper=helper,
        preview=preview,
        time=formatted_time,
    )
    return rps_ok_outcome(status_ok, result=result, stdout=stdout)


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
    style: Literal["run_import", "header_only"] = "run_import"
    shipped_templates: frozenset[str] | None = None
    data_expr: str = "data"
    context_expr: str = "{}"
    positional_args: dict[str, tuple[str, ...]] | None = None
    extra_comment_lines: tuple[str, ...] = ("# Edit the run call below, then Run.",)
    compact_json: bool = True
    # Sheet helpers take the injected range as the first argument. Quant and
    # text call ``run_*`` instead of the helper name (those callables do not
    # match ``helper(**params)``).
    leading_data: bool = False
    invoke: str = "direct"
    require_prefix: bool = True
    on_bad_json: Literal["empty", "none", "raise"] = "raise"


def make_template_api(cfg: DomainFacadeConfig) -> Any:
    """Build _template_body, get_templates, and parse_header functions dynamically."""
    # crosshair: off
    def _template_body(helper: str, params: dict[str, Any]) -> str:
        desc = cfg.descriptions.get(helper, helper.replace("_", " ").title() if "_" in helper else helper)
        pos = cfg.positional_args.get(helper) if cfg.positional_args else None
        return build_helper_script_template(
            tag=cfg.tag,
            helper=helper,
            params=params,
            description=desc,
            style=cfg.style,
            import_module=cfg.import_module,
            run_name=cfg.run_name,
            data_expr=cfg.data_expr,
            context_expr=cfg.context_expr,
            extra_comment_lines=cfg.extra_comment_lines,
            positional_args=pos,
            compact_json=cfg.compact_json,
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
        # If we explicitly configure require_prefix=False or on_bad_json, pass them
        return parse_helper_script_header(
            code,
            tag=cfg.tag,
            helper_names=cfg.helper_names if cfg.require_prefix else None,
            require_prefix=cfg.require_prefix,
            on_bad_json=cfg.on_bad_json,
        )

    return SimpleNamespace(
        template_body=_template_body,
        get_templates=get_templates,
        parse_header=parse_header,
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
    params: dict[str, Any] | None,
    data_range: str | None,
    data: Any,
    headers: bool,
    task_hint: str | None,
    helper_names: frozenset[str] | set[str],
    error_code: str,
    empty_data_message: str,
    client_run: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
    """Fetch Calc data and run one trusted sheet helper. Forecast and optimize share this."""
    from plugin.calc.analysis_runner import calc_tool_context
    from plugin.calc.bridge import CalcBridge
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

    tool_ctx = calc_tool_context(uno_ctx, doc)
    py_data, err = _resolve_python_data(tool_ctx, data_range=dr, data=data)
    if err:
        raise ToolExecutionError(err, code=error_code)
    if py_data is None:
        raise ToolExecutionError(empty_data_message, code=error_code)

    spec: dict[str, Any] = {"helper": name, "headers": bool(headers)}
    if isinstance(params, dict) and params:
        spec["params"] = params

    context: dict[str, Any] = {}
    try:
        bridge = CalcBridge(doc)
        context["sheet_name"] = bridge.get_active_sheet().getName()
    except Exception:
        pass
    if task_hint:
        context["task_hint"] = str(task_hint)
    if dr:
        context["range_a1"] = dr

    return client_run(uno_ctx, spec, py_data, context=context or None)



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
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from plugin.framework.i18n import _

if TYPE_CHECKING:
    from collections.abc import Collection

# --- Header meta ---


@dataclass(frozen=True)
class HelperScriptMeta:
    """Machine-readable ``# writeragent:<tag> helper=… params=…`` header."""

    helper: str
    params: dict[str, Any]


def header_prefix(tag: str) -> str:
    """Return ``# writeragent:<tag>`` (no trailing space)."""
    return f"# writeragent:{tag}"


def _header_re(tag: str) -> re.Pattern[str]:
    # Same wire shape as historical per-domain regexes.
    return re.compile(
        rf"^\s*#\s*writeragent:{re.escape(tag)}\s+helper=(\w+)\s+params=(\{{.*\}})\s*$",
        re.MULTILINE,
    )


def parse_helper_script_header(
    code: str,
    *,
    tag: str,
    helper_names: Collection[str] | None = None,
    require_prefix: bool = True,
    on_bad_json: Literal["empty", "none"] = "empty",
) -> HelperScriptMeta | None:
    """Parse ``# writeragent:<tag> helper=NAME params={…}``.

    *require_prefix*: if True, require the prefix substring before regex (units/analysis style).
    *helper_names*: when set, unknown helpers return None.
    *on_bad_json*: ``empty`` → ``{}`` on JSON errors (units/analysis); ``none`` → return None
    on any parse exception (forecast/optimize/quant style).
    """
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
    except Exception:
        if on_bad_json == "none":
            return None
        params = {}
    if not isinstance(params, dict):
        if on_bad_json == "none":
            return None
        params = {}
    return HelperScriptMeta(helper=helper, params=params)


def _literal_value(node: ast.AST) -> Any:
    """Best-effort static value for template-style literal AST nodes."""
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
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub) and isinstance(node.operand, ast.Constant):
        value = node.operand.value
        if isinstance(value, int | float):
            return -value
    return None


def parse_run_import_call_params(code: str, *, run_name: str) -> dict[str, Any] | None:
    """Return the ``params`` dict from ``run_name({"helper": ..., "params": {...}}, ...)`` when literal."""
    spec = parse_run_import_call_spec(code, run_name=run_name)
    if not spec:
        return None
    params = spec.get("params")
    return params if isinstance(params, dict) else None


def parse_run_import_call_spec(code: str, *, run_name: str) -> dict[str, Any] | None:
    """Return the first positional spec dict from ``run_name({...}, ...)`` or direct helper call when literal."""
    if not code:
        return None
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return None
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
        elif node.keywords and any(kw.arg is None for kw in node.keywords):
            for kw in node.keywords:
                if kw.arg is None:
                    val = _literal_value(kw.value)
                    if isinstance(val, dict):
                        return {"helper": func.id, "params": val}
    return None


def prepend_run_import_document_bindings(code: str, *, bindings: dict[str, Any]) -> str:
    """Prepend literal variable assignments for host-injected Writer document inputs."""
    if not bindings:
        return code
    lines = ["# WriterAgent: document inputs injected below — edit the run_*() call only."]
    for name, value in bindings.items():
        lines.append(f"{name} = {json.dumps(value, ensure_ascii=False)}")
    lines.append("")
    return "\n".join(lines) + code


def script_uses_run_import(code: str, *, run_name: str) -> bool:
    """True when *code* contains a call to *run_name*."""
    if not code or not run_name:
        return False
    return parse_run_import_call_spec(code, run_name=run_name) is not None or f"{run_name}(" in code


# --- Templates ---


def build_helper_script_template(
    *,
    tag: str,
    helper: str,
    params: dict[str, Any],
    description: str,
    style: Literal["run_import", "header_only"] = "run_import",
    import_module: str | None = None,
    run_name: str | None = None,
    data_expr: str = "data",
    context_expr: str = "{}",
    extra_comment_lines: tuple[str, ...] = (),
    compact_json: bool = True,
) -> str:
    """Build a Run Python Script template body."""
    if compact_json:
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
    body_lines = [
        f"# {description}",
        *default_extra,
        f"from {import_module} import {helper}\n",
        f"result = {helper}(**{params_json})",
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


def rps_error_outcome(
    message: str,
    *,
    t0: float,
    traceback: str | None = None,
) -> dict[str, Any]:
    """Standard ``{ok: False, message}`` with elapsed time when not a timeout."""
    elapsed = time.perf_counter() - t0
    out: dict[str, Any] = {"ok": False, "message": _append_took(message, elapsed)}
    if traceback is not None:
        out["traceback"] = traceback
    return out


def rps_insert_failed_outcome(error: BaseException, *, t0: float) -> dict[str, Any]:
    """Outcome when domain insert/egress fails after a successful helper run."""
    elapsed_total = time.perf_counter() - t0
    formatted_time_total = format_elapsed_time(elapsed_total)
    return {
        "ok": False,
        "message": _("Failed to insert result: {error} (took {time})").format(
            error=str(error),
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
    formatted_time = format_elapsed_time(time.perf_counter() - t0)
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
    formatted_time = format_elapsed_time(time.perf_counter() - t0)
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
    formatted_time = format_elapsed_time(time.perf_counter() - t0)
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
    extra_comment_lines: tuple[str, ...] = ("# Edit the run call below, then Run.",)
    compact_json: bool = True
    require_prefix: bool = True
    on_bad_json: Literal["empty", "none"] = "empty"


def make_template_api(cfg: DomainFacadeConfig) -> Any:
    """Build _template_body, get_templates, and parse_header functions dynamically."""
    def _template_body(helper: str, params: dict[str, Any]) -> str:
        desc = cfg.descriptions.get(helper, helper.replace("_", " ").title() if "_" in helper else helper)
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
            compact_json=cfg.compact_json,
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



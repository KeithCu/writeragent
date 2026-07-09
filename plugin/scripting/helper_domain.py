# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared host glue for trusted helper domains (headers, templates, RPS outcomes).

Domain modules keep public parse/template wrappers; compute and egress stay domain-specific.
No imports of domain modules here (avoids cycles).
"""

from __future__ import annotations

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
    """Build a Run Python Script template body (header wire format preserved)."""
    if compact_json:
        params_json = json.dumps(params, separators=(",", ":"))
    else:
        params_json = json.dumps(params)
    prefix = header_prefix(tag)
    header_line = f"{prefix} helper={helper} params={params_json}"

    if style == "header_only":
        lines = [
            header_line,
            "#",
            f"# {description}",
            *extra_comment_lines,
        ]
        return "\n".join(lines) + "\n"

    # run_import style (analysis / units / viz / math / …)
    if not import_module or not run_name:
        raise ValueError("import_module and run_name required for style='run_import'")
    default_extra = extra_comment_lines or ("# Edit params above, then Run.",)
    body_lines = [
        header_line,
        f"# {description}",
        *default_extra,
        f"from {import_module} import {run_name}\n",
        f"result = {run_name}(",
        f'    {{"helper": "{helper}", "params": {params_json}}},',
        f"    {data_expr},",
        f"    {context_expr},",
        ")",
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

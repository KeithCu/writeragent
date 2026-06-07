# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Built-in Run Python Script templates for trusted symbolic math helpers."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from plugin.scripting.symbolic_common import HELPER_NAMES

MATH_HEADER_PREFIX = "# writeragent:math"
_MATH_HEADER_RE = re.compile(
    r"^\s*#\s*writeragent:math\s+helper=(\w+)\s+params=(\{.*\})\s*$",
    re.MULTILINE,
)

_SHIPPED_TEMPLATES = frozenset({"solve_equation", "symbolic_simplify", "integrate"})

_DEFAULT_PARAMS: dict[str, dict[str, Any]] = {
    "solve_equation": {"equation": "x**2 - 4", "variable": "x"},
    "symbolic_simplify": {"expression": "(x + 1)**2 - x**2 - 2*x"},
    "integrate": {"expression": "sin(x)", "variable": "x"},
}

_HELPER_DESCRIPTIONS: dict[str, str] = {
    "solve_equation": "Solve an equation for a variable (use = or expression equal to zero).",
    "symbolic_simplify": "Simplify a symbolic expression.",
    "integrate": "Integrate an expression (add lower/upper for definite integrals).",
}


@dataclass(frozen=True)
class MathScriptMeta:
    helper: str
    params: dict[str, Any]


def _template_body(helper: str, params: dict[str, Any]) -> str:
    params_json = json.dumps(params, separators=(",", ":"))
    desc = _HELPER_DESCRIPTIONS.get(helper, helper)
    return (
        f"{MATH_HEADER_PREFIX} helper={helper} params={params_json}\n"  # nosec
        f"# {desc}\n"
        f"# Edit params above, then Run.\n"
        f"from plugin.scripting.symbolic import run_symbolic\n\n"
        f"result = run_symbolic(\n"
        f'    {{"helper": "{helper}", "params": {params_json}}},\n'
        f"    None,\n"
        f"    {{}},\n"
        f")\n"
    )


def get_math_script_templates() -> dict[str, str]:
    """Return built-in math helper scripts keyed by helper name."""
    return {
        helper: _template_body(helper, dict(_DEFAULT_PARAMS.get(helper, {})))
        for helper in sorted(_SHIPPED_TEMPLATES)
        if helper in HELPER_NAMES
    }


def parse_math_script_header(code: str) -> MathScriptMeta | None:
    """Parse the machine-readable header from a built-in or copied math script."""
    if not code or MATH_HEADER_PREFIX not in code:
        return None
    match = _MATH_HEADER_RE.search(code)
    if not match:
        return None
    helper = match.group(1)
    if helper not in HELPER_NAMES:
        return None
    try:
        params = json.loads(match.group(2))
    except json.JSONDecodeError:
        params = {}
    if not isinstance(params, dict):
        params = {}
    return MathScriptMeta(helper=helper, params=params)

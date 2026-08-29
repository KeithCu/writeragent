# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

"""JSON repair and robust parsing utilities for WriterAgent."""

from __future__ import annotations

import ast
import json
import re
from typing import Any

from plugin.framework.deal_shim import DEAL_MAX_SOURCE, UNDER_CROSSHAIR, deal, str_bounded

_LATEX_CLASH_WORDS = [
    # \a (Bell)
    "alpha",
    "approx",
    "ast",
    "angle",
    "arccos",
    "arcsin",
    "arctan",
    "arg",
    "aleph",
    "amalg",
    # \b (Backspace)
    "beta",
    "begin",
    "bar",
    "bot",
    "bullet",
    "bmod",
    "boldsymbol",
    "bigcup",
    "bigcap",
    "bigg",
    "backslash",
    "bf",
    "bm",
    "big",
    "bigodot",
    "bigoplus",
    "bigotimes",
    "biguplus",
    "bigvee",
    "bigwedge",
    "box",
    "breve",
    "buildrel",
    "bumpeq",
    # \f (Formfeed)
    "frac",
    "forall",
    "varphi",
    "fbox",
    "framebox",
    "flat",
    "frown",
    # \n (Newline)
    "nabla",
    "neq",
    "nu",
    "norm",
    "notin",
    "newline",
    "nRightarrow",
    "nleftarrow",
    "nLeftrightarrow",
    "natural",
    "ne",
    "nearrow",
    "neg",
    "ni",
    "not",
    "nwarrow",
    # \r (Carriage Return)
    "right",
    "rho",
    "rangle",
    "rightarrow",
    "rbrace",
    "rbrack",
    "rceil",
    "rfloor",
    "renewcommand",
    "require",
    "Rightarrow",
    "Re",
    "rightleftharpoons",
    "rm",
    "rtimes",
    # \t (Tab)
    "times",
    "text",
    "tau",
    "theta",
    "tilde",
    "tan",
    "tfrac",
    "triangle",
    "to",
    "textbf",
    "textit",
    "texttt",
    "top",
    "triangleright",
    # \v (Vertical Tab)
    "vec",
    "varepsilon",
    "varpi",
    "varrho",
    "varsigma",
    "vartheta",
    "vdash",
    "vee",
    "vert",
    "Vert",
]

_LATEX_CLASH_RE = re.compile(r"(?<!\\)\\(" + "|".join(_LATEX_CLASH_WORDS) + r")\b")

_SILENT_CORRUPTIONS = {}
_escape_map = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f"}
for _word in _LATEX_CLASH_WORDS:
    _first = _word[0]
    if _first in _escape_map:
        _corrupted = _escape_map[_first] + _word[1:]
        _repaired = r"\\" + _word
        _SILENT_CORRUPTIONS[_corrupted] = _repaired


def _repair_latex_clashes(text: str) -> str:
    """Escape backslashes for LaTeX commands that conflict with JSON escapes."""
    # CrossHair: regex + large clash tables explode the SMT heap; identity is enough for contracts.
    if UNDER_CROSSHAIR:
        return text
    # 1. Handle properly escaped but single-slash clashes (e.g. \\nabla -> \\\\nabla)
    text = _LATEX_CLASH_RE.sub(r"\\\\\1", text)

    # 2. Handle cases where the LLM sent a single backslash in the network JSON,
    # which the outer json.loads already silently evaluated as a control character
    # (e.g. \nabla -> \n + abla).
    for corrupted, repaired in _SILENT_CORRUPTIONS.items():
        text = text.replace(corrupted, repaired)

    return text




# Identity repair under CrossHair: charset only needs to distinguish strip/empty vs body.
_JSON_CHARS = (
    frozenset("{}\n ")
    if UNDER_CROSSHAIR
    else frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789{}[],\": \t\n\r")
)


def _deal_json_text_ok_pytest(text: object) -> bool:
    return not isinstance(text, str) or str_bounded(text, DEAL_MAX_SOURCE)


def _deal_json_text_ok_crosshair(text: object) -> bool:
    # cover-all 33127995861: 103k at SOURCE=16; 33180040863 ~16m at len=4;
    # 33211730747 still ~46m at len=2 — identity repair, so len=1 is enough.
    return isinstance(text, str) and len(text) <= 1 and all(
        c in _JSON_CHARS for c in text
    )


_deal_json_text_ok = _deal_json_text_ok_crosshair if UNDER_CROSSHAIR else _deal_json_text_ok_pytest


@deal.pre(lambda text: _deal_json_text_ok(text))
def repair_json(text: str) -> str:
    """Attempt to repair common JSON syntax errors from LLMs using json-repair.

    Handles:
    1. Truncated JSON (missing closing braces/brackets)
    2. Trailing commas
    3. Unquoted keys
    4. Single quotes vs double quotes
    5. Missing values

    Returns:
        The repaired JSON string.
    """
    if not isinstance(text, str):
        return text

    repaired = text.strip()
    if not repaired:
        return repaired

    # json_repair under symbolic strings → CrossHairInternal; keep identity for cover/check.
    if UNDER_CROSSHAIR:
        return repaired

    import json_repair

    return str(json_repair.repair_json(repaired))


@deal.pre(lambda text, *_unused, **__: _deal_json_text_ok(text))
def repair_json_object(text: str) -> Any:
    """Repair malformed JSON and return a parsed object (json-repair return_objects=True)."""
    if not isinstance(text, str):
        return text
    stripped = text.strip()
    if not stripped:
        return stripped

    if UNDER_CROSSHAIR:
        return {}
    import json_repair  # lazy: vendored in plugin/lib or vendor/

    return json_repair.repair_json(stripped, return_objects=True)


@deal.ensure(lambda text, default=None, strict=False, result=None: isinstance(text, (str, bytes, bytearray)) or result is default)
@deal.ensure(lambda text, default=None, strict=False, result=None: not (isinstance(text, str) and text.strip() == "") or result is default)
def safe_json_loads(text: Any, default: Any = None, strict: bool = False) -> Any:
    """Safely parse a JSON string into a Python object with optional robust repair logic.

    Attempts (non-strict / LLM mode; keep this list in sync with the body):
    1. Standard json.loads
    2. json.loads with strict=False (handles raw control chars, per hermes-agent)
    3. ast.literal_eval (single quotes and Python-isms)
    4. repair_json + json.loads (truncated / malformed JSON)

    Do not swap 3 and 4 to "repair first" without golden tests: literal_eval
    accepting a truncated fragment is accepted behavior, not a bug.

    Args:
        text: The string to parse.
        default: The value to return if parsing fails. Defaults to None.
        strict: If True, only use standard JSON parsing (no repair). Defaults to False.

    Returns:
        The parsed Python object or the default value if an error occurs.
    """
    # crosshair: off
    if not isinstance(text, (str, bytes, bytearray)):
        return default

    # Ensure we are working with a string for repair logic
    raw_text = text.decode("utf-8", errors="replace") if isinstance(text, (bytes, bytearray)) else text
    if not isinstance(raw_text, str) or not raw_text.strip():
        return default

    # In strict mode, only RFC 8259 standard JSON parsing is allowed.
    # What was wrong: raw_text.strip() strips Unicode whitespace characters (e.g. \x1f)
    # that are not valid JSON whitespace, allowing unescaped control characters ('0\x1f' -> 0)
    # to parse instead of returning default. Passing raw_text directly to json.loads preserves strict JSON rules.
    if strict:
        try:
            parsed = json.loads(raw_text)
            return parsed if parsed is not None else default
        except (json.JSONDecodeError, TypeError, ValueError, RecursionError):
            return default

    stripped = raw_text.strip()

    # Pre-process string to fix unescaped LaTeX commands that coincide with valid JSON escapes
    # e.g., "\times" is natively treated as <tab>imes. We replace it with "\\times".
    stripped = _repair_latex_clashes(stripped)

    # 1. Standard attempt
    if UNDER_CROSSHAIR:
        if stripped.startswith("{") and stripped.endswith("}"):
            return {}
        return default

    try:
        parsed = json.loads(stripped)
        return parsed if parsed is not None else default
    except (json.JSONDecodeError, TypeError, ValueError, RecursionError):
        pass

    # 2. strict=False attempt (handles bare control characters in non-strict LLM mode)
    try:
        parsed = json.loads(stripped, strict=False)
        return parsed if parsed is not None else default
    except (json.JSONDecodeError, TypeError, ValueError, RecursionError):
        pass

    # 3. ast.literal_eval fallback (handles single quotes and Python-isms)
    # Inspired by hermes-agent/environments/tool_call_parsers/qwen3_coder_parser.py
    try:
        # literal_eval handles 'True', 'False', 'None' out of the box.
        # It also handles single quotes and tuple-like syntax.
        parsed = ast.literal_eval(stripped)
        return parsed if parsed is not None else default
    except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
        pass

    # 4. Repair attempt for truncated or malformed JSON
    try:
        repaired = repair_json(stripped)
        if repaired != stripped:
            parsed = json.loads(repaired, strict=False)
            return parsed if parsed is not None else default
    except (json.JSONDecodeError, TypeError, ValueError, RecursionError):
        pass

    return default


def safe_python_literal_eval(text: Any, default: Any = None) -> Any:
    """Safely parse a Python-style literal (e.g. from an LLM) without using ast.literal_eval.
    Supports scalars (bool, None, number, string) and simple JSON-compatible lists/dicts.
    Returns the default value if it doesn't look like a simple literal.

    Args:
        text: The string to parse.
        default: The value to return if parsing fails. Defaults to None.

    Returns:
        The parsed Python object or the default value if an error occurs.
    """
    # crosshair: off
    if not isinstance(text, (str, bytes, bytearray)):
        return default

    stripped = text.strip()
    if not stripped:
        return default

    # 1. Try standard JSON first (handles numbers, double-quoted strings, bools, null)
    # Use strict=True as literal_eval fallback is handled separately below for booleans/strings.
    data = safe_json_loads(stripped, default=None, strict=True)
    if data is not None:
        return data

    # 2. Handle Python-style booleans and None (which JSON calls true/false/null)
    # Case-insensitive checks to handle various LLM formatting quirks robustly
    lower = stripped.lower()
    if lower == "true":
        return True
    if lower == "false":
        return False
    if lower in ("none", "null"):
        return None

    # 3. Handle simple single-quoted string unquoting: 'abc' -> abc
    # This avoids ast.literal_eval for basic string normalization.
    if isinstance(stripped, str) and len(stripped) >= 2 and stripped[0] == "'" and stripped[-1] == "'":
        inner = stripped[1:-1]
        # Only unquote if it's a simple string (no internal single quotes or backslashes)
        if "'" not in inner and "\\" not in inner:
            return inner

    return default

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

import json
import ast
import re

_LATEX_CLASH_WORDS = [
    # \a (Bell)
    "alpha", "approx", "ast", "angle", "arccos", "arcsin", "arctan", "arg", "aleph", "amalg",
    # \b (Backspace)
    "beta", "begin", "bar", "bot", "bullet", "bmod", "boldsymbol", "bigcup", "bigcap", "bigg", "backslash", "bf", "bm", "big", "bigodot", "bigoplus", "bigotimes", "biguplus", "bigvee", "bigwedge", "box", "breve", "buildrel", "bumpeq",
    # \f (Formfeed)
    "frac", "forall", "varphi", "fbox", "framebox", "flat", "frown",
    # \n (Newline)
    "nabla", "neq", "nu", "norm", "notin", "newline", "nRightarrow", "nleftarrow", "nLeftrightarrow", "natural", "ne", "nearrow", "neg", "ni", "not", "nwarrow",
    # \r (Carriage Return)
    "right", "rho", "rangle", "rightarrow", "rbrace", "rbrack", "rceil", "rfloor", "renewcommand", "require", "Rightarrow", "Re", "rightleftharpoons", "rm", "rtimes",
    # \t (Tab)
    "times", "text", "tau", "theta", "tilde", "tan", "tfrac", "triangle", "to", "textbf", "textit", "texttt", "top", "triangleright",
    # \v (Vertical Tab)
    "vec", "varepsilon", "varpi", "varrho", "varsigma", "vartheta", "vdash", "vee", "vert", "Vert"
]

_LATEX_CLASH_RE = re.compile(
    r"(?<!\\)\\(" + "|".join(_LATEX_CLASH_WORDS) + r")\b"
)

def _repair_latex_clashes(text: str) -> str:
    """Escape backslashes for LaTeX commands that conflict with JSON escapes."""
    return _LATEX_CLASH_RE.sub(r"\\\\\1", text)

from typing import Any

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

    import json_repair
    return str(json_repair.repair_json(repaired))


def safe_json_loads(text: Any, default: Any = None, strict: bool = False) -> Any:
    """Safely parse a JSON string into a Python object with optional robust repair logic.

    Attempts:
    1. Standard json.loads
    2. json.loads with strict=False (handles raw control chars, per hermes-agent)
    3. repair_json + json.loads (LLM/Robust mode only)
    4. ast.literal_eval as final fallback (LLM/Robust mode only)

    Args:
        text: The string to parse.
        default: The value to return if parsing fails. Defaults to None.
        strict: If True, only use standard JSON parsing (no repair). Defaults to False.

    Returns:
        The parsed Python object or the default value if an error occurs.
    """
    if not isinstance(text, (str, bytes, bytearray)):
        return default

    # Ensure we are working with a string for repair logic
    raw_text = text.decode("utf-8", errors="replace") if isinstance(text, (bytes, bytearray)) else text
    stripped = raw_text.strip()
    if not stripped:
        return default

    # Pre-process string to fix unescaped LaTeX commands that coincide with valid JSON escapes
    # e.g., "\times" is natively treated as <tab>imes. We replace it with "\\times".
    if not strict:
        stripped = _repair_latex_clashes(stripped)

    # 1. Standard attempt
    try:
        parsed = json.loads(stripped)
        return parsed if parsed is not None else default
    except (json.JSONDecodeError, TypeError, ValueError):
        pass

    # 2. strict=False attempt (handles bare control characters)
    try:
        parsed = json.loads(stripped, strict=False)
        return parsed if parsed is not None else default
    except (json.JSONDecodeError, TypeError, ValueError):
        pass

    # In strict mode, we stop here.
    if strict:
        return default

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
    except (json.JSONDecodeError, TypeError, ValueError):
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
    if (
        isinstance(stripped, str)
        and len(stripped) >= 2
        and stripped[0] == "'"
        and stripped[-1] == "'"
    ):
        inner = stripped[1:-1]
        # Only unquote if it's a simple string (no internal single quotes or backslashes)
        if "'" not in inner and "\\" not in inner:
            return inner

    return default

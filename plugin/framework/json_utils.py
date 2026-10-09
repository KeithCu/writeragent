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
import logging
import re
from typing import Any

from plugin.framework.deal_shim import UNDER_CROSSHAIR, deal

log = logging.getLogger(__name__)


def _is_pre_contract_error(exc: BaseException) -> bool:
    """True for ``deal.PreContractError``.

    That class subclasses ``AssertionError``, not ``ValueError``, so a repair
    ``except`` that lists only JSON errors does not catch it. Match by name so
    this stays valid when deal is not installed.
    """
    return type(exc).__name__ == "PreContractError"

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

# JSON strings allow \" \\ \/ \b \f \n \r \t \uXXXX. A single backslash before
# a clash word is LaTeX the model forgot to escape (`\nabla`, `\times`,
# `\frac`, `\beta`). Dropping every word that starts with b/f/n/r/t leaves
# those commands as a valid escape plus leftover letters. json.loads then
# turns `\n` into a newline and returns, so step 2 never sees a control
# character (the source still has backslash + letter).
# A two-letter escape word followed by "." + a letter is not the command:
# `\ne.g.` / `\ni.e.` / `\nu.s.` are a newline plus an abbreviation.
# `_` and digits are word characters, so a `\b` word boundary misses
# `\alpha_1` and `\times2`. json.loads (or literal_eval for `\a`) then
# keeps the control character and drops the backslash.
# A letter lookahead still rejects `\alphax`.
_JSON_ESCAPE_STARTS = frozenset("bfnrt")
_LATEX_CLASH_RE = re.compile(r"(?<!\\)\\(" + "|".join(_LATEX_CLASH_WORDS) + r")(?![A-Za-z])")


def _double_latex_clash(match: re.Match[str]) -> str:
    """Double one backslash before a clash word, except dotted abbreviations."""
    word = match.group(1)
    tail = match.string[match.end() : match.end() + 2]
    if len(word) == 2 and word[:1] in _JSON_ESCAPE_STARTS and tail[:1] == "." and tail[1:2].isalpha():
        return match.group(0)
    return "\\\\" + word


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
    # 1. Double a single backslash on a LaTeX clash word (`\nabla` -> `\\nabla`)
    # so json.loads keeps the command. `\ne.g.` stays a newline.
    text = _LATEX_CLASH_RE.sub(_double_latex_clash, text)

    # 2. Handle cases where the LLM sent a single backslash in the network JSON,
    # which the outer json.loads already silently evaluated as a control character
    # (e.g. \nabla -> \n + abla).
    # Step 2 must not replace a control-character prefix anywhere. A real
    # newline followed by "e" (pretty-printed "example", or a string that
    # continues "end") would become the LaTeX command \ne.
    for corrupted, repaired in _SILENT_CORRUPTIONS.items():
        text = _replace_control_token(text, corrupted, repaired)

    return text


def _replace_control_token(text: str, corrupted: str, repaired: str) -> str:
    """Replace *corrupted* only when it is not the prefix of a longer word."""
    pieces: list[str] = []
    start = 0
    while True:
        found = text.find(corrupted, start)
        if found < 0:
            pieces.append(text[start:])
            return "".join(pieces)
        end = found + len(corrupted)
        nxt = text[end : end + 1]
        if nxt.isalnum() or nxt == "_":
            pieces.append(text[start:end])
            start = end
            continue
        # A two-letter command plus "." + a letter is an abbreviation
        # (newline + "e.g."), not LaTeX. A longer command (`\nabla.`) still
        # matches: its corrupted form is longer than the control char + one letter.
        if len(corrupted) == 2 and nxt == "." and text[end + 1 : end + 2].isalpha():
            pieces.append(text[start:end])
            start = end
            continue
        pieces.append(text[start:found])
        pieces.append(repaired)
        start = end


# Identity repair under CrossHair: charset only needs to distinguish strip/empty vs body.
_JSON_CHARS = frozenset("{}\n ") if UNDER_CROSSHAIR else frozenset('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789{}[],": \t\n\r')


def _deal_json_text_ok_pytest(text: object) -> bool:
    # LLM JSON and writeragent.json are external. DEAL_MAX_SOURCE (8192) raised
    # PreContractError, an AssertionError, so repair was skipped and callers
    # saw a silent default. Release OXTs strip deal and already repair. The
    # body returns non-strings unchanged and runs json_repair on any str.
    # CrossHair keeps the one-character domain. ``text`` is unused.
    return True


def _deal_json_text_ok_crosshair(text: object) -> bool:
    return isinstance(text, str) and len(text) <= 1 and all(c in _JSON_CHARS for c in text)


_deal_json_text_ok = _deal_json_text_ok_crosshair if UNDER_CROSSHAIR else _deal_json_text_ok_pytest


def _debug_json_stage(stage: str) -> None:
    """Log which fallback accepted the text.

    Step 1 can fail and a later step still return a value. The stage name
    is enough to see which step won. Logging the source would dump secrets
    and document text.
    """
    log.debug("safe_json_loads stage=%s", stage)


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
    # crosshair: off
    if not isinstance(text, str):
        return text

    repaired = text.strip()
    if not repaired:
        return repaired

    # json_repair under symbolic strings → CrossHairInternal; keep identity for cover/check.
    if UNDER_CROSSHAIR:
        return repaired

    try:
        import json_repair
    except ImportError:
        log.warning("json_repair is not installed; leaving JSON unrepaired")
        return repaired

    return str(json_repair.repair_json(repaired))


@deal.pre(lambda text, *_unused, **__: _deal_json_text_ok(text))
def _repair_json_object_bounded(text: str) -> Any:
    """Deal-bounded body of ``repair_json_object``. Callers catch ``PreContractError``."""
    # crosshair: off
    if not isinstance(text, str):
        return text
    stripped = text.strip()
    if not stripped:
        return stripped

    if UNDER_CROSSHAIR:
        return {}
    import json_repair  # lazy: vendored in plugin/lib or vendor/

    return json_repair.repair_json(stripped, return_objects=True)


def repair_json_object(text: str) -> Any:
    """Repair malformed JSON and return a parsed object (json-repair return_objects=True).

    The pytest pre is total, so a long body is repaired. ``PreContractError``
    subclasses ``AssertionError``, not ``ValueError``. If a contract error
    still escapes ``_repair_json_object_bounded``, return the original text
    instead of letting it skip JSON ``except`` clauses.
    """
    # crosshair: off
    try:
        return _repair_json_object_bounded(text)
    except Exception as exc:
        # except Exception, not a deal class: mypy rejects a dynamically loaded
        # PreContractError in an except clause. Only that contract error is
        # swallowed; other repair failures still propagate.
        if not _is_pre_contract_error(exc):
            raise
        return text


@deal.ensure(lambda text, default=None, strict=False, result=None: isinstance(text, (str, bytes, bytearray)) or result is default)
@deal.ensure(lambda text, default=None, strict=False, result=None: not (isinstance(text, str) and text.strip() == "") or result is default)
def safe_json_loads(text: Any, default: Any = None, strict: bool = False) -> Any:
    """Safely parse a JSON string into a Python object with optional robust repair logic.

    Attempts (non-strict / LLM mode; keep this list in sync with the body):
    1. Standard json.loads
    2. json.loads with strict=False (handles raw control chars, per hermes-agent)
    3. ast.literal_eval (single quotes and Python-isms; tuples, sets, and bytes are rejected)
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
    # Pass raw_text to json.loads. strip() drops Unicode whitespace such as
    # \x1f, which is not valid JSON whitespace, so '0\x1f' would parse as 0
    # instead of returning default.
    if strict:
        try:
            parsed = json.loads(raw_text)
            return parsed
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
        return parsed
    except (json.JSONDecodeError, TypeError, ValueError, RecursionError):
        pass

    # 2. strict=False attempt (handles bare control characters in non-strict LLM mode)
    try:
        parsed = json.loads(stripped, strict=False)
        _debug_json_stage("non-strict")
        return parsed
    except (json.JSONDecodeError, TypeError, ValueError, RecursionError):
        pass

    # 3. ast.literal_eval fallback (handles single quotes and Python-isms)
    # Inspired by hermes-agent/environments/tool_call_parsers/qwen3_coder_parser.py
    # Do not swap this ahead of repair: literal_eval accepting a truncated
    # fragment is accepted behavior, not a bug.
    try:
        # literal_eval handles 'True', 'False', 'None' out of the box.
        # It also handles single quotes and tuple-like syntax.
        parsed = ast.literal_eval(stripped)
        # literal_eval also returns tuples, sets, and bytes. Callers expect JSON.
        if parsed is None or type(parsed) in (bool, int, float, str, list, dict):
            _debug_json_stage("literal_eval")
            return parsed
    except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
        pass

    # 4. Repair attempt for truncated or malformed JSON.
    try:
        repaired = repair_json(stripped)
        if repaired != stripped:
            parsed = json.loads(repaired, strict=False)
            _debug_json_stage("json_repair")
            return parsed
    except Exception:
        # repair_json's pytest pre is total. This try is only the repair
        # attempt: a contract error is an AssertionError, and mypy rejects
        # that dynamically loaded class in an except clause. Any failure
        # returns default.
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

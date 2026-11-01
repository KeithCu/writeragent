# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Parse and rebuild ``=PY()`` / ``=PYTHON()`` formula strings for the Monaco cell editor.

Calc registers both English tokens (programmatic names ``py`` / ``python``). New formulas
use the shorter ``PY``; existing ``PYTHON`` cells keep their prefix when edited in place.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from plugin.framework.deal_shim import (
    DEAL_MAX_CELL_REF,
    DEAL_MAX_SHAPE_DIM,
    DEAL_MAX_SOURCE,
    DEAL_MAX_TOKEN,
    UNDER_CROSSHAIR,
    ascii_bounded,
    deal,
    inverse_ensure,
    str_bounded,
)
from plugin.framework.i18n import _

# Sheet/A1 range tokens. Space and ``"`` are product (``My Sheet.A1``, quoted sheets).
_RANGE_ADDR_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789.!:'$_ \"")

# Under pytest this is str_bounded (Unicode); under CrossHair it is ascii_bounded.
_DEAL_ASCII_STR = ascii_bounded if UNDER_CROSSHAIR else str_bounded


def _deal_range_addr_ok(s: object) -> bool:
    """Closed A1 / sheet-range domain for formatters and ``_deal_data_args_ok``."""
    return isinstance(s, str) and len(s) <= DEAL_MAX_TOKEN and all(c in _RANGE_ADDR_CHARS for c in s)


def _deal_data_args_ok(data_args: object) -> bool:
    """CrossHair domain for =PY() data-arg lists."""
    return (
        isinstance(data_args, list)
        and len(data_args) <= DEAL_MAX_SHAPE_DIM
        and all(_deal_range_addr_ok(x) for x in data_args)
    )


def _quoted_parse_result_ok(s: str, start: int, result: tuple[str, int] | None) -> bool:
    # crosshair: off
    """Postcondition check for double-quoted string parser."""
    if result is None:
        return True
    code, end = result
    return isinstance(code, str) and isinstance(end, int) and 0 <= start < end <= len(s)


def _parts_result_ok(result: Any) -> bool:
    # crosshair: off
    """Postcondition check for PythonFormulaParts parser."""
    if result is None:
        return True
    prefix = result.prefix
    code = result.code
    data_suffix = result.data_suffix
    return (
        isinstance(prefix, str)
        and prefix.endswith("(")
        and isinstance(code, str)
        and isinstance(data_suffix, str)
        and data_suffix.endswith(")")
    )

# Preferred display name for newly built formulas; PYTHON remains a backward-compatible alias.
CALC_PYTHON_FN = "PY"
# getFormula() stores add-ins as OriginalName (service.method), not the
# Function Wizard token. Without these, follow-ref save cannot parse live cells.
# Longest first so PYTHON is not parsed as PY + "THON".
_CALC_PYTHON_FN_ALIASES_BY_LEN = (
    "ORG.EXTENSION.WRITERAGENT.PYTHONFUNCTION.PYTHON",
    "ORG.EXTENSION.LIBREPY.PYTHONFUNCTION.PYTHON",
    "ORG.EXTENSION.WRITERAGENT.PYTHONFUNCTION.PY",
    "ORG.EXTENSION.LIBREPY.PYTHONFUNCTION.PY",
    "PYTHON",
    "PY",
)
_MAX_PY_ALIAS_LEN = max(len(alias) for alias in _CALC_PYTHON_FN_ALIASES_BY_LEN)
# Curly/smart quotes Calc sometimes stores in localized formulas.
_QUOTE_NORMALIZE = str.maketrans({"\u201c": '"', "\u201d": '"', "\u2018": "'", "\u2019": "'"})


def _match_py_alias(raw: str, *, require_equals: bool, aliases: tuple[str, ...]) -> int | None:
    # crosshair: off
    # Same prefix scan as py_call_open_end.
    i = 0
    n = len(raw)
    if require_equals:
        if i >= n or raw[i] != "=":
            return None
        i += 1
        while i < n and raw[i].isspace():
            i += 1
    head = raw[i : i + _MAX_PY_ALIAS_LEN].upper()
    alias = next((name for name in aliases if head.startswith(name)), None)
    if alias is None:
        return None
    i += len(alias)
    while i < n and raw[i].isspace():
        i += 1
    if i < n and raw[i] == "(":
        return i + 1
    return None


_STORED_ADDIN_ALIASES = tuple(a for a in _CALC_PYTHON_FN_ALIASES_BY_LEN if a.startswith("ORG.EXTENSION."))


def stored_addin_call_open_end(raw: str) -> int | None:
    # crosshair: off
    # Same prefix scan as py_call_open_end. Fully qualified OriginalNames only.
    return _match_py_alias(raw, require_equals=True, aliases=_STORED_ADDIN_ALIASES)


def py_call_open_end(raw: str, *, require_equals: bool) -> int | None:
    # crosshair: off
    """Return index after the opening ``(`` of a PY/PYTHON call, or None.

    CrossHair check of ``normalize_formula_string('PY')`` raised
    ``PatternError: missing ), unterminated subpattern at position 0`` at
    ``_PYTHON_NO_EQUALS_RE.match``. CPython does not: the compiled
    ``^(?:PY|PYTHON)\\s*\\(`` is a valid pattern. CrossHair's relib
    re-parses ``Pattern.pattern`` on every symbolic match and blows up on
    that alternation (same crash on ``parse_python_formula('PY')`` via
    normalize). Scan aliases with ``startswith`` instead of regex.
    """
    return _match_py_alias(raw, require_equals=require_equals, aliases=_CALC_PYTHON_FN_ALIASES_BY_LEN)


@dataclass(frozen=True)
class PythonFormulaParts:
    """Decomposed ``=PY(code; data…)`` or ``=PYTHON(code; data…)`` formula."""

    prefix: str  # e.g. "=PY(" or "=PYTHON("
    code: str
    data_suffix: str  # remainder after code arg, e.g. ";A1:B10)" or ")"



def _skip_quoted(s: str, i: int, quote: str) -> int | None:
    """Index after the closing quote, or None. ``i < 0`` must not wrap (CrossHair ``start=-1``)."""
    # crosshair: off
    if i < 0 or i >= len(s) or s[i] != quote:
        return None
    j = i + 1
    n = len(s)
    while j < n:
        if s[j] == quote:
            if j + 1 < n and s[j + 1] == quote:
                j += 2
                continue
            return j + 1
        j += 1
    return None


# Cap start to len(s): `start >= 0` alone lets CrossHair feed a giant int.
# Nested end-bounds ensure (~2m deep) is skipped under CrossHair; cheap post stays.
@deal.pre(lambda s, start: str_bounded(s, DEAL_MAX_SOURCE) and isinstance(start, int) and 0 <= start <= len(s))
@deal.post(lambda result: result is None or (isinstance(result, tuple) and len(result) == 2))
@inverse_ensure(lambda s, start, result: _quoted_parse_result_ok(s, start, result))
def _parse_quoted_string(s: str, start: int) -> tuple[str, int] | None:
    """Parse a Calc double-quoted string starting at *start* (must point to ``"``)."""
    end = _skip_quoted(s, start, '"')
    if end is None:
        return None
    return s[start + 1 : end - 1].replace('""', '"'), end


@deal.pre(lambda inner_body: _DEAL_ASCII_STR(inner_body, DEAL_MAX_SOURCE))
@deal.post(lambda result: result is None or (isinstance(result, str) and not result.startswith('"')))
def _parse_unquoted_code_arg(inner_body: str) -> str | None:
    """Parse ``=PY(sp.prime(100))`` when Calc omits string quotes around code."""
    s = inner_body.strip()
    if not s or s.startswith('"'):
        return None
    depth = 0
    i = 0
    while i < len(s):
        ch = s[i]
        if ch == '"':
            parsed = _parse_quoted_string(s, i)
            if parsed is None:
                return None
            i = parsed[1]
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            if depth == 0:
                return s[:i].strip()
            depth -= 1
        elif ch in (";", ",") and depth == 0:
            return s[:i].strip()
        i += 1
    return s


@deal.pre(lambda formula: str_bounded(formula, DEAL_MAX_SOURCE))
@deal.post(lambda result: isinstance(result, str))
def normalize_formula_string(formula: str) -> str:
    """Normalize LibreOffice ``getFormula()`` / ``FormulaLocal`` variants for parsing."""
    raw = (formula or "").strip()
    if raw.startswith("{") and raw.endswith("}"):
        raw = raw[1:-1].strip()
    if raw and not raw.startswith("=") and py_call_open_end(raw, require_equals=False) is not None:
        raw = "=" + raw
    start = py_call_open_end(raw, require_equals=True)
    if start is None:
        return raw.translate(_QUOTE_NORMALIZE)
    i = start
    while i < len(raw) and raw[i].isspace():
        i += 1
    # Only the code argument's delimiters are rewritten; curly quotes inside the
    # Python source (print(“hi”)) must survive a load→save round trip.
    if i < len(raw) and raw[i] == '"':
        parsed = _parse_quoted_string(raw, i)
        if parsed is not None:
            end = parsed[1]
            return raw[:i].translate(_QUOTE_NORMALIZE) + raw[i:end] + raw[end:].translate(_QUOTE_NORMALIZE)
    elif i < len(raw) and raw[i] == "\u201c":
        close = raw.rfind("\u201d", i + 1)
        if close > i:
            return raw[:i].translate(_QUOTE_NORMALIZE) + '"' + raw[i + 1 : close] + '"' + raw[close + 1 :].translate(_QUOTE_NORMALIZE)
    return raw.translate(_QUOTE_NORMALIZE)


def _split_data_binding_tokens(raw: str) -> list[str]:
    """Split on ``,``/``;`` outside single-quoted sheet names and double-quoted strings.

    Returns stripped tokens (empties included; the caller drops them).
    """
    tokens: list[str] = []
    start = 0
    i = 0
    while i < len(raw):
        ch = raw[i]
        if ch in ("'", '"'):
            end = _skip_quoted(raw, i, ch)
            if end is None:
                break
            i = end
            continue
        if ch in (",", ";"):
            tokens.append(raw[start:i])
            start = i + 1
        i += 1
    tokens.append(raw[start:])
    return [t.strip() for t in tokens]


def _python_call_parts(formula: str) -> tuple[str, str, str] | None:
    """Return ``(prefix, code, rest)`` for a PY/PYTHON call, or None.

    ``rest`` is the tail after the code argument. An invalid data suffix
    still returns the code; ``parse_python_formula`` rejects that tail.
    """
    # crosshair: off
    raw = normalize_formula_string(formula)
    inner_start = py_call_open_end(raw, require_equals=True)
    if inner_start is None:
        return None
    inner = raw[inner_start:]
    if not inner.endswith(")"):
        return None
    inner_body = inner[:-1].strip()
    prefix = raw[:inner_start]
    if not inner_body.startswith('"'):
        code = _parse_unquoted_code_arg(inner_body)
        if code is None:
            return None
        rest = inner_body[len(code) :].strip() if code != inner_body else ""
        return prefix, code, rest
    code_parsed = _parse_quoted_string(inner_body, 0)
    if code_parsed is None:
        return None
    code, end = code_parsed
    return prefix, code, inner_body[end:].strip()


def extract_python_code_loose(formula: str) -> str | None:
    """Best-effort code extraction from a PY/PYTHON-like formula string."""
    # crosshair: off
    parsed = _python_call_parts(formula)
    return None if parsed is None else parsed[1]


# Nested _parts_result_ok (py_call_open_end on prefix) hung deep check
# (~45m, 232k lines). Skip under CrossHair; cheap @deal.post still runs.
@deal.pre(lambda formula: str_bounded(formula, DEAL_MAX_SOURCE))
@deal.post(lambda result: result is None or isinstance(result, PythonFormulaParts))
@inverse_ensure(lambda formula, result: _parts_result_ok(result))
def parse_python_formula(formula: str) -> PythonFormulaParts | None:
    """Return code and data suffix if *formula* is a ``=PY()`` or ``=PYTHON()`` call."""
    parsed = _python_call_parts(formula)
    if parsed is None:
        return None
    prefix, code, rest = parsed
    if rest[:1] in (";", ","):
        data_suffix = rest + ")"
    elif rest == "":
        data_suffix = ")"
    else:
        return None
    return PythonFormulaParts(prefix=prefix, code=code, data_suffix=data_suffix)


@deal.pre(lambda code: str_bounded(code, DEAL_MAX_SOURCE + 256))
@deal.post(lambda result: isinstance(result, str))
@deal.ensure(lambda code, result: result == (code or "").replace('"', '""'))
def escape_code_for_formula(code: str) -> str:
    """Escape Python source for embedding in a Calc string literal by doubling quotes.

    ASCII-quoted Calc strings are opaque to the formula lexer, so hand-written
    code is preserved verbatim without unwanted rewrites.
    """
    # crosshair: off
    return (code or "").replace('"', '""')


def assemble_python_formula(prefix: str, token: str, suffix: str, *, quoted: bool) -> str:
    """Assemble a PY formula string from prefix, inner token (code/ref), and suffix."""
    if quoted:
        return f'{prefix}"{token}"{suffix}'
    return f"{prefix}{token}{suffix}"


# Keep parts.prefix. Hardcoding =PY( rewrites an existing =PYTHON( or
# =py( cell.
@deal.pre(lambda parts, new_code: isinstance(parts, PythonFormulaParts) and str_bounded(new_code, DEAL_MAX_SOURCE + 256))
@deal.post(lambda result: isinstance(result, str))
@deal.ensure(lambda parts, new_code, result: parts.data_suffix in result and result.startswith(f'{parts.prefix}"'))
def rebuild_python_formula(parts: PythonFormulaParts, new_code: str) -> str:
    """Rebuild a formula from parsed parts and new inline code (preserves prefix and ``data_suffix``)."""
    # Calls escape → quote doubling.
    # crosshair: off
    escaped = escape_code_for_formula(new_code)
    return assemble_python_formula(parts.prefix, escaped, parts.data_suffix, quoted=True)


# A1 / range display: closed alphabet (``\\x1c`` control whitespace is ASCII).
# Unicode strip+lstrip fixed-point under str_bounded read 1.36M CrossHair lines.
@deal.pre(lambda data_suffix: ascii_bounded(data_suffix, DEAL_MAX_SOURCE))
@deal.post(lambda result: isinstance(result, str))
@deal.ensure(lambda data_suffix, result: not result or (not result.startswith(";") and not result.startswith(",") and not result.endswith(")")))
def format_data_binding_display(data_suffix: str) -> str:
    """Human-readable range/index args from ``data_suffix`` (e.g. ``;A1:B10)`` → ``A1:B10``)."""
    # crosshair: off
    s = data_suffix or ""
    while True:
        prev = s
        s = s.strip().lstrip(";,").rstrip(")")
        if s == prev:
            break
    return s


# Editor textbox of A1 / range tokens; re.split on unbounded Unicode hung deep check.
@deal.pre(lambda text: ascii_bounded(text, DEAL_MAX_SOURCE))
@deal.post(lambda result: isinstance(result, list) and all(isinstance(x, str) for x in result))
def parse_data_binding_text(text: str) -> list[str]:
    """Parse editor textbox content into formula data arguments."""
    # crosshair: off
    raw = (text or "").strip()
    if not raw:
        return []
    if raw.startswith("[") and raw.endswith("]"):
        raw = raw[1:-1].strip()
    return [t for t in _split_data_binding_tokens(raw) if t]


@deal.pre(lambda *args, **kwargs: bool(args) and _deal_data_args_ok(args[0]))
@deal.post(lambda result: isinstance(result, str))
def format_data_binding_text(data_args: list[str]) -> str:
    # crosshair: off
    """Format data args for the editor textbox (comma-separated)."""
    cleaned = [stripped for arg in data_args if (stripped := arg.strip())]
    return ", ".join(cleaned)


@deal.pre(lambda c: isinstance(c, str) and len(c) == 1 and c.isascii())
def _is_ascii_letter(c: str) -> bool:
    return "A" <= c <= "Z" or "a" <= c <= "z"


@deal.pre(lambda s: _DEAL_ASCII_STR(s, DEAL_MAX_TOKEN))
def _is_a1_cell_prefix(s: str) -> bool:
    """Optional ``$`` + 1–3 letters + optional ``$`` + a digit.

    Replaces ``re.match(r'^\\$?[A-Z]{1,3}\\$?\\d', s, re.I)``. CrossHair's relib
    raises ``TypeError: ord() expected a character, but string of length 0``
    on NUL/control ASCII in that pattern.
    """
    i = 0
    n = len(s)
    if i < n and s[i] == "$":
        i += 1
    letters = 0
    while i < n and _is_ascii_letter(s[i]):
        letters += 1
        i += 1
    if letters == 0:
        return False
    # Max column letters in Calc / Excel is XFD (3 letters).
    if letters > 3:
        return False
    if i < n and s[i] == "$":
        i += 1
    return i < n and "0" <= s[i] <= "9"


@deal.pre(lambda s: _DEAL_ASCII_STR(s, DEAL_MAX_TOKEN))
def _is_sheet_identifier(s: str) -> bool:
    """``[A-Za-z_][A-Za-z0-9_]*`` without regex."""
    if not s:
        return False
    first = s[0]
    if not (first == "_" or _is_ascii_letter(first)):
        return False
    for c in s[1:]:
        if not (c == "_" or "0" <= c <= "9" or _is_ascii_letter(c)):
            return False
    return True


def _split_sheet_range(range_addr: str) -> tuple[str, str] | None:
    """Split a sheet-qualified range into (unquoted_or_escaped_sheet, rest), or None if not qualified.

    A quoted sheet may contain `.` and `!`; `''` inside quotes is one apostrophe;
    an unquoted left side that looks like an A1 cell (`A1.B5`, but not `Sheet1.A1`
    whose column letters exceed 3) is not a sheet
    on the dot form; this function does not call `split_sheet_prefix` (regex, drops
    `$`, rejects `O'Brien.A1`).
    """
    addr = str(range_addr).strip()
    if addr.startswith("'"):
        # Bugfix: partition on the first '.' turned "'My.Sheet'.A1:B2" into sheet "My"
        # and rest "Sheet'.A1:B2" because the dot inside the quotes was treated as the
        # separator. What was wrong: naive string partition split on dots or exclamation marks
        # inside quoted sheet names. How it happened: Calc allows sheet names with dots and spaces
        # enclosed in single quotes. Why this change fixes it: we scan quoted sheet names first,
        # accounting for doubled apostrophe escaping (''), and only split on the separator
        # following the closing quote.
        n = len(addr)
        end = _skip_quoted(addr, 0, "'")
        if end is not None:
            j = end
            while j < n and addr[j].isspace():
                j += 1
            if j < n and addr[j] in (".", "!"):
                j += 1
                while j < n and addr[j].isspace():
                    j += 1
                rest = addr[j:]
                if rest:
                    return addr[1 : end - 1], rest
            return None
    if "!" in addr:
        sheet, _unused, rest = addr.partition("!")
        return sheet.strip("'\""), rest
    if "." in addr:
        sheet, _unused, rest = addr.partition(".")
        if sheet and rest and not _is_a1_cell_prefix(sheet.replace("$", "")):
            return sheet.strip("'\""), rest
    return None


def _format_qualified_range(range_addr: str, *, separator: str) -> str:
    split = _split_sheet_range(range_addr)
    if split is None:
        return str(range_addr).strip()
    sheet, rest = split
    # Calc absolute-sheet '$' (e.g. $Sheet1) is intentionally preserved.
    clean_sheet = sheet.replace("$", "")
    needs_quotes = not _is_sheet_identifier(clean_sheet) if separator == "." else (bool(clean_sheet) and not _is_sheet_identifier(clean_sheet))
    if needs_quotes:
        s = sheet.replace("''", "'").replace("'", "''")
        return f"'{s}'{separator}{rest}"
    return f"{sheet}{separator}{rest}"


@deal.pre(lambda range_addr: _deal_range_addr_ok(range_addr))
@deal.post(lambda result: isinstance(result, str))
def format_py_data_range(range_addr: str) -> str:
    """Format a range for ``=PY()`` data args (quote sheet names with spaces/special chars)."""
    return _format_qualified_range(range_addr, separator=".")


@deal.pre(lambda range_addr: _deal_range_addr_ok(range_addr))
@deal.post(lambda result: isinstance(result, str))
def format_excel_data_range(range_addr: str) -> str:
    """Format a range for Excel OOXML ``=PY()`` data args (``Sheet!A1`` style)."""
    return _format_qualified_range(range_addr, separator="!")



# Defaulted kwargs: deal only forwards provided args + result= (see framework/formal-verification.md §8.1 A).
@deal.pre(lambda data_args, *_unused, **__: _deal_data_args_ok(data_args))
@deal.ensure(lambda *args, result=None, **kwargs: isinstance(result, str) and result.endswith(")"))
def build_data_suffix(data_args: list[str], *, separator: str = ";") -> str:
    """Build the ``data_suffix`` fragment from parsed range/index tokens.

    *separator* is ``;`` for Calc formulas and ``,`` for Excel OOXML formulas.
    """
    # crosshair: off
    fmt = format_excel_data_range if separator == "," else format_py_data_range
    args = [fmt(a.strip()) for a in data_args if a.strip()]
    if not args:
        return ")"
    return f"{separator}{separator.join(args)})"


# code is Python source (Unicode-legal); ascii_bounded would reject café comments.
@deal.pre(lambda code, data_args, *_unused, **__: str_bounded(code, DEAL_MAX_SOURCE + 256) and _deal_data_args_ok(data_args))
@deal.ensure(lambda *args, result=None, **kwargs: isinstance(result, str) and result.endswith(")"))
def rebuild_python_formula_with_data(code: str, data_args: list[str], *, parts: PythonFormulaParts | None = None, separator: str = ";") -> str:
    """Build ``=PY("…"; ranges…)`` from code and data arguments.

    Preserves ``parts.prefix`` when provided; otherwise defaults to ``={CALC_PYTHON_FN}(``.
    ``separator=","`` writes OOXML formulas (comma separators and ``Sheet!A1`` ranges).
    """
    # crosshair: off
    escaped = escape_code_for_formula(code)
    prefix = parts.prefix if parts is not None else f"={CALC_PYTHON_FN}("
    suffix = build_data_suffix(data_args, separator=separator)
    return assemble_python_formula(prefix, escaped, suffix, quoted=True)


def build_editor_formula_save(*, parsed_parts: PythonFormulaParts | None, new_code: str, cell_has_unparsed_python: bool, data_binding_text: str | None = None) -> str | dict[str, Any]:
    """Build ``=PY("…")`` for formula-mode save, or an error dict when args cannot be preserved."""
    if data_binding_text is not None:
        data_args = parse_data_binding_text(data_binding_text)
        return rebuild_python_formula_with_data(new_code, data_args, parts=parsed_parts)
    if parsed_parts is not None:
        return rebuild_python_formula(parsed_parts, new_code)
    if cell_has_unparsed_python:
        return {"type": "error", "message": _('Could not preserve this cell\'s PY formula arguments (e.g. data ranges). Edit the formula in Calc, or use a quoted code string like =PY("code"; A1:B10).')}
    return rebuild_python_formula_with_data(new_code, [])


def py_code_arg_is_cell_ref(code: str) -> bool:
    """True when ``=PY``'s first argument is a single cell address, not Python.

    ``=PY($A$1; data)`` / ``=PY(Sheet.A1)`` store source in that cell. Ranges
    (``A1:B10``) and unquoted Python (``sp.prime(100)``) return False.
    """
    # crosshair: off
    s = code.strip()
    if not s or ":" in s or "\n" in s:
        return False
    from plugin.calc.address_utils import parse_address, split_sheet_prefix

    _sheet, rest = split_sheet_prefix(s)
    bare = rest.replace("$", "").strip()
    if not bare:
        return False
    # Probe only: long =PY source must not trip parse_address @deal.pre (DEAL_MAX_CELL_REF).
    if not ascii_bounded(bare, DEAL_MAX_CELL_REF, min_len=1):
        return False
    try:
        parse_address(bare)
    except Exception:
        return False
    return True


def py_formula_has_unquoted_code_ref(formula: str) -> bool:
    """True when the formula is ``=PY($A$1; …)`` (unquoted), not ``=PY(\"A1\")``."""
    # crosshair: off
    parts = parse_python_formula(formula)
    if parts is None or not py_code_arg_is_cell_ref(parts.code):
        return False
    return not normalize_formula_string(formula)[len(parts.prefix) :].lstrip().startswith('"')


def rebuild_python_formula_with_code_ref(code_ref: str, data_args: list[str], *, separator: str = ";") -> str:
    """Build ``=PY(Sheet.A1; ranges…)`` with code taken from a cell (Excel script-bank shape).

    Avoids Calc ``MAXSTRLEN`` by keeping Python source out of the formula string.
    *code_ref* is a sheet-qualified address (``py_code_Pivots.A1`` or ``py_code_Pivots!A1``).
    """
    # crosshair: off
    fmt = format_excel_data_range if separator == "," else format_py_data_range
    ref = fmt(code_ref.strip())
    prefix = f"={CALC_PYTHON_FN}("
    suffix = build_data_suffix(data_args, separator=separator)
    return assemble_python_formula(prefix, ref, suffix, quoted=False)


def cell_looks_python_like(formula: str) -> bool:
    """True if *formula* appears to be a PY/PYTHON call (even if strict parse failed)."""
    # crosshair: off
    return extract_python_code_loose(formula) is not None

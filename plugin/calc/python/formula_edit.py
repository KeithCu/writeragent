# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Parse and rebuild ``=PY()`` / ``=PYTHON()`` formula strings for the Monaco cell editor.

Calc registers both English tokens (programmatic names ``py`` / ``python``). New formulas
use the shorter ``PY``; existing ``PYTHON`` cells keep their prefix when edited in place.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

from plugin.framework.deal_shim import (
    DEAL_MAX_SHAPE_DIM,
    DEAL_MAX_SOURCE,
    DEAL_MAX_TOKEN,
    UNDER_CROSSHAIR,
    ascii_bounded,
    deal,
    inverse_ensure,
    str_bounded,
)

# Preferred display name for newly built formulas; PYTHON remains a backward-compatible alias.
CALC_PYTHON_FN = "PY"
CALC_PYTHON_FN_ALIASES = ("PY", "PYTHON")
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
_MAX_PYTHON_ALIAS_LEN = max(len(a) for a in _CALC_PYTHON_FN_ALIASES_BY_LEN)
# Curly/smart quotes Calc sometimes stores in localized formulas.
_QUOTE_NORMALIZE = str.maketrans({"\u201c": '"', "\u201d": '"', "\u2018": "'", "\u2019": "'"})
# Sheet/A1 range tokens. Space and ``"`` are product (``My Sheet.A1``, quoted sheets).
_RANGE_ADDR_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789.!:'$_ \"")


def _py_call_open_end(raw: str, *, require_equals: bool) -> int | None:
    """Return index after the opening ``(`` of a PY/PYTHON call, or None.

    CrossHair check of ``normalize_formula_string('PY')`` raised
    ``PatternError: missing ), unterminated subpattern at position 0`` at
    ``_PYTHON_NO_EQUALS_RE.match``. CPython does not: the compiled
    ``^(?:PY|PYTHON)\\s*\\(`` is a valid pattern. CrossHair's relib
    re-parses ``Pattern.pattern`` on every symbolic match and blows up on
    that alternation (same crash on ``parse_python_formula('PY')`` via
    normalize). Scan aliases with ``startswith`` instead of regex.
    """
    i = 0
    n = len(raw)
    if require_equals:
        if i >= n or raw[i] != "=":
            return None
        i += 1
        while i < n and raw[i].isspace():
            i += 1
    head = raw[i : i + _MAX_PYTHON_ALIAS_LEN].upper()
    matched = 0
    for alias in _CALC_PYTHON_FN_ALIASES_BY_LEN:
        if head.startswith(alias):
            matched = len(alias)
            break
    if not matched:
        return None
    i += matched
    while i < n and raw[i].isspace():
        i += 1
    if i < n and raw[i] == "(":
        return i + 1
    return None


@dataclass(frozen=True)
class PythonFormulaParts:
    """Decomposed ``=PY(code; data…)`` or ``=PYTHON(code; data…)`` formula."""

    prefix: str  # e.g. "=PY(" or "=PYTHON("
    code: str
    data_suffix: str  # remainder after code arg, e.g. ";A1:B10)" or ")"


def _quoted_parse_result_ok(s: str, start: int, result: tuple[str, int] | None) -> bool:
    if result is None:
        return True
    code, end = result
    return isinstance(code, str) and isinstance(end, int) and 0 <= start < end <= len(s)


def _deal_range_addr_ok(s: object) -> bool:
    """Closed A1 / sheet-range domain for formatters and ``_deal_data_args_ok``.

    Product chars are ``A–Z a–z 0–9 . ! : ' $ _`` plus space / ``"`` so
    ``My Sheet.A1`` still quotes. Length is ``DEAL_MAX_TOKEN`` (16 CrossHair /
    64 pytest): ``Sheet!A1`` fits, ``DEAL_MAX_CELL_REF=4`` under CrossHair does
    not, and ``DEAL_MAX_SOURCE`` (8192 pytest / 16 CrossHair printable ASCII)
    was the 15:05 / 6:44 sink (check-all 32877875221).
    """
    return isinstance(s, str) and len(s) <= DEAL_MAX_TOKEN and all(c in _RANGE_ADDR_CHARS for c in s)


def _deal_data_args_ok(data_args: object) -> bool:
    """CrossHair domain for =PY() data-arg lists (pytest still allows DEAL_MAX_SHAPE_DIM).

    Items are A1 / range tokens (same alphabet as the formatters), not formula source.
    """
    return (
        isinstance(data_args, list)
        and len(data_args) <= DEAL_MAX_SHAPE_DIM
        and all(_deal_range_addr_ok(x) for x in data_args)
    )


def _parts_result_ok(result: PythonFormulaParts | None) -> bool:
    if result is None:
        return True
    return (
        isinstance(result, PythonFormulaParts)
        and isinstance(result.prefix, str)
        and bool(result.prefix)
        and _py_call_open_end(result.prefix, require_equals=True) == len(result.prefix)
        and isinstance(result.code, str)
        and isinstance(result.data_suffix, str)
        and result.data_suffix.endswith(")")
    )


# Cap start to len(s): `start >= 0` alone lets CrossHair feed a giant int.
# Nested end-bounds ensure (~2m deep) is skipped under CrossHair; cheap post stays.
@deal.pre(
    lambda s, start: str_bounded(s, DEAL_MAX_SOURCE)
    and isinstance(start, int)
    and 0 <= start <= len(s)
)
@deal.post(lambda result: result is None or (isinstance(result, tuple) and len(result) == 2))
@inverse_ensure(lambda s, start, result: _quoted_parse_result_ok(s, start, result))
def _parse_quoted_string(s: str, start: int) -> tuple[str, int] | None:
    """Parse a Calc double-quoted string starting at *start* (must point to ``"``)."""
    # Reject negatives too: `start >= len(s)` alone misses start=-1 (CrossHair counterexample).
    if start < 0 or start >= len(s) or s[start] != '"':
        return None
    i = start + 1
    chars: list[str] = []
    while i < len(s):
        ch = s[i]
        if ch == '"':
            if i + 1 < len(s) and s[i + 1] == '"':
                chars.append('"')
                i += 2
                continue
            return "".join(chars), i + 1
        chars.append(ch)
        i += 1
    return None


@deal.pre(lambda inner_body: str_bounded(inner_body, DEAL_MAX_SOURCE))
@deal.post(lambda result: result is None or (isinstance(result, str) and not result.startswith('"')))
def _parse_unquoted_code_arg(inner_body: str) -> str | None:
    """Parse ``=PY(sp.prime(100))`` when Calc omits string quotes around code."""
    s = inner_body.strip()
    if not s or s.startswith('"'):
        return None
    depth = 0
    for i, ch in enumerate(s):
        if ch == "(":
            depth += 1
        elif ch == ")":
            if depth == 0:
                return s[:i].strip()
            depth -= 1
        elif ch in (";", ",") and depth == 0:
            return s[:i].strip()
    return s


def _is_data_arg_separator(rest: str) -> bool:
    """True when *rest* begins a PY/PYTHON data-argument suffix (``;`` or ``,``)."""
    return bool(rest) and rest[0] in (";", ",")


def extract_python_code_loose(formula: str) -> str | None:
    """Best-effort code extraction from a PY/PYTHON-like formula string."""
    # crosshair: off
    # cover-all 33797534946 (~4.4h formula_edit module, 201 examples). Combinatoric loose parse. Doable later with tiny PY alphabet.
    parts = parse_python_formula(formula)
    if parts is not None:
        return parts.code
    raw = normalize_formula_string(formula)
    inner_start = _py_call_open_end(raw, require_equals=True)
    if inner_start is None:
        return None
    inner = raw[inner_start:]
    if not inner.endswith(")"):
        return None
    body = inner[:-1].strip()
    if body.startswith('"'):
        parsed = _parse_quoted_string(body, 0)
        return parsed[0] if parsed else None
    return _parse_unquoted_code_arg(body)


# Nested curly-quote membership hung deep check (~17m, 105k lines). Skip under
# CrossHair (import-time inverse_ensure no-op); cheap @deal.post still runs.
@deal.pre(lambda formula: str_bounded(formula, DEAL_MAX_SOURCE))
@deal.post(lambda result: isinstance(result, str))
@inverse_ensure(
    lambda formula, result: "\u201c" not in result
    and "\u201d" not in result
    and "\u2018" not in result
    and "\u2019" not in result
)
def normalize_formula_string(formula: str) -> str:
    """Normalize LibreOffice ``getFormula()`` / ``FormulaLocal`` variants for parsing."""
    raw = (formula or "").strip().translate(_QUOTE_NORMALIZE)
    if raw.startswith("{") and raw.endswith("}"):
        raw = raw[1:-1].strip()
    if raw and not raw.startswith("=") and _py_call_open_end(raw, require_equals=False) is not None:
        raw = "=" + raw
    return raw


def build_new_python_formula(code: str) -> str:
    """Build a fresh ``=PY("…")`` formula (single code argument, no data range)."""
    # crosshair: off
    # cover-all 33797534946 (~4.4h formula_edit module, 297 examples). Thin escape wrapper. Doable later with DEAL_MAX_SOURCE code alphabet.
    escaped = escape_code_for_formula(code)
    return f'={CALC_PYTHON_FN}("{escaped}")'


# Nested _parts_result_ok (_py_call_open_end on prefix) hung deep check
# (~45m, 232k lines). Skip under CrossHair; cheap @deal.post still runs.
@deal.pre(lambda formula: str_bounded(formula, DEAL_MAX_SOURCE))
@deal.post(lambda result: result is None or isinstance(result, PythonFormulaParts))
@inverse_ensure(lambda formula, result: _parts_result_ok(result))
def parse_python_formula(formula: str) -> PythonFormulaParts | None:
    """Return code and data suffix if *formula* is a ``=PY()`` or ``=PYTHON()`` call."""
    if not formula:
        return None
    raw = normalize_formula_string(formula)
    if not raw:
        return None
    inner_start = _py_call_open_end(raw, require_equals=True)
    if inner_start is None:
        return None
    if inner_start >= len(raw) or raw[inner_start - 1] != "(":
        return None
    inner = raw[inner_start:]
    if not inner.endswith(")"):
        return None
    inner_body = inner[:-1].strip()
    if not inner_body.startswith('"'):
        code = _parse_unquoted_code_arg(inner_body)
        if code is None:
            return None
        rest = ""
        if code != inner_body:
            rest = inner_body[len(code) :].strip()
        if _is_data_arg_separator(rest):
            data_suffix = rest + ")"
        elif rest == "":
            data_suffix = ")"
        else:
            return None
        return PythonFormulaParts(prefix=raw[:inner_start], code=code, data_suffix=data_suffix)

    code_parsed = _parse_quoted_string(inner_body, 0)
    if code_parsed is None:
        return None
    code, end = code_parsed
    rest = inner_body[end:].strip()
    if _is_data_arg_separator(rest):
        data_suffix = rest + ")"
    elif rest == "":
        data_suffix = ")"
    else:
        return None
    return PythonFormulaParts(prefix=raw[:inner_start], code=code, data_suffix=data_suffix)


# Defensive rewrites when *emitting* Calc ``=PY("…")`` formulas.
#
# Corrected diagnosis (2026-07): ASCII-quoted strings are already opaque in
# ScCompiler::NextSymbol (ssGetString). ``=PY("float(1)")`` does not #NAME? from
# scanning inside quotes. ``#NAME?`` happens for *unquoted* ``float(`` (unknown
# spreadsheet function). Real LO limits for long Excel-style Python are
# MAXSTRLEN (1024) → Err:513 and curly quotes → Err:508 — see
# docs/enabling_numpy_in_libreoffice.md#future-libreoffice-formula-string-work.
#
# TODO(libreoffice): one day raise/grow string-symbol limit and accept/normalize
# curly quotes in Calc core; then this sanitizer can be slimmed or removed.
# Until then we still rewrite float/int/str when building Calc formulas in case
# quotes are lost or tooling strips them.
_LEXER_COLLISION_FLOAT_RE = re.compile(r"\bfloat\s*\(")
_LEXER_COLLISION_INT_RE = re.compile(r"\bint\s*\(")
_LEXER_COLLISION_STR_RE = re.compile(r"\bstr\s*\(")
_LEXER_COLLISION_XL_TEXT_RE = re.compile(r"\.text\s*\(")


@deal.pre(lambda s, open_idx: str_bounded(s, DEAL_MAX_SOURCE) and isinstance(open_idx, int) and 0 <= open_idx < len(s))
@deal.post(lambda result: isinstance(result, int) and result >= -1)
@deal.ensure(
    lambda s, open_idx, result: result == -1
    or (0 <= open_idx <= result < len(s) and s[result] == ")")
)
def _find_matching_paren(s: str, open_idx: int) -> int:
    """Return index of ``)`` matching ``(`` at *open_idx*, or -1."""
    # Reject out-of-range open_idx: negatives index from the end (CrossHair '', -1).
    if open_idx < 0 or open_idx >= len(s):
        return -1
    depth = 0
    i = open_idx
    while i < len(s):
        ch = s[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


# Deep check hangs synthesizing rewrite_inner (Callable) + regex loop.
# Public wrappers that call this body (sanitize / escape / rebuild) are also
# CrossHair-off: cheap isinstance(result, str) post still wandered hours at
# DEAL_MAX_SOURCE=16 (parse_address-class). ``@deal`` stays for pytest.
# Callers pass hardcoded float/int/str; ``re.escape`` so a metacharacter token cannot
# PatternError. Body is separate so sanitize can call it after ``dtype=float`` grows
# past DEAL_MAX_SOURCE (CrossHair 16) without a nested PreconditionFailed.
def _rewrite_token_calls_body(code: str, token: str, rewrite_inner: Callable[[str], str]) -> str:
    """Replace ``token(inner)`` calls. *token* is escaped before compile."""
    # crosshair: off
    pattern = re.compile(rf"\b{re.escape(token)}\s*\(")
    out: list[str] = []
    pos = 0
    while True:
        m = pattern.search(code, pos)
        if not m:
            out.append(code[pos:])
            break
        out.append(code[pos : m.start()])
        open_paren = m.end() - 1
        close_paren = _find_matching_paren(code, open_paren)
        if close_paren < 0:
            out.append(code[m.start() :])
            break
        inner = code[open_paren + 1 : close_paren]
        out.append(rewrite_inner(inner))
        pos = close_paren + 1
    return "".join(out)


@deal.pre(
    lambda code, token, rewrite_inner: str_bounded(code, DEAL_MAX_SOURCE)
    and ascii_bounded(token, 32, min_len=1)
    and token.isalpha()
    and callable(rewrite_inner)
)
@deal.post(lambda result: isinstance(result, str))
def _rewrite_token_calls(code: str, token: str, rewrite_inner: Callable[[str], str]) -> str:  # pyright: ignore[reportUnusedFunction]
    """Deal-wrapped rewrite for pytest; sanitize/escape call ``_rewrite_token_calls_body``."""
    # crosshair: off
    return _rewrite_token_calls_body(code, token, rewrite_inner)


@deal.pre(lambda code: str_bounded(code, DEAL_MAX_SOURCE + 256))
@deal.post(lambda result: isinstance(result, str))
def sanitize_inline_py_code(code: str) -> str:
    """Defensive rewrite of tokens that are dangerous if formula quotes are lost.

    Not required for correct Calc parsing of ASCII-quoted ``=PY("float(…)")``
    (strings are opaque). Kept when *emitting* Calc formulas until LibreOffice
    raises ``MAXSTRLEN`` / curly-quote handling — see module comment above.
    """
    # Regex rewrite hang even at DEAL_MAX_SOURCE=16 (~3h13m on cheap str post).
    # crosshair: off
    if not code or UNDER_CROSSHAIR:
        return code
    sanitized = code.replace("dtype=float", "dtype=np.float64")
    sanitized = _LEXER_COLLISION_XL_TEXT_RE.sub(".fmt(", sanitized)
    # Body, not the deal-wrapped helper: ``dtype=float`` → ``dtype=np.float64``
    # grows +5, so a DEAL_MAX_SOURCE=16 input like ``dtype=float\\x00`` is still
    # a legal caller string but fails the nested ``str_bounded`` pre.
    sanitized = _rewrite_token_calls_body(sanitized, "float", lambda inner: f"({inner})+0.0")
    sanitized = _rewrite_token_calls_body(sanitized, "int", lambda inner: f"(({inner})//1)")
    sanitized = _rewrite_token_calls_body(sanitized, "str", lambda inner: f"calc.py_str({inner})")
    return sanitized


@deal.pre(lambda code: str_bounded(code, DEAL_MAX_SOURCE + 256))
@deal.post(lambda result: isinstance(result, list) and all(isinstance(x, str) for x in result))
def inline_py_code_has_lexer_collisions(code: str) -> list[str]:
    """Return token names still present that ``sanitize_inline_py_code`` would rewrite."""
    # crosshair: off
    # four regex searches on free strings (cover-all 33314946731: ~5.3h, 500k lines). Same rewrite-regex class as sanitize. Doable later with a tiny token alphabet.
    hits: list[str] = []
    if _LEXER_COLLISION_FLOAT_RE.search(code):
        hits.append("float")
    if _LEXER_COLLISION_INT_RE.search(code):
        hits.append("int")
    if _LEXER_COLLISION_STR_RE.search(code):
        hits.append("str")
    if _LEXER_COLLISION_XL_TEXT_RE.search(code):
        hits.append("calc.text")
    return hits


@deal.pre(lambda code: str_bounded(code, DEAL_MAX_SOURCE + 256))
@deal.post(lambda result: isinstance(result, str))
@deal.ensure(lambda code, result: result == sanitize_inline_py_code(code or "").replace('"', '""'))
def escape_code_for_formula(code: str) -> str:
    """Escape Python source for embedding in a Calc string literal.

    Applies defensive sanitization (``float(`` etc.) then doubles quotes.
    """
    # Same rewrite path as sanitize; deep check died here after ~1h35m.
    # crosshair: off
    return sanitize_inline_py_code(code).replace('"', '""')


def escape_code_for_excel_formula(code: str) -> str:
    """Quote-escape Python for Excel ``=PY("…")`` / OOXML — no Calc sanitizer rewrites."""
    # crosshair: off
    # cover-all 33797534946 (~4.4h formula_edit module, 270 examples). Free-string quote doubling. Doable later with DEAL_MAX_SOURCE alphabet.
    return (code or "").replace('"', '""')


@deal.pre(lambda parts, new_code: isinstance(parts, PythonFormulaParts) and str_bounded(new_code, DEAL_MAX_SOURCE + 256))
@deal.post(lambda result: isinstance(result, str) and result.startswith(f'={CALC_PYTHON_FN}("'))
@deal.ensure(lambda parts, new_code, result: parts.data_suffix in result)
def rebuild_python_formula(parts: PythonFormulaParts, new_code: str) -> str:
    """Rebuild a formula from parsed parts and new inline code (preserves ``data_suffix``)."""
    # Calls escape → sanitize → rewrite loop; same class as sanitize hang.
    # crosshair: off
    escaped = escape_code_for_formula(new_code)
    return f'={CALC_PYTHON_FN}("{escaped}"{parts.data_suffix}'


# A1 / range display: closed alphabet (``\\x1c`` control whitespace is ASCII).
# Unicode strip+lstrip fixed-point under str_bounded read 1.36M CrossHair lines.
@deal.pre(lambda data_suffix: ascii_bounded(data_suffix, DEAL_MAX_SOURCE))
@deal.post(lambda result: isinstance(result, str))
@deal.ensure(lambda data_suffix, result: not result or (not result.startswith(";") and not result.startswith(",") and not result.endswith(")")))
def format_data_binding_display(data_suffix: str) -> str:
    """Human-readable range/index args from ``data_suffix`` (e.g. ``;A1:B10)`` → ``A1:B10``)."""
    # Deep check-all run 32840960268: Prev 95:09, 2.26M lines on this while
    # strip/lstrip(';,').rstrip(')') fixed-point. ascii_bounded still allows
    # control ASCII; the engine kept exploding.
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
@deal.post(lambda result: isinstance(result, list) and all(isinstance(x, str) and '"' not in x for x in result))
def parse_data_binding_text(text: str) -> list[str]:
    """Parse editor textbox content into formula data arguments."""
    # Deep check-all run 32840960268: Prev 15:20 on re.split of the textbox.
    # Split is now replace+str.split; still off — dropping regex alone is not proven.
    # crosshair: off
    raw = (text or "").strip()
    if not raw:
        return []
    if raw.startswith("[") and raw.endswith("]"):
        raw = raw[1:-1].strip()
    parts = [p.strip() for p in raw.replace(";", ",").split(",") if p.strip()]
    return [p for p in parts if '"' not in p]


@deal.pre(lambda *args, **kwargs: bool(args) and _deal_data_args_ok(args[0]))
@deal.post(lambda result: isinstance(result, str))
def format_data_binding_text(data_args: list[str]) -> str:
    """Format data args for the editor textbox (comma-separated)."""
    cleaned = [a.strip() for a in data_args if a.strip()]
    return ", ".join(cleaned)


def _is_ascii_letter(c: str) -> bool:
    return "A" <= c <= "Z" or "a" <= c <= "z"


def _is_a1_cell_prefix(s: str) -> bool:
    """Optional ``$`` + letters + optional ``$`` + a digit.

    Replaces ``re.match(r'^\\$?[A-Z]+\\$?\\d', s, re.I)``. CrossHair's relib
    raises ``TypeError: ord() expected a character, but string of length 0``
    on NUL/control ASCII in that pattern (check-all run 32840960268).
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
    if i < n and s[i] == "$":
        i += 1
    return i < n and "0" <= s[i] <= "9"


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


def _has_whitespace(s: str) -> bool:
    return any(c.isspace() for c in s)


def _sheet_needs_excel_quotes(s: str) -> bool:
    """True when *s* has a non-``[A-Za-z0-9_]`` char or starts with a digit.

    Replaces ``re.search(r'[^\\w]', s)`` plus a leading-digit check.
    """
    if s[:1].isdigit():
        return True
    return any(not (c == "_" or "0" <= c <= "9" or _is_ascii_letter(c)) for c in s)


def _quote_py_sheet(sheet: str, rest: str) -> str:
    if _has_whitespace(sheet) or not _is_sheet_identifier(sheet):
        return f"'{sheet}'.{rest}"
    return f"{sheet}.{rest}"


def _format_py_data_range_body(range_addr: str) -> str:
    """Calc-style range quoting. No regex — relib TypeError on NUL in ``re.match``."""
    addr = str(range_addr).strip().replace("$", "")
    if "!" in addr:
        sheet, _unused, rest = addr.partition("!")
        sheet = sheet.strip("'\"")
        rest = rest.replace("$", "")
        return _quote_py_sheet(sheet, rest)
    if "." not in addr:
        return addr
    sheet, _unused, rest = addr.partition(".")
    if not sheet or not rest:
        return addr
    if _is_a1_cell_prefix(sheet):
        return addr
    if _has_whitespace(sheet) or not _is_sheet_identifier(sheet):
        quoted = sheet if sheet.startswith("'") else f"'{sheet}'"
        return f"{quoted}.{rest}"
    return f"{sheet}.{rest}"


def _format_excel_data_range_body(range_addr: str) -> str:
    """Excel ``Sheet!A1`` quoting. No regex — same relib TypeError class as PY format."""
    addr = str(range_addr).strip().replace("$", "")
    # Calc-style Sheet.A1 → Sheet!A1
    if "!" not in addr and "." in addr:
        sheet, _unused, rest = addr.partition(".")
        if sheet and rest and not _is_a1_cell_prefix(sheet):
            addr = f"{sheet}!{rest}"
    if "!" in addr:
        sheet, _unused, rest = addr.partition("!")
        sheet = sheet.strip("'\"")
        rest = rest.replace("$", "")
        if _sheet_needs_excel_quotes(sheet):
            return f"'{sheet}'!{rest}"
        return f"{sheet}!{rest}"
    return addr


# Closed range alphabet (not printable-ascii SOURCE): control-free junk through
# the sheet-quote parser was 15:05 / 6:44 after the NUL/ord fix (32877875221).
@deal.pre(lambda range_addr: _deal_range_addr_ok(range_addr))
@deal.post(lambda result: isinstance(result, str))
def format_py_data_range(range_addr: str) -> str:
    """Format a range for ``=PY()`` data args (quote sheet names with spaces/special chars)."""
    return _format_py_data_range_body(range_addr)


@deal.pre(lambda range_addr: _deal_range_addr_ok(range_addr))
@deal.post(lambda result: isinstance(result, str))
def format_excel_data_range(range_addr: str) -> str:
    """Format a range for Excel OOXML ``=PY()`` data args (``Sheet!A1`` style)."""
    return _format_excel_data_range_body(range_addr)


# Defaulted kwargs: deal only forwards provided args + result= (see framework/formal-verification.md §8.1 A).
@deal.pre(lambda data_args, *_unused, **__: _deal_data_args_ok(data_args))
@deal.post(lambda result: isinstance(result, str) and result.endswith(")"))
@deal.ensure(lambda *args, result=None, **kwargs: isinstance(result, str) and result.endswith(")"))
def build_data_suffix(data_args: list[str], *, separator: str = ";", excel_ranges: bool = False) -> str:
    """Build the ``data_suffix`` fragment from parsed range/index tokens.

    *separator* is ``;`` for Calc formulas and ``,`` for Excel OOXML formulas.
    """
    # crosshair: off
    # cover-all 33797534946 (~4.4h formula_edit module, 192 examples). Combinatoric list+formatter. Doable later with tiny data_args alphabet.
    # Call unwrapped bodies so a nested formatter pre cannot PreconditionFailed
    # if quoting grows the string (same class as #449 dtype=float growth).
    sep = separator if separator in (";", ",") else ";"
    fmt = _format_excel_data_range_body if excel_ranges or sep == "," else _format_py_data_range_body
    args = [fmt(a.strip()) for a in data_args if a.strip()]
    if not args:
        return ")"
    return f"{sep}{sep.join(args)})"


# code is Python source (Unicode-legal); ascii_bounded would reject café comments.
@deal.pre(
    lambda code, data_args, *_unused, **__: str_bounded(code, DEAL_MAX_SOURCE + 256)
    and _deal_data_args_ok(data_args)
)
@deal.post(lambda result: isinstance(result, str) and result.startswith(f"={CALC_PYTHON_FN}("))
@deal.ensure(lambda *args, result=None, **kwargs: isinstance(result, str) and result.endswith(")"))
def rebuild_python_formula_with_data(
    code: str,
    data_args: list[str],
    *,
    parts: PythonFormulaParts | None = None,
    separator: str = ";",
    excel_escape: bool = False,
) -> str:
    """Build ``=PY("…"; ranges…)`` from code and data arguments.

    Use ``separator=","`` and ``excel_escape=True`` when writing OOXML ``.xlsx``
    formulas so Excel/LibreOffice do not see Calc ``;`` separators or Calc-only
    source sanitization.
    """
    # Non-excel path calls escape → sanitize → rewrite loop.
    # crosshair: off
    escaped = escape_code_for_excel_formula(code) if excel_escape else escape_code_for_formula(code)
    prefix = f"={CALC_PYTHON_FN}("
    return f'{prefix}"{escaped}"{build_data_suffix(data_args, separator=separator, excel_ranges=excel_escape or separator == ",")}'


def py_code_arg_is_cell_ref(code: str) -> bool:
    """True when ``=PY``'s first argument is a single cell address, not Python.

    ``=PY($A$1; data)`` / ``=PY(Sheet.A1)`` store source in that cell. Ranges
    (``A1:B10``) and unquoted Python (``sp.prime(100)``) return False.
    """
    # crosshair: off
    # parse_address/split_sheet_prefix on free strings (cover-all 33314946731: ~14m, 22k lines). Doable later with the closed A1/_deal_range_addr_ok domain.
    if type(code) is not str:
        return False
    s = code.strip()
    if not s or ":" in s or "\n" in s:
        return False
    from plugin.calc.address_utils import parse_address, split_sheet_prefix

    _sheet, rest = split_sheet_prefix(s)
    bare = rest.replace("$", "").strip()
    if not bare:
        return False
    try:
        parse_address(bare)
    except ValueError:
        return False
    return True


def py_formula_has_unquoted_code_ref(formula: str) -> bool:
    """True when the formula is ``=PY($A$1; …)`` (unquoted), not ``=PY(\"A1\")``."""
    # crosshair: off
    # cover-all 33797534946 (~4.4h formula_edit module, 200 examples). Combinatoric parse+cell-ref. Doable later with closed A1 PY alphabet.
    parts = parse_python_formula(formula)
    if parts is None or not py_code_arg_is_cell_ref(parts.code):
        return False
    raw = normalize_formula_string(formula)
    start = _py_call_open_end(raw, require_equals=True)
    if start is None:
        return False
    body = raw[start:]
    if body.endswith(")"):
        body = body[:-1]
    return not body.strip().startswith('"')


def rebuild_python_formula_with_code_ref(
    code_ref: str,
    data_args: list[str],
    *,
    separator: str = ";",
    excel_ranges: bool = False,
) -> str:
    """Build ``=PY(Sheet.A1; ranges…)`` with code taken from a cell (Excel script-bank shape).

    Avoids Calc ``MAXSTRLEN`` by keeping Python source out of the formula string.
    *code_ref* is a sheet-qualified address (``py_code_Pivots.A1`` or ``py_code_Pivots!A1``).
    """
    # crosshair: off
    # cover-all 33797534946 (~4.4h formula_edit module, 126 examples). Combinatoric rebuild+formatter. Doable later with closed A1/data_args domain.
    use_excel = excel_ranges or separator == ","
    fmt = format_excel_data_range if use_excel else format_py_data_range
    ref = fmt(code_ref.strip())
    prefix = f"={CALC_PYTHON_FN}("
    return f"{prefix}{ref}{build_data_suffix(data_args, separator=separator, excel_ranges=use_excel)}"


def cell_looks_python_like(formula: str) -> bool:
    """True if *formula* appears to be a PY/PYTHON call (even if strict parse failed)."""
    # crosshair: off
    # cover-all 33797534946 (~4.4h formula_edit module, 201 examples). Thin parse/loose wrapper. Doable later with tiny PY alphabet.
    if not formula:
        return False
    if parse_python_formula(formula) is not None:
        return True
    return extract_python_code_loose(formula) is not None


def replace_python_code(formula: str, new_code: str) -> str | None:
    """Return a new formula with the first ``code`` string argument replaced."""
    # crosshair: off
    # cover-all 33797534946 (~4.4h formula_edit module, 136 examples). Thin parse+rebuild wrapper. Doable later with DEAL_MAX_SOURCE alphabet.
    parts = parse_python_formula(normalize_formula_string(formula))
    if parts is None:
        return None
    return rebuild_python_formula(parts, new_code)

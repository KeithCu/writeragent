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
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

from plugin.framework.deal_shim import (
    DEAL_MAX_CELL_REF,
    DEAL_MAX_SHAPE_DIM,
    DEAL_MAX_SOURCE,
    DEAL_MAX_TOKEN,
    ascii_bounded,
    deal,
    inverse_ensure,
    str_bounded,
)
from plugin.calc.address_utils import is_cell_token, split_sheet_prefix

# Sheet/A1 range tokens. Space and ``"`` are product (``My Sheet.A1``, quoted sheets).
_RANGE_ADDR_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789.!:'$_ \"")

# Under pytest this is str_bounded (Unicode); under CrossHair it is ascii_bounded.
_DEAL_ASCII_STR = str_bounded


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
    """Postcondition check for PyFormula parser."""
    if result is None:
        return True
    prefix = getattr(result, "prefix", None)
    code = getattr(result, "code", None)
    data_suffix = getattr(result, "data_suffix", None)
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
_PY_CALLS: tuple[tuple[str, str], ...] = (
    ("ORG.COLLABORAOFFICE.SHEET.ADDIN.PYTHONCOMPUTEFUNCTIONS.GETPYTHON", "getpython"),
    ("ORG.COLLABORAOFFICE.SHEET.ADDIN.PYTHONCOMPUTEFUNCTIONS.GETPY", "getpy"),
    ("ORG.EXTENSION.WRITERAGENT.PYTHONFUNCTION.PYTHON", "python"),
    ("ORG.EXTENSION.LIBREPY.PYTHONFUNCTION.PYTHON", "python"),
    ("ORG.EXTENSION.WRITERAGENT.PYTHONFUNCTION.PY", "py"),
    ("ORG.EXTENSION.LIBREPY.PYTHONFUNCTION.PY", "py"),
    ("PYTHON", "python"),
    ("PY", "py"),
)
_MAX_PY_ALIAS_LEN = max(len(alias) for alias, _ in _PY_CALLS)
# Curly/smart quotes Calc sometimes stores in localized formulas.
_QUOTE_NORMALIZE = str.maketrans({"\u201c": '"', "\u201d": '"', "\u2018": "'", "\u2019": "'"})


def _match_py_alias(raw: str, *, require_equals: bool) -> tuple[int, str] | None:
    # crosshair: off
    i = 0
    n = len(raw)
    if require_equals:
        if i >= n or raw[i] != "=":
            return None
        i += 1
        while i < n and raw[i].isspace():
            i += 1
    head = raw[i : i + _MAX_PY_ALIAS_LEN].upper()
    match = next(((name, spelling) for name, spelling in _PY_CALLS if head.startswith(name)), None)
    if match is None:
        return None
    alias, spelling = match
    i += len(alias)
    while i < n and raw[i].isspace():
        i += 1
    if i < n and raw[i] == "(":
        return i + 1, spelling
    return None


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
    if raw and not raw.startswith("=") and _match_py_alias(raw, require_equals=False) is not None:
        raw = "=" + raw
    matched = _match_py_alias(raw, require_equals=True)
    if matched is None:
        return raw.translate(_QUOTE_NORMALIZE)
    start = matched[0]
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


def _format_qualified_range(range_addr: str, *, separator: str) -> str:
    addr = str(range_addr).strip()
    sheet, rest = split_sheet_prefix(addr)
    if sheet is None and addr.startswith("!"):
        sheet, rest = "", addr[1:].strip()
    elif sheet is None and "!" in addr:
        sheet, _unused, rest = addr.partition("!")
        sheet = sheet.strip("'\"")
    elif sheet is None and "." in addr:
        cand_sheet, _unused, cand_rest = addr.partition(".")
        if cand_sheet and cand_rest and not is_cell_token(cand_sheet.replace("$", "")):
            sheet, rest = cand_sheet.strip("'\""), cand_rest
    if sheet is None or is_cell_token(sheet.replace("$", "")):
        return addr
    clean_sheet = sheet.replace("$", "")
    needs_quotes = not _is_sheet_identifier(clean_sheet) if separator == "." else (bool(clean_sheet) and not _is_sheet_identifier(clean_sheet))
    dollar = "$" if addr.startswith("$") else ""
    if needs_quotes:
        s = clean_sheet.replace("''", "'").replace("'", "''")
        return f"{dollar}'{s}'{separator}{rest}"
    return f"{dollar}{clean_sheet}{separator}{rest}"


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


def _data_range_formatter(separator: str) -> Callable[[str], str]:
    # crosshair: off
    return format_excel_data_range if separator == "," else format_py_data_range


# Defaulted kwargs: deal only forwards provided args + result= (see framework/formal-verification.md §8.1 A).
@deal.pre(lambda data_args, *_unused, **__: _deal_data_args_ok(data_args))
@deal.ensure(lambda *args, result=None, **kwargs: isinstance(result, str) and result.endswith(")"))
def build_data_suffix(data_args: list[str], *, separator: str = ";") -> str:
    """Build the ``data_suffix`` fragment from parsed range/index tokens.

    *separator* is ``;`` for Calc formulas and ``,`` for Excel OOXML formulas.
    """
    # crosshair: off
    fmt = _data_range_formatter(separator)
    args = [fmt(a.strip()) for a in data_args if a.strip()]
    if not args:
        return ")"
    return f"{separator}{separator.join(args)})"


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


@dataclass(frozen=True)
class PyFormula:
    """Decomposed ``=PY(code; data…)`` or ``=PYTHON(code; data…)`` formula."""

    prefix: str  # normalized text through "(", including case and spaces
    code: str
    code_is_ref: bool = False
    data_suffix: str = ")"
    spelling: str = "py"

    def data_args(self) -> list[str]:
        return parse_data_binding_text(format_data_binding_display(self.data_suffix))

    @staticmethod
    def open_end(raw: str, *, require_equals: bool) -> int | None:
        """Return index after the opening ``(`` of a PY/PYTHON call, or None."""
        # crosshair: off
        matched = _match_py_alias(raw, require_equals=require_equals)
        return None if matched is None else matched[0]

    @staticmethod
    @deal.pre(lambda formula: str_bounded(formula, DEAL_MAX_SOURCE))
    @deal.post(lambda result: result is None or isinstance(result, PyFormula))
    @inverse_ensure(lambda formula, result: _parts_result_ok(result))
    def parse(formula: str) -> PyFormula | None:
        raw = normalize_formula_string(formula)
        matched = _match_py_alias(raw, require_equals=True)
        if matched is None:
            return None
        inner_start, spelling = matched
        inner = raw[inner_start:]
        if not inner.endswith(")"):
            return None
        inner_body = inner[:-1].strip()
        prefix = raw[:inner_start]
        if not inner_body.startswith('"'):
            code = _parse_unquoted_code_arg(inner_body)
            if code is None:
                return None
            rest = inner_body[len(code) :].strip()
            code_is_ref = py_code_arg_is_cell_ref(code)
        else:
            code_parsed = _parse_quoted_string(inner_body, 0)
            if code_parsed is None:
                return None
            code, end = code_parsed
            rest = inner_body[end:].strip()
            code_is_ref = False

        if rest[:1] in (";", ","):
            data_suffix = rest + ")"
        elif rest == "":
            data_suffix = ")"
        else:
            return None
        return PyFormula(
            prefix=prefix,
            code=code,
            code_is_ref=code_is_ref,
            data_suffix=data_suffix,
            spelling=spelling,
        )

    @deal.pre(
        lambda self, code, *, data_args=None, **__: isinstance(self, PyFormula)
        and str_bounded(code, DEAL_MAX_SOURCE + 256)
        and (data_args is None or _deal_data_args_ok(data_args))
    )
    @deal.post(lambda result: isinstance(result, str))
    @deal.ensure(
        lambda self, code, *args, result=None, data_args=None, **kwargs: isinstance(result, str)
        and (data_args is not None or self.data_suffix in result)
        and result.startswith(f'{self.prefix}"')
    )
    def render(self, code: str, *, data_args: list[str] | None = None, separator: str = ";") -> str:
        # crosshair: off
        escaped = escape_code_for_formula(code)
        suffix = self.data_suffix if data_args is None else build_data_suffix(data_args, separator=separator)
        return assemble_python_formula(self.prefix, escaped, suffix, quoted=True)

    @staticmethod
    @deal.pre(
        lambda code, data_args=None, *args, **kw: str_bounded(code, DEAL_MAX_SOURCE + 256)
        and (data_args is None or _deal_data_args_ok(data_args))
    )
    @deal.ensure(lambda *args, result=None, **kwargs: isinstance(result, str) and result.endswith(")"))
    def compose(
        code: str,
        data_args: list[str] | None = None,
        *,
        prefix: str | None = None,
        code_is_ref: bool = False,
        separator: str = ";",
    ) -> str:
        # crosshair: off
        pfx = prefix if prefix is not None else f"={CALC_PYTHON_FN}("
        suffix = build_data_suffix([] if data_args is None else data_args, separator=separator)
        if code_is_ref:
            fmt = _data_range_formatter(separator)
            ref = fmt(code.strip())
            return assemble_python_formula(pfx, ref, suffix, quoted=False)
        escaped = escape_code_for_formula(code)
        return assemble_python_formula(pfx, escaped, suffix, quoted=True)


def build_editor_formula_save(
    *,
    parsed_parts: PyFormula | None,
    new_code: str,
    data_binding_text: str | None = None,
) -> str:
    """Build ``=PY("…")`` for formula-mode save."""
    if data_binding_text is not None:
        data_args = parse_data_binding_text(data_binding_text)
        if parsed_parts is not None:
            return parsed_parts.render(new_code, data_args=data_args)
        return PyFormula.compose(new_code, data_args)
    if parsed_parts is not None:
        return parsed_parts.render(new_code)
    return PyFormula.compose(new_code, [])

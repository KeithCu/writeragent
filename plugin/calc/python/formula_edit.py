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

# Preferred display name for newly built formulas; PYTHON remains a backward-compatible alias.
CALC_PYTHON_FN = "PY"
CALC_PYTHON_FN_ALIASES = ("PY", "PYTHON")
_CALC_PYTHON_FN_PATTERN = "|".join(CALC_PYTHON_FN_ALIASES)
_PYTHON_HEAD_RE = re.compile(rf"^=\s*(?:{_CALC_PYTHON_FN_PATTERN})\s*\(", re.IGNORECASE)
_PYTHON_NO_EQUALS_RE = re.compile(rf"^(?:{_CALC_PYTHON_FN_PATTERN})\s*\(", re.IGNORECASE)
# Curly/smart quotes Calc sometimes stores in localized formulas.
_QUOTE_NORMALIZE = str.maketrans({"\u201c": '"', "\u201d": '"', "\u2018": "'", "\u2019": "'"})


@dataclass(frozen=True)
class PythonFormulaParts:
    """Decomposed ``=PY(code; data…)`` or ``=PYTHON(code; data…)`` formula."""

    prefix: str  # e.g. "=PY(" or "=PYTHON("
    code: str
    data_suffix: str  # remainder after code arg, e.g. ";A1:B10)" or ")"


def _parse_quoted_string(s: str, start: int) -> tuple[str, int] | None:
    """Parse a Calc double-quoted string starting at *start* (must point to ``"``)."""
    if start >= len(s) or s[start] != '"':
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
    parts = parse_python_formula(formula)
    if parts is not None:
        return parts.code
    raw = normalize_formula_string(formula)
    m = _PYTHON_HEAD_RE.match(raw)
    if not m:
        return None
    inner = raw[m.end() :]
    if not inner.endswith(")"):
        return None
    body = inner[:-1].strip()
    if body.startswith('"'):
        parsed = _parse_quoted_string(body, 0)
        return parsed[0] if parsed else None
    return _parse_unquoted_code_arg(body)


def normalize_formula_string(formula: str) -> str:
    """Normalize LibreOffice ``getFormula()`` / ``FormulaLocal`` variants for parsing."""
    raw = (formula or "").strip().translate(_QUOTE_NORMALIZE)
    if raw.startswith("{") and raw.endswith("}"):
        raw = raw[1:-1].strip()
    if raw and not raw.startswith("=") and _PYTHON_NO_EQUALS_RE.match(raw):
        raw = "=" + raw
    return raw


def build_new_python_formula(code: str) -> str:
    """Build a fresh ``=PY("…")`` formula (single code argument, no data range)."""
    escaped = escape_code_for_formula(code)
    return f'={CALC_PYTHON_FN}("{escaped}")'


def parse_python_formula(formula: str) -> PythonFormulaParts | None:
    """Return code and data suffix if *formula* is a ``=PY()`` or ``=PYTHON()`` call."""
    if not formula:
        return None
    raw = normalize_formula_string(formula)
    if not raw:
        return None
    m = _PYTHON_HEAD_RE.match(raw)
    if not m:
        return None
    inner_start = m.end()
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


# Calc's formula lexer scans inside PY code strings and treats these as spreadsheet calls.
_LEXER_COLLISION_FLOAT_RE = re.compile(r"\bfloat\s*\(")
_LEXER_COLLISION_INT_RE = re.compile(r"\bint\s*\(")
_LEXER_COLLISION_STR_RE = re.compile(r"\bstr\s*\(")
_LEXER_COLLISION_XL_TEXT_RE = re.compile(r"\.text\s*\(")


def _find_matching_paren(s: str, open_idx: int) -> int:
    """Return index of ``)`` matching ``(`` at *open_idx*, or -1."""
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


def _rewrite_token_calls(code: str, token: str, rewrite_inner) -> str:
    """Replace ``token(inner)`` calls; *token* must not contain regex metacharacters."""
    pattern = re.compile(rf"\b{token}\s*\(")
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


def sanitize_inline_py_code(code: str) -> str:
    """Rewrite tokens that trigger Calc ``#NAME?`` inside ``=PY("…")`` code strings."""
    if not code:
        return code
    sanitized = code.replace("dtype=float", "dtype=np.float64")
    sanitized = _LEXER_COLLISION_XL_TEXT_RE.sub(".fmt(", sanitized)
    sanitized = _rewrite_token_calls(sanitized, "float", lambda inner: f"({inner})+0.0")
    sanitized = _rewrite_token_calls(sanitized, "int", lambda inner: f"(({inner})//1)")
    sanitized = _rewrite_token_calls(sanitized, "str", lambda inner: f"xl.py_str({inner})")
    return sanitized


def inline_py_code_has_lexer_collisions(code: str) -> list[str]:
    """Return Calc-lexer collision token names still present in *code*."""
    hits: list[str] = []
    if _LEXER_COLLISION_FLOAT_RE.search(code):
        hits.append("float")
    if _LEXER_COLLISION_INT_RE.search(code):
        hits.append("int")
    if _LEXER_COLLISION_STR_RE.search(code):
        hits.append("str")
    if _LEXER_COLLISION_XL_TEXT_RE.search(code):
        hits.append("xl.text")
    return hits


def escape_code_for_formula(code: str) -> str:
    """Escape Python source for embedding in a Calc string literal."""
    return sanitize_inline_py_code(code).replace('"', '""')


def rebuild_python_formula(parts: PythonFormulaParts, new_code: str) -> str:
    """Rebuild a formula from parsed parts and new inline code (preserves ``data_suffix``)."""
    escaped = escape_code_for_formula(new_code)
    return f'{parts.prefix}"{escaped}"{parts.data_suffix}'


def format_data_binding_display(data_suffix: str) -> str:
    """Human-readable range/index args from ``data_suffix`` (e.g. ``;A1:B10)`` → ``A1:B10``)."""
    s = (data_suffix or "").strip()
    if s in (")", ""):
        return ""
    if s.startswith(";") or s.startswith(","):
        s = s[1:]
    if s.endswith(")"):
        s = s[:-1]
    return s.strip()


def parse_data_binding_text(text: str) -> list[str]:
    """Parse editor textbox content into formula data arguments."""
    raw = (text or "").strip()
    if not raw:
        return []
    if raw.startswith("[") and raw.endswith("]"):
        raw = raw[1:-1].strip()
    parts = [p.strip() for p in re.split(r"[,;]", raw) if p.strip()]
    return [p for p in parts if '"' not in p]


def format_data_binding_text(data_args: list[str]) -> str:
    """Format data args for the editor textbox (comma-separated)."""
    cleaned = [a.strip() for a in data_args if a.strip()]
    return ", ".join(cleaned)


def format_py_data_range(range_addr: str) -> str:
    """Format a range for ``=PY()`` data args (quote sheet names with spaces/special chars)."""
    addr = str(range_addr).strip().replace("$", "")
    if "!" in addr:
        sheet, _, rest = addr.partition("!")
        sheet = sheet.strip("'\"")
        rest = rest.replace("$", "")
        if re.search(r"\s", sheet) or not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", sheet):
            return f"'{sheet}'.{rest}"
        return f"{sheet}.{rest}"
    if "." not in addr:
        return addr
    sheet, _, rest = addr.partition(".")
    if not sheet or not rest:
        return addr
    if re.match(r"^\$?[A-Z]+\$?\d", sheet, re.IGNORECASE):
        return addr
    if re.search(r"\s", sheet) or not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", sheet):
        quoted = sheet if sheet.startswith("'") else f"'{sheet}'"
        return f"{quoted}.{rest}"
    return f"{sheet}.{rest}"


def build_data_suffix(data_args: list[str]) -> str:
    """Build the ``data_suffix`` fragment from parsed range/index tokens."""
    args = [format_py_data_range(a.strip()) for a in data_args if a.strip()]
    if not args:
        return ")"
    return f';{";".join(args)})'


def rebuild_python_formula_with_data(
    code: str,
    data_args: list[str],
    *,
    parts: PythonFormulaParts | None = None,
) -> str:
    """Build ``=PY("…"; ranges…)`` from code and data arguments."""
    escaped = escape_code_for_formula(code)
    prefix = parts.prefix if parts is not None else f"={CALC_PYTHON_FN}("
    return f'{prefix}"{escaped}"{build_data_suffix(data_args)}'


def cell_looks_python_like(formula: str) -> bool:
    """True if *formula* appears to be a PY/PYTHON call (even if strict parse failed)."""
    if not formula:
        return False
    if parse_python_formula(formula) is not None:
        return True
    return extract_python_code_loose(formula) is not None


def replace_python_code(formula: str, new_code: str) -> str | None:
    """Return a new formula with the first ``code`` string argument replaced."""
    parts = parse_python_formula(normalize_formula_string(formula))
    if parts is None:
        return None
    return rebuild_python_formula(parts, new_code)

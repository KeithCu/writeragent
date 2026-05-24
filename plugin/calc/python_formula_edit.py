# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Parse and rebuild ``=PYTHON()`` formula strings for the Monaco cell editor.

The add-in function name is always the English token ``PYTHON`` (programmatic name
``python``). Calc must not store a localized alias in ``getFormula()`` / ``FormulaLocal``;
if it does, treat that as a bug in the add-in registration, not something this module
should accept or translate.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_PYTHON_HEAD_RE = re.compile(r"^=\s*PYTHON\s*\(", re.IGNORECASE)
_PYTHON_NO_EQUALS_RE = re.compile(r"^PYTHON\s*\(", re.IGNORECASE)
# Curly/smart quotes Calc sometimes stores in localized formulas.
_QUOTE_NORMALIZE = str.maketrans({"\u201c": '"', "\u201d": '"', "\u2018": "'", "\u2019": "'"})


@dataclass(frozen=True)
class PythonFormulaParts:
    """Decomposed ``=PYTHON(code; data…)`` formula."""

    prefix: str  # e.g. "=PYTHON("
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
    """Parse ``=PYTHON(sp.prime(100))`` when Calc omits string quotes around code."""
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
        elif ch == ";" and depth == 0:
            return s[:i].strip()
    return s


def extract_python_code_loose(formula: str) -> str | None:
    """Best-effort code extraction from a PYTHON-like formula string."""
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
    """Build a fresh ``=PYTHON("…")`` formula (single code argument, no data range)."""
    escaped = escape_code_for_formula(code)
    return f'=PYTHON("{escaped}")'


def parse_python_formula(formula: str) -> PythonFormulaParts | None:
    """Return code and data suffix if *formula* is a ``=PYTHON()`` call."""
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
        if rest.startswith(";"):
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
    if rest.startswith(";"):
        data_suffix = rest + ")"
    elif rest == "":
        data_suffix = ")"
    else:
        return None
    return PythonFormulaParts(prefix=raw[:inner_start], code=code, data_suffix=data_suffix)


def escape_code_for_formula(code: str) -> str:
    """Escape Python source for embedding in a Calc string literal."""
    return code.replace('"', '""')


def rebuild_python_formula(parts: PythonFormulaParts, new_code: str) -> str:
    """Rebuild a formula from parsed parts and new inline code (preserves ``data_suffix``)."""
    escaped = escape_code_for_formula(new_code)
    return f'{parts.prefix}"{escaped}"{parts.data_suffix}'


def format_data_binding_display(data_suffix: str) -> str:
    """Human-readable range/index args from ``data_suffix`` (e.g. ``;A1:B10)`` → ``A1:B10``)."""
    s = (data_suffix or "").strip()
    if s in (")", ""):
        return ""
    if s.startswith(";"):
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


def build_data_suffix(data_args: list[str]) -> str:
    """Build the ``data_suffix`` fragment from parsed range/index tokens."""
    args = [a.strip() for a in data_args if a.strip()]
    if not args:
        return ")"
    return f';{";".join(args)})'


def rebuild_python_formula_with_data(
    code: str,
    data_args: list[str],
    *,
    parts: PythonFormulaParts | None = None,
) -> str:
    """Build ``=PYTHON("…"; ranges…)`` from code and data arguments."""
    escaped = escape_code_for_formula(code)
    prefix = parts.prefix if parts is not None else "=PYTHON("
    return f'{prefix}"{escaped}"{build_data_suffix(data_args)}'


def cell_looks_python_like(formula: str) -> bool:
    """True if *formula* appears to be a PYTHON call (even if strict parse failed)."""
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

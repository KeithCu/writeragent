# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Normalize LibreOffice formula strings for Excel-oriented parsers."""

from __future__ import annotations

from plugin.calc.python.formula_edit import normalize_formula_string
from plugin.framework.deal_shim import DEAL_MAX_SOURCE, str_bounded, deal, inverse_ensure


def _no_unquoted_semicolon(s: str) -> bool:
    """True when every ``;`` in *s* sits inside a Calc double-quoted string (or none exist)."""
    in_quote = False
    i = 0
    while i < len(s):
        ch = s[i]
        if ch == '"':
            if in_quote and i + 1 < len(s) and s[i + 1] == '"':
                i += 2
                continue
            in_quote = not in_quote
            i += 1
            continue
        if ch == ";" and not in_quote:
            return False
        i += 1
    return True


# Deep check-all run 32840960268: two nested ensures (curly-quote scan +
# _no_unquoted_semicolon) cost ~7+7+6 min. Skip under CrossHair; cheap str post
# and the implementation loop stay.
@deal.pre(lambda formula: str_bounded(formula, DEAL_MAX_SOURCE))
@deal.post(lambda result: isinstance(result, str))
@inverse_ensure(lambda formula, result: "\u201c" not in result and "\u201d" not in result)
@inverse_ensure(lambda formula, result: _no_unquoted_semicolon(result))
def normalize_lo_formula_for_parse(formula: str) -> str:
    """Map LO ``;`` argument separators to ``,`` for parse-only backends.

    Only replaces ``;`` outside double-quoted strings. Array literals ``{=…}``
    braces are not special-cased in v1 (rare in P1 corpus).
    """
    raw = normalize_formula_string(formula)
    if not raw:
        return raw

    out: list[str] = []
    i = 0
    in_quote = False
    while i < len(raw):
        ch = raw[i]
        if ch == '"':
            if in_quote and i + 1 < len(raw) and raw[i + 1] == '"':
                out.append('""')
                i += 2
                continue
            in_quote = not in_quote
            out.append(ch)
            i += 1
            continue
        if ch == ";" and not in_quote:
            out.append(",")
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)

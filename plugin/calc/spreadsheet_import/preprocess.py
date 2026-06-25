# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Normalize LibreOffice formula strings for Excel-oriented parsers."""

from __future__ import annotations

from plugin.calc.python.formula_edit import normalize_formula_string


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

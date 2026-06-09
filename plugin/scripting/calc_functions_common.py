# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared calc_functions constants — importable without numpy on the LO host."""
from __future__ import annotations

HELPER_NAMES = frozenset(
    {
        "iferror",
        "ifna",
        "match_criteria",
        "sumif",
        "sumifs",
        "countif",
        "countifs",
        "averageif",
        "averageifs",
        "xlookup",
        "lookup",
        "xmatch",
        "textjoin",
        "regex",
        "text",
        "eomonth",
        "networkdays",
        "edate",
        "datedif",
        "weekday",
        "weeknum",
        "workday",
        "isblank",
        "isnumber",
        "isna",
        "iserror",
        "istext",
        "islogical",
        "iserr",
        "isnontext",
        "subtotal",
        "sumproduct",
        "averagea",
        "quartile",
        "rank",
        "large",
        "small",
        "mode",
        "even",
        "odd",
        "filter",
        "sort",
        "unique",
        "sortby",
    }
)

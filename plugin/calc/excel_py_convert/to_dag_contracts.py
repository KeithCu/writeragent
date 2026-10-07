# SPDX-License-Identifier: GPL-3.0-or-later
"""Contracts and verification domains for Excel/Calc PY DAG translation."""

from __future__ import annotations

from plugin.framework.deal_shim import (
    DEAL_MAX_CELL_REF,
    DEAL_MAX_CMD_ARGS,
    DEAL_MAX_SOURCE,
    UNDER_CROSSHAIR,
    ascii_bounded,
    str_bounded,
)

__all__ = [
    "_AST_OFFSET_CHARS",
    "_AST_OFFSET_MAX_COL",
    "_AST_OFFSET_MAX_LINENO",
    "_AST_OFFSET_MAX_SRC",
    "_CONVERT_DEP_A1_DIGIT",
    "_CONVERT_DEP_A1_LETTER",
    "_DEAL_BINDING_A1_LEN",
    "_DEAL_CONVERT_LIST",
    "_DEAL_CONVERT_STR",
    "_DEAL_NOTE_LEN",
    "_DEAL_RESOLVED_LEN",
    "_DEAL_REWRITE_SRC",
    "_EXCEL_PLACEHOLDER_CHARS",
    "_RESOLVED_KINDS",
    "_deal_ast_offset_src_ok",
    "_deal_ast_offset_src_ok_crosshair",
    "_deal_ast_offset_src_ok_pytest",
    "_deal_convert_cell_ok",
    "_deal_convert_dep_ok",
    "_deal_convert_dep_ok_crosshair",
    "_deal_convert_dep_ok_pytest",
    "_deal_convert_scripts_ok",
    "_deal_excel_src_ok",
    "_deal_excel_src_ok_crosshair",
    "_deal_excel_src_ok_pytest",
    "_deal_note_ok",
]

# Identifier / placeholder / xl() alphabet. Pytest keeps Unicode ``str_bounded``
# so real Excel scripts stay legal; CrossHair's unrestricted Unicode of length 16
# is how regular cover synthesized ~193 junk examples of ``_normalize_excel_placeholders``.
_EXCEL_PLACEHOLDER_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789%_#'\" \t\n\\()=,.:+-[]{}")
# Tiny rewrite walk + convert lists. Defined before _deal_excel_src_ok so the
# CrossHair src pre cannot exceed _skip_string's str_bounded(_DEAL_REWRITE_SRC).
_DEAL_CONVERT_LIST = 1 if UNDER_CROSSHAIR else DEAL_MAX_CMD_ARGS
# CrossHair convert deps must be real A1 (letter+digit). Len-1 junk like ``M`` /
# ``_`` resolves unresolved with a long note, then nested-fails ``_normalize_bindings``
# (``_DEAL_NOTE_LEN=1``; check-all 33954468213). Pytest keeps DEAL_MAX_SOURCE.
_DEAL_CONVERT_STR = 2 if UNDER_CROSSHAIR else DEAL_MAX_SOURCE
_DEAL_REWRITE_SRC = 1 if UNDER_CROSSHAIR else DEAL_MAX_SOURCE


def _deal_excel_src_ok_pytest(src: object) -> bool:
    return str_bounded(src, DEAL_MAX_SOURCE)


def _deal_excel_src_ok_crosshair(src: object) -> bool:
    # Must not exceed _skip_string's str_bounded(_DEAL_REWRITE_SRC): CrossHair
    # used to pass len-2 src such as '\t"' into _find_xl_calls, then
    # _normalize_excel_placeholders called _skip_string and PreconditionFailed.
    return isinstance(src, str) and str_bounded(src, _DEAL_REWRITE_SRC) and all(c in _EXCEL_PLACEHOLDER_CHARS for c in src)


# Import-time only — do not branch inside ``@deal.pre`` lambdas.
_deal_excel_src_ok = _deal_excel_src_ok_crosshair if UNDER_CROSSHAIR else _deal_excel_src_ok_pytest
# ast_source_offset lineno: CrossHair uses 4 so SMT stays tiny. Pytest must
# accept real multiline ``xl(`` (AST ``end_lineno`` can exceed 4). Cap at
# DEAL_MAX_SOURCE — a ``str_bounded`` script cannot have more lines than chars.
_AST_OFFSET_MAX_LINENO = 2 if UNDER_CROSSHAIR else DEAL_MAX_SOURCE
_AST_OFFSET_MAX_SRC = 2 if UNDER_CROSSHAIR else DEAL_MAX_SOURCE
_AST_OFFSET_MAX_COL = 2 if UNDER_CROSSHAIR else DEAL_MAX_SOURCE
# Tiny alphabet: 33127995861 1.05M lines at SOURCE=16; 33180040863 still ~44m at len=4.
_AST_OFFSET_CHARS = frozenset("AB \n")
_DEAL_BINDING_A1_LEN = DEAL_MAX_CELL_REF if UNDER_CROSSHAIR else DEAL_MAX_SOURCE
_DEAL_RESOLVED_LEN = 1 if UNDER_CROSSHAIR else DEAL_MAX_CMD_ARGS
# Note/convert still multi-10m at len 4 (33211730747); floor to 1.
_DEAL_NOTE_LEN = 1 if UNDER_CROSSHAIR else DEAL_MAX_SOURCE
# Pytest notes include Unicode (e.g. ``ANCHORARRAY(A6) → A6:B254``); CrossHair is ascii-only.
_deal_note_ok = ascii_bounded if UNDER_CROSSHAIR else str_bounded
_RESOLVED_KINDS = frozenset(("range", "unresolved", "table_snapshot", "anchor_snapshot"))


def _deal_ast_offset_src_ok_pytest(src: object) -> bool:
    return str_bounded(src, DEAL_MAX_SOURCE)


def _deal_ast_offset_src_ok_crosshair(src: object) -> bool:
    return isinstance(src, str) and len(src) <= _AST_OFFSET_MAX_SRC and all(c in _AST_OFFSET_CHARS for c in src)


_deal_ast_offset_src_ok = _deal_ast_offset_src_ok_crosshair if UNDER_CROSSHAIR else _deal_ast_offset_src_ok_pytest


def _deal_convert_scripts_ok(model: object) -> bool:
    # Match rewrite_excel_code / convert_cell_to_dag nested pre: str_bounded alone
    # let CrossHair build scripts=['\x00'] then PreconditionFailed inside
    # (check-all 33935176527). Pytest _deal_excel_src_ok is still str_bounded.
    scripts = getattr(model, "scripts", None)
    return isinstance(scripts, list) and len(scripts) <= _DEAL_CONVERT_LIST and all(_deal_excel_src_ok(s) for s in scripts)


# CrossHair convert deps are exact two-char A1 (``A1``…``Z9``). Broader
# ``ascii_bounded`` / A1-ish alphabets allowed ``deps=['_']`` (relib PatternError,
# check-all 33940151004) and ``deps=['M']`` (unresolved long note → nested
# ``_normalize_bindings`` PreconditionFailed on ``_DEAL_NOTE_LEN=1``, check-all
# 33954468213). Pytest keeps ascii_bounded so ``_xlfn.ANCHORARRAY`` /
# ``Table[#All]`` stay legal. Different hole from #599 (NUL *scripts*).
_CONVERT_DEP_A1_LETTER = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
_CONVERT_DEP_A1_DIGIT = frozenset("0123456789")


def _deal_convert_dep_ok_pytest(dep: object) -> bool:
    return isinstance(dep, str) and ascii_bounded(dep, _DEAL_CONVERT_STR)


def _deal_convert_dep_ok_crosshair(dep: object) -> bool:
    # Exact A1 so resolve_dep returns kind=range with empty note (passes the
    # off'd ``_normalize_bindings`` note bound without widening that pre).
    return isinstance(dep, str) and len(dep) == _DEAL_CONVERT_STR and dep[0] in _CONVERT_DEP_A1_LETTER and dep[1] in _CONVERT_DEP_A1_DIGIT


_deal_convert_dep_ok = _deal_convert_dep_ok_crosshair if UNDER_CROSSHAIR else _deal_convert_dep_ok_pytest


def _deal_convert_cell_ok(cell: object) -> bool:
    """Cell fields ``convert_cell_to_dag`` requires; script_index range is body-checked."""
    script_index = getattr(cell, "script_index", None)
    deps = getattr(cell, "deps", None)
    return type(script_index) is int and isinstance(deps, list) and len(deps) <= _DEAL_CONVERT_LIST and all(_deal_convert_dep_ok(d) for d in deps)

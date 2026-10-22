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
# Identifier / placeholder / xl() alphabet. Pytest keeps Unicode ``str_bounded``
# so real Excel scripts stay legal; CrossHair's unrestricted Unicode of length 16
# is how regular cover synthesized ~193 junk examples of ``_normalize_excel_placeholders``.
_EXCEL_PLACEHOLDER_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789%_#'\" \t\n\\()=,.:+-[]{}")
# Tiny rewrite walk + convert lists. Defined before _deal_excel_src_ok so the
# CrossHair src pre cannot exceed _skip_string's str_bounded(_DEAL_REWRITE_SRC).
_DEAL_CONVERT_LIST = 1 if UNDER_CROSSHAIR else DEAL_MAX_CMD_ARGS
# CrossHair convert deps must be real A1 (letter+digit). Len-1 junk like ``M`` /
# ``_`` resolves unresolved with a long note, then nested-fails ``_normalize_bindings``
# (``_DEAL_NOTE_LEN=1``). Pytest keeps DEAL_MAX_SOURCE.
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
_DEAL_BINDING_A1_LEN = DEAL_MAX_CELL_REF if UNDER_CROSSHAIR else DEAL_MAX_SOURCE
_DEAL_RESOLVED_LEN = 1 if UNDER_CROSSHAIR else DEAL_MAX_CMD_ARGS
# Note/convert floor to 1 for CrossHair.
_DEAL_NOTE_LEN = 1 if UNDER_CROSSHAIR else DEAL_MAX_SOURCE
# Pytest notes include Unicode (e.g. ``ANCHORARRAY(A6) → A6:B254``); CrossHair is ascii-only.
_deal_note_ok = ascii_bounded if UNDER_CROSSHAIR else str_bounded
_RESOLVED_KINDS = frozenset(("range", "unresolved", "table_snapshot", "anchor_snapshot"))


def _deal_convert_scripts_ok(model: object) -> bool:  # pyright: ignore[reportUnusedFunction]
    # Match rewrite_excel_code / convert_cell_to_dag nested pre: str_bounded alone
    # let CrossHair build scripts=['\x00'] then PreconditionFailed inside.
    # Pytest _deal_excel_src_ok is still str_bounded.
    scripts = getattr(model, "scripts", None)
    return isinstance(scripts, list) and len(scripts) <= _DEAL_CONVERT_LIST and all(_deal_excel_src_ok(s) for s in scripts)


# CrossHair convert deps are exact two-char A1 (``A1``…``Z9``). Broader
# ``ascii_bounded`` / A1-ish alphabets allowed ``deps=['_']`` (relib PatternError)
# and ``deps=['M']`` (unresolved long note → nested ``_normalize_bindings``
# PreconditionFailed on ``_DEAL_NOTE_LEN=1``). Pytest keeps ascii_bounded so
# ``_xlfn.ANCHORARRAY`` / ``Table[#All]`` stay legal. Different hole from #599 (NUL *scripts*).
_CONVERT_DEP_A1_LETTER = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
_CONVERT_DEP_A1_DIGIT = frozenset("0123456789")


def _deal_convert_dep_ok_pytest(dep: object) -> bool:
    return isinstance(dep, str) and ascii_bounded(dep, _DEAL_CONVERT_STR)


def _deal_convert_dep_ok_crosshair(dep: object) -> bool:
    # Exact A1 so resolve_dep returns kind=range with empty note (passes the
    # off'd ``_normalize_bindings`` note bound without widening that pre).
    return isinstance(dep, str) and len(dep) == _DEAL_CONVERT_STR and dep[0] in _CONVERT_DEP_A1_LETTER and dep[1] in _CONVERT_DEP_A1_DIGIT


_deal_convert_dep_ok = _deal_convert_dep_ok_crosshair if UNDER_CROSSHAIR else _deal_convert_dep_ok_pytest


def _deal_convert_cell_ok(cell: object) -> bool:  # pyright: ignore[reportUnusedFunction]
    """Cell fields ``convert_cell_to_dag`` requires; script_index range is body-checked."""
    script_index = getattr(cell, "script_index", None)
    deps = getattr(cell, "deps", None)
    return type(script_index) is int and isinstance(deps, list) and len(deps) <= _DEAL_CONVERT_LIST and all(_deal_convert_dep_ok(d) for d in deps)

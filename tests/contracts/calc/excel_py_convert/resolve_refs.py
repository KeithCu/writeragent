"""Contracts sidecar for plugin.calc.excel_py_convert.resolve_refs."""

from __future__ import annotations

import deal

from plugin.calc.excel_py_convert import resolve_refs as _prod
from tests.contracts.bounds import DEAL_MAX_SOURCE, str_bounded

_lookup_anchor = deal.pre(
    lambda model, anchor, sheet_hint="": str_bounded(anchor, DEAL_MAX_SOURCE)
    and str_bounded(sheet_hint, DEAL_MAX_SOURCE)
)(_prod._lookup_anchor)

resolve_dep = deal.pre(
    lambda dep, model, sheet_hint="": str_bounded(dep, DEAL_MAX_SOURCE)
    and str_bounded(sheet_hint, DEAL_MAX_SOURCE)
)(_prod.resolve_dep)

PYTEST = [
    "_lookup_anchor",
    "resolve_dep",
]

TARGETS = [
    f"{__name__}._lookup_anchor",
]

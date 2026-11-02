# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Contract sidecar for plugin.scripting.calc_range."""

from __future__ import annotations

from typing import Any

import deal

from plugin.scripting import calc_range as _prod
from tests.contracts.bounds import (
    DEAL_MAX_SHAPE_DIM,
    DEAL_MAX_TOKEN,
    UNDER_CROSSHAIR,
    ascii_bounded,
    profile,
    str_bounded,
)

_DEAL_GRID_DIM = 1 if UNDER_CROSSHAIR else DEAL_MAX_SHAPE_DIM
_DEAL_CELL_INT_ABS = 0 if UNDER_CROSSHAIR else 8
_DEAL_CELL_STR_LEN = 1 if UNDER_CROSSHAIR else 4
_DEAL_COL_NAME_LEN = _DEAL_CELL_STR_LEN if UNDER_CROSSHAIR else DEAL_MAX_TOKEN

_deal_inner_grid_cell_ok = profile(
    lambda c: c is None
    or (
        isinstance(c, (int, float, str, bool))
        and not (isinstance(c, float) and (c != c or c in (float("inf"), float("-inf"))))
    ),
    lambda c: c is None
    or (-_DEAL_CELL_INT_ABS <= c <= _DEAL_CELL_INT_ABS if isinstance(c, int) else False)
    or (ascii_bounded(c, _DEAL_CELL_STR_LEN) if isinstance(c, str) else False)
    or c is False,
)


def _deal_grid_values_ok(values: object) -> bool:
    if not isinstance(values, list) or len(values) > _DEAL_GRID_DIM:
        return False
    return all(
        isinstance(row, list)
        and len(row) <= _DEAL_GRID_DIM
        and all(_deal_inner_grid_cell_ok(c) for c in row)
        for row in values
    )


def _deal_calc_range_other_ok_crosshair(other: object) -> bool:
    if other is None:
        return True
    if isinstance(other, int):
        return -_DEAL_CELL_INT_ABS <= other <= _DEAL_CELL_INT_ABS
    if isinstance(other, str):
        return ascii_bounded(other, _DEAL_CELL_STR_LEN)
    return isinstance(other, _prod.CalcRange) and _deal_grid_values_ok(other._values)


def _deal_calc_range_other_ok_pytest(other: object) -> bool:
    if isinstance(other, (int, float, str, bool)):
        return True
    return isinstance(other, _prod.CalcRange) and _deal_grid_values_ok(other._values)


_deal_calc_range_other_ok = profile(
    _deal_calc_range_other_ok_crosshair,
    _deal_calc_range_other_ok_pytest,
)

_deal_binary_op_pre = profile(
    lambda self, other: _deal_grid_values_ok(self._values) and _deal_calc_range_other_ok(other)
)


def _deal_json_list_of_grids_arg_ok_crosshair(obj: object) -> bool:
    if not isinstance(obj, (list, tuple)) or len(obj) > _DEAL_GRID_DIM:
        return False
    for item in obj:
        if not isinstance(item, (list, tuple)) or len(item) > _DEAL_GRID_DIM:
            return False
        for row in item:
            if isinstance(row, (list, tuple)):
                if len(row) > _DEAL_GRID_DIM:
                    return False
                if not all(_deal_inner_grid_cell_ok(c) for c in row):
                    return False
            elif not _deal_inner_grid_cell_ok(row):
                return False
    return True


_deal_json_list_of_grids_arg_ok = profile(_deal_json_list_of_grids_arg_ok_crosshair)


def _deal_materialize_inner_ok_crosshair(inner: object) -> bool:
    if not isinstance(inner, (list, tuple)):
        return False
    rows = list(inner)
    return len(rows) <= _DEAL_GRID_DIM and all(
        isinstance(r, (list, tuple))
        and len(r) <= _DEAL_GRID_DIM
        and all(_deal_inner_grid_cell_ok(c) for c in r)
        for r in rows
    )


_deal_materialize_inner_ok = profile(_deal_materialize_inner_ok_crosshair)


def _deal_labeled_grid_ok_crosshair(
    columns: object,
    data: object = None,
    **__: Any,
) -> bool:
    if not isinstance(columns, list) or len(columns) > _DEAL_GRID_DIM:
        return False
    if not all(str_bounded(c, _DEAL_COL_NAME_LEN) for c in columns):
        return False
    if data is not None:
        if isinstance(data, list):
            if len(data) > _DEAL_GRID_DIM:
                return False
            for row in data:
                if isinstance(row, list):
                    if len(row) > _DEAL_GRID_DIM:
                        return False
                    if not all(_deal_inner_grid_cell_ok(c) for c in row):
                        return False
                elif not _deal_inner_grid_cell_ok(row):
                    return False
        elif not isinstance(data, dict):
            return False
    return True


_deal_labeled_grid_ok = profile(_deal_labeled_grid_ok_crosshair)

ensure_rectangular_2d = deal.post(lambda result: isinstance(result, list))(
    _prod.ensure_rectangular_2d
)

pack_calc_range_envelope = deal.post(
    lambda result: isinstance(result, dict) and _prod.is_calc_range_payload(result)
)(_prod.pack_calc_range_envelope)

_materialize_inner_grid = deal.pre(lambda inner: _deal_materialize_inner_ok(inner))(
    _prod._materialize_inner_grid
)

_is_json_list_of_grids = deal.pre(lambda obj: _deal_json_list_of_grids_arg_ok(obj))(
    _prod._is_json_list_of_grids
)

dataframe_to_labeled_grid = deal.pre(
    lambda columns, data=None, **__: _deal_labeled_grid_ok(columns, data)
)(_prod.dataframe_to_labeled_grid)

PYTEST = [
    "ensure_rectangular_2d",
    "pack_calc_range_envelope",
    "_materialize_inner_grid",
    "_is_json_list_of_grids",
    "dataframe_to_labeled_grid",
]
TARGETS: list[str] = []

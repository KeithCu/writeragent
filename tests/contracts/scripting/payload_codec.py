# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Contract sidecar for plugin.scripting.payload_codec."""

from __future__ import annotations

import array
from typing import Any

import deal

from plugin.scripting import payload_codec as _prod
from plugin.scripting.payload_codec import (
    BINARY_MIN_CELLS,
    PAYLOAD_CALC_RANGE,
    PAYLOAD_DATAFRAME,
    PAYLOAD_IMAGE,
    PAYLOAD_MULTI_DATA,
    PAYLOAD_SPLIT_GRID,
    SPLIT_GRID_WIRE_DTYPE,
    _FLOAT64_BYTES,
    _HOST_UNPACK_ERRORS,
    _is_ndarray,
    _is_split_grid_envelope,
    _nonneg_shape_dim,
)
from tests.contracts.bounds import (
    DEAL_MAX_COL_INDEX,
    DEAL_MAX_ROW_INDEX,
    DEAL_MAX_SHAPE_DIM,
    DEAL_MAX_SHAPE_RANK,
    inverse_ensure,
    profile,
)

_DEAL_RETURN = object()


def _deal_return(*args: Any, result: Any = _DEAL_RETURN, **_kwargs: Any) -> Any:
    if result is not _DEAL_RETURN:
        return result
    return args[-1] if args else None


def _deal_product_grid_ok(grid: object) -> bool:
    if not isinstance(grid, list) or len(grid) > DEAL_MAX_SHAPE_DIM:
        return False
    ncols: int | None = None
    for row in grid:
        if not isinstance(row, list) or len(row) > DEAL_MAX_COL_INDEX:
            return False
        if ncols is None:
            ncols = len(row)
        elif len(row) != ncols:
            return False
    return True


def _deal_wire_dict_ok_crosshair(envelope: object) -> bool:
    if not isinstance(envelope, dict) or len(envelope) > DEAL_MAX_SHAPE_DIM:
        return False
    return not isinstance(envelope.get("shape"), list) or len(envelope["shape"]) <= DEAL_MAX_SHAPE_RANK


_deal_wire_dict_ok = profile(_deal_wire_dict_ok_crosshair)

_SHAPE_DIM_CAP = profile(DEAL_MAX_SHAPE_DIM, DEAL_MAX_ROW_INDEX + 1)


def _deal_shape_ok(shape: object) -> bool:
    return (
        isinstance(shape, (list, tuple))
        and len(shape) <= DEAL_MAX_SHAPE_RANK
        and all(isinstance(d, int) and 0 <= d <= _SHAPE_DIM_CAP for d in shape)
    )


def _deal_force_min_cells_ok(force: object, min_cells: object) -> bool:
    return (
        force in ("auto", "always", "never")
        and isinstance(min_cells, int)
        and 0 <= min_cells <= _SHAPE_DIM_CAP
    )


def _deal_host_unpack_wire_ok_crosshair(wire: object) -> bool:
    return (
        wire is None
        or isinstance(wire, (int, float, str, bool, bytes, bytearray, list, dict, tuple))
        or _is_ndarray(wire)
    )


_deal_host_unpack_wire_ok = profile(_deal_host_unpack_wire_ok_crosshair)


def _deal_child_unpack_wire_ok_crosshair(wire: object) -> bool:
    return (
        wire is None
        or isinstance(wire, (int, float, str, bool, bytes, bytearray, list, dict, tuple))
        or _is_ndarray(wire)
    )


_deal_child_unpack_wire_ok = profile(_deal_child_unpack_wire_ok_crosshair)

is_multi_data = _prod.is_multi_data
is_multi_data = (inverse_ensure(
    lambda obj, result: not result
    or (
        isinstance(obj, dict)
        and obj.get("__wa_payload__") == PAYLOAD_MULTI_DATA
        and isinstance(obj.get("items"), list)
    )
))(is_multi_data)
is_multi_data = (deal.post(lambda result: isinstance(result, bool)))(is_multi_data)
is_multi_data = (deal.pre(lambda obj: _deal_wire_dict_ok(obj)))(is_multi_data)

is_image_payload = _prod.is_image_payload
is_image_payload = (inverse_ensure(
    lambda obj, result: not result
    or (
        isinstance(obj, dict)
        and obj.get("__wa_payload__") == PAYLOAD_IMAGE
        and isinstance(obj.get("data"), bytes)
        and isinstance(obj.get("format"), str)
    )
))(is_image_payload)
is_image_payload = (deal.post(lambda result: isinstance(result, bool)))(is_image_payload)
is_image_payload = (deal.pre(lambda obj: _deal_wire_dict_ok(obj)))(is_image_payload)

is_dataframe_payload = _prod.is_dataframe_payload
is_dataframe_payload = (inverse_ensure(
    lambda obj, result: not result
    or (
        isinstance(obj, dict)
        and obj.get("__wa_payload__") == PAYLOAD_DATAFRAME
        and isinstance(obj.get("columns"), list)
        and "data" in obj
    )
))(is_dataframe_payload)
is_dataframe_payload = (deal.post(lambda result: isinstance(result, bool)))(is_dataframe_payload)
is_dataframe_payload = (deal.pre(lambda obj: _deal_wire_dict_ok(obj)))(is_dataframe_payload)

is_calc_range_payload = _prod.is_calc_range_payload
is_calc_range_payload = (inverse_ensure(
    lambda obj, result: not result
    or (
        isinstance(obj, dict)
        and obj.get("__wa_payload__") == PAYLOAD_CALC_RANGE
        and isinstance(obj.get("shape"), list)
        and len(obj["shape"]) == 2
        and all(_nonneg_shape_dim(d) for d in obj["shape"])
        and "data" in obj
    )
))(is_calc_range_payload)
is_calc_range_payload = (deal.post(lambda result: isinstance(result, bool)))(is_calc_range_payload)
is_calc_range_payload = (deal.pre(lambda obj: _deal_wire_dict_ok(obj)))(is_calc_range_payload)

_is_any_payload_envelope = _prod._is_any_payload_envelope
_is_any_payload_envelope = (deal.post(lambda result: isinstance(result, bool)))(_is_any_payload_envelope)
_is_any_payload_envelope = (deal.pre(lambda obj: _deal_wire_dict_ok(obj)))(_is_any_payload_envelope)

envelope_column_kinds = _prod.envelope_column_kinds
envelope_column_kinds = (deal.pre(
    lambda envelope, *_unused, ncols=0, **__: _deal_wire_dict_ok(envelope)
    and isinstance(ncols, int)
    # Same Calc column cap as ``_deal_product_grid_ok`` (pack). SHAPE_DIM (256)
    # rejected a wide sheet after pack succeeded: PreContractError on unpack.
    # Release strips deal, so the body already accepts this width; the pre must too.
    and 0 <= ncols <= DEAL_MAX_COL_INDEX + 1
))(envelope_column_kinds)

cell_count = _prod.cell_count
cell_count = (deal.ensure(lambda shape, *a, result=_DEAL_RETURN, **k: len(shape) != 0 or _deal_return(*a, result=result) == 1))(cell_count)
cell_count = (deal.ensure(lambda shape, *a, result=_DEAL_RETURN, **k: _deal_return(*a, result=result) >= 0))(cell_count)
cell_count = (deal.post(lambda *a, result=_DEAL_RETURN, **k: isinstance(_deal_return(*a, result=result), int)))(cell_count)
cell_count = (deal.pre(lambda shape: _deal_shape_ok(shape)))(cell_count)

should_use_binary_envelope = _prod.should_use_binary_envelope
should_use_binary_envelope = (deal.ensure(lambda *a, result=_DEAL_RETURN, force="auto", **k: force != "never" or _deal_return(*a, result=result) is False))(should_use_binary_envelope)
should_use_binary_envelope = (deal.ensure(lambda *a, result=_DEAL_RETURN, force="auto", **k: force != "always" or _deal_return(*a, result=result) is True))(should_use_binary_envelope)
should_use_binary_envelope = (deal.post(lambda *a, result=_DEAL_RETURN, **k: isinstance(_deal_return(*a, result=result), bool)))(should_use_binary_envelope)
should_use_binary_envelope = (deal.pre(
    lambda shape, *_unused, min_cells=BINARY_MIN_CELLS, force="auto", **__: _deal_force_min_cells_ok(
        min_cells, force
    )
))(should_use_binary_envelope)
should_use_binary_envelope = (deal.pre(lambda shape, *_unused, **__: _deal_shape_ok(shape)))(should_use_binary_envelope)

is_numeric_grid = _prod.is_numeric_grid
is_numeric_grid = (deal.ensure(lambda grid, *a, result=_DEAL_RETURN, **k: len(grid) > 0 or _deal_return(*a, result=result) is True))(is_numeric_grid)
is_numeric_grid = (deal.post(lambda *a, result=_DEAL_RETURN, **k: isinstance(_deal_return(*a, result=result), bool)))(is_numeric_grid)
is_numeric_grid = (deal.pre(lambda grid: isinstance(grid, list) and _deal_product_grid_ok(grid)))(is_numeric_grid)

wire_cell_count = _prod.wire_cell_count
wire_cell_count = (deal.raises(ValueError))(wire_cell_count)
wire_cell_count = (deal.ensure(lambda data, *a, result=_DEAL_RETURN, **k: data is not None or _deal_return(*a, result=result) == 0))(wire_cell_count)
wire_cell_count = (deal.post(lambda *a, result=_DEAL_RETURN, **k: isinstance(_deal_return(*a, result=result), int) and _deal_return(*a, result=result) >= 0))(wire_cell_count)

grid_from_nested_list = _prod.grid_from_nested_list
grid_from_nested_list = (deal.post(lambda result: isinstance(result, list)))(grid_from_nested_list)
grid_from_nested_list = (deal.pre(lambda grid: isinstance(grid, list) and _deal_product_grid_ok(grid)))(grid_from_nested_list)

_flatten_grid_to_components = _prod._flatten_grid_to_components
_flatten_grid_to_components = (deal.raises(ValueError))(_flatten_grid_to_components)
_flatten_grid_to_components = (deal.ensure(lambda grid, *a, result=_DEAL_RETURN, **k: (r := _deal_return(*a, result=result)) is not None and ((not grid) or len(r[0]) == (r[3][0] * r[3][1] if len(r[3]) == 2 else r[3][0]))))(_flatten_grid_to_components)
_flatten_grid_to_components = (deal.ensure(lambda grid, *a, result=_DEAL_RETURN, **k: all(kind in ("int", "float", "bool") for kind in _deal_return(*a, result=result)[2])))(_flatten_grid_to_components)
_flatten_grid_to_components = (deal.ensure(lambda grid, *a, result=_DEAL_RETURN, **k: all(isinstance(v, str) for v in _deal_return(*a, result=result)[1].values())))(_flatten_grid_to_components)
_flatten_grid_to_components = (deal.ensure(lambda grid, *a, result=_DEAL_RETURN, **k: (r := _deal_return(*a, result=result)) is not None and len(r[2]) == (0 if not grid else (r[3][1] if len(r[3]) == 2 else 1))))(_flatten_grid_to_components)
_flatten_grid_to_components = (deal.ensure(lambda grid, *a, result=_DEAL_RETURN, **k: all(isinstance(key, int) for key in _deal_return(*a, result=result)[1].keys())))(_flatten_grid_to_components)
_flatten_grid_to_components = (deal.ensure(lambda grid, *a, result=_DEAL_RETURN, **k: (r := _deal_return(*a, result=result)) is not None and (not grid) == (len(r[0]) == 0 and r[1] == {} and r[2] == [] and r[3] == [0])))(_flatten_grid_to_components)
_flatten_grid_to_components = (deal.post(lambda *a, result=_DEAL_RETURN, **k: (r := _deal_return(*a, result=result)) is not None and isinstance(r, tuple) and len(r) == 4 and isinstance(r[0], array.array) and isinstance(r[1], dict) and isinstance(r[2], list) and isinstance(r[3], list)))(_flatten_grid_to_components)
_flatten_grid_to_components = (deal.pre(lambda grid: _deal_product_grid_ok(grid)))(_flatten_grid_to_components)

host_pack_split_grid = _prod.host_pack_split_grid
host_pack_split_grid = (deal.raises(ValueError))(host_pack_split_grid)
host_pack_split_grid = (deal.ensure(lambda grid, *a, result=_DEAL_RETURN, **k: (r := _deal_return(*a, result=result)) is not None and len(r.get("column_kinds", [])) == (0 if not grid else (r["shape"][1] if len(r["shape"]) == 2 else 1))))(host_pack_split_grid)
host_pack_split_grid = (deal.ensure(lambda grid, *a, result=_DEAL_RETURN, **k: (r := _deal_return(*a, result=result)) is not None and (len(r["buffer"]) == 0 if not grid else len(r["buffer"]) % _FLOAT64_BYTES == 0)))(host_pack_split_grid)
host_pack_split_grid = (deal.ensure(lambda grid, *a, result=_DEAL_RETURN, **k: isinstance(_deal_return(*a, result=result).get("shape"), list)))(host_pack_split_grid)
host_pack_split_grid = (deal.ensure(lambda grid, *a, result=_DEAL_RETURN, **k: isinstance(_deal_return(*a, result=result).get("column_kinds"), list)))(host_pack_split_grid)
host_pack_split_grid = (deal.ensure(lambda grid, *a, result=_DEAL_RETURN, **k: all(isinstance(key, int) for key in _deal_return(*a, result=result).get("strings", {}))))(host_pack_split_grid)
host_pack_split_grid = (deal.ensure(lambda grid, *a, result=_DEAL_RETURN, **k: isinstance(_deal_return(*a, result=result).get("strings"), dict)))(host_pack_split_grid)
host_pack_split_grid = (deal.ensure(lambda grid, *a, result=_DEAL_RETURN, **k: isinstance(_deal_return(*a, result=result).get("buffer"), bytes)))(host_pack_split_grid)
host_pack_split_grid = (deal.ensure(lambda grid, *a, result=_DEAL_RETURN, **k: _deal_return(*a, result=result).get("dtype") == SPLIT_GRID_WIRE_DTYPE))(host_pack_split_grid)
host_pack_split_grid = (deal.ensure(lambda grid, *a, result=_DEAL_RETURN, **k: _deal_return(*a, result=result).get("__wa_payload__") == PAYLOAD_SPLIT_GRID))(host_pack_split_grid)
host_pack_split_grid = (deal.post(lambda *a, result=_DEAL_RETURN, **k: isinstance(_deal_return(*a, result=result), dict)))(host_pack_split_grid)
host_pack_split_grid = (deal.pre(lambda grid: _deal_product_grid_ok(grid)))(host_pack_split_grid)

host_pack_data = _prod.host_pack_data
host_pack_data = (deal.raises(ValueError))(host_pack_data)
host_pack_data = (deal.post(lambda *a, result=_DEAL_RETURN, **k: _deal_return(*a, result=result) is not None))(host_pack_data)
host_pack_data = (deal.pre(
    lambda grid, *_unused, min_cells=BINARY_MIN_CELLS, force="auto", **__: _deal_force_min_cells_ok(
        min_cells, force
    )
))(host_pack_data)
host_pack_data = (deal.pre(lambda grid, *_unused, **__: _deal_product_grid_ok(grid)))(host_pack_data)

host_unpack_split_grid = _prod.host_unpack_split_grid
host_unpack_split_grid = (deal.raises(ValueError, OverflowError))(host_unpack_split_grid)
host_unpack_split_grid = (deal.post(lambda *a, result=_DEAL_RETURN, **k: isinstance(_deal_return(*a, result=result), list)))(host_unpack_split_grid)
host_unpack_split_grid = (deal.pre(lambda envelope, *_unused, **__: _is_split_grid_envelope(envelope)))(host_unpack_split_grid)

host_unpack_data = _prod.host_unpack_data
host_unpack_data = (deal.raises(*_HOST_UNPACK_ERRORS))(host_unpack_data)
host_unpack_data = (deal.pre(lambda wire, *_unused, **__: _deal_host_unpack_wire_ok(wire)))(host_unpack_data)

is_split_grid = _prod.is_split_grid
is_split_grid = (inverse_ensure(
    lambda obj, result: not result
    or (
        isinstance(obj, dict)
        and obj.get("__wa_payload__") == PAYLOAD_SPLIT_GRID
        and isinstance(obj.get("shape"), list)
        and len(obj["shape"]) in (1, 2)
        and all(_nonneg_shape_dim(d) for d in obj["shape"])
        and (isinstance(obj.get("buffer"), bytes) or isinstance(obj.get("b64"), str))
    )
))(is_split_grid)
is_split_grid = (deal.post(lambda result: isinstance(result, bool)))(is_split_grid)
is_split_grid = (deal.pre(lambda obj: _deal_wire_dict_ok(obj)))(is_split_grid)

child_unpack_split_grid = _prod.child_unpack_split_grid
child_unpack_split_grid = (deal.raises(ValueError, TypeError, AttributeError))(child_unpack_split_grid)
child_unpack_split_grid = (deal.ensure(lambda envelope, *a, result=_DEAL_RETURN, **k: not envelope.get("strings") or isinstance(_deal_return(*a, result=result), list)))(child_unpack_split_grid)
child_unpack_split_grid = (deal.post(lambda *a, result=_DEAL_RETURN, **k: _deal_return(*a, result=result) is not None))(child_unpack_split_grid)
child_unpack_split_grid = (deal.pre(lambda envelope: _is_split_grid_envelope(envelope)))(child_unpack_split_grid)

_child_unpack_single_data = _prod._child_unpack_single_data
_child_unpack_single_data = (deal.raises(ValueError, TypeError, AttributeError))(_child_unpack_single_data)
_child_unpack_single_data = (deal.post(lambda *a, result=_DEAL_RETURN, **k: _deal_return(*a, result=result) is not None))(_child_unpack_single_data)

child_unpack_data = _prod.child_unpack_data
child_unpack_data = (deal.raises(ValueError, TypeError, AttributeError))(child_unpack_data)
child_unpack_data = (deal.post(lambda *a, result=_DEAL_RETURN, **k: _deal_return(*a, result=result) is not None))(child_unpack_data)
child_unpack_data = (deal.pre(lambda wire, *_unused, **__: _deal_child_unpack_wire_ok(wire)))(child_unpack_data)

child_pack_split_grid = _prod.child_pack_split_grid
child_pack_split_grid = (deal.raises(ValueError, TypeError, AttributeError))(child_pack_split_grid)
child_pack_split_grid = (deal.ensure(lambda arr, *a, result=_DEAL_RETURN, **k: _deal_return(*a, result=result).get("strings") == {}))(child_pack_split_grid)
child_pack_split_grid = (deal.ensure(lambda arr, *a, result=_DEAL_RETURN, **k: isinstance(_deal_return(*a, result=result).get("buffer"), bytes)))(child_pack_split_grid)
child_pack_split_grid = (deal.ensure(lambda arr, *a, result=_DEAL_RETURN, **k: _deal_return(*a, result=result).get("dtype") == SPLIT_GRID_WIRE_DTYPE))(child_pack_split_grid)
child_pack_split_grid = (deal.ensure(lambda arr, *a, result=_DEAL_RETURN, **k: _deal_return(*a, result=result).get("__wa_payload__") == PAYLOAD_SPLIT_GRID))(child_pack_split_grid)
child_pack_split_grid = (deal.post(lambda *a, result=_DEAL_RETURN, **k: isinstance(_deal_return(*a, result=result), dict)))(child_pack_split_grid)
child_pack_split_grid = (deal.pre(lambda arr: _is_ndarray(arr)))(child_pack_split_grid)

child_pack_result = _prod.child_pack_result
child_pack_result = (deal.raises(ValueError, TypeError, AttributeError))(child_pack_result)

PYTEST = ['is_multi_data', 'is_image_payload', 'is_dataframe_payload', 'is_calc_range_payload', '_is_any_payload_envelope', 'envelope_column_kinds', 'cell_count', 'should_use_binary_envelope', 'is_numeric_grid', 'wire_cell_count', 'grid_from_nested_list', '_flatten_grid_to_components', 'host_pack_split_grid', 'host_pack_data', 'host_unpack_split_grid', 'host_unpack_data', 'is_split_grid', 'child_unpack_split_grid', '_child_unpack_single_data', 'child_unpack_data', 'child_pack_split_grid', 'child_pack_result']
TARGETS = [
    f"{__name__}.cell_count",
    f"{__name__}.should_use_binary_envelope",
]

# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""First-class Calc range value for host↔venv data handoff.

Every sheet range is a rectangular 2D grid. ``split_grid`` remains a private
transport optimization; user scripts see :class:`CalcRange` with explicit
``.values`` / ``.to_numpy()`` / ``.to_pandas()`` conversions.

Host stays NumPy-free: pack/unpack helpers here use only stdlib. NumPy/pandas
imports live inside conversion methods (venv only).
"""

from __future__ import annotations

import math
import operator
from typing import Any, Callable, ClassVar, Iterator, cast

from plugin.framework.deal_shim import (
    DEAL_MAX_SHAPE_DIM,
    DEAL_MAX_TOKEN,
    UNDER_CROSSHAIR,
    _profile,
    ascii_bounded,
    deal,
    str_bounded,
)
from plugin.scripting.payload_codec import PAYLOAD_CALC_RANGE, is_calc_range_payload

# Cover: 1×1 grid, int 0, 1-char ascii. Dim 4 / ±8 still ~2.3h (33211730747).
_DEAL_GRID_DIM = 1 if UNDER_CROSSHAIR else DEAL_MAX_SHAPE_DIM
_DEAL_CELL_INT_ABS = 0 if UNDER_CROSSHAIR else 8
_DEAL_CELL_STR_LEN = 1 if UNDER_CROSSHAIR else 4
_DEAL_COL_NAME_LEN = _DEAL_CELL_STR_LEN if UNDER_CROSSHAIR else DEAL_MAX_TOKEN


def _cells_of_row(row: Any) -> list[Any]:
    """One logical row. Non-sequence objects, ``str``, and ``bytes`` are a single cell.

    Once the first row is a real sequence, every later row used to go through
    ``list(row)``.
    """
    # list(row) raises TypeError on a non-iterable cell such as an int.
    # Any non-list/tuple (including str and bytes) is one cell: [row].
    if not isinstance(row, (list, tuple)) or isinstance(row, (str, bytes)):
        return [row]
    return list(row)


@deal.post(lambda result: isinstance(result, list))
def ensure_rectangular_2d(grid: Any) -> list[list[Any]]:
    """Normalize any scalar / 1D / 2D input into a rectangular ``list[list]``.

    Orientation is preserved: a single row stays ``[[a, b, c]]``; a single
    column stays ``[[a], [b], [c]]``; a scalar becomes ``[[v]]``. A ``str`` or
    ``bytes`` row is one cell, not a sequence of characters.
    """
    # crosshair: off
    if grid is None:
        return []
    # Plain list/tuple only — namedtuple subclasses tuple; treating them as grids
    # iterates fields and blows up on non-sequence members (e.g. SplitResultBytes ints).
    if isinstance(grid, (str, bytes)) or (type(grid) is not list and type(grid) is not tuple):
        return [[grid]]
    if not grid:
        return []
    first = grid[0]
    if isinstance(first, (list, tuple)):
        rows = [_cells_of_row(row) for row in grid]
        width = max((len(row) for row in rows), default=0)
        return [row + [None] * (width - len(row)) for row in rows]
    # Flat sequence → single row (Calc 1D row) unless callers pass column shape.
    return [list(grid)]


_deal_column_vector_ok = _profile(
    lambda values: isinstance(values, list) and len(values) <= _DEAL_GRID_DIM
)


@deal.pre(lambda values: _deal_column_vector_ok(values))
@deal.post(lambda result: isinstance(result, list) and all(isinstance(row, list) and len(row) == 1 for row in result))
@deal.ensure(lambda values, result: len(result) == len(values))
def column_vector_as_2d(values: list[Any]) -> list[list[Any]]:
    """Wrap a flat column vector as ``[[v], …]`` (N×1)."""
    return [[v] for v in values]


@deal.post(lambda result: isinstance(result, dict) and is_calc_range_payload(result))
def pack_calc_range_envelope(
    grid: list[list[Any]],
    *,
    address: str | None = None,
    pack_inner: Any | None = None,
) -> dict[str, Any]:
    """Build a ``calc_range`` wire envelope around an already-packed or raw grid.

    *pack_inner*, when provided, is a callable ``(grid) -> wire`` (typically
    ``host_pack_data``). When omitted, the rectangular list is stored as-is.
    """
    # crosshair: off
    rows = ensure_rectangular_2d(grid)
    nrows = len(rows)
    ncols = len(rows[0]) if rows else 0
    inner = pack_inner(rows) if callable(pack_inner) else rows
    envelope: dict[str, Any] = {
        "__wa_payload__": PAYLOAD_CALC_RANGE,
        "shape": [nrows, ncols],
        "data": inner,
    }
    if address:
        envelope["address"] = str(address)
    return envelope


def _deal_grid_values_ok(values: object) -> bool:
    return (
        isinstance(values, list)
        and len(values) <= _DEAL_GRID_DIM
        and all(
            isinstance(row, list)
            and len(row) <= _DEAL_GRID_DIM
            and all(_deal_inner_grid_cell_ok(c) for c in row)
            for row in values
        )
    )


def _deal_calc_range_other_ok_crosshair(other: object) -> bool:
    # cover-all: unbounded int/str on binary ops exploded CrossHair the same way
    # inner grid cells did; keep a tiny numeric/ascii slice under the engine.
    if other is None or isinstance(other, bool):
        return True
    if isinstance(other, int):
        return -_DEAL_CELL_INT_ABS <= other <= _DEAL_CELL_INT_ABS
    if isinstance(other, float):
        return True
    if isinstance(other, str):
        return ascii_bounded(other, _DEAL_CELL_STR_LEN)
    return isinstance(other, CalcRange) and _deal_grid_values_ok(other._values)


def _deal_calc_range_other_ok_pytest(other: object) -> bool:
    if other is None or isinstance(other, (bool, int, float, str)):
        return True
    return isinstance(other, CalcRange) and _deal_grid_values_ok(other._values)


_deal_calc_range_other_ok = _profile(
    _deal_calc_range_other_ok_crosshair,
    _deal_calc_range_other_ok_pytest,
)

_deal_binary_op_pre = _profile(
    lambda self, other: _deal_grid_values_ok(self._values) and _deal_calc_range_other_ok(other)
)


def _binop(op: Any, reverse: bool = False) -> Any:
    """Factory for binary operator dunders delegating to _binary_op."""
    @deal.pre(_deal_binary_op_pre)
    def method(self: Any, other: Any) -> Any:
        # crosshair: off  # thin wrapper around _binary_op; doable later (cover-all 33258921875)
        return self._binary_op(other, op, is_reverse=reverse)

    return method


class CalcRange:
    """Rectangular sheet range exposed to user/venv scripts.

    Attributes:
        values: Exact 2D cell values (``None`` for blanks). Never mutates orientation.
        address: Optional source A1 / sheet hint from the host.
        shape: ``(nrows, ncols)``.
    """

    __slots__: ClassVar[tuple[str, ...]] = ("_values", "_address")
    _values: list[list[Any]]
    _address: str | None

    def __init__(self, values: Any, *, address: str | None = None) -> None:
        # crosshair: off
        # ensure_rectangular_2d treats a non-list/tuple as a scalar, so an
        # ndarray or DataFrame became a 1x1 range. _materialize_inner_grid
        # unpacks those, and nested structures, into a rectangular 2D list.
        self._values = _materialize_inner_grid(values)
        self._address = address

    @property
    def values(self) -> list[list[Any]]:
        # crosshair: off
        # cover-all 33689813185 leftover: combinatoric instance surface. Doable later: tiny 1x1 grid domain.
        return self._values

    @property
    def address(self) -> str | None:
        # crosshair: off
        # cover-all 33689813185 leftover: combinatoric instance surface. Doable later: tiny 1x1 grid domain.
        return self._address

    @property
    def shape(self) -> tuple[int, int]:
        # crosshair: off
        # cover-all 33689813185 leftover: combinatoric instance surface. Doable later: tiny 1x1 grid domain.
        nrows = len(self._values)
        ncols = len(self._values[0]) if self._values else 0
        return (nrows, ncols)

    @property
    def nrows(self) -> int:
        # crosshair: off
        # cover-all 33689813185 leftover: combinatoric instance surface. Doable later: tiny 1x1 grid domain.
        return self.shape[0]

    @property
    def ncols(self) -> int:
        # crosshair: off
        # cover-all 33689813185 leftover: combinatoric instance surface. Doable later: tiny 1x1 grid domain.
        return self.shape[1]

    def __repr__(self) -> str:
        # crosshair: off
        # cover-all 33689813185 leftover: combinatoric instance surface. Doable later: tiny 1x1 grid domain.
        r, c = self.shape
        addr = f" address={self._address!r}" if self._address else ""
        return f"CalcRange({r}x{c}{addr})"

    def __len__(self) -> int:
        # crosshair: off
        # cover-all 33689813185 leftover: combinatoric instance surface. Doable later: tiny 1x1 grid domain.
        return self.nrows

    def __iter__(self) -> Iterator[list[Any]]:
        # crosshair: off
        # cover-all 33689813185 leftover: combinatoric instance surface. Doable later: tiny 1x1 grid domain.
        """Iterate rows (each a list). Does not flatten to cells."""
        return iter(self._values)

    def __getitem__(self, key: Any) -> Any:
        # crosshair: off
        # cover-all 33689813185 leftover: combinatoric instance surface. Doable later: tiny 1x1 grid domain.
        """Row access (``data[0]``) or slice of rows — not cell flattening."""
        return self._values[key]

    def __array__(self, dtype: Any = None, copy: bool | None = None) -> Any:
        # crosshair: off
        # NumPy 2's __array__ protocol passes copy=. Forward it to to_numpy
        # or the call warns.
        return self.to_numpy(dtype=dtype, copy=copy)

    def to_numpy(self, *, dtype: Any = None, copy: bool | None = None) -> Any:
        # crosshair: off
        # cover-all 33689813185 leftover: combinatoric instance surface. Doable later: tiny 1x1 grid domain.
        """Explicit NumPy conversion (same as ``np.asarray(range)``)."""
        import numpy as np

        def _cell(v: Any) -> Any:
            if v is None:
                return math.nan
            return v

        grid = [[_cell(v) for v in row] for row in self._values]
        kwargs: dict[str, Any] = {}
        if copy is not None:
            kwargs["copy"] = copy

        if not self._values:
            # np.array([]) is shape (0,), while .shape is (0, 0). A 0-row
            # range has no row to carry a column count, so both are (0, 0).
            dt = np.float64 if dtype is None else dtype
            return np.array([], dtype=dt, **kwargs).reshape(0, 0)

        if dtype is not None:
            return np.array(grid, dtype=dtype, **kwargs)

        # np.asarray(..., dtype=float64) turns "1.5" into 1.5 and True into 1.0.
        # Use float64 only when every cell is a real number (int/float, not bool) or None.
        is_numeric = all(
            cell is None or (not isinstance(cell, bool) and isinstance(cell, (int, float)))
            for row in self._values
            for cell in row
        )
        if is_numeric:
            return np.array(grid, dtype=np.float64, **kwargs)
        return np.array(self._values, dtype=object, **kwargs)

    def to_pandas(
        self,
        *,
        header_row: int | None = 0,
        index_col: int | None = None,
        parse_strings: bool = False,
        date_cols: list[str | int] | bool = False,
        date_origin: str = "1899-12-30",
    ) -> Any:
        """Convert to a pandas DataFrame with an explicit header policy.

        Args:
            header_row: Row index used as column names, or ``None`` for
                synthetic ``col_0..col_n`` names (all rows are data).
            index_col: Optional column to use as the DataFrame index.
            parse_strings: When True, apply optional currency/percent/numeric
                and datetime string parsing. Default False keeps text cells as text.
            date_cols: Specific column names/indices or True to coerce numeric
                serials/date strings to datetime64.
            date_origin: Base epoch for serial numbers (default '1899-12-30').
        """
        # crosshair: off
        from plugin.scripting.venv.coerce import grid_to_dataframe

        return grid_to_dataframe(
            self._values,
            header_row=header_row,
            index_col=index_col,
            parse_strings=parse_strings,
            date_cols=date_cols,
            date_origin=date_origin,
            sheet_hint=self._address,
        ).df

    # --- Issue #412: Arithmetic, comparison, and scalar protocols ---

    __hash__: None = None  # type: ignore[assignment]  # pyright: ignore[reportAssignmentType, reportGeneralTypeIssues, reportIncompatibleMethodOverride]  # CalcRange is mutable / unhashable like ndarray

    def __bool__(self) -> bool:
        # crosshair: off
        # cover-all 33689813185 leftover: combinatoric instance surface. Doable later: tiny 1x1 grid domain.
        if self.shape == (1, 1):
            return bool(self._values[0][0])
        if self.shape == (0, 0) or not self._values:
            return False
        raise ValueError(
            f"The truth value of a CalcRange with shape {self.shape} is ambiguous. "
            "Use data.to_numpy().any() or data.to_numpy().all()"
        )

    def __str__(self) -> str:
        # crosshair: off
        # cover-all 33689813185 leftover: combinatoric instance surface. Doable later: tiny 1x1 grid domain.
        if self.shape == (1, 1):
            return str(self._values[0][0])
        return self.__repr__()

    def __format__(self, format_spec: str) -> str:
        # crosshair: off
        # cover-all 33689813185 leftover: combinatoric instance surface. Doable later: tiny 1x1 grid domain.
        if self.shape == (1, 1):
            return format(self._values[0][0], format_spec)
        return format(str(self), format_spec)

    # Scalar conversions
    def __float__(self) -> float:
        # crosshair: off
        # cover-all 33689813185 leftover: combinatoric instance surface. Doable later: tiny 1x1 grid domain.
        if self.shape == (1, 1):
            val = self._values[0][0]
            if val is None:
                raise TypeError("Cannot convert empty cell (None) to float")
            return float(val)
        raise TypeError(f"Only 1x1 CalcRange can be converted to float, got shape {self.shape}")

    def __int__(self) -> int:
        # crosshair: off
        # cover-all 33689813185 leftover: combinatoric instance surface. Doable later: tiny 1x1 grid domain.
        if self.shape == (1, 1):
            val = self._values[0][0]
            if val is None:
                raise TypeError("Cannot convert empty cell (None) to int")
            return int(val)
        raise TypeError(f"Only 1x1 CalcRange can be converted to int, got shape {self.shape}")

    def __round__(self, ndigits: int | None = None) -> Any:
        # crosshair: off
        # cover-all 33689813185 leftover: combinatoric instance surface. Doable later: tiny 1x1 grid domain.
        if self.shape == (1, 1):
            val = self._values[0][0]
            if val is None:
                raise TypeError("Cannot round empty cell (None)")
            return round(val, ndigits) if ndigits is not None else round(val)
        raise TypeError(f"Only 1x1 CalcRange can be rounded, got shape {self.shape}")

    def __trunc__(self) -> int:
        # crosshair: off
        # cover-all 33689813185 leftover: combinatoric instance surface. Doable later: tiny 1x1 grid domain.
        return math.trunc(self.__float__())

    def __floor__(self) -> int:
        # crosshair: off
        # cover-all 33689813185 leftover: combinatoric instance surface. Doable later: tiny 1x1 grid domain.
        return math.floor(self.__float__())

    def __ceil__(self) -> int:
        # crosshair: off
        # cover-all 33689813185 leftover: combinatoric instance surface. Doable later: tiny 1x1 grid domain.
        return math.ceil(self.__float__())

    # Dispatchers
    def _binary_op(self, other: Any, op: Any, *, is_reverse: bool = False) -> Any:
        # crosshair: off
        # A 1x1 None raises TypeError on arithmetic (None + 1). Multi-cell
        # ranges already map None to nan; do the same here except for equality.
        if self.shape == (1, 1):
            val = self._values[0][0]
            if val is None and op not in (operator.eq, operator.ne):
                val = math.nan
            if isinstance(other, CalcRange):
                if other.shape == (1, 1):
                    other_val = other._values[0][0]
                    if other_val is None and op not in (operator.eq, operator.ne):
                        other_val = math.nan
                    return op(other_val, val) if is_reverse else op(val, other_val)
                try:
                    other_arr = other.to_numpy()
                except Exception as exc:
                    raise TypeError(f"Multi-cell arithmetic requires NumPy: {exc}") from exc
                return op(other_arr, val) if is_reverse else op(val, other_arr)
            if other is None and op not in (operator.eq, operator.ne):
                other = math.nan
            return op(other, val) if is_reverse else op(val, other)

        try:
            self_arr = self.to_numpy()
        except Exception as exc:
            raise TypeError(f"Multi-cell arithmetic requires NumPy: {exc}") from exc
        if isinstance(other, CalcRange):
            other_val = other._values[0][0] if other.shape == (1, 1) else other.to_numpy()
            if other.shape == (1, 1) and other_val is None and op not in (operator.eq, operator.ne):
                other_val = math.nan
            other = other_val
        elif other is None and op not in (operator.eq, operator.ne):
            other = math.nan
        return op(other, self_arr) if is_reverse else op(self_arr, other)

    def _unary_op(self, op: Any) -> Any:
        # crosshair: off
        # cover-all 33689813185 leftover: combinatoric instance surface. Doable later: tiny 1x1 grid domain.
        if self.shape == (1, 1):
            val = self._values[0][0]
            if val is None:
                val = math.nan
            return op(val)
        try:
            return op(self.to_numpy())
        except Exception as exc:
            raise TypeError(f"Multi-cell arithmetic requires NumPy: {exc}") from exc

    # Binary arithmetic
    __add__: Callable[..., Any] = _binop(operator.add)
    __radd__: Callable[..., Any] = _binop(operator.add, reverse=True)
    __sub__: Callable[..., Any] = _binop(operator.sub)
    __rsub__: Callable[..., Any] = _binop(operator.sub, reverse=True)
    __mul__: Callable[..., Any] = _binop(operator.mul)
    __rmul__: Callable[..., Any] = _binop(operator.mul, reverse=True)
    __truediv__: Callable[..., Any] = _binop(operator.truediv)
    __rtruediv__: Callable[..., Any] = _binop(operator.truediv, reverse=True)
    __floordiv__: Callable[..., Any] = _binop(operator.floordiv)
    __rfloordiv__: Callable[..., Any] = _binop(operator.floordiv, reverse=True)
    __mod__: Callable[..., Any] = _binop(operator.mod)
    __rmod__: Callable[..., Any] = _binop(operator.mod, reverse=True)
    __pow__: Callable[..., Any] = _binop(operator.pow)
    __rpow__: Callable[..., Any] = _binop(operator.pow, reverse=True)

    # Unary
    def __neg__(self) -> Any:
        # crosshair: off  # thin wrapper around _unary_op; doable later (cover-all 33258921875)
        return self._unary_op(operator.neg)

    def __pos__(self) -> Any:
        # crosshair: off  # thin wrapper around _unary_op; doable later (cover-all 33258921875)
        return self._unary_op(operator.pos)

    def __abs__(self) -> Any:
        # crosshair: off  # thin wrapper around _unary_op; doable later (cover-all 33258921875)
        return self._unary_op(operator.abs)

    # Rich comparisons (aligned through _binary_op: 1x1 returns bool; multi-cell returns bool ndarray)
    __eq__: Callable[..., Any] = _binop(operator.eq)
    __ne__: Callable[..., Any] = _binop(operator.ne)
    __lt__: Callable[..., Any] = _binop(operator.lt)
    __le__: Callable[..., Any] = _binop(operator.le)
    __gt__: Callable[..., Any] = _binop(operator.gt)
    __ge__: Callable[..., Any] = _binop(operator.ge)


def materialize_calc_range(wire: Any) -> CalcRange:
    """Build a :class:`CalcRange` from a ``calc_range`` envelope or raw grid/split_grid."""
    # crosshair: off
    if isinstance(wire, CalcRange):
        return wire
    if is_calc_range_payload(wire):
        inner = wire.get("data")
        address = wire.get("address")
        addr = address.strip() if isinstance(address, str) and address.strip() else None
        # __init__ calls _materialize_inner_grid. Unpacking here first built
        # the rectangular list, then __init__ walked every cell again.
        return CalcRange(inner, address=addr)

    # Legacy / test wires: bare split_grid or nested list (no calc_range wrapper).
    return CalcRange(wire)


_deal_inner_grid_cell_ok = _profile(
    lambda c: (
        c is None
        or (-_DEAL_CELL_INT_ABS <= c <= _DEAL_CELL_INT_ABS if isinstance(c, int) else False)
        or (ascii_bounded(c, _DEAL_CELL_STR_LEN) if isinstance(c, str) else False)
    ),
    pytest_fn=lambda c: isinstance(c, (str, int, float, bool, type(None))) or hasattr(c, "dtype"),
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


_deal_json_list_of_grids_arg_ok = _profile(_deal_json_list_of_grids_arg_ok_crosshair)


def _deal_materialize_inner_ok_crosshair(inner: object) -> bool:
    if type(inner) is not list and type(inner) is not tuple:
        return True
    rows = cast("list[Any] | tuple[Any, ...]", inner)
    return len(rows) <= _DEAL_GRID_DIM and all(
        type(r) not in (list, tuple)
        or (
            len(r) <= _DEAL_GRID_DIM
            and all(_deal_inner_grid_cell_ok(c) for c in r)
        )
        for r in rows
    )


_deal_materialize_inner_ok = _profile(_deal_materialize_inner_ok_crosshair)


@deal.pre(lambda inner: _deal_materialize_inner_ok(inner))
def _materialize_inner_grid(inner: Any) -> list[list[Any]]:
    """Unpack split_grid / ndarray / DataFrame / nested lists to a rectangular ``list[list]``."""
    # crosshair: off  # Any/numpy/split_grid combinatorics; tiny list domain later (cover-all 33258921875: 575k lines)
    from plugin.scripting.payload_codec import (
        _numpy_scalar_item,
        child_unpack_data,
        is_split_grid,
    )

    if isinstance(inner, CalcRange):
        return [list(row) for row in inner.values]

    if is_split_grid(inner):
        unpacked = child_unpack_data(inner)
    else:
        unpacked = inner

    if hasattr(unpacked, "to_numpy") and callable(unpacked.to_numpy):
        try:
            unpacked = unpacked.to_numpy()
        except Exception:
            pass

    try:
        import numpy as np

        if isinstance(unpacked, np.ndarray):
            if unpacked.ndim == 0:
                val = unpacked.item()
                return [[_numpy_scalar_item(val) if unpacked.dtype == object else val]]
            # tolist() already returns native Python values for non-object
            # arrays, so those cells do not need an imported-numpy unwrap.
            if unpacked.dtype != object:
                raw_list = unpacked.tolist()
                if unpacked.ndim == 1:
                    return [raw_list]
                return raw_list
            if unpacked.ndim == 1:
                return [[_numpy_scalar_item(v) for v in unpacked.tolist()]]
            return [[_numpy_scalar_item(c) for c in row] for row in unpacked.tolist()]
    except ImportError:
        pass

    if isinstance(unpacked, (list, tuple)):
        return ensure_rectangular_2d(unpacked)
    return ensure_rectangular_2d([[unpacked]])


def materialize_inputs(wire: Any) -> tuple[CalcRange, ...]:
    """Materialize worker ``data`` wire into a stable tuple of CalcRange.

    - ``calc_range`` → ``(range,)``
    - ``multi_data`` of ranges/grids → one CalcRange per item
    - JSON list-of-2D-grids (Online compute) → one CalcRange per item
    - bare grid / list → single CalcRange
    - ``None`` → empty tuple
    """
    # crosshair: off
    if wire is None:
        return ()
    from plugin.scripting.payload_codec import is_multi_data

    if is_calc_range_payload(wire):
        return (materialize_calc_range(wire),)
    if is_multi_data(wire):
        items = wire.get("items") or []
        return tuple(materialize_calc_range(item) for item in items)
    if isinstance(wire, (list, tuple)) and wire and all(is_calc_range_payload(x) or isinstance(x, CalcRange) for x in wire):
        return tuple(materialize_calc_range(x) for x in wire)
    if _is_json_list_of_grids(wire):
        return tuple(materialize_calc_range(item) for item in wire)
    return (materialize_calc_range(wire),)


@deal.pre(lambda obj: _deal_json_list_of_grids_arg_ok(obj))
def _is_json_list_of_grids(obj: Any) -> bool:
    # crosshair: off  # nested list/tuple shape walk (cover-all 33569420452: 13580 examples / ~527s est despite dual-profile pre). Doable later: fixed small grid domain.
    """True when *obj* is a JSON array of 2D grids (Online =PY multi-range without multi_data).

    A normal 2D sheet block ``[[1, 2], [3, 4]]`` has scalar cells — not a list of grids.
    ``[[[1, 2]], [[3], [4]]]`` is two rectangular ranges.
    """
    if not isinstance(obj, (list, tuple)) or len(obj) < 2:
        return False
    if not all(isinstance(item, (list, tuple)) for item in obj):
        return False
    # At least one item must itself be a 2D grid (first cell is a sequence).
    return any(item and isinstance(item[0], (list, tuple)) and not isinstance(item[0], (str, bytes)) for item in obj)



def _deal_labeled_grid_ok_crosshair(
    columns: object, data: object = None, include_header: object = True
) -> bool:
    return (
        type(columns) is list
        and len(columns) <= _DEAL_GRID_DIM
        and all(str_bounded(c, _DEAL_COL_NAME_LEN) for c in columns)
        and (
            data is None
            or (
                type(data) is list
                and len(data) <= _DEAL_GRID_DIM
                and all(
                    (
                        type(row) is list
                        and len(row) <= _DEAL_GRID_DIM
                        and all(_deal_inner_grid_cell_ok(c) for c in row)
                    )
                    if type(row) is list
                    else _deal_inner_grid_cell_ok(row)
                    for row in data
                )
            )
        )
        and type(include_header) is bool
    )


_deal_labeled_grid_ok = _profile(_deal_labeled_grid_ok_crosshair)


@deal.pre(lambda columns, data=None, include_header=True, **__: _deal_labeled_grid_ok(columns, data, include_header))
def dataframe_to_labeled_grid(
    columns: list[str],
    data: list[list[Any]] | list[Any] | None,
    *,
    include_header: bool = True,
) -> list[list[Any]]:
    # crosshair: off  # columns+data nested lists (cover-all 33569420452: 3337 examples / ~130s est despite _DEAL_GRID_DIM). Doable later.
    """Build a Calc-ready grid from a dataframe envelope (optional header row)."""
    body: list[list[Any]]
    if data is None:
        body = []
    elif isinstance(data, list):
        if not data:
            body = []
        elif isinstance(data[0], (list, tuple)):
            body = [list(row) for row in data]
        else:
            # 1D / Series body → one column
            body = [[cell] for cell in data]
    else:
        body = [[data]]
    if not include_header:
        return body
    header = [str(c) for c in columns]
    if body and len(body[0]) != len(header):
        # Pad/truncate header to body width if inconsistent.
        width = len(body[0])
        header = (header + [f"col_{i}" for i in range(len(header), width)])[:width]
    return [header] + body


__all__ = [
    "PAYLOAD_CALC_RANGE",
    "CalcRange",
    "column_vector_as_2d",
    "dataframe_to_labeled_grid",
    "ensure_rectangular_2d",
    "is_calc_range_payload",
    "materialize_calc_range",
    "materialize_inputs",
    "pack_calc_range_envelope",
]

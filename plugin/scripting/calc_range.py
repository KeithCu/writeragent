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

import logging
import math
import operator
from typing import Any, Callable, ClassVar, Iterator, NoReturn, cast

from plugin.framework.deal_shim import (
    DEAL_MAX_SHAPE_DIM,
    DEAL_MAX_TOKEN,
    UNDER_CROSSHAIR,
    _profile,
    ascii_bounded,
    deal,
    str_bounded,
)
from plugin.scripting.payload_codec import (
    PAYLOAD_CALC_RANGE,
    is_calc_range_payload,
    is_dataframe_payload,
    is_multi_data,
)

log = logging.getLogger(__name__)

# Cover: 1×1 grid, int 0, 1-char ascii.
_DEAL_GRID_DIM = 1 if UNDER_CROSSHAIR else DEAL_MAX_SHAPE_DIM
_DEAL_CELL_INT_ABS = 0 if UNDER_CROSSHAIR else 8
_DEAL_CELL_STR_LEN = 1 if UNDER_CROSSHAIR else 4
_DEAL_COL_NAME_LEN = _DEAL_CELL_STR_LEN if UNDER_CROSSHAIR else DEAL_MAX_TOKEN


def _assert_rectangular(grid: list[list[Any]]) -> None:
    """Assert rectangular 2D invariant: list of lists with uniform width."""
    if not grid:
        return
    width = len(grid[0])
    assert all(isinstance(row, list) and len(row) == width for row in grid), "Grid must be rectangular list[list]"


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
        width = 0
        for row in grid:
            rlen = len(row) if isinstance(row, (list, tuple)) and not isinstance(row, (str, bytes)) else 1
            if rlen > width:
                width = rlen
        out: list[list[Any]] = []
        for row in grid:
            if not isinstance(row, (list, tuple)) or isinstance(row, (str, bytes)):
                pad = width - 1
                out.append([row] if pad <= 0 else [row] + [None] * pad)
            else:
                pad = width - len(row)
                out.append(list(row) if pad <= 0 else list(row) + [None] * pad)
        return out
    # Flat sequence → single row (Calc 1D row) unless callers pass column shape.
    return [list(grid)]


@deal.post(lambda result: isinstance(result, dict) and is_calc_range_payload(result))
def pack_calc_range_envelope(
    grid: Any,
    *,
    address: str | None = None,
    pack_inner: Any | None = None,
) -> dict[str, Any]:
    """Build a ``calc_range`` wire envelope around an already-packed or raw grid.

    A scalar, a flat sequence, or a 2D grid is accepted; ``ensure_rectangular_2d``
    runs once here. *pack_inner*, when provided, is a callable ``(grid) -> wire``
    (typically ``host_pack_data``). When omitted, the rectangular list is stored as-is.
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


def _whole_floats_to_int(grid: list[list[Any]]) -> list[list[Any]]:
    """All numeric cells finite whole (abs < 1e15) -> ints; any fractional keeps the grid float."""
    nums = [c for row in grid for c in row if type(c) is float]
    if not all(math.isfinite(c) and c.is_integer() and abs(c) < 1e15 for c in nums):
        return grid
    return [[int(c) if type(c) is float else c for c in row] for row in grid]


def _numpy_required(exc: ImportError) -> NoReturn:
    # Missing NumPy is ImportError. Any other to_numpy failure propagates.
    raise TypeError(f"Multi-cell arithmetic requires NumPy: {exc}") from exc


def _blank_to_nan(val: Any, op: Any) -> Any:
    if val is None and op not in (operator.eq, operator.ne):
        return math.nan
    return val


def _arith_operand(value: Any, op: Any, *, wrap_import: bool) -> Any:
    """One side of a binary/unary op.

    1x1 CalcRange stays a Python scalar. to_numpy() is only for a multi-cell
    CalcRange. wrap_import is True only where the previous code caught
    ImportError: a multi-cell self, a multi-cell right operand when self is
    1x1, and unary multi-cell. A multi-cell self does not wrap ImportError
    from the right operand's to_numpy.
    """
    if isinstance(value, CalcRange):
        if value.shape == (1, 1):
            return _blank_to_nan(value._values[0][0], op)
        try:
            return value.to_numpy()
        except ImportError as exc:
            if wrap_import:
                _numpy_required(exc)
            raise
    return _blank_to_nan(value, op)


def _binop(op: Any, reverse: bool = False) -> Any:
    """Factory for binary operator dunders delegating to _binary_op."""
    @deal.pre(_deal_binary_op_pre)
    def method(self: Any, other: Any) -> Any:
        # crosshair: off
        return self._binary_op(other, op, is_reverse=reverse)

    return method


class CalcRange:
    """Rectangular sheet range exposed to user/venv scripts.

    Attributes:
        values: Exact 2D cell values (``None`` for blanks); the lists are the live storage, writing into them changes the range, and reading does not reshape.
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
        # Calc numbers are doubles; whole numbers arrive as ints so scalar-only
        # APIs like sp.prime(data) work (lost in ff217d9d4).
        self._values = _whole_floats_to_int(_materialize_inner_grid(values))
        _assert_rectangular(self._values)
        self._address = address

    @property
    def values(self) -> list[list[Any]]:
        # crosshair: off
        """Live 2D cells. ``None`` is a blank. These lists are the range's storage; writing into them changes the range."""
        return self._values

    @property
    def address(self) -> str | None:
        # crosshair: off
        return self._address

    @property
    def shape(self) -> tuple[int, int]:
        # crosshair: off
        nrows = len(self._values)
        ncols = len(self._values[0]) if self._values else 0
        return (nrows, ncols)

    @property
    def nrows(self) -> int:
        # crosshair: off
        return self.shape[0]

    @property
    def ncols(self) -> int:
        # crosshair: off
        return self.shape[1]

    def __repr__(self) -> str:
        # crosshair: off
        r, c = self.shape
        addr = f" address={self._address!r}" if self._address else ""
        return f"CalcRange({r}x{c}{addr})"

    def __len__(self) -> int:
        # crosshair: off
        return self.nrows

    def __iter__(self) -> Iterator[list[Any]]:
        # crosshair: off
        """Iterate rows (each a list). Does not flatten to cells."""
        return iter(self._values)

    def __getitem__(self, key: Any) -> Any:
        # crosshair: off
        """Row access (``data[0]``) or slice of rows — not cell flattening."""
        return self._values[key]

    def __array__(self, dtype: Any = None, copy: bool | None = None) -> Any:
        # crosshair: off
        # NumPy 2's __array__ protocol passes copy=. Forward it to to_numpy
        # or the call warns.
        return self.to_numpy(dtype=dtype, copy=copy)

    def to_numpy(self, *, dtype: Any = None, copy: bool | None = None) -> Any:
        # crosshair: off
        """NumPy view of the cells.

        With no ``dtype``, a grid of ``None`` and real numbers (``int`` / ``float``,
        not ``bool``) is ``float64`` and blanks are NaN. Any other grid is an object
        array and blanks stay ``None``.

        An explicit ``dtype`` is ``np.array`` on the raw cells. Blanks are not
        rewritten first.
        """
        import numpy as np

        kwargs: dict[str, Any] = {}
        if copy is not None:
            kwargs["copy"] = copy

        if not self._values:
            dt = np.float64 if dtype is None else dtype
            return np.array([], dtype=dt, **kwargs).reshape(0, 0)

        # What was wrong: every explicit dtype was built from a grid that had
        # already replaced None with math.nan. dtype=object stored nan, and
        # dtype=bool stored True because nan is truthy.
        # How it happened: _cell ran before the dtype branch. np.array on the
        # raw cells keeps None for object, maps None to False for bool, and
        # maps None to nan for float.
        # Why this fixes it: a named dtype is passed the raw cells. Only the
        # default all-numeric path maps blanks to NaN.
        if dtype is not None:
            return np.array(self._values, dtype=dtype, **kwargs)

        is_numeric = all(
            cell is None or (not isinstance(cell, bool) and isinstance(cell, (int, float)))
            for row in self._values
            for cell in row
        )
        if is_numeric and all(type(c) is int for row in self._values for c in row):
            return np.array(self._values, dtype=np.int64, **kwargs)
        if is_numeric:
            grid = [[math.nan if v is None else v for v in row] for row in self._values]
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
        if self.shape == (1, 1):
            return str(self._values[0][0])
        return self.__repr__()

    def __format__(self, format_spec: str) -> str:
        # crosshair: off
        if self.shape == (1, 1):
            return format(self._values[0][0], format_spec)
        return format(str(self), format_spec)

    def _scalar(self, what: str, *, none_message: str | None = None) -> Any:
        # crosshair: off
        if self.shape != (1, 1):
            raise TypeError(f"Only 1x1 CalcRange can be {what}, got shape {self.shape}")
        val = self._values[0][0]
        if val is None and none_message is not None:
            raise TypeError(none_message)
        return val

    # Scalar conversions
    def __float__(self) -> float:
        # crosshair: off
        return float(self._scalar("converted to float", none_message="Cannot convert empty cell (None) to float"))

    def __int__(self) -> int:
        # crosshair: off
        return int(self._scalar("converted to int", none_message="Cannot convert empty cell (None) to int"))

    def __index__(self) -> int:
        # crosshair: off
        return operator.index(self._scalar("used as an index"))

    def __round__(self, ndigits: int | None = None) -> Any:
        # crosshair: off
        val = self._scalar("rounded", none_message="Cannot round empty cell (None)")
        return round(val, ndigits) if ndigits is not None else round(val)

    def __trunc__(self) -> int:
        # crosshair: off
        return math.trunc(self.__float__())

    def __floor__(self) -> int:
        # crosshair: off
        return math.floor(self.__float__())

    def __ceil__(self) -> int:
        # crosshair: off
        return math.ceil(self.__float__())

    # Dispatchers
    def _binary_op(self, other: Any, op: Any, *, is_reverse: bool = False) -> Any:
        # crosshair: off
        if self.shape == (1, 1):
            val = _arith_operand(self, op, wrap_import=False)
            other_v = _arith_operand(other, op, wrap_import=True)
        else:
            val = _arith_operand(self, op, wrap_import=True)
            other_v = _arith_operand(other, op, wrap_import=False)
        return op(other_v, val) if is_reverse else op(val, other_v)

    def _unary_op(self, op: Any) -> Any:
        # crosshair: off
        return op(_arith_operand(self, op, wrap_import=True))

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
        # crosshair: off
        return self._unary_op(operator.neg)

    def __pos__(self) -> Any:
        # crosshair: off
        return self._unary_op(operator.pos)

    def __abs__(self) -> Any:
        # crosshair: off
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
        cr = CalcRange(inner, address=addr)
        if "shape" in wire:
            assert cr.shape == tuple(wire["shape"]), f"Shape mismatch: {cr.shape} != {tuple(wire['shape'])}"
        elif "rows" in wire and "cols" in wire:
            assert cr.shape == (wire["rows"], wire["cols"]), f"Shape mismatch: {cr.shape} != ({wire['rows']}, {wire['cols']})"
        return cr

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


def _is_pandas_frame_or_series(obj: Any) -> bool:
    """True for a pandas DataFrame or Series, including subclasses, without importing pandas.

    Host LibreOffice Python has no pandas.
    """
    for cls in type(obj).__mro__:
        if cls.__name__ in ("DataFrame", "Series"):
            mod = getattr(cls, "__module__", None)
            if isinstance(mod, str) and (mod == "pandas" or mod.startswith("pandas.")):
                return True
    return False


@deal.pre(lambda inner: _deal_materialize_inner_ok(inner))
def _materialize_inner_grid(inner: Any) -> list[list[Any]]:
    """Unpack split_grid / ndarray / DataFrame / nested lists to a rectangular ``list[list]``."""
    # crosshair: off
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

    # What was wrong: any object with to_numpy() was converted, and a raised
    # to_numpy was swallowed so the object became a 1×1 cell.
    # How it happened: duck-typing plus ``except Exception: pass``.
    # Why this fixes it: only a pandas DataFrame or Series is a grid here.
    # A failure from that to_numpy propagates. Anything else stays one cell.
    if _is_pandas_frame_or_series(unpacked):
        unpacked = unpacked.to_numpy()

    np: Any = None
    try:
        import numpy as np
    except ImportError:
        pass
    if np is not None and isinstance(unpacked, np.ndarray):
        if unpacked.ndim == 0:
            val = unpacked.item()
            if unpacked.dtype == object:
                val = _numpy_scalar_item(val)
            return [[val]]
        raw = unpacked.tolist()
        if unpacked.dtype == object:
            if unpacked.ndim == 1:
                raw = [_numpy_scalar_item(v) for v in raw]
            else:
                raw = [[_numpy_scalar_item(c) for c in row] for row in raw]
        if unpacked.ndim == 1:
            return [raw]
        return raw

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
    if is_calc_range_payload(wire):
        return (materialize_calc_range(wire),)
    selected: Any = None
    if is_multi_data(wire):
        selected = wire.get("items") or []
    elif isinstance(wire, (list, tuple)) and wire and all(is_calc_range_payload(x) or isinstance(x, CalcRange) for x in wire):
        selected = wire
    elif _is_json_list_of_grids(wire):
        selected = wire
    if selected is not None:
        return tuple(materialize_calc_range(item) for item in selected)
    return (materialize_calc_range(wire),)


@deal.pre(lambda obj: _deal_json_list_of_grids_arg_ok(obj))
def _is_json_list_of_grids(obj: Any) -> bool:
    # crosshair: off
    """True when *obj* is a JSON array of 2D grids (Online =PY multi-range without multi_data).

    A normal 2D sheet block ``[[1, 2], [3, 4]]`` has scalar cells — not a list of grids.
    ``[[[1, 2]], [[3], [4]]]`` is two rectangular ranges.
    """
    if not isinstance(obj, (list, tuple)) or len(obj) < 1:
        return False
    if not all(isinstance(item, (list, tuple)) for item in obj):
        return False
    # At least one item must itself be a 2D grid (first cell is a sequence).
    return any(item and isinstance(item[0], (list, tuple)) and not isinstance(item[0], (str, bytes)) for item in obj)



def _deal_labeled_grid_ok_crosshair(
    columns: object, data: object = None
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
    )


_deal_labeled_grid_ok = _profile(_deal_labeled_grid_ok_crosshair)


@deal.pre(lambda columns, data=None, **__: _deal_labeled_grid_ok(columns, data))
def dataframe_to_labeled_grid(
    columns: list[str],
    data: list[list[Any]] | list[Any] | None,
) -> list[list[Any]]:
    # crosshair: off
    """Build a Calc-ready grid from a dataframe envelope (starts with header row)."""
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
    header = [str(c) for c in columns]
    if body and len(body[0]) != len(header):
        # Pad/truncate header to body width if inconsistent.
        width = len(body[0])
        header = (header + [f"col_{i}" for i in range(len(header), width)])[:width]
    out = [header] + body
    _assert_rectangular(out)
    return out


def _host_ndarray_as_list(value: Any) -> list[Any] | None:
    """Turn a NumPy array into a nested list without importing NumPy on the host.

    ``tolist`` is the array method. A small numeric result that stays an
    ndarray across the pipe fails ``float()`` when the array has more than
    one cell, and ``to_calc_compatible`` then returns the array's text.
    Pandas objects are left alone (their module is not ``numpy``).
    """
    if isinstance(value, (str, bytes, bytearray, list, tuple, dict)) or value is None:
        return None
    module = getattr(type(value), "__module__", "")
    if not (isinstance(module, str) and (module == "numpy" or module.startswith("numpy."))):
        return None
    tolist = getattr(value, "tolist", None)
    if not callable(tolist):
        return None
    try:
        listed = tolist()
    except Exception:
        log.debug("result_to_calc_grid: ndarray tolist failed", exc_info=True)
        return None
    if isinstance(listed, list):
        return listed
    return None


def result_to_calc_grid(result: Any) -> Any:
    """Normalize worker results for Calc consumers.

    DataFrame envelopes become a labeled 2D grid (header row + body).
    Lists pass through. A leftover ndarray body is converted with ``tolist``.
    Unknown iterables (e.g. set, generator) become lists; scalars stay unchanged.
    """
    if is_dataframe_payload(result):
        cols = list(result.get("columns") or [])
        data = result.get("data")
        if not isinstance(data, list):
            listed = _host_ndarray_as_list(data)
            data = listed if listed is not None else []
        return dataframe_to_labeled_grid(cols, data)
    listed = _host_ndarray_as_list(result)
    if listed is not None:
        return listed
    if isinstance(result, (str, bytes, bytearray, dict)):
        return result
    if hasattr(result, "__iter__"):
        try:
            return list(result)
        except Exception:
            return result
    return result


__all__ = [
    "PAYLOAD_CALC_RANGE",
    "CalcRange",
    "dataframe_to_labeled_grid",
    "ensure_rectangular_2d",
    "is_calc_range_payload",
    "materialize_calc_range",
    "materialize_inputs",
    "pack_calc_range_envelope",
    "result_to_calc_grid",
]

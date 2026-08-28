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
from typing import Any

from plugin.framework.deal_shim import DEAL_MAX_SHAPE_DIM, DEAL_MAX_TOKEN, UNDER_CROSSHAIR, ascii_bounded, str_bounded, deal
from plugin.scripting.payload_codec import PAYLOAD_CALC_RANGE, is_calc_range_payload


@deal.post(lambda result: isinstance(result, list))
def ensure_rectangular_2d(grid: Any) -> list[list[Any]]:
    """Normalize any scalar / 1D / 2D input into a rectangular ``list[list]``.

    Orientation is preserved: a single row stays ``[[a, b, c]]``; a single
    column stays ``[[a], [b], [c]]``; a scalar becomes ``[[v]]``.
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
        rows = [list(row) for row in grid]
        width = max((len(row) for row in rows), default=0)
        return [row + [None] * (width - len(row)) for row in rows]
    # Flat sequence → single row (Calc 1D row) unless callers pass column shape.
    return [list(grid)]


@deal.pre(lambda values: isinstance(values, list) and len(values) <= DEAL_MAX_SHAPE_DIM)
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


@deal.pre(
    lambda names: isinstance(names, list)
    and len(names) <= DEAL_MAX_SHAPE_DIM
    and all(str_bounded(x, DEAL_MAX_TOKEN) for x in names)
)
@deal.post(lambda result: isinstance(result, list) and len(set(result)) == len(result))
def _dedupe_column_names(names: list[str]) -> list[str]:
    seen: dict[str, int] = {}
    out: list[str] = []
    for raw in names:
        base = (raw or "column").strip() or "column"
        count = seen.get(base, 0)
        if count:
            out.append(f"{base}_{count}")
        else:
            out.append(base)
        seen[base] = count + 1
    return out


def _deal_grid_values_ok(values: object) -> bool:
    return (
        isinstance(values, list)
        and len(values) <= DEAL_MAX_SHAPE_DIM
        and all(
            isinstance(row, list)
            and len(row) <= DEAL_MAX_SHAPE_DIM
            and all(_deal_inner_grid_cell_ok(c) for c in row)
            for row in values
        )
    )


def _deal_calc_range_other_ok(other: object) -> bool:
    t = type(other)
    if t is type(None) or t is bool:
        return True
    if t is int:
        return -8 <= other <= 8
    if t is float:
        return True
    if t is str:
        return ascii_bounded(other, 4)
    return isinstance(other, CalcRange) and _deal_grid_values_ok(other._values)


def _deal_binary_op_pre(self: Any, other: object) -> bool:
    return _deal_grid_values_ok(self._values) and _deal_calc_range_other_ok(other)


class CalcRange:
    """Rectangular sheet range exposed to user/venv scripts.

    Attributes:
        values: Exact 2D cell values (``None`` for blanks). Never mutates orientation.
        address: Optional source A1 / sheet hint from the host.
        shape: ``(nrows, ncols)``.
    """

    __slots__ = ("_values", "_address")

    def __init__(self, values: Any, *, address: str | None = None) -> None:
        self._values = ensure_rectangular_2d(values)
        self._address = address

    @property
    def values(self) -> list[list[Any]]:
        return self._values

    @property
    def address(self) -> str | None:
        return self._address

    @property
    def shape(self) -> tuple[int, int]:
        nrows = len(self._values)
        ncols = len(self._values[0]) if self._values else 0
        return (nrows, ncols)

    @property
    def nrows(self) -> int:
        return self.shape[0]

    @property
    def ncols(self) -> int:
        return self.shape[1]

    def __repr__(self) -> str:
        r, c = self.shape
        addr = f" address={self._address!r}" if self._address else ""
        return f"CalcRange({r}x{c}{addr})"

    def __len__(self) -> int:
        return self.nrows

    def __iter__(self):
        """Iterate rows (each a list). Does not flatten to cells."""
        return iter(self._values)

    def __getitem__(self, key: Any) -> Any:
        """Row access (``data[0]``) or slice of rows — not cell flattening."""
        return self._values[key]

    def __array__(self, dtype: Any = None) -> Any:
        """NumPy array protocol — enables ``np.mean(data)`` without flattening.

        ``None`` cells become ``nan`` when a numeric dtype is used so ``np.sum`` /
        ``np.mean`` match the historic ndarray ingress behavior.
        """
        return self.to_numpy(dtype=dtype)

    def to_numpy(self, *, dtype: Any = None) -> Any:
        """Explicit NumPy conversion (same as ``np.asarray(range)``)."""
        import numpy as np

        def _cell(v: Any) -> Any:
            if v is None:
                return math.nan
            return v

        grid = [[_cell(v) for v in row] for row in self._values]
        if dtype is not None:
            return np.asarray(grid, dtype=dtype)
        try:
            return np.asarray(grid, dtype=np.float64)
        except (TypeError, ValueError):
            # Mixed / string cells — keep object array with original values (None restored).
            return np.asarray(self._values, dtype=object)

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

    __hash__ = None  # type: ignore[assignment]  # pyright: ignore[reportAssignmentType, reportGeneralTypeIssues]  # CalcRange is mutable / unhashable like ndarray

    def __bool__(self) -> bool:
        if self.shape == (1, 1):
            return bool(self._values[0][0])
        if self.shape == (0, 0) or not self._values:
            return False
        raise ValueError(
            f"The truth value of a CalcRange with shape {self.shape} is ambiguous. "
            "Use data.to_numpy().any() or data.to_numpy().all()"
        )

    def __str__(self) -> str:
        if self.shape == (1, 1):
            return str(self._values[0][0])
        return self.__repr__()

    def __format__(self, format_spec: str) -> str:
        if self.shape == (1, 1):
            return format(self._values[0][0], format_spec)
        return format(str(self), format_spec)

    # Scalar conversions
    def __float__(self) -> float:
        if self.shape == (1, 1):
            val = self._values[0][0]
            if val is None:
                raise TypeError("Cannot convert empty cell (None) to float")
            return float(val)
        raise TypeError(f"Only 1x1 CalcRange can be converted to float, got shape {self.shape}")

    def __int__(self) -> int:
        if self.shape == (1, 1):
            val = self._values[0][0]
            if val is None:
                raise TypeError("Cannot convert empty cell (None) to int")
            return int(val)
        raise TypeError(f"Only 1x1 CalcRange can be converted to int, got shape {self.shape}")

    def __round__(self, ndigits: int | None = None) -> Any:
        if self.shape == (1, 1):
            val = self._values[0][0]
            if val is None:
                raise TypeError("Cannot round empty cell (None)")
            return round(val, ndigits) if ndigits is not None else round(val)
        raise TypeError(f"Only 1x1 CalcRange can be rounded, got shape {self.shape}")

    def __trunc__(self) -> int:
        return math.trunc(self.__float__())

    def __floor__(self) -> int:
        return math.floor(self.__float__())

    def __ceil__(self) -> int:
        return math.ceil(self.__float__())

    # Dispatchers
    def _binary_op(self, other: Any, op: Any, *, is_reverse: bool = False) -> Any:
        if self.shape == (1, 1):
            val = self._values[0][0]
            if isinstance(other, CalcRange):
                if other.shape == (1, 1):
                    other_val = other._values[0][0]
                    return op(other_val, val) if is_reverse else op(val, other_val)
                try:
                    other_arr = other.to_numpy()
                except Exception as exc:
                    raise TypeError(f"Multi-cell arithmetic requires NumPy: {exc}") from exc
                return op(other_arr, val) if is_reverse else op(val, other_arr)
            return op(other, val) if is_reverse else op(val, other)

        try:
            self_arr = self.to_numpy()
        except Exception as exc:
            raise TypeError(f"Multi-cell arithmetic requires NumPy: {exc}") from exc
        if isinstance(other, CalcRange):
            other = other._values[0][0] if other.shape == (1, 1) else other.to_numpy()
        return op(other, self_arr) if is_reverse else op(self_arr, other)

    def _unary_op(self, op: Any) -> Any:
        if self.shape == (1, 1):
            return op(self._values[0][0])
        try:
            return op(self.to_numpy())
        except Exception as exc:
            raise TypeError(f"Multi-cell arithmetic requires NumPy: {exc}") from exc

    # Binary arithmetic
    @deal.pre(_deal_binary_op_pre)
    def __add__(self, other: Any) -> Any:
        return self._binary_op(other, operator.add)

    @deal.pre(_deal_binary_op_pre)
    def __radd__(self, other: Any) -> Any:
        return self._binary_op(other, operator.add, is_reverse=True)

    @deal.pre(_deal_binary_op_pre)
    def __sub__(self, other: Any) -> Any:
        return self._binary_op(other, operator.sub)

    @deal.pre(_deal_binary_op_pre)
    def __rsub__(self, other: Any) -> Any:
        return self._binary_op(other, operator.sub, is_reverse=True)

    @deal.pre(_deal_binary_op_pre)
    def __mul__(self, other: Any) -> Any:
        return self._binary_op(other, operator.mul)

    @deal.pre(_deal_binary_op_pre)
    def __rmul__(self, other: Any) -> Any:
        return self._binary_op(other, operator.mul, is_reverse=True)

    @deal.pre(_deal_binary_op_pre)
    def __truediv__(self, other: Any) -> Any:
        return self._binary_op(other, operator.truediv)

    @deal.pre(_deal_binary_op_pre)
    def __rtruediv__(self, other: Any) -> Any:
        return self._binary_op(other, operator.truediv, is_reverse=True)

    @deal.pre(_deal_binary_op_pre)
    def __floordiv__(self, other: Any) -> Any:
        return self._binary_op(other, operator.floordiv)

    @deal.pre(_deal_binary_op_pre)
    def __rfloordiv__(self, other: Any) -> Any:
        return self._binary_op(other, operator.floordiv, is_reverse=True)

    @deal.pre(_deal_binary_op_pre)
    def __mod__(self, other: Any) -> Any:
        return self._binary_op(other, operator.mod)

    @deal.pre(_deal_binary_op_pre)
    def __rmod__(self, other: Any) -> Any:
        return self._binary_op(other, operator.mod, is_reverse=True)

    @deal.pre(_deal_binary_op_pre)
    def __pow__(self, other: Any) -> Any:
        return self._binary_op(other, operator.pow)

    @deal.pre(_deal_binary_op_pre)
    def __rpow__(self, other: Any) -> Any:
        return self._binary_op(other, operator.pow, is_reverse=True)

    # Unary
    def __neg__(self) -> Any:
        return self._unary_op(operator.neg)

    def __pos__(self) -> Any:
        return self._unary_op(operator.pos)

    def __abs__(self) -> Any:
        return self._unary_op(operator.abs)

    # Rich comparisons (aligned through _binary_op: 1x1 returns bool; multi-cell returns bool ndarray)
    @deal.pre(_deal_binary_op_pre)
    def __eq__(self, other: Any) -> Any:
        return self._binary_op(other, operator.eq)

    @deal.pre(_deal_binary_op_pre)
    def __ne__(self, other: Any) -> Any:
        return self._binary_op(other, operator.ne)

    @deal.pre(_deal_binary_op_pre)
    def __lt__(self, other: Any) -> Any:
        return self._binary_op(other, operator.lt)

    @deal.pre(_deal_binary_op_pre)
    def __le__(self, other: Any) -> Any:
        return self._binary_op(other, operator.le)

    @deal.pre(_deal_binary_op_pre)
    def __gt__(self, other: Any) -> Any:
        return self._binary_op(other, operator.gt)

    @deal.pre(_deal_binary_op_pre)
    def __ge__(self, other: Any) -> Any:
        return self._binary_op(other, operator.ge)


def materialize_calc_range(wire: Any) -> CalcRange:
    """Build a :class:`CalcRange` from a ``calc_range`` envelope or raw grid/split_grid."""
    # crosshair: off
    if isinstance(wire, CalcRange):
        return wire
    if is_calc_range_payload(wire):

        inner = wire.get("data")
        address = wire.get("address")
        if isinstance(address, str) and not address.strip():
            address = None
        addr = address if isinstance(address, str) else None
        return CalcRange(_materialize_inner_grid(inner), address=addr)

    # Legacy / test wires: bare split_grid or nested list (no calc_range wrapper).
    return CalcRange(_materialize_inner_grid(wire))


def _deal_inner_grid_cell_ok_pytest(c: object) -> bool:
    return isinstance(c, (str, int, float, bool, type(None))) or hasattr(c, "dtype")


def _deal_inner_grid_cell_ok_crosshair(c: object) -> bool:
    # cover-all 33127995861: unbounded str/int cells made _materialize 530k lines
    # and compare dunders 90–172k each. Tiny ints + 4-char ascii still hits None/str/int.
    t = type(c)
    if t is type(None):
        return True
    if t is int:
        return -8 <= c <= 8
    if t is str:
        return ascii_bounded(c, 4)
    return False


_deal_inner_grid_cell_ok = _deal_inner_grid_cell_ok_crosshair if UNDER_CROSSHAIR else _deal_inner_grid_cell_ok_pytest


def _deal_json_list_of_grids_arg_ok_pytest(obj: object) -> bool:
    return (not isinstance(obj, (list, tuple, str, bytes, dict, set))) or len(obj) <= DEAL_MAX_SHAPE_DIM


def _deal_json_list_of_grids_arg_ok_crosshair(obj: object) -> bool:
    if type(obj) not in (list, tuple) or len(obj) > DEAL_MAX_SHAPE_DIM:
        return False
    for item in obj:
        if type(item) not in (list, tuple) or len(item) > DEAL_MAX_SHAPE_DIM:
            return False
        for row in item:
            if type(row) in (list, tuple):
                if len(row) > DEAL_MAX_SHAPE_DIM:
                    return False
                if not all(_deal_inner_grid_cell_ok(c) for c in row):
                    return False
            elif not _deal_inner_grid_cell_ok(row):
                return False
    return True


_deal_json_list_of_grids_arg_ok = (
    _deal_json_list_of_grids_arg_ok_crosshair if UNDER_CROSSHAIR else _deal_json_list_of_grids_arg_ok_pytest
)


@deal.pre(
    lambda inner: (type(inner) not in (list, tuple))
    or (
        len(inner) <= DEAL_MAX_SHAPE_DIM
        and all(
            type(r) not in (list, tuple)
            or (
                len(r) <= DEAL_MAX_SHAPE_DIM
                and all(_deal_inner_grid_cell_ok(c) for c in r)
            )
            for r in inner
        )
    )
)
def _materialize_inner_grid(inner: Any) -> list[list[Any]]:
    """Unpack split_grid / ndarray / nested lists to a rectangular ``list[list]``."""
    from plugin.scripting.payload_codec import child_unpack_data, is_split_grid

    if is_split_grid(inner):
        unpacked = child_unpack_data(inner)
    else:
        unpacked = inner

    try:
        import numpy as np

        if isinstance(unpacked, np.ndarray):
            if unpacked.ndim == 0:
                return [[_scalar(unpacked.item())]]
            if unpacked.ndim == 1:
                # 1D ndarray → single row (preserve length); callers that need N×1
                # already pack rectangular 2D before the wire.
                return [[_scalar(v) for v in unpacked.tolist()]]
            return [[_scalar(c) for c in row] for row in unpacked.tolist()]
    except ImportError:
        pass

    if isinstance(unpacked, (list, tuple)):
        return ensure_rectangular_2d(unpacked)
    return ensure_rectangular_2d([[unpacked]])


def _scalar(v: Any) -> Any:
    try:
        import numpy as np

        if isinstance(v, np.generic):
            return v.item()
    except Exception:
        pass
    return v


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


@deal.pre(
    lambda columns, data=None, include_header=True, **__: type(columns) is list
    and len(columns) <= DEAL_MAX_SHAPE_DIM
    and all(str_bounded(c, DEAL_MAX_TOKEN) for c in columns)
    and (
        data is None
        or (
            type(data) is list
            and len(data) <= DEAL_MAX_SHAPE_DIM
            and all(
                (
                    type(row) is list
                    and len(row) <= DEAL_MAX_SHAPE_DIM
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
def dataframe_to_labeled_grid(
    columns: list[str],
    data: list[list[Any]] | list[Any] | None,
    *,
    include_header: bool = True,
) -> list[list[Any]]:
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
    "_dedupe_column_names",
]

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

from typing import Any

PAYLOAD_CALC_RANGE = "calc_range"


def ensure_rectangular_2d(grid: Any) -> list[list[Any]]:
    """Normalize any scalar / 1D / 2D input into a rectangular ``list[list]``.

    Orientation is preserved: a single row stays ``[[a, b, c]]``; a single
    column stays ``[[a], [b], [c]]``; a scalar becomes ``[[v]]``.
    """
    if grid is None:
        return []
    if isinstance(grid, (str, bytes)) or not isinstance(grid, (list, tuple)):
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


def column_vector_as_2d(values: list[Any]) -> list[list[Any]]:
    """Wrap a flat column vector as ``[[v], …]`` (N×1)."""
    return [[v] for v in values]


def is_calc_range_payload(obj: Any) -> bool:
    if not isinstance(obj, dict):
        return False
    if obj.get("__wa_payload__") != PAYLOAD_CALC_RANGE:
        return False
    shape = obj.get("shape")
    if not isinstance(shape, list) or len(shape) != 2:
        return False
    if not all(isinstance(d, int) and d >= 0 for d in shape):
        return False
    return "data" in obj


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
    rows = ensure_rectangular_2d(grid)
    nrows = len(rows)
    ncols = len(rows[0]) if rows else 0
    inner = pack_inner(rows) if pack_inner is not None else rows
    envelope: dict[str, Any] = {
        "__wa_payload__": PAYLOAD_CALC_RANGE,
        "shape": [nrows, ncols],
        "data": inner,
    }
    if address:
        envelope["address"] = str(address)
    return envelope


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
        import math

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
    ) -> Any:
        """Convert to a pandas DataFrame with an explicit header policy.

        Args:
            header_row: Row index used as column names, or ``None`` for
                synthetic ``col_0..col_n`` names (all rows are data).
            index_col: Optional column to use as the DataFrame index.
            parse_strings: When True, apply optional currency/percent/numeric
                and datetime string parsing. Default False keeps text cells as text.
        """
        from plugin.scripting.venv.coerce import grid_to_dataframe

        return grid_to_dataframe(
            self._values,
            header_row=header_row,
            index_col=index_col,
            parse_strings=parse_strings,
            sheet_hint=self._address,
        ).df


def materialize_calc_range(wire: Any) -> CalcRange:
    """Build a :class:`CalcRange` from a ``calc_range`` envelope or raw grid/split_grid."""
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
    """Materialize worker ``data`` wire into a stable ``inputs`` tuple of CalcRange.

    - ``calc_range`` → ``(range,)``
    - ``multi_data`` of ranges/grids → one CalcRange per item
    - JSON list-of-2D-grids (Online compute) → one CalcRange per item
    - bare grid / list → single CalcRange
    - ``None`` → empty tuple
    """
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

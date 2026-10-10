# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Range inspection, broadcasting, and mapping for trusted venv helpers.

Provides polymorphic execution over scalars, Calc ranges, 1D/2D lists, and
NumPy/pandas sequences while preserving orientation (N x 1 column in ->
N x 1 column out) and handling blanks and LibreOffice error tokens.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

from plugin.scripting.calc_range import CalcRange, ensure_rectangular_2d
from plugin.scripting.venv.coerce import is_missing_value

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class InspectedInput:
    """Structural metadata of an inspected argument for rewrapping."""

    flat_items: list[Any]
    is_scalar: bool
    is_python_1d: bool
    nrows: int
    ncols: int
    length: int = 0

    def __post_init__(self) -> None:
        if not self.length and self.flat_items:
            object.__setattr__(self, "length", len(self.flat_items))


class BroadcastResult(tuple[Any, ...]):
    """Result tuple from broadcast_args supporting 3-element unpacking and vector metadata."""

    primary: InspectedInput
    b_args: list[list[Any]]
    b_kwargs: dict[str, list[Any]]
    vector_arg_indices: list[int]
    vector_kwarg_keys: list[str]

    def __new__(
        cls,
        primary: InspectedInput,
        b_args: list[list[Any]],
        b_kwargs: dict[str, list[Any]],
        vector_arg_indices: list[int],
        vector_kwarg_keys: list[str],
    ) -> BroadcastResult:
        instance = super().__new__(cls, (primary, b_args, b_kwargs))
        instance.primary = primary
        instance.b_args = b_args
        instance.b_kwargs = b_kwargs
        instance.vector_arg_indices = vector_arg_indices
        instance.vector_kwarg_keys = vector_kwarg_keys
        return instance


def _to_plain_sequence(val: Any) -> Any:
    """Unwrap NumPy ndarray, pandas Series/DataFrame to nested lists."""
    if isinstance(val, CalcRange):
        return val
    # DataFrame and Series have no tolist(). to_numpy().tolist() is the plain list.
    if hasattr(val, "to_numpy") and callable(val.to_numpy):
        try:
            arr: Any = val.to_numpy()
            return arr.tolist()
        except Exception:
            pass
    if hasattr(val, "tolist") and callable(val.tolist):
        try:
            return val.tolist()
        except Exception:
            pass
    if hasattr(val, "to_list") and callable(val.to_list):
        try:
            return val.to_list()
        except Exception:
            pass
    return val


def inspect_input(val: Any) -> InspectedInput:
    """Inspect input shape and flatten to 1D items while tracking orientation.

    Rules:
    - Scalar (number, str, bool, None) -> scalar (nrows=1, ncols=1).
    - 1x1 CalcRange or 2D list [[v]] -> scalar (nrows=1, ncols=1).
    - 1D Python list/tuple -> python_1d (nrows=1, ncols=len).
    - N x 1 CalcRange / 2D list (N >= 2) -> column (nrows=N, ncols=1).
    - 1 x N CalcRange / 2D list (N >= 2) -> row (nrows=1, ncols=N).
    - M x N 2D grid (M, N >= 2) -> grid (nrows=M, ncols=N).
    """
    val = _to_plain_sequence(val)

    if val is None or isinstance(val, (str, bytes, int, float, bool)):
        return InspectedInput(flat_items=[val], is_scalar=True, is_python_1d=False, nrows=1, ncols=1, length=1)

    grid: Any
    if isinstance(val, CalcRange):
        grid = val.values
    elif isinstance(val, (list, tuple)):
        grid = val
    else:
        # Unknown non-sequence object
        return InspectedInput(flat_items=[val], is_scalar=True, is_python_1d=False, nrows=1, ncols=1, length=1)

    if not grid:
        return InspectedInput(flat_items=[], is_scalar=False, is_python_1d=True, nrows=0, ncols=0, length=0)

    # Distinguish 1D list from 2D list
    if not isinstance(grid[0], (list, tuple)):
        # Plain 1D Python sequence: [10, 20] or [10]
        items = list(grid)
        return InspectedInput(flat_items=items, is_scalar=False, is_python_1d=True, nrows=1, ncols=len(items), length=len(items))

    # 2D sequence or CalcRange
    rect_grid = ensure_rectangular_2d(grid)
    nrows = len(rect_grid)
    ncols = len(rect_grid[0]) if rect_grid else 0

    if nrows == 1 and ncols == 1:
        # 1x1 CalcRange / [[v]] is treated as scalar for single-cell formulas
        return InspectedInput(flat_items=[rect_grid[0][0]], is_scalar=True, is_python_1d=False, nrows=1, ncols=1, length=1)

    flat = [cell for row in rect_grid for cell in row]
    return InspectedInput(flat_items=flat, is_scalar=False, is_python_1d=False, nrows=nrows, ncols=ncols, length=len(flat))


def rewrap_output(results: list[Any], inspected: InspectedInput) -> Any:
    """Reconstruct output matching the input shape and orientation.

    - Scalar -> single value.
    - 1D list -> 1D list.
    - N x 1 column -> [[r1], [r2], ...].
    - 1 x N row -> [[r1, r2, ...]].
    - M x N grid -> [[...], [...]].
    """
    if inspected.is_scalar:
        return results[0] if results else None
    if inspected.is_python_1d:
        return list(results)
    if inspected.ncols == 1:
        return [[r] for r in results]
    if inspected.nrows == 1:
        return [list(results)]

    # M x N grid
    nrows = inspected.nrows
    ncols = inspected.ncols
    return [list(results[i * ncols : (i + 1) * ncols]) for i in range(nrows)]


def broadcast_args(
    *args: Any,
    **kwargs: Any,
) -> tuple[InspectedInput, list[list[Any]], dict[str, list[Any]]]:
    """Inspect arguments and broadcast scalars across vector length.

    Returns:
        (primary_inspected, broadcasted_args, broadcasted_kwargs)
    Raises:
        ValueError: If vector lengths mismatch, M×N grid is paired with a non-equal-shape vector,
            or vector orientations mismatch (e.g. 1×N row vs N×1 column).
    """
    inspected_args = [inspect_input(a) for a in args]
    inspected_kwargs = {k: inspect_input(v) for k, v in kwargs.items()}

    all_inspected = inspected_args + list(inspected_kwargs.values())

    vector_items = [insp for insp in all_inspected if not insp.is_scalar]
    vector_arg_indices = [i for i, insp in enumerate(inspected_args) if not insp.is_scalar]
    vector_kwarg_keys = [k for k, insp in inspected_kwargs.items() if not insp.is_scalar]

    if not vector_items:
        # All scalar
        primary = inspected_args[0] if inspected_args else (list(inspected_kwargs.values())[0] if inspected_kwargs else inspect_input(None))
        scalar_args = [[insp.flat_items[0]] for insp in inspected_args]
        scalar_kwargs = {k: [insp.flat_items[0]] for k, insp in inspected_kwargs.items()}
        return BroadcastResult(primary, scalar_args, scalar_kwargs, vector_arg_indices, vector_kwarg_keys)

    # Compare (nrows, ncols), not just length. Equal-shape grids are valid.
    # A 1×N vector zipped with an N×1 vector is an orientation mismatch.
    target_shape = (vector_items[0].nrows, vector_items[0].ncols)
    target_length = vector_items[0].length
    primary = vector_items[0]

    for insp in vector_items[1:]:
        if (insp.nrows, insp.ncols) != target_shape:
            has_grid = (insp.nrows > 1 and insp.ncols > 1) or (target_shape[0] > 1 and target_shape[1] > 1)
            if has_grid:
                raise ValueError("Cannot pair an M×N grid with a vector parameter; broadcast only with scalars.")
            if insp.length != target_length:
                raise ValueError(f"Vector length mismatch: {insp.length} != {target_length}")
            raise ValueError(f"Vector orientation mismatch: {(insp.nrows, insp.ncols)} != {target_shape}")

    b_args: list[list[Any]] = []
    for insp in inspected_args:
        if insp.is_scalar:
            scalar_val = insp.flat_items[0] if insp.flat_items else None
            b_args.append([scalar_val] * target_length)
        else:
            b_args.append(list(insp.flat_items))

    b_kwargs: dict[str, list[Any]] = {}
    for k, insp in inspected_kwargs.items():
        if insp.is_scalar:
            scalar_val = insp.flat_items[0] if insp.flat_items else None
            b_kwargs[k] = [scalar_val] * target_length
        else:
            b_kwargs[k] = list(insp.flat_items)

    return BroadcastResult(primary, b_args, b_kwargs, vector_arg_indices, vector_kwarg_keys)


def map_over_range(
    fn: Callable[..., Any],
    *args: Any,
    handle_blanks: bool = True,
    return_errors: bool = True,
    **kwargs: Any,
) -> Any:
    """Map a scalar function elementwise over scalar/range/vector inputs.

    Args:
        fn: Scalar compute function.
        *args: Positional arguments (may be scalars or ranges).
        handle_blanks: If True, missing cells (None, "", NaN, error tokens)
            produce empty string "" without calling fn.
        return_errors: If True, per-element exceptions return "#VALUE!" (or "#DIV/0!").
        **kwargs: Keyword arguments (may be scalars or ranges).

    Returns:
        Scalar or list/nested list matching the primary vector shape.
    """
    broadcast = broadcast_args(*args, **kwargs)
    primary, b_args, b_kwargs = broadcast[0], broadcast[1], broadcast[2]
    vector_arg_indices = getattr(broadcast, "vector_arg_indices", [])
    vector_kwarg_keys = getattr(broadcast, "vector_kwarg_keys", [])
    n_items = primary.length

    results: list[Any] = []
    for i in range(n_items):
        row_args = [arg_list[i] for arg_list in b_args]
        row_kwargs = {k: kwarg_list[i] for k, kwarg_list in b_kwargs.items()}

        if handle_blanks:
            # A missing value in any vector argument blanks this row. When every
            # argument is a scalar, only the primary input can blank the row.
            if vector_arg_indices or vector_kwarg_keys:
                is_blank = any(is_missing_value(row_args[idx]) for idx in vector_arg_indices) or any(
                    is_missing_value(row_kwargs[k]) for k in vector_kwarg_keys
                )
            else:
                main_val = row_args[0] if row_args else (next(iter(row_kwargs.values())) if row_kwargs else None)
                is_blank = is_missing_value(main_val)
            if is_blank:
                results.append("")
                continue

        try:
            res = fn(*row_args, **row_kwargs)
            results.append(res)
        except ZeroDivisionError as exc:
            # #DIV/0!, not a generic #VALUE!.
            log.debug("map_over_range division by zero: %s", exc, exc_info=True)
            if return_errors:
                results.append("#DIV/0!")
            else:
                raise
        except Exception as exc:
            # Log before returning #VALUE! so the cell failure is visible.
            log.debug("map_over_range evaluation error: %s", exc, exc_info=True)
            if return_errors:
                results.append("#VALUE!")
            else:
                raise

    return rewrap_output(results, primary)

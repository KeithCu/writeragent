# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Single wire serialization walker for pickle and JSON boundaries."""
from __future__ import annotations

import base64
import datetime
import decimal
import fractions
import math
import sys
from typing import Any, Literal

from plugin.framework.ipc import HOST_PICKLE_LEAF_TYPES
from plugin.scripting.payload_codec import (
    ForceBinary,
    PAYLOAD_DATAFRAME,
    _is_numeric_wire_kind,
    child_pack_result,
    wire_str_key,
)
from plugin.scripting.venv import optional_import
from plugin.scripting.venv.coerce import dedupe_column_names, flat_label

_MAX_DEPTH = 64


def _set_or_list(original: set[Any] | frozenset[Any], elements: list[Any]) -> Any:
    """Rebuild *original*'s set type, or a list when an element is unhashable.

    ``range`` becomes a list and a matplotlib Figure becomes a dict. Either
    is unhashable, and rebuilding the set would raise after the cell already
    succeeded. Hashable elements stay a set. Unhashable ones become a list
    the host unpickler accepts.
    """
    try:
        return type(original)(elements)
    except TypeError:
        return elements


def _dtype_kind(obj: Any) -> str | None:
    dtype = getattr(obj, "dtype", None)
    kind = getattr(dtype, "kind", None)
    return kind if isinstance(kind, str) else None


def _strip_datetime_tz(dt: datetime.datetime) -> datetime.datetime:
    if dt.tzinfo is not None:
        return dt.replace(tzinfo=None)
    return dt


_TD64_US_PER_COUNT: dict[str, fractions.Fraction] = {
    "ns": fractions.Fraction(1, 1000),
    "us": fractions.Fraction(1, 1),
    "ms": fractions.Fraction(1000, 1),
    "s": fractions.Fraction(10**6, 1),
    "m": fractions.Fraction(60 * 10**6, 1),
    "h": fractions.Fraction(3600 * 10**6, 1),
    "D": fractions.Fraction(86400 * 10**6, 1),
    "W": fractions.Fraction(7 * 86400 * 10**6, 1),
    "ps": fractions.Fraction(1, 10**6),
    "fs": fractions.Fraction(1, 10**9),
    "as": fractions.Fraction(1, 10**12),
    "Y": fractions.Fraction(31556952000000, 1),
    "M": fractions.Fraction(2629746000000, 1),
}


def _timedelta64_to_stdlib(value: Any) -> datetime.timedelta | None:
    """Convert numpy.timedelta64 to datetime.timedelta or None.

    Converts numpy timedelta64 to datetime.timedelta via the microsecond table when pandas is absent or rejects the unit, so wire values are durations rather than raw integer counts.
    """
    item_fn = getattr(value, "item", None)
    item = item_fn() if callable(item_fn) else None
    if str(value) == "NaT" or item is None:
        return None
    if isinstance(item, datetime.timedelta):
        return item

    dtype = getattr(value, "dtype", None)
    dtype_name = getattr(dtype, "name", "")
    if dtype_name == "timedelta64":
        unit = "ns"
    elif dtype_name.startswith("timedelta64[") and dtype_name.endswith("]"):
        unit = dtype_name[len("timedelta64[") : -1]
    else:
        unit = ""

    scale = _TD64_US_PER_COUNT.get(unit)
    if scale is None or not isinstance(item, int):
        raise ValueError(
            f"numpy.timedelta64 value {value!r} cannot cross the LibreOffice pickle boundary"
        )
    try:
        us = float(item * scale)
        return datetime.timedelta(microseconds=us)
    except OverflowError as err:
        raise ValueError(
            f"numpy.timedelta64 value {value!r} cannot cross the LibreOffice pickle boundary"
        ) from err


def _temporal_cell_to_stdlib(value: Any, pd_mod: Any) -> Any:
    """Convert pandas/numpy temporal values to stdlib types the host can pickle.

    LibreOffice's embedded Python has no pandas/numpy, so Timestamp/datetime64
    must not cross the Pickle5 boundary as native objects.
    """
    try:
        if pd_mod is not None and pd_mod.isna(value):
            return None
    except Exception:
        pass
    if isinstance(value, datetime.datetime):
        return _strip_datetime_tz(value).isoformat()
    if isinstance(value, datetime.date):
        return value.isoformat()
    if isinstance(value, datetime.timedelta):
        return value
    to_pydt = getattr(value, "to_pydatetime", None)
    if callable(to_pydt):
        try:
            dt = to_pydt()
            if isinstance(dt, datetime.datetime):
                return _strip_datetime_tz(dt).isoformat()
            if isinstance(dt, datetime.date):
                return dt.isoformat()
            return dt
        except Exception:
            pass
    to_pytd = getattr(value, "to_pytimedelta", None)
    if callable(to_pytd):
        try:
            return to_pytd()
        except Exception:
            pass
    kind = _dtype_kind(value)
    if kind == "M":
        try:
            if pd_mod is not None:
                ts = pd_mod.Timestamp(value)
                if pd_mod.isna(ts):
                    return None
                return _strip_datetime_tz(ts.to_pydatetime()).isoformat()
        except Exception:
            pass
        text = str(value)
        return None if text == "NaT" else text
    if kind == "m":
        return _timedelta64_to_stdlib(value)
    return value


def _temporal_ndarray_to_python(arr: Any, pd_mod: Any) -> Any:
    """datetime64/timedelta64 ndarray → nested Python lists of stdlib values."""
    if arr.ndim == 0:
        # arr[()] keeps the numpy scalar and its dtype. arr.item() on
        # datetime64[ns] / timedelta64[ns] is a Python int, so the cell
        # would become a raw count before _temporal_cell_to_stdlib.
        # Rank 1+ already iterates those scalars.
        return _temporal_cell_to_stdlib(arr[()], pd_mod)
    if arr.ndim > 2:
        # One plane at a time, same as child_pack_result. shape[0] by shape[1]
        # drops every axis after the second. The caller turns timedelta into
        # fractional days.
        return [_temporal_ndarray_to_python(arr[i], pd_mod) for i in range(int(arr.shape[0]))]
    # Iterate datetime64 scalars — .tolist() on datetime64[ns] yields Python ints (ns), not datetimes.
    flat = [_temporal_cell_to_stdlib(v, pd_mod) for v in arr.ravel()]
    if arr.ndim == 1:
        return flat
    nrows, ncols = int(arr.shape[0]), int(arr.shape[1])
    return [flat[i * ncols : (i + 1) * ncols] for i in range(nrows)]


def _figure_to_image_payload(fig: Any, *, fmt: str = "svg") -> dict[str, Any]:
    """Render a matplotlib Figure to an image payload envelope.

    *fmt* ``"svg"`` (default) produces resolution-independent vector graphics that
    render crisply at any zoom in LibreOffice Calc/Writer.  ``"png"`` produces a
    150 DPI raster, preferred when the consumer cannot handle SVG (e.g. chat HTML).
    """
    import io

    buf = io.BytesIO()
    if fmt == "svg":
        fig.savefig(buf, format="svg", bbox_inches="tight")
    else:
        fig.savefig(buf, format="png", bbox_inches="tight", dpi=150)
    buf.seek(0)
    return {"__wa_payload__": "image", "format": fmt, "data": buf.read()}


def _pil_image_to_payload(img: Any) -> dict[str, Any]:
    """Convert a PIL Image to an image payload dict."""
    import io
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return {"__wa_payload__": "image", "format": "png", "data": buf.getvalue()}


def _tree_contains_dict(tree: Any) -> bool:
    if isinstance(tree, dict):
        return True
    if isinstance(tree, (list, tuple, set, frozenset)):
        return any(_tree_contains_dict(item) for item in tree)
    return False


def _json_clean_packed(val: Any) -> Any:
    if isinstance(val, float):
        return None if not math.isfinite(val) else val
    if isinstance(val, list):
        return [_json_clean_packed(x) for x in val]
    if isinstance(val, tuple):
        return tuple(_json_clean_packed(x) for x in val)
    if isinstance(val, dict):
        return {k: _json_clean_packed(v) for k, v in val.items()}
    return val


def to_wire(
    obj: Any,
    *,
    target: Literal["pickle", "json"],
    force: ForceBinary = "auto",
) -> Any:
    """Convert a Python value to wire-safe format for pickle or JSON.

    One recursion, depth-capped at 64, with cycle detection.
    """
    from plugin.scripting.calc_range import CalcRange, is_calc_range_payload

    effective_force: ForceBinary = "never" if target == "json" else force
    seen: set[int] = set()

    def _walk(val: Any, depth: int, is_packing_root: bool) -> Any:
        if depth > _MAX_DEPTH:
            raise ValueError("Result is too deeply nested to cross the LibreOffice pickle boundary")

        # 1. CalcRange / calc-range payload
        if isinstance(val, CalcRange):
            if val.shape == (1, 1) and val.values and val.values[0]:
                return _walk(val.values[0][0], depth, is_packing_root)
            return _walk(val.values, depth, True)

        if target == "pickle" and is_calc_range_payload(val):
            return val

        # 2. Matplotlib Figure
        mpl_fig = sys.modules.get("matplotlib.figure")
        if mpl_fig is not None and isinstance(val, mpl_fig.Figure):
            payload = _figure_to_image_payload(val)
            if target == "json":
                payload["data"] = base64.b64encode(payload["data"]).decode("ascii")
            return payload

        # 3. PIL Image
        pil_mod = optional_import.optional_module("PIL.Image")
        if pil_mod is not None and isinstance(val, pil_mod.Image):
            payload = _pil_image_to_payload(val)
            if target == "json":
                payload["data"] = base64.b64encode(payload["data"]).decode("ascii")
            return payload

        # 4. ndarray
        np_mod = optional_import.optional_module("numpy")
        pd_mod = optional_import.optional_module("pandas")

        if np_mod is not None and isinstance(val, np_mod.ndarray):
            kind = _dtype_kind(val)
            if kind in ("M", "m"):
                py_tree = _temporal_ndarray_to_python(val, pd_mod)
                return _walk(py_tree, depth, is_packing_root=True)
            if kind == "O":
                packed = child_pack_result(val, force=effective_force)
                if isinstance(packed, dict):
                    if target == "json":
                        packed = _json_clean_packed(packed)
                    return packed
                return _walk(packed, depth, is_packing_root=False)
            if kind is not None and _is_numeric_wire_kind(kind):
                packed = child_pack_result(val, force=effective_force)
                if target == "json":
                    packed = _json_clean_packed(packed)
                return packed
            return _walk(val.tolist(), depth, is_packing_root=True)

        # 5. np.datetime64 / np.timedelta64 before np.integer
        if np_mod is not None:
            if isinstance(val, (np_mod.datetime64, np_mod.timedelta64)):
                converted = _temporal_cell_to_stdlib(val, pd_mod)
                if converted is not val:
                    return _walk(converted, depth, is_packing_root)
            elif isinstance(val, (np_mod.integer, np_mod.floating, np_mod.bool_)):
                packed = child_pack_result(val, force=effective_force)
                if target == "json":
                    packed = _json_clean_packed(packed)
                return packed

        # 6. DataFrame
        if pd_mod is not None and isinstance(val, pd_mod.DataFrame):
            df: Any = val
            columns = dedupe_column_names([flat_label(c) for c in df.columns])

            def _dataframe_cell(cell_val: Any) -> Any:
                return _temporal_cell_to_stdlib(cell_val, pd_mod)

            if len(df) == 0 or len(df.columns) == 0:
                data_part: Any = []
            else:
                try:
                    arr = df.to_numpy(copy=False)
                    kind = _dtype_kind(arr)
                    if kind is not None and _is_numeric_wire_kind(kind):
                        data_part = child_pack_result(arr, force=effective_force)
                        if target == "json":
                            data_part = _json_clean_packed(data_part)
                    else:
                        grid = [
                            [_dataframe_cell(cell) for cell in row]
                            for row in df.itertuples(index=False, name=None)
                        ]
                        data_part = _walk(grid, depth + 1, is_packing_root=True)
                except Exception:
                    grid = [
                        [_dataframe_cell(cell) for cell in row]
                        for row in df.itertuples(index=False, name=None)
                    ]
                    data_part = _walk(grid, depth + 1, is_packing_root=True)
            return {
                "__wa_payload__": PAYLOAD_DATAFRAME,
                "columns": columns,
                "data": data_part,
            }

        # 7. Series
        if pd_mod is not None and isinstance(val, pd_mod.Series):
            s: Any = val
            name = getattr(s, "name", None)
            if len(s) == 0:
                s_packed: Any = []
            else:
                def _series_cell(cell_val: Any) -> Any:
                    return _temporal_cell_to_stdlib(cell_val, pd_mod)

                try:
                    arr = s.to_numpy(copy=False)
                    kind = _dtype_kind(arr)
                    if kind is not None and _is_numeric_wire_kind(kind):
                        s_packed = child_pack_result(arr, force=effective_force)
                        if target == "json":
                            s_packed = _json_clean_packed(s_packed)
                    else:
                        grid = [_series_cell(v) for v in s.tolist()]
                        s_packed = _walk(grid, depth + 1, is_packing_root=True)
                except Exception:
                    grid = [_series_cell(v) for v in s.tolist()]
                    s_packed = _walk(grid, depth + 1, is_packing_root=True)
            if name is not None:
                return {
                    "__wa_payload__": PAYLOAD_DATAFRAME,
                    "columns": [flat_label(name)],
                    "data": s_packed,
                }
            return s_packed

        # 8. Containers
        if isinstance(val, (dict, list, tuple, set, frozenset)):
            oid = id(val)
            if oid in seen:
                raise ValueError(
                    "Result contains a self-referential container and cannot cross "
                    "the LibreOffice pickle boundary"
                )
            seen.add(oid)
            try:
                if isinstance(val, dict):
                    used: set[str] = set()
                    out_dict: dict[str, Any] = {}
                    for key, item in val.items():
                        sk = wire_str_key(key, used)
                        out_dict[sk] = _walk(item, depth + 1, is_packing_root=True)
                    return out_dict
                if isinstance(val, (set, frozenset)):
                    elements = [_walk(item, depth + 1, is_packing_root=True) for item in val]
                    if target == "json":
                        return elements
                    return _set_or_list(val, elements)
                if isinstance(val, list):
                    rebuilt_list = [_walk(item, depth + 1, is_packing_root=False) for item in val]
                    if is_packing_root and not _tree_contains_dict(rebuilt_list):
                        packed_list = child_pack_result(rebuilt_list, force=effective_force)
                        if target == "json":
                            packed_list = _json_clean_packed(packed_list)
                        return packed_list
                    return rebuilt_list
                if isinstance(val, tuple):
                    rebuilt_tuple = tuple(
                        _walk(item, depth + 1, is_packing_root=False) for item in val
                    )
                    if target == "json":
                        return rebuilt_tuple
                    if is_packing_root and not _tree_contains_dict(rebuilt_tuple):
                        return child_pack_result(rebuilt_tuple, force=effective_force)
                    return rebuilt_tuple
            finally:
                seen.discard(oid)

        # 9. Scalars
        if target == "pickle" and isinstance(val, HOST_PICKLE_LEAF_TYPES):
            return val
        if val is None:
            return None
        if isinstance(val, bool):
            return val
        if isinstance(val, int):
            return val
        if isinstance(val, float):
            return None if not math.isfinite(val) else val
        if isinstance(val, str):
            return val
        if isinstance(val, (bytes, bytearray)):
            return base64.b64encode(val).decode("ascii")
        if isinstance(val, complex):
            return val

        if isinstance(val, datetime.datetime):
            return _strip_datetime_tz(val).isoformat()
        if isinstance(val, datetime.date):
            return val.isoformat()
        if isinstance(val, datetime.time):
            return val.isoformat()
        if isinstance(val, datetime.timedelta):
            return val.total_seconds() / 86400.0
        if isinstance(val, (decimal.Decimal, fractions.Fraction)):
            flt = float(val)
            if target == "json":
                return None if not math.isfinite(flt) else flt
            return flt
        if isinstance(val, range):
            return _walk(list(val), depth, is_packing_root)

        converted_scalar = _temporal_cell_to_stdlib(val, pd_mod)
        if converted_scalar is not val:
            return _walk(converted_scalar, depth, is_packing_root)

        if np_mod is not None and isinstance(val, np_mod.generic):
            if type(val).__name__ == "clongdouble":
                raise ValueError(
                    "numpy.clongdouble cannot cross the LibreOffice pickle boundary: "
                    ".item() returns another clongdouble, not a builtin complex"
                )
            try:
                plain = val.item()
            except Exception:
                plain = val
            if plain is not val and not isinstance(plain, np_mod.generic):
                return _walk(plain, depth, is_packing_root)
            raise ValueError(
                f"Result type {type(val).__module__}.{type(val).__name__} "
                "cannot cross the LibreOffice pickle boundary"
            )

        if type(val).__name__ == "clongdouble":
            raise ValueError(
                "numpy.clongdouble cannot cross the LibreOffice pickle boundary: "
                ".item() returns another clongdouble, not a builtin complex"
            )
        raise ValueError(
            f"Result type {type(val).__module__}.{type(val).__name__} "
            "cannot cross the LibreOffice pickle boundary"
        )

    return _walk(obj, 0, is_packing_root=True)

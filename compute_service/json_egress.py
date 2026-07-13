# WriterAgent - Python Compute Service
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Convert sandboxed execution results to kit-safe dumb JSON (no split_grid / ndarray)."""

from __future__ import annotations

import base64
import math
from typing import Any

from plugin.scripting.payload_codec import (
    PAYLOAD_DATAFRAME,
    find_image_payloads,
    host_unpack_data,
    is_dataframe_payload,
    is_image_payload,
    is_multi_data,
    is_split_grid,
)


def _is_ndarray(obj: object) -> bool:
    return type(obj).__name__ == "ndarray" and getattr(type(obj), "__module__", "") == "numpy"


def _finite_or_none(x: float) -> float | None:
    if math.isnan(x) or math.isinf(x):
        return None
    return x


def sanitize_for_strict_json(obj: Any) -> Any:
    """Replace NaN/Inf with null; leave other values recursive-ready for json.dumps(allow_nan=False)."""
    if isinstance(obj, float):
        return _finite_or_none(obj)
    if isinstance(obj, dict):
        return {str(k): sanitize_for_strict_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [sanitize_for_strict_json(x) for x in obj]
    return obj


def _ndarray_to_lists(arr: Any) -> Any:
    """Convert ndarray to nested Python lists with NaN/Inf → null."""
    import numpy as np

    if arr.ndim == 0:
        val = arr.item()
        if isinstance(val, float):
            return _finite_or_none(val)
        if isinstance(val, (np.integer,)):
            return int(val)
        if isinstance(val, (np.bool_,)):
            return bool(val)
        return val
    # object / string arrays: tolist then sanitize
    as_list = arr.tolist()
    return sanitize_for_strict_json(as_list)


def _image_to_json(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data")
    if isinstance(data, bytes):
        data_b64 = base64.b64encode(data).decode("ascii")
    elif isinstance(data, str):
        data_b64 = data
    else:
        data_b64 = ""
    return {
        "format": str(payload.get("format") or "png"),
        "data_b64": data_b64,
    }


def to_dumb_json_value(obj: Any) -> Any:
    """Unpack desktop wire envelopes / ndarrays into plain JSON-friendly trees."""
    if obj is None or isinstance(obj, (str, bool, int)):
        return obj
    if isinstance(obj, float):
        return _finite_or_none(obj)
    if _is_ndarray(obj):
        return _ndarray_to_lists(obj)
    if is_image_payload(obj):
        return _image_to_json(obj)
    if is_split_grid(obj):
        return sanitize_for_strict_json(host_unpack_data(obj, as_nested_list=True))
    if is_multi_data(obj):
        items = obj.get("items") or []
        return [to_dumb_json_value(x) for x in items]
    if is_dataframe_payload(obj):
        # Kit spill wants a grid; return data matrix (and columns as sibling if useful).
        cols = obj.get("columns") or []
        data = to_dumb_json_value(obj.get("data"))
        if cols:
            return {"__wa_payload__": PAYLOAD_DATAFRAME, "columns": list(cols), "data": data}
        return data
    if isinstance(obj, dict):
        # Desktop may still leave nested envelopes.
        if obj.get("__wa_payload__") == "image":
            return _image_to_json(obj)
        return {str(k): to_dumb_json_value(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_dumb_json_value(x) for x in obj]
    # numpy scalars that slipped through
    mod = getattr(type(obj), "__module__", "")
    if mod == "numpy":
        try:
            import numpy as np

            if isinstance(obj, (np.integer,)):
                return int(obj)
            if isinstance(obj, (np.floating,)):
                return _finite_or_none(float(obj))
            if isinstance(obj, np.bool_):
                return bool(obj)
        except Exception:
            pass
    return obj


def normalize_execute_response(payload: dict[str, Any]) -> dict[str, Any]:
    """Map venv_sandbox payload → §8 HTTP response {status, result|error, stdout?, images?}."""
    status = payload.get("status")
    if status != "ok":
        err = payload.get("error") or payload.get("message") or "execution failed"
        out: dict[str, Any] = {
            "status": "error",
            "error": str(err),
            "stdout": payload.get("stdout") or "",
        }
        # Keep message for older tests during transition
        out["message"] = out["error"]
        return sanitize_for_strict_json(out)

    result = payload.get("result")
    images: list[dict[str, Any]] = []

    # Promote image payloads to top-level images[]; clear result if it *is* the image.
    if is_image_payload(result) or (isinstance(result, dict) and result.get("__wa_payload__") == "image"):
        images.append(_image_to_json(result if isinstance(result, dict) else {}))
        result_out: Any = None
    elif is_multi_data(result) and all(is_image_payload(x) or (isinstance(x, dict) and x.get("__wa_payload__") == "image") for x in (result.get("items") or [])):
        for item in result.get("items") or []:
            images.append(_image_to_json(item if isinstance(item, dict) else {}))
        result_out = None
    else:
        # Collect nested images without requiring them as the sole result
        for img in find_image_payloads(result):
            images.append(_image_to_json(img))
        result_out = to_dumb_json_value(result)

    out = {
        "status": "ok",
        "result": result_out,
        "stdout": payload.get("stdout") or "",
    }
    if images:
        out["images"] = images
    return sanitize_for_strict_json(out)

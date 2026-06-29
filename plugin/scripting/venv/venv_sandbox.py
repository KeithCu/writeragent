# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Venv worker sandbox: path setup for vendored smolagents + LocalPythonExecutor.

Used by worker_harness.py (venv child adds repo root to sys.path for ``plugin.*`` imports).
Import policy is only VENV_AUTHORIZED_IMPORTS passed to LocalPythonExecutor—no find_spec pre-checks.

Fixed host RPC stubs (vision, embeddings_index, …) bypass the executor entirely — see
``_is_trusted_*_stub`` and ``run_sandboxed_code``.
"""

from __future__ import annotations

import ast
import importlib
import logging
import sys
import threading
from typing import Any

log = logging.getLogger(__name__)

from plugin.contrib.smolagents.local_python_executor import InterpreterError, LocalPythonExecutor
from plugin.scripting.payload_codec import (
    PAYLOAD_DATAFRAME,
    child_pack_result,
    child_unpack_data,
    describe_wire_value,
    is_image_payload,
    is_multi_data,
    is_split_grid,
    find_image_payloads,
)
from plugin.scripting.config_limits import python_exec_timeout_default
from plugin.framework.constants import AUTO_IMPORTS
from plugin.scripting.sandbox import VENV_AUTHORIZED_IMPORTS

# Shared-kernel executors keyed by workbook session_id (calc:…). Cleared on reset_session
# or worker process exit; not tied to document close in Phase 1.
_SESSION_EXECUTORS: dict[str, LocalPythonExecutor] = {}
_SESSION_LOCK = threading.Lock()

# Init scripts run once in calc:{workbook}:init; isolated cells seed from that snapshot.
_INIT_SCRIPT_HASH: dict[str, str] = {}
_CELL_SESSION_INIT_DIGEST: dict[str, str] = {}
_INIT_STATE_SKIP_KEYS = frozenset(
    {
        "__name__",
        "_print_outputs",
        "_operations_count",
        "result",
        "data",
        "data_list",
    }
)


def is_module_imported(code_str: str, module_name: str) -> bool:
    """Check if ``module_name`` is imported in any form in ``code_str``."""
    try:
        tree = ast.parse(code_str)
    except SyntaxError:
        # Fallback to simple substring match in case of syntax error.
        return f"import {module_name}" in code_str or f"from {module_name}" in code_str

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == module_name or alias.name.startswith(module_name + "."):
                    return True
        elif isinstance(node, ast.ImportFrom):
            if node.module == module_name or (node.module and node.module.startswith(module_name + ".")):
                return True
    return False


def optional_module(name: str) -> Any | None:
    if name in sys.modules:
        return sys.modules[name]
    try:
        return importlib.import_module(name)
    except Exception:
        return None


def apply_auto_imports(code: str) -> tuple[str, int]:
    """Prepend imports from AUTO_IMPORTS if missing and available. Returns (new_code, lines_added)."""
    prepended_lines = []
    for module_name, import_stmt in AUTO_IMPORTS.items():
        if not is_module_imported(code, module_name):
            if optional_module(module_name) is not None:
                prepended_lines.append(import_stmt)

    if not prepended_lines:
        return code, 0

    return "\n".join(prepended_lines) + "\n" + code, len(prepended_lines)


def inject_auto_imports(executor: LocalPythonExecutor, code: str) -> None:
    """Inject auto imports into executor state if referenced but not imported in code."""
    bindings = {}
    for module_name, import_stmt in AUTO_IMPORTS.items():
        if not is_module_imported(code, module_name):
            mod = optional_module(module_name)
            if mod is not None:
                alias = import_stmt.split(" as ")[-1].strip() if " as " in import_stmt else module_name
                bindings[alias] = mod
    if bindings:
        executor.send_variables(bindings)


def serialize_result(obj: Any) -> Any:
    """Convert numpy/pandas and containers to JSON-safe values (split_grid for large numeric/mixed arrays).

    DataFrames (and named Series) are returned as a dataframe envelope with 'columns' and 'data'
    (the latter is a split_grid envelope when large enough, or nested lists). This replaces the
    previous to_dict(orient="records") path which produced expensive list-of-dicts and bypassed
    the binary grid fast path.
    """
    try:
        return _serialize_result_impl(obj)
    except Exception:
        log.exception(
            "venv_sandbox serialize_result failed for value %s",
            describe_wire_value(obj),
        )
        raise


def _merge_figures_to_image_payload(figs: list[Any], *, fmt: str = "svg") -> dict[str, Any]:
    """Combine multiple open figures into one image envelope (vertical stack)."""
    if not figs:
        raise ValueError("figs must not be empty")
    if len(figs) == 1:
        return _figure_to_image_payload(figs[0], fmt=fmt)

    import io

    pil_mod = optional_module("PIL.Image")
    if pil_mod is None:
        return _figure_to_image_payload(figs[-1], fmt=fmt)

    images = []
    for fig in figs:
        buf = io.BytesIO()
        fig.savefig(buf, format="png", bbox_inches="tight", dpi=150)
        buf.seek(0)
        images.append(pil_mod.open(buf))

    total_w = max(im.width for im in images)
    total_h = sum(im.height for im in images)
    combined = pil_mod.new("RGB", (total_w, total_h), "white")
    y = 0
    for im in images:
        combined.paste(im, (0, y))
        y += im.height
        im.close()

    out = io.BytesIO()
    if fmt == "svg":
        # LO Calc/Writer handle SVG well; merged stacks use PNG raster for simplicity.
        combined.save(out, format="PNG")
        return {"__wa_payload__": "image", "format": "png", "data": out.getvalue()}
    combined.save(out, format="PNG")
    return {"__wa_payload__": "image", "format": "png", "data": out.getvalue()}


def _capture_open_figures_payload(*, fmt: str = "svg") -> tuple[dict[str, Any] | None, str]:
    """Return (image payload from open pyplot figures, optional stdout note)."""
    plt_mod = optional_module("matplotlib.pyplot")
    if plt_mod is None:
        return None, ""
    fignums = plt_mod.get_fignums()
    if not fignums:
        return None, ""

    figs = [plt_mod.figure(num) for num in fignums]
    note = ""
    if len(figs) > 1:
        items = [_figure_to_image_payload(fig, fmt=fmt) for fig in figs]
        payload = {
            "__wa_payload__": "multi_data",
            "items": items,
        }
        note = f"Captured {len(figs)} open figures.\n"
    else:
        payload = _figure_to_image_payload(figs[0], fmt=fmt)
    plt_mod.close("all")
    return payload, note


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


def _has_custom_serialize_objects(obj: Any) -> bool:
    mpl_fig = optional_module("matplotlib.figure")
    pd_mod = optional_module("pandas")
    pil_mod = optional_module("PIL.Image")

    custom_types = []
    if mpl_fig is not None:
        custom_types.append(mpl_fig.Figure)
    if pd_mod is not None:
        custom_types.extend([pd_mod.DataFrame, pd_mod.Series])
    if pil_mod is not None:
        custom_types.append(pil_mod.Image)

    if not custom_types:
        return False

    custom_tuple = tuple(custom_types)
    if isinstance(obj, custom_tuple):
        return True
    if isinstance(obj, (list, tuple)):
        return any(isinstance(x, custom_tuple) for x in obj)
    if isinstance(obj, dict):
        return any(isinstance(v, custom_tuple) for v in obj.values())
    return False


def _serialize_result_impl(obj: Any) -> Any:
    mpl_fig = optional_module("matplotlib.figure")
    if mpl_fig is not None and isinstance(obj, mpl_fig.Figure):
        return _figure_to_image_payload(obj)
    pil_mod = optional_module("PIL.Image")
    if pil_mod is not None and isinstance(obj, pil_mod.Image):
        return _pil_image_to_payload(obj)
    np_mod = optional_module("numpy")
    if np_mod is not None:
        if isinstance(obj, (np_mod.ndarray, np_mod.integer, np_mod.floating, np_mod.bool_)):
            return child_pack_result(obj)
    pd_mod = optional_module("pandas")
    if pd_mod is not None:
        if isinstance(obj, pd_mod.DataFrame):
            df: Any = obj
            columns = [str(c) for c in df.columns]
            # Build rectangular data for packing: ndarray fast path for homogeneous numeric;
            # list-of-lists for mixed so strings/None go through the split_grid strings map
            # instead of the old per-row to_dict("records") which defeated binary envelopes.
            if len(df) == 0 or len(df.columns) == 0:
                data_part: Any = []
            else:
                try:
                    arr = df.to_numpy(copy=False)
                    if getattr(arr, "dtype", None) is not None and arr.dtype.kind not in ("O", "U", "S"):
                        data_part = child_pack_result(arr)
                    else:
                        grid = [list(row) for row in df.itertuples(index=False, name=None)]
                        data_part = child_pack_result(grid)
                except Exception:
                    grid = [list(row) for row in df.itertuples(index=False, name=None)]
                    data_part = child_pack_result(grid)
            return {
                "__wa_payload__": PAYLOAD_DATAFRAME,
                "columns": columns,
                "data": data_part,
            }
        if isinstance(obj, pd_mod.Series):
            s: Any = obj
            name = getattr(s, "name", None)
            try:
                arr = s.to_numpy(copy=False)
                if getattr(arr, "dtype", None) is not None and arr.dtype.kind in ("O", "U", "S"):
                    lst = s.tolist()
                    packed = child_pack_result(lst)
                else:
                    packed = child_pack_result(arr)
            except Exception:
                packed = child_pack_result(s.tolist())
            if name is not None:
                return {
                    "__wa_payload__": PAYLOAD_DATAFRAME,
                    "columns": [str(name)],
                    "data": packed,
                }
            return packed
    if isinstance(obj, (dict, list, tuple)):
        if _has_custom_serialize_objects(obj):
            if isinstance(obj, dict):
                return {str(k): serialize_result(v) for k, v in obj.items()}
            elif isinstance(obj, list):
                return [serialize_result(v) for v in obj]
            else:
                return tuple(serialize_result(v) for v in obj)
        return child_pack_result(obj)
    return obj



def _new_executor(timeout_sec: int) -> LocalPythonExecutor:
    executor = LocalPythonExecutor(
        additional_authorized_imports=list(VENV_AUTHORIZED_IMPORTS),
        timeout_seconds=timeout_sec,
    )
    # Upstream only merges BASE_PYTHON_TOOLS (sum, len, …) after send_tools(); without this,
    # static_tools stays None and builtins like sum() are rejected.
    executor.send_tools({})
    return executor


def _get_or_create_session_executor(session_id: str, timeout_sec: int) -> LocalPythonExecutor:
    with _SESSION_LOCK:
        executor = _SESSION_EXECUTORS.get(session_id)
        if executor is None:
            executor = _new_executor(timeout_sec)
            _SESSION_EXECUTORS[session_id] = executor
        return executor


def _related_init_session_id(session_id: str) -> str | None:
    """Return ``calc:…:init`` companion for a ``calc:…`` workbook session, if applicable."""
    if session_id.startswith("calc:") and not session_id.endswith(":init"):
        return f"{session_id}:init"
    return None


def _cell_session_for_init(init_session_id: str) -> str | None:
    if init_session_id.endswith(":init"):
        return init_session_id[: -len(":init")]
    return None


def _clear_init_session_unlocked(init_session_id: str) -> None:
    cell_sid = _cell_session_for_init(init_session_id)
    _SESSION_EXECUTORS.pop(init_session_id, None)
    _INIT_SCRIPT_HASH.pop(init_session_id, None)
    if cell_sid:
        _SESSION_EXECUTORS.pop(cell_sid, None)
        _CELL_SESSION_INIT_DIGEST.pop(cell_sid, None)


def _invalidate_init_session(init_session_id: str) -> None:
    with _SESSION_LOCK:
        _clear_init_session_unlocked(init_session_id)


def reset_sandbox_session(session_id: str) -> dict[str, Any]:
    """Drop the persistent executor for *session_id* (idempotent).

    Also clears the workbook's ``:init`` session when resetting a ``calc:…`` cell session.
    """
    if not (session_id or "").strip():
        return {"status": "error", "message": "No session_id provided."}
    with _SESSION_LOCK:
        _SESSION_EXECUTORS.pop(session_id, None)
        init_sid = _related_init_session_id(session_id)
        if init_sid:
            _SESSION_EXECUTORS.pop(init_sid, None)
            _INIT_SCRIPT_HASH.pop(init_sid, None)
        if session_id.endswith(":init"):
            _INIT_SCRIPT_HASH.pop(session_id, None)
        _CELL_SESSION_INIT_DIGEST.pop(session_id, None)
    return {"status": "ok"}


def clear_all_sandbox_sessions() -> None:
    """Clear every cached session executor (tests)."""
    with _SESSION_LOCK:
        _SESSION_EXECUTORS.clear()
        _INIT_SCRIPT_HASH.clear()
        _CELL_SESSION_INIT_DIGEST.clear()


def _snapshot_init_bindings(init_session_id: str) -> dict[str, Any]:
    """Copy user-visible names from the init executor (references, not deep copies)."""
    with _SESSION_LOCK:
        executor = _SESSION_EXECUTORS.get(init_session_id)
    if executor is None:
        return {}
    return {
        key: value
        for key, value in executor.state.items()
        if key not in _INIT_STATE_SKIP_KEYS and not (isinstance(key, str) and key.startswith("_"))
    }


def _seed_executor_from_init(executor: LocalPythonExecutor, init_session_id: str) -> None:
    bindings = _snapshot_init_bindings(init_session_id)
    if bindings:
        executor.send_variables(bindings)


def _ensure_init_executed(
    init_session_id: str,
    init_script: str,
    *,
    timeout_sec: int,
    init_script_hash: str | None = None,
) -> dict[str, Any] | None:
    """Run *init_script* once in the persistent init session. Returns error dict or None."""
    script = (init_script or "").strip()
    if not script:
        return None

    digest = init_script_hash or ""
    with _SESSION_LOCK:
        prior = _INIT_SCRIPT_HASH.get(init_session_id)
        if prior is not None and prior != digest:
            _clear_init_session_unlocked(init_session_id)
        elif prior == digest and init_session_id in _SESSION_EXECUTORS:
            return None

    init_executor = _get_or_create_session_executor(init_session_id, timeout_sec)
    inject_auto_imports(init_executor, script)
    result = _run_on_executor(init_executor, script)
    if result.get("status") != "ok":
        with _SESSION_LOCK:
            _SESSION_EXECUTORS.pop(init_session_id, None)
            _INIT_SCRIPT_HASH.pop(init_session_id, None)
        return result

    with _SESSION_LOCK:
        _INIT_SCRIPT_HASH[init_session_id] = digest
    return None


def convert_datetimes_and_deltas(data: Any, locale: str | None, convert_datetime: bool) -> Any:
    if not convert_datetime:
        return data
    try:
        import pandas as pd
        import dateparser
    except ImportError as e:
        raise ImportError(
            "Date-time and Timedelta conversion requires both 'pandas' and 'dateparser' packages to be installed in the virtual environment. "
            "Please run: uv pip install pandas dateparser"
        ) from e

    import re
    import numpy as np
    lang = locale.split("_")[0] if locale else "en"

    def _rec(val: Any) -> Any:
        if isinstance(val, str):
            if re.search(r'[a-zA-Z:]', val):
                try:
                    td = pd.to_timedelta(val)
                    if not pd.isna(td):
                        return td.to_pytimedelta()
                except Exception:
                    pass
            try:
                parsed = dateparser.parse(val, languages=[lang])
                if parsed is not None:
                    return parsed
            except Exception:
                pass
            return val
        elif isinstance(val, list):
            return [_rec(item) for item in val]
        elif isinstance(val, tuple):
            return tuple(_rec(item) for item in val)
        elif isinstance(val, np.ndarray):
            if val.dtype == object or np.issubdtype(val.dtype, np.character):
                flat_list = val.ravel().tolist()
                converted_flat = [_rec(item) for item in flat_list]
                return np.array(converted_flat, dtype=object).reshape(val.shape)
            return val
        return val

    return _rec(data)


def _inject_data(executor: LocalPythonExecutor, data: Any | None, locale: str | None = None, convert_datetime: bool = False) -> None:
    if data is None:
        return
    if is_split_grid(data):
        log.debug("venv_sandbox injecting data %s", describe_wire_value(data))
    unpacked = child_unpack_data(data)
    unpacked = convert_datetimes_and_deltas(unpacked, locale, convert_datetime)
    variables: dict[str, Any] = {"data": unpacked}
    variables["data_list"] = unpacked if is_multi_data(data) else [unpacked]
    executor.send_variables(variables)


_TRUSTED_VISION_STUB_MARKER = "from plugin.vision.venv.vision import run_vision"
_TRUSTED_EMBEDDINGS_STUB_MARKER = "from plugin.embeddings.venv.embeddings_index import"
_TRUSTED_FOLDER_FTS_STUB_MARKER = "from plugin.embeddings.venv.folder_fts import"


def _is_trusted_vision_stub(code: str) -> bool:
    return _TRUSTED_VISION_STUB_MARKER in (code or "")


def _is_trusted_embeddings_stub(code: str) -> bool:
    c = code or ""
    return _TRUSTED_EMBEDDINGS_STUB_MARKER in c or _TRUSTED_FOLDER_FTS_STUB_MARKER in c


def _unpack_trusted_payload(data: Any | None) -> dict[str, Any]:
    if data is None:
        return {}
    unpacked = child_unpack_data(data)
    if is_multi_data(unpacked):
        if isinstance(unpacked, list) and unpacked and isinstance(unpacked[0], dict):
            return unpacked[0]
        return {}
    if isinstance(unpacked, dict):
        return unpacked
    return {}


def _run_trusted_vision_payload(data: Any | None) -> dict[str, Any]:
    """Run vision helpers outside LocalPythonExecutor (docling/paddle are not sandbox imports)."""
    from plugin.vision.venv.vision import run_vision

    payload = _unpack_trusted_payload(data)
    try:
        spec = payload.get("spec")
        if spec is None:
            spec = {}
        result = run_vision(
            spec,
            payload.get("image"),
            context=payload.get("context") or {},
        )
        return {"status": "ok", "result": serialize_result(result), "stdout": ""}
    except Exception as e:
        import traceback

        log.exception("trusted vision unsandboxed run failed")
        return {
            "status": "error",
            "message": str(e),
            "traceback": traceback.format_exc(),
            "stdout": "",
        }


def _run_trusted_embeddings_payload(code: str, data: Any | None) -> dict[str, Any]:
    """Run fixed embeddings_index / folder_fts RPC stubs outside LocalPythonExecutor."""
    from plugin.embeddings.venv import embeddings_index

    payload = _unpack_trusted_payload(data)
    stub = code or ""
    try:
        if _TRUSTED_FOLDER_FTS_STUB_MARKER in stub:
            from plugin.embeddings.venv import folder_fts

            if "maintain_folder_fts" in stub:
                from typing import cast, Literal

                mode_raw = str(payload.get("mode") or "auto")
                if mode_raw not in ("auto", "cold", "incremental"):
                    mode_raw = "auto"
                result = folder_fts.maintain_folder_fts(
                    str(payload.get("listing_root") or ""),
                    cast("Literal['auto', 'cold', 'incremental']", mode_raw),
                )
            elif "search_folder_fts" in stub:
                result = folder_fts.search_folder_fts(
                    str(payload.get("fts_db_path") or ""),
                    str(payload.get("query") or ""),
                    k=int(payload.get("k") or 10),
                    near_slop=int(payload.get("near_slop") or 10),
                )
            elif "fts_stats" in stub:
                result = folder_fts.fts_stats(
                    str(payload.get("fts_db_path") or ""),
                    str(payload.get("meta_path") or ""),
                )
            else:
                return {
                    "status": "error",
                    "message": "Unrecognized trusted folder FTS stub.",
                    "stdout": "",
                }
            return {"status": "ok", "result": serialize_result(result), "stdout": ""}

        if "maintain_folder_index" in stub:
            result = embeddings_index.maintain_folder_index(
                str(payload.get("listing_root") or ""),
                str(payload.get("model") or ""),
                str(payload.get("mode") or "auto"),
                search_mode=str(payload.get("search_mode") or "embeddings"),
            )
        elif "index_paragraphs" in stub:
            result = embeddings_index.index_paragraphs(
                str(payload.get("db_path") or ""),
                str(payload.get("meta_path") or ""),
                str(payload.get("model") or ""),
                list(payload.get("rows") or []),
                build_fts=bool(payload.get("build_fts")),
                build_vectors=bool(payload.get("build_vectors", True)),
            )
        elif "delete_paragraphs" in stub:
            result = embeddings_index.delete_paragraphs(
                str(payload.get("db_path") or ""),
                str(payload.get("meta_path") or ""),
                list(payload.get("keys") or []),
                model_name=str(payload.get("model") or ""),
                build_fts=bool(payload.get("build_fts")),
                build_vectors=bool(payload.get("build_vectors", True)),
            )
        elif "knn_search" in stub:
            result = embeddings_index.knn_search(
                str(payload.get("db_path") or ""),
                str(payload.get("query") or ""),
                int(payload.get("k") or 5),
                model_name=str(payload.get("model") or ""),
                doc_url_filter=payload.get("doc_url_filter"),
                use_mmr=bool(payload.get("use_mmr", True)),
                rerank_model=payload.get("rerank_model"),
                search_mode=str(payload.get("search_mode") or "embeddings"),
            )
        elif "hybrid_search" in stub:
            result = embeddings_index.hybrid_search(
                str(payload.get("db_path") or ""),
                str(payload.get("query") or ""),
                int(payload.get("k") or 10),
                model_name=str(payload.get("model") or ""),
                near_slop=int(payload.get("near_slop") or 10),
                doc_url_filter=payload.get("doc_url_filter"),
                use_mmr=bool(payload.get("use_mmr", True)),
                rerank_model=payload.get("rerank_model"),
                search_mode=str(payload.get("search_mode") or "hybrid"),
            )
        elif "collection_stats" in stub:
            result = embeddings_index.collection_stats(
                str(payload.get("db_path") or ""),
                str(payload.get("meta_path") or ""),
                model_name=str(payload.get("model") or ""),
            )
        elif "embed_texts" in stub:
            result = embeddings_index.embed_texts(
                str(payload.get("model") or ""),
                list(payload.get("texts") or []),
            )
        elif "_get_embedder" in stub:
            model_name = str(payload.get("model") or "")
            if not model_name:
                import re
                match = re.search(r"_get_embedder\((['\"])(.*?)\1\)", stub)
                if match:
                    model_name = match.group(2)
            if model_name:
                embeddings_index._get_embedder(model_name)
            result = {"status": "warmed"}
        else:
            return {
                "status": "error",
                "message": "Unrecognized trusted embeddings stub.",
                "stdout": "",
            }
        return {"status": "ok", "result": serialize_result(result), "stdout": ""}
    except Exception as e:
        import traceback

        log.exception("trusted embeddings unsandboxed run failed")
        return {
            "status": "error",
            "message": str(e),
            "traceback": traceback.format_exc(),
            "stdout": "",
        }


def _run_on_executor(executor: LocalPythonExecutor, code: str) -> dict[str, Any]:
    try:
        code_output = executor(code)
        result = executor.state.get("result", code_output.output)
        serialized = serialize_result(result)

        extra_stdout = ""
        if not find_image_payloads(serialized):
            captured, note = _capture_open_figures_payload()
            if captured is not None:
                serialized = captured
                extra_stdout = note
        else:
            plt_mod = optional_module("matplotlib.pyplot")
            if plt_mod is not None:
                plt_mod.close("all")

        if is_split_grid(serialized):
            log.debug("venv_sandbox worker result %s", describe_wire_value(serialized))
        stdout = (code_output.logs or "") + extra_stdout
        return {
            "status": "ok",
            "result": serialized,
            "stdout": stdout,
        }
    except InterpreterError as e:
        return {
            "status": "error",
            "message": str(e),
            "stdout": str(executor.state.get("_print_outputs", "")),
        }
    except Exception as e:
        import traceback

        return {
            "status": "error",
            "message": str(e),
            "traceback": traceback.format_exc(),
            "stdout": "",
        }


def run_sandboxed_code(
    code: str,
    data: Any | None = None,
    *,
    timeout_sec: int | None = None,
    session_id: str | None = None,
    init_script: str | None = None,
    init_session_id: str | None = None,
    init_script_hash: str | None = None,
    locale: str | None = None,
    convert_datetime: bool = False,
) -> dict[str, Any]:
    """Run *code* in LocalPythonExecutor.

    Without *session_id*, each call uses a new namespace. With *session_id*, reuse one
    executor per id (shared kernel / workbook session).

    When *init_script* is set, it runs once in *init_session_id* (typically ``calc:…:init``).
    Isolated cell runs seed a fresh executor from that snapshot; shared kernel seeds the
    workbook session executor once, then reuses it for cell code.
    """
    if timeout_sec is None:
        timeout_sec = python_exec_timeout_default()

    # Trusted host RPC stubs use real imports (not LocalPythonExecutor).
    if _is_trusted_vision_stub(code):
        return _run_trusted_vision_payload(data)
    if _is_trusted_embeddings_stub(code):
        return _run_trusted_embeddings_payload(code, data)

    # Force non-interactive backend so plt.show() doesn't block in the subprocess.
    mpl = optional_module("matplotlib")
    if mpl is not None:
        mpl.use("Agg")

    init_sid = init_session_id if isinstance(init_session_id, str) and init_session_id.strip() else None
    if init_sid and (init_script or "").strip():
        init_err = _ensure_init_executed(
            init_sid,
            init_script or "",
            timeout_sec=timeout_sec,
            init_script_hash=init_script_hash,
        )
        if init_err is not None:
            return init_err

    if session_id:
        executor = _get_or_create_session_executor(session_id, timeout_sec)
        if init_sid:
            with _SESSION_LOCK:
                digest = _INIT_SCRIPT_HASH.get(init_sid)
                seeded = _CELL_SESSION_INIT_DIGEST.get(session_id)
            if digest and seeded != digest:
                _seed_executor_from_init(executor, init_sid)
                with _SESSION_LOCK:
                    _CELL_SESSION_INIT_DIGEST[session_id] = digest
    else:
        executor = _new_executor(timeout_sec)
        if init_sid:
            _seed_executor_from_init(executor, init_sid)

    inject_auto_imports(executor, code)
    _inject_data(executor, data, locale=locale, convert_datetime=convert_datetime)
    return _run_on_executor(executor, code)

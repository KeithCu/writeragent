# WriterAgent — finite input domains and mock stubs for CrossHair concolic execution
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Finite input domains and mock stubs for CrossHair concolic execution.

Installs an import-time hook on ``deal_shim`` and target ``plugin.*`` modules to:
1. Rebind ``DEAL_MAX_*`` finite input domains to small bounds after ``deal_shim`` loads.
2. Rebind ``inverse_ensure`` to a no-op contract.
3. Rebind CrossHair predicates and swap bytecodes on host/IO stubs.

This runner module must NOT import ``plugin`` at import time.
"""

from __future__ import annotations

import importlib.abc
import sys
from typing import Any, Callable

_installed = False


def _swap_code(target_fn: Any, stub_fn: Any) -> None:
    """Swap target_fn bytecode with stub_fn bytecode, preserving source file and line info."""
    unwrapped = target_fn
    while hasattr(unwrapped, "__wrapped__"):
        unwrapped = unwrapped.__wrapped__
    target_code = unwrapped.__code__
    stub_code = stub_fn.__code__.replace(
        co_filename=target_code.co_filename,
        co_firstlineno=target_code.co_firstlineno,
    )
    unwrapped.__code__ = stub_code


# --- Stubs ---


def _stub_window_none(*args: Any, **kwargs: Any) -> Any:
    return None


def _stub_resolve_style(win: Any) -> tuple[bool, int]:
    return False, 0xE0E1E2


def _stub_format_agent_tool_stream_line(prefix: str, data: Any) -> str:
    return "\n%s %s\n" % (prefix, str(data))


def _stub_as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return False


def _stub_parse_int_robust(val: Any) -> int:
    import math

    if isinstance(val, bool):
        return int(val)
    if isinstance(val, int):
        return val
    if isinstance(val, float):
        if not math.isfinite(val):
            raise ValueError(f"Cannot parse non-finite float as int: {val!r}")
        return int(val)
    raise ValueError("Cannot parse symbolic type as int under CrossHair")


def _stub_parse_float_robust(val: Any) -> float:
    if isinstance(val, (int, float)):
        try:
            return float(val)
        except OverflowError as exc:
            raise ValueError(f"Could not robustly parse float from {val!r}") from exc
    raise ValueError("Cannot parse symbolic type as float under CrossHair")


def _stub_writeragent_exception_init(
    self: Any,
    message: Any,
    *,
    code: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    text = "mock"
    Exception.__init__(self, text)
    self.message = text
    if code is not None:
        self.code = code
    self.details = details or {}


def _stub_format_error_payload(e: BaseException) -> dict[str, Any]:
    if getattr(e, "code", None) is not None and hasattr(e, "message"):
        payload: dict[str, Any] = {
            "status": "error",
            "code": getattr(e, "code"),
            "message": getattr(e, "message"),
        }
        if getattr(e, "details", None):
            payload["details"] = getattr(e, "details")
        return payload
    return {
        "status": "error",
        "code": "INTERNAL_ERROR",
        "message": "mock",
        "details": {"type": "ValueError"},
    }


def _stub_format_error_message(e: BaseException) -> str:
    import ssl
    import urllib.error

    msg = "mock"
    if isinstance(e, ssl.SSLError):
        return f"TLS/SSL Error: {msg}"
    if isinstance(e, urllib.error.HTTPError):
        try:
            code = int(e.code)
        except (TypeError, ValueError, AttributeError):
            code = 0
        if code == 401:
            return "Invalid API Key. Please check your settings."
        if code == 403:
            return "API access Forbidden. Your key may lack permissions for this model."
        if code == 404:
            return "Endpoint not found (404). Check your URL and Model name."
        if code == 429:
            return "Rate limited (429). Wait a moment and try again."
        if code >= 500:
            return f"Server error ({code}). The AI provider is having issues."
        return f"HTTP Error {code}: mock"

    if getattr(e, "code", None) == "missing_api_key":
        provider = str(getattr(e, "details", {}).get("provider") or "").strip()
        if provider and provider != "custom":
            return f"No API key configured for {provider}. Open Settings and add a key."
        return "No API key configured. Open Settings and add a key."

    reason = "mock"
    _FILESYSTEM_ERRNOS = frozenset({2, 13, 20, 21, 28, 30})
    _FILESYSTEM_CLASSES = (FileNotFoundError, PermissionError, IsADirectoryError, NotADirectoryError)
    is_fs = isinstance(e, _FILESYSTEM_CLASSES) or (getattr(e, "errno", None) in _FILESYSTEM_ERRNOS)
    if isinstance(e, (urllib.error.URLError, OSError)) and not is_fs:
        return f"Cannot connect to AI service: {reason}"
    return msg


def _stub_script_facts_first_run_spec(self, run_name: str) -> dict[str, Any] | None:
    return None


def _stub_elapsed_since(t0: float) -> float:
    return 0.0


def _stub_template_body(helper: str, params: dict[str, Any]) -> str:
    params_json = "{}"
    note = (
        "Results cap at 1000 rows (MAX_TABLE_ROWS). truncated/warning/flags mean "
        "the table is incomplete — add LIMIT or aggregate."
    )
    desc = {
        "query_folder_sql": "Query CSV, Parquet, or JSON files in the document folder via DuckDB SQL.",
        "query_sheet_sql": "Query Calc sheet data using SQL (registered as table 'data').",
    }.get(helper, helper)
    if helper == "query_sheet_sql":
        how_to = "# Set the Data range in the toolbar (or select cells), then Run.\n"
        call_args = (
            "    None,  # folder not used for sheet\n"
            '    "SELECT ... FROM data",\n'
            "    None,\n"
            '    {"data": data},  # provided by Run Python Script UI from data_range\n'
        )
    else:
        how_to = "# Files must live beside the saved .ods/.xlsx. Edit the files list.\n"
        call_args = (
            "    None,  # resolved by runner from document folder (or pass explicit)\n"
            '    "SELECT ... FROM \'yourfile.csv\'",\n'
            f"    {params_json}.get('files', ['yourfile.csv']),\n"
        )
    return (
        f"# writeragent:sql helper={helper} params={params_json}\n"
        f"# {desc}\n"
        f"# {note}\n"
        f"{how_to}"
        f"from writeragent.scripting.duckdb_sql import query_folder_sql\n\n"
        f"result = query_folder_sql(\n"
        f"{call_args}"
        f")\n"
    )


def _stub_deal_sql_result_value_ok_crosshair(value: object) -> bool:
    if not isinstance(value, dict):
        return True
    if len(value) > 4:
        return False
    for k, v in value.items():
        if not isinstance(k, str) or len(k) > 8:
            return False
        if isinstance(v, str) and len(v) > 8:
            return False
    return True


def _stub_deal_ipc_dict_ok_crosshair(msg: object, allow_nested: bool = True) -> bool:
    from plugin.framework.deal_shim import ascii_bounded

    return type(msg) is dict and len(msg) <= 4 and all(
        type(k) is str
        and ascii_bounded(k, 16)
        and (
            v is None
            or (isinstance(v, str) and ascii_bounded(v, 16))
            or (
                allow_nested
                and isinstance(v, dict)
                and _stub_deal_ipc_dict_ok_crosshair(v, allow_nested=False)
            )
        )
        for k, v in msg.items()
    )


def _stub_deal_scrub_env_ok_crosshair(base: object) -> bool:
    from plugin.framework.deal_shim import str_bounded

    return base is None or (
        isinstance(base, dict)
        and len(base) <= 1
        and all(
            isinstance(k, str)
            and str_bounded(k, 2)
            and isinstance(v, str)
            and str_bounded(v, 4)
            for k, v in base.items()
        )
    )


def _stub_deal_chunk_base_meta_ok_crosshair(base_meta: object) -> bool:
    from plugin.framework.deal_shim import ascii_bounded

    return (
        type(base_meta) is dict
        and len(base_meta) <= 1
        and all(
            type(k) is str
            and ascii_bounded(k, 16)
            and (
                v is None
                or type(v) in (int, float, bool)
                or (isinstance(v, str) and ascii_bounded(v, 16))
            )
            for k, v in base_meta.items()
        )
    )


def _stub_deal_sentences_spans_pre_crosshair(sentences: object) -> bool:
    from plugin.framework.deal_shim import ascii_bounded

    return (
        isinstance(sentences, list)
        and len(sentences) <= 1
        and all(
            isinstance(s, tuple)
            and len(s) == 3
            and type(s[0]) is int
            and type(s[1]) is int
            and 0 <= s[0] <= 1
            and 0 <= s[1] <= 1
            and type(s[2]) is str
            and ascii_bounded(s[2], 1)
            for s in sentences
        )
    )


def _stub_deal_json_list_of_grids_arg_ok_crosshair(module: Any, obj: object) -> bool:
    if not isinstance(obj, (list, tuple)) or len(obj) > 1:
        return False
    for item in obj:
        if not isinstance(item, (list, tuple)) or len(item) > 1:
            return False
        for row in item:
            if isinstance(row, (list, tuple)):
                if len(row) > 1:
                    return False
                if not all(module._deal_inner_grid_cell_ok(c) for c in row):
                    return False
            elif not module._deal_inner_grid_cell_ok(row):
                return False
    return True


def _stub_deal_materialize_inner_ok_crosshair(module: Any, inner: object) -> bool:
    if type(inner) is not list and type(inner) is not tuple:
        return True
    rows = inner
    return len(rows) <= 1 and all(
        type(r) not in (list, tuple)
        or (
            len(r) <= 1
            and all(module._deal_inner_grid_cell_ok(c) for c in r)
        )
        for r in rows
    )


def _stub_deal_labeled_grid_ok_crosshair(
    module: Any, columns: object, data: object = None
) -> bool:
    if not isinstance(columns, (list, tuple)) or len(columns) > 1:
        return False
    if not all(isinstance(c, str) and c.isascii() and len(c) <= 1 for c in columns):
        return False
    if data is not None:
        if not isinstance(data, (list, tuple)) or len(data) > 1:
            return False
        for row in data:
            if isinstance(row, (list, tuple)):
                if len(row) > 1 or not all(module._deal_inner_grid_cell_ok(c) for c in row):
                    return False
            elif not module._deal_inner_grid_cell_ok(row):
                return False
    return True


# --- Module Patchers ---


def _patch_deal_shim(module: Any) -> None:
    module.inverse_ensure = module._noop_contract
    module.DEAL_MAX_COL_LETTERS = 1
    module.DEAL_MAX_COL_INDEX = 25
    module.DEAL_MAX_CELL_REF = 4
    module.DEAL_MAX_ROW_INDEX = 20
    module.DEAL_MAX_ARGV = 32
    module.DEAL_MAX_CMD_ARGS = 4
    module.DEAL_MAX_SHAPE_DIM = 4
    module.DEAL_MAX_SHAPE_RANK = 2
    module.DEAL_MAX_PLACEHOLDER_INDEX = 4
    module.DEAL_MAX_SOURCE = 16
    module.DEAL_MAX_PATH = 32
    module.DEAL_MAX_TOKEN = 16
    module.DEAL_MAX_XL_EXPR = 32
    module.DEAL_MAX_ORIGIN = 32
    module.DEAL_MAX_URL = 32
    module.DEAL_MAX_HTML_CHUNK = 16


def _patch_appearance(module: Any) -> None:
    module._DEAL_DARKEN_FACTORS = (0.0, 1.0)
    module._deal_theme_color_ok = lambda color: type(color) is int and color in (0, 0xFFFFFF)
    _swap_code(module._window_from_doc, _stub_window_none)
    _swap_code(module._window_from_desktop, _stub_window_none)
    _swap_code(module._resolve_style, _stub_resolve_style)


def _patch_async_stream(module: Any) -> None:
    _swap_code(module._format_agent_tool_stream_line, _stub_format_agent_tool_stream_line)


def _patch_config_schema(module: Any) -> None:
    _swap_code(module.as_bool, _stub_as_bool)
    _swap_code(module.parse_int_robust, _stub_parse_int_robust)
    _swap_code(module.parse_float_robust, _stub_parse_float_robust)


def _patch_errors(module: Any) -> None:
    _stub_writeragent_exception_init.__module__ = module.WriterAgentException.__module__
    _stub_writeragent_exception_init.__qualname__ = (
        f"{module.WriterAgentException.__qualname__}.__init__"
    )
    module.WriterAgentException.__init__ = _stub_writeragent_exception_init
    _swap_code(module.format_error_payload, _stub_format_error_payload)
    _swap_code(module.format_error_message, _stub_format_error_message)


def _patch_auth(module: Any) -> None:
    module._deal_provider_id_ok = (
        lambda provider_id: provider_id is None or provider_id in module._PROVIDER_HINTS
    )


def _patch_openrouter_model_id(module: Any) -> None:
    module._deal_model_id_ok = lambda model_id: isinstance(model_id, str) and len(model_id) <= 16
    module._deal_catalog_ids_ok = lambda catalog_ids: (
        catalog_ids is None
        or (isinstance(catalog_ids, (list, tuple, set, frozenset)) and len(catalog_ids) <= 4)
    )


def _patch_formula_edit(module: Any) -> None:
    from plugin.framework.deal_shim import ascii_bounded

    module._DEAL_ASCII_STR = ascii_bounded


def _patch_preprocess(module: Any) -> None:
    module._deal_lo_formula_ok = (
        lambda formula: isinstance(formula, str)
        and len(formula) <= 16
        and all(c in module._LO_FORMULA_CHARS for c in formula)
    )


def _patch_sheet_filter_criteria(module: Any) -> None:
    from plugin.framework.deal_shim import ascii_bounded

    module._deal_filter_op_name_ok = (
        lambda name: isinstance(name, str) and name in module.FILTER_OPERATOR2_LABELS
    )
    module._deal_filter_connection_ok = (
        lambda name: name is None or name in ("AND", "OR", "and", "or", "")
    )
    module._deal_filter_criterion_ok = lambda raw: (
        isinstance(raw, dict)
        and len(raw) <= 4
        and (not isinstance(raw.get("operator"), str) or ascii_bounded(raw.get("operator"), 16))
        and (not isinstance(raw.get("connection"), str) or ascii_bounded(raw.get("connection"), 16))
    )


def _patch_memory(module: Any) -> None:
    from plugin.framework.deal_shim import str_bounded

    module._deal_memory_args_ok = lambda arguments: (
        isinstance(arguments, dict)
        and len(arguments) <= 4
        and (not isinstance(arguments.get("key"), str) or str_bounded(arguments.get("key"), 16))
        and (
            not isinstance(arguments.get("content"), str)
            or str_bounded(arguments.get("content"), 16)
        )
    )

    def _stub_deal_memory_chat_line_ok(arguments: object) -> bool:
        if isinstance(arguments, str):
            return str_bounded(arguments, 16)
        get = getattr(arguments, "get", None)
        if not callable(get):
            return False
        key = get("key")
        content = get("content")
        return (not isinstance(key, str) or str_bounded(key, 16)) and (
            not isinstance(content, str) or str_bounded(content, 16)
        )

    module._deal_memory_chat_line_ok = _stub_deal_memory_chat_line_ok


def _patch_cors(module: Any) -> None:
    from plugin.framework.deal_shim import ascii_bounded

    module.deal_origin_ok = (
        lambda origin: isinstance(origin, str)
        and len(origin) <= 32
        and all(c in module._ORIGIN_CHARS for c in origin)
    )
    module._deal_allow_headers_ok = lambda value: (
        isinstance(value, str)
        and ascii_bounded(value, 32)
        and value.count(",") <= 4
        and all(c in module._HEADER_LIST_CHARS for c in value)
    )
    module._deal_origins_config_ok = lambda value: (
        value is None
        or (isinstance(value, str) and module.deal_origin_ok(value))
        or (
            isinstance(value, list)
            and len(value) <= 4
            and all(module.deal_origin_ok(item) for item in value)
        )
    )


def _patch_wire_types(module: Any) -> None:
    from plugin.framework.deal_shim import ascii_bounded, str_bounded

    module._deal_jsonrpc_msg_ok = lambda msg: not isinstance(msg, dict) or (
        len(msg) <= 4 and all(type(k) is str and ascii_bounded(k, 16) for k in msg)
    )
    module._deal_jsonrpc_failure_ok = lambda message, code: (
        str_bounded(message, 16) and isinstance(code, int)
    )
    module._deal_image_b64_ok = lambda data_b64, mime_type="image/png": (
        str_bounded(data_b64, 16) and ascii_bounded(mime_type, 16, min_len=1)
    )


def _patch_audio_silence_detector(module: Any) -> None:
    module._deal_pcm_ok = lambda pcm: isinstance(pcm, (bytes, bytearray)) and len(pcm) <= 32


def _patch_calc_range(module: Any) -> None:
    module._DEAL_GRID_DIM = 1
    module._DEAL_CELL_INT_ABS = 0
    module._DEAL_CELL_STR_LEN = 1
    module._DEAL_COL_NAME_LEN = 1

    module._deal_inner_grid_cell_ok = lambda c: (
        c is None
        or (c == 0 if isinstance(c, int) else False)
        or (isinstance(c, str) and c.isascii() and len(c) <= 1)
    )
    module._deal_calc_range_other_ok = lambda other: (
        isinstance(other, module.CalcRange) and module._deal_grid_values_ok(other._values)
    )
    module._deal_binary_op_pre = lambda self, other: (
        module._deal_grid_values_ok(self._values) and module._deal_calc_range_other_ok(other)
    )
    module._deal_json_list_of_grids_arg_ok = (
        lambda obj: _stub_deal_json_list_of_grids_arg_ok_crosshair(module, obj)
    )
    module._deal_materialize_inner_ok = lambda inner: _stub_deal_materialize_inner_ok_crosshair(
        module, inner
    )
    module._deal_labeled_grid_ok = (
        lambda columns, data=None, **__: _stub_deal_labeled_grid_ok_crosshair(
            module, columns, data
        )
    )


def _patch_duckdb_sql(module: Any) -> None:
    module._DEAL_SQL_RESULT_KEYS = 4
    module._DEAL_SQL_RESULT_STR = 8
    module._deal_sql_code_ok = lambda code: isinstance(code, str) and len(code) <= 16
    module._deal_sql_result_value_ok = _stub_deal_sql_result_value_ok_crosshair
    _swap_code(module._template_body, _stub_template_body)


def _patch_editor_errors(module: Any) -> None:
    module._deal_text_ok = lambda value: isinstance(value, str) and len(value) <= 16
    module._deal_optional_exc_ok = lambda exc: exc is None


def _patch_editor_ipc(module: Any) -> None:
    module._deal_ipc_dict_ok = _stub_deal_ipc_dict_ok_crosshair


def _patch_helper_domain(module: Any) -> None:
    from plugin.framework.deal_shim import ascii_bounded, str_bounded

    module._deal_user_code_ok = lambda code, run_name="": (
        str_bounded(code, 16) and ascii_bounded(run_name, 16)
    )
    _swap_code(module.ScriptFacts.first_run_spec, _stub_script_facts_first_run_spec)
    _swap_code(module._elapsed_since, _stub_elapsed_since)


def _patch_payload_codec(module: Any) -> None:
    module._SHAPE_DIM_CAP = 4
    module._deal_wire_dict_ok = lambda obj: not isinstance(obj, dict) or len(obj) <= 4
    module._deal_host_unpack_wire_ok = lambda wire, *_unused, **__: (
        module._is_any_payload_envelope(wire)
        or isinstance(wire, (list, tuple, dict, str, int, float, bool))
        or wire is None
        or module._is_ndarray(wire)
        or getattr(type(wire), "__module__", "") == "numpy"
    )
    module._deal_child_unpack_wire_ok = lambda wire, *_unused, **__: (
        module._is_any_payload_envelope(wire)
        or isinstance(wire, (list, tuple, dict, str, int, float, bool))
        or wire is None
        or (hasattr(wire, "__class__") and wire.__class__.__name__ == "ndarray")
    )


def _patch_subprocess_env(module: Any) -> None:
    module._DEAL_SCRUB_DICT = 1
    module._DEAL_SCRUB_KEY = 2
    module._DEAL_SCRUB_VAL = 4
    module._deal_scrub_env_ok = _stub_deal_scrub_env_ok_crosshair


def _patch_sandbox(module: Any) -> None:
    module._DEAL_BASENAME_LEN = 8


def _patch_sandbox_cache(module: Any) -> None:
    from plugin.framework.deal_shim import ascii_bounded

    module._deal_sandbox_imports_ok = lambda authorized_imports: (
        isinstance(authorized_imports, (list, tuple))
        and len(authorized_imports) <= 4
        and all(ascii_bounded(x, 16) for x in authorized_imports)
    )
    module._deal_sandbox_code_ok = lambda code: (
        isinstance(code, str) and ascii_bounded(code, 16) and "\0" not in code
    )


def _patch_word_diff_split(module: Any) -> None:
    import deal
    from plugin.framework.deal_shim import str_bounded

    module.split_change = deal.pre(
        lambda old, new, threshold=module._DEFAULT_THRESHOLD: (
            not isinstance(old, str) or str_bounded(old, 16)
        )
        and (not isinstance(new, str) or str_bounded(new, 16))
    )(module.split_change)


def _patch_embeddings_split(module: Any) -> None:
    module._DEAL_SENT_LIST_LEN = 1
    module._DEAL_SENT_SPAN = 1
    module._DEAL_SENT_TEXT_LEN = 1
    module._DEAL_PASSAGE_LEN = 1
    module._deal_sentence_locale_ok = (
        lambda locale: locale is module.DEFAULT_SENTENCE_LOCALE
    )
    module._deal_chunk_base_meta_ok = _stub_deal_chunk_base_meta_ok_crosshair
    module._deal_run_locale_ok = lambda locale: locale is None
    module._deal_passage_text_ok = lambda text: (
        isinstance(text, str)
        and len(text) <= 1
        and text.isascii()
        and all(c.isprintable() or c in "\n\t\r" for c in text)
    )
    module._deal_passage_str_ok = lambda passage: isinstance(passage, str) and len(passage) <= 1
    module._deal_ws_passage_ok = (
        lambda passage: isinstance(passage, str) and passage.isascii() and len(passage) <= 1
    )
    module._deal_span_list_ok = lambda spans: isinstance(spans, list) and len(spans) <= 1
    module._deal_sentences_spans_pre = _stub_deal_sentences_spans_pre_crosshair
    module._sentences_spans_shape_ok = lambda sentences: isinstance(sentences, list) and len(sentences) <= 1


_PATCHERS: dict[str, Callable[[Any], None]] = {
    "plugin.framework.deal_shim": _patch_deal_shim,
    "plugin.framework.appearance": _patch_appearance,
    "plugin.framework.async_stream": _patch_async_stream,
    "plugin.framework.config_schema": _patch_config_schema,
    "plugin.framework.errors": _patch_errors,
    "plugin.framework.client.auth": _patch_auth,
    "plugin.framework.openrouter_model_id": _patch_openrouter_model_id,
    "plugin.calc.python.formula_edit": _patch_formula_edit,
    "plugin.calc.spreadsheet_import.preprocess": _patch_preprocess,
    "plugin.calc.sheet_filter_criteria": _patch_sheet_filter_criteria,
    "plugin.chatbot.memory": _patch_memory,
    "plugin.mcp.cors": _patch_cors,
    "plugin.mcp.wire_types": _patch_wire_types,
    "plugin.scripting.audio_silence_detector": _patch_audio_silence_detector,
    "plugin.scripting.calc_range": _patch_calc_range,
    "plugin.scripting.duckdb_sql": _patch_duckdb_sql,
    "plugin.scripting.editor_errors": _patch_editor_errors,
    "plugin.scripting.editor_ipc": _patch_editor_ipc,
    "plugin.scripting.helper_domain": _patch_helper_domain,
    "plugin.scripting.payload_codec": _patch_payload_codec,
    "plugin.scripting.sandbox": _patch_sandbox,
    "plugin.framework.subprocess_env": _patch_subprocess_env,
    "plugin.scripting.sandbox_cache": _patch_sandbox_cache,
    "plugin.writer.word_diff_split": _patch_word_diff_split,
    "plugin.embeddings.embeddings_split": _patch_embeddings_split,
}


def _normalize_name(fullname: str) -> str:
    if fullname in _PATCHERS:
        return fullname
    if fullname.startswith("writeragent."):
        cand = "plugin." + fullname[len("writeragent.") :]
        if cand in _PATCHERS:
            return cand
    if not fullname.startswith("plugin."):
        cand = "plugin." + fullname
        if cand in _PATCHERS:
            return cand
    return fullname


def _patch_module(fullname: str, module: Any) -> None:
    norm = _normalize_name(fullname)
    patcher = _PATCHERS.get(norm)
    if patcher is not None:
        patcher(module)


class _WrappedLoader:
    def __init__(self, loader: Any, fullname: str) -> None:
        self._loader = loader
        self._fullname = fullname

    def create_module(self, spec: Any) -> Any:
        if hasattr(self._loader, "create_module"):
            return self._loader.create_module(spec)
        return None

    def exec_module(self, module: Any) -> None:
        self._loader.exec_module(module)
        _patch_module(self._fullname, module)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._loader, name)


class _CrossHairDomainFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname: str, path: Any = None, target: Any = None) -> Any:
        norm = _normalize_name(fullname)
        if norm not in _PATCHERS:
            return None
        for finder in sys.meta_path:
            if finder is self:
                continue
            if hasattr(finder, "find_spec"):
                spec = finder.find_spec(fullname, path, target)
                if spec is not None and spec.loader is not None:
                    spec.loader = _WrappedLoader(spec.loader, fullname)
                    return spec
        return None


def install() -> None:
    """Insert the meta-path finder and patch any already-imported target modules."""
    global _installed
    if not _installed:
        sys.meta_path.insert(0, _CrossHairDomainFinder())
        _installed = True
    for mod_name, mod in list(sys.modules.items()):
        norm = _normalize_name(mod_name)
        if norm in _PATCHERS and mod is not None:
            _patch_module(mod_name, mod)

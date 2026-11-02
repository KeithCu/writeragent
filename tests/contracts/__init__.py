"""Collector for deal contracts in tests/contracts."""

from __future__ import annotations

import importlib
from typing import Any, Callable, Iterator

# Import all sidecars to register/validate
from tests.contracts.calc import (
    address_utils as _calc_address_utils,
    calc_addin_data as _calc_calc_addin_data,
    sheet_filter_criteria as _calc_sheet_filter_criteria,
)
from tests.contracts.calc.excel_py_convert import (
    resolve_refs as _calc_excel_py_convert_resolve_refs,
    to_dag as _calc_excel_py_convert_to_dag,
)
from tests.contracts.calc.python import (
    formula_edit as _calc_python_formula_edit,
    xl_static_rewrite as _calc_python_xl_static_rewrite,
)
from tests.contracts.calc.spreadsheet_import import (
    preprocess as _calc_spreadsheet_import_preprocess,
)
from tests.contracts.chatbot import (
    audio_recorder_state as _chatbot_audio_recorder_state,
    chat_sidebar_mode as _chatbot_chat_sidebar_mode,
    memory as _chatbot_memory,
    send_state as _chatbot_send_state,
    state_machine as _chatbot_state_machine,
    tool_loop_state as _chatbot_tool_loop_state,
    web_research_cache as _chatbot_web_research_cache,
)
from tests.contracts.embeddings import (
    embeddings_split as _embeddings_embeddings_split,
)
from tests.contracts.framework import (
    appearance as _framework_appearance,
    ast_stmt_edit as _framework_ast_stmt_edit,
    async_stream as _framework_async_stream,
    config_schema as _framework_config_schema,
    constants as _framework_constants,
    default_models as _framework_default_models,
    errors as _framework_errors,
    html_stripper as _framework_html_stripper,
    i18n as _framework_i18n,
    json_utils as _framework_json_utils,
    openrouter_model_id as _framework_openrouter_model_id,
    stream_delta as _framework_stream_delta,
    tool as _framework_tool,
    tool_schema as _framework_tool_schema,
    url_utils as _framework_url_utils,
)
from tests.contracts.framework.client import (
    auth as _framework_client_auth,
    response_normalizers as _framework_client_response_normalizers,
    stream_normalizer as _framework_client_stream_normalizer,
)
from tests.contracts.mcp import (
    cors as _mcp_cors,
    tunnel_state as _mcp_tunnel_state,
    wire_types as _mcp_wire_types,
)
from tests.contracts.scripting import (
    audio_silence_detector as _scripting_audio_silence_detector,
    calc_range as _scripting_calc_range,
    config_limits as _scripting_config_limits,
    duckdb_sql as _scripting_duckdb_sql,
    editor_errors as _scripting_editor_errors,
    editor_ipc as _scripting_editor_ipc,
    excel_xl as _scripting_excel_xl,
    helper_domain as _scripting_helper_domain,
    payload_codec as _scripting_payload_codec,
    sandbox as _scripting_sandbox,
    sandbox_cache as _scripting_sandbox_cache,
    trusted_rpc as _scripting_trusted_rpc,
)
from tests.contracts.scripting.venv import (
    coerce as _scripting_venv_coerce,
)
from tests.contracts.writer import (
    word_diff_split as _writer_word_diff_split,
    xhtml_style_postprocess as _writer_xhtml_style_postprocess,
)

SIDECARS = [
    _calc_address_utils,
    _calc_calc_addin_data,
    _calc_sheet_filter_criteria,
    _calc_excel_py_convert_resolve_refs,
    _calc_excel_py_convert_to_dag,
    _calc_python_formula_edit,
    _calc_python_xl_static_rewrite,
    _calc_spreadsheet_import_preprocess,
    _chatbot_audio_recorder_state,
    _chatbot_chat_sidebar_mode,
    _chatbot_memory,
    _chatbot_send_state,
    _chatbot_state_machine,
    _chatbot_tool_loop_state,
    _chatbot_web_research_cache,
    _embeddings_embeddings_split,
    _framework_appearance,
    _framework_ast_stmt_edit,
    _framework_async_stream,
    _framework_config_schema,
    _framework_constants,
    _framework_default_models,
    _framework_errors,
    _framework_html_stripper,
    _framework_i18n,
    _framework_json_utils,
    _framework_openrouter_model_id,
    _framework_stream_delta,
    _framework_tool,
    _framework_tool_schema,
    _framework_url_utils,
    _framework_client_auth,
    _framework_client_response_normalizers,
    _framework_client_stream_normalizer,
    _mcp_cors,
    _mcp_tunnel_state,
    _mcp_wire_types,
    _scripting_audio_silence_detector,
    _scripting_calc_range,
    _scripting_config_limits,
    _scripting_duckdb_sql,
    _scripting_editor_errors,
    _scripting_editor_ipc,
    _scripting_excel_xl,
    _scripting_helper_domain,
    _scripting_payload_codec,
    _scripting_sandbox,
    _scripting_sandbox_cache,
    _scripting_trusted_rpc,
    _scripting_venv_coerce,
    _writer_word_diff_split,
    _writer_xhtml_style_postprocess,
]

PYTEST: dict[str, list[str]] = {
    mod.__name__: getattr(mod, "PYTEST", []) for mod in SIDECARS
}

TARGETS: list[str] = []
for mod in SIDECARS:
    for target in getattr(mod, "TARGETS", []):
        TARGETS.append(f"{mod.__name__}.{target}")


def iter_targets() -> Iterator[str]:
    """Yield each target FQN."""
    yield from TARGETS


def get_contract_fn(fqn: str) -> Callable[..., Any]:
    """Dynamically load the sidecar attribute for a given FQN."""
    mod_name, fn_name = fqn.rsplit(".", 1)
    mod = importlib.import_module(mod_name)
    return getattr(mod, fn_name)

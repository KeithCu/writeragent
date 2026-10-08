"""LibrePy bundle includes writeragent namespace stub, not full tool API."""

from __future__ import annotations

import sys
from pathlib import Path
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.librepy_bundle_paths import collect_librepy_plugin_paths  # noqa: E402


def test_librepy_bundle_includes_writeragent_namespace():
    paths = collect_librepy_plugin_paths(str(_REPO_ROOT))
    assert "plugin/scripting/writeragent_namespace.py" in paths
    assert "plugin/scripting/named_scripts.py" in paths
    assert "plugin/scripting/host_rpc.py" in paths


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("plugin/scripting/writeragent_api.py", id="test_librepy_bundle_excludes_writeragent_api"),
        pytest.param("plugin/framework/tool.py", id="test_librepy_bundle_excludes_dead_tool_module"),
    ],
)
def test_librepy_bundle_excludes_writeragent_api(value):
    paths = collect_librepy_plugin_paths(str(_REPO_ROOT))
    assert value not in paths


def test_librepy_bundle_includes_settings_fields():
    paths = collect_librepy_plugin_paths(str(_REPO_ROOT))
    assert "plugin/chatbot/settings_fields.py" in paths
    assert "plugin/scripting/venv_probe_ui.py" in paths


@pytest.mark.parametrize(
    "value",
    [
        # Still on the LibrePy allowlist. No plugin module imports it; strip_code does at build time.
        pytest.param("plugin/framework/ast_stmt_edit.py", id="test_librepy_bundle_includes_ast_stmt_edit"),
        # constants and other framework modules import deal via deal_shim; must ship in LibrePy.oxt.
        pytest.param("plugin/framework/deal_shim.py", id="test_librepy_bundle_includes_deal_shim"),
    ],
)
def test_librepy_bundle_includes_ast_stmt_edit(value):
    paths = collect_librepy_plugin_paths(str(_REPO_ROOT))
    assert value in paths

@pytest.mark.parametrize(
    "value, value_2, value_3, value_4",
    [
        pytest.param("plugin/doc/doc_type.py", "plugin/doc/text_helpers.py", "plugin/calc/datetime_wire.py", "plugin/calc/analyzer.py", id="test_librepy_bundle_includes_doc_type_and_datetime_wire"),
        # Weekly update check + sync_request deps must ship in LibrePy.oxt.
        pytest.param("plugin/chatbot/extension_update_check.py", "plugin/framework/client/requests.py", "plugin/framework/client/ssl_helpers.py", "plugin/framework/client/provider_detection.py", id="test_librepy_bundle_includes_extension_update_check"),
    ],
)
def test_librepy_bundle_includes_doc_type_and_datetime_wire(value, value_2, value_3, value_4):
    paths = collect_librepy_plugin_paths(str(_REPO_ROOT))
    assert value in paths
    assert value_2 in paths
    assert value_3 in paths
    assert value_4 in paths

def test_librepy_bundle_excludes_chat_document_helpers_and_smolagents_tools():
    paths = collect_librepy_plugin_paths(str(_REPO_ROOT))
    assert "plugin/doc/document_helpers.py" not in paths
    assert "plugin/writer/review_authors.py" not in paths
    assert "plugin/draw/bridge.py" not in paths
    assert "plugin/chatbot/settings_tab_order.py" not in paths
    assert "plugin/contrib/smolagents/tools.py" not in paths
    assert "plugin/contrib/smolagents/agent_types.py" not in paths
    assert "plugin/contrib/smolagents/tool_validation.py" not in paths
    assert "plugin/contrib/smolagents/_function_type_hints_utils.py" not in paths
    assert "plugin/contrib/smolagents/local_python_executor.py" in paths
    assert "plugin/contrib/smolagents/utils.py" in paths


def test_librepy_bundle_excludes_llm_image_gen_and_llm_client():
    """LLM image gen and llm_client must not ship in LibrePy; analyzer stays (later use)."""
    paths = collect_librepy_plugin_paths(str(_REPO_ROOT))
    assert "plugin/writer/images/image_utils.py" not in paths
    assert "plugin/framework/client/llm_client.py" not in paths
    assert "plugin/writer/images/image_tools.py" in paths
    assert "plugin/calc/analyzer.py" in paths


def test_librepy_bundle_excludes_writeragent_grammar_engines():
    """Vale and LanguageTool live under writer.locale and are WriterAgent-only."""
    paths = collect_librepy_plugin_paths(str(_REPO_ROOT))
    assert "plugin/writer/locale/vale.py" not in paths
    assert "plugin/writer/locale/languagetool.py" not in paths
    assert "plugin/scripting/venv/vale.py" not in paths
    assert "plugin/scripting/venv/languagetool.py" not in paths


def test_librepy_bundle_includes_xl_static_rewrite():
    paths = collect_librepy_plugin_paths(str(_REPO_ROOT))
    assert "plugin/calc/python/xl_static_rewrite.py" in paths
    assert "plugin/calc/python/addin_impl.py" in paths
    assert "plugin/scripting/native_binaries.py" in paths
    assert "plugin/scripting/audio_recorder_service.py" not in paths
    assert "plugin/calc/python/workbook_lifecycle.py" in paths
    assert "plugin/calc/python/geometric_recalc.py" in paths
    assert "plugin/calc/python/sheet_modify.py" in paths


def test_librepy_bundle_includes_notebook_and_nbformat():
    """File → Open .ipynb ships in LibrePy; chat/LLM stay excluded."""
    paths = collect_librepy_plugin_paths(str(_REPO_ROOT))
    assert "plugin/notebook/import_filter.py" in paths
    assert "plugin/notebook/writer_importer.py" in paths
    assert "plugin/notebook/notebook_runner.py" in paths
    assert "plugin/contrib/nbformat/reader.py" in paths
    assert "plugin/framework/async_stream.py" in paths
    assert "plugin/framework/client/llm_client.py" not in paths
    assert "plugin/doc/document_helpers.py" not in paths



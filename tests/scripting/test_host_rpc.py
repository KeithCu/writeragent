# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for venv → LibreOffice tool RPC (host side)."""

from __future__ import annotations

import io
from unittest.mock import MagicMock, patch

from plugin.scripting.ipc import read_pickle_frame
from plugin.scripting.host_rpc import (
    TOOL_RPC_DISABLED,
    execute_tool,
    handle_tool_call_frame,
    resolve_allowed_tools,
)


def test_resolve_allowed_tools_unrestricted():
    assert resolve_allowed_tools(None) is None


def test_resolve_allowed_tools_disabled():
    assert resolve_allowed_tools(TOOL_RPC_DISABLED) == frozenset()
    assert resolve_allowed_tools("") == frozenset()


def test_resolve_allowed_tools_writer_domain_includes_apply():
    allowed = resolve_allowed_tools("writer")
    assert allowed is not None
    assert "apply_document_content" in allowed
    assert "list_open_documents" in allowed
    assert "write_formula_range" not in allowed


def test_resolve_allowed_tools_plural_domain_name():
    # Chat specialized_domain is "footnotes"; generated DOMAIN_TOOLS key is "footnote".
    allowed = resolve_allowed_tools("footnotes")
    assert allowed is not None
    assert "footnotes_insert" in allowed
    assert "list_open_documents" in allowed


def test_format_script_api_catalog_embeds_full_proxy_docstrings():
    """Inner catalog is the generated Args text, not a one-line summary."""
    import inspect

    import plugin.scripting.writeragent_api as api
    from plugin.scripting.host_rpc import (
        _domain_tools_map,
        _proxy_methods_by_tool,
        format_script_api_catalog,
        inner_script_tool_domain,
    )
    from plugin.scripting.writeragent_api import DOMAIN_TOOLS

    catalog = format_script_api_catalog(["shapes"])
    assert catalog.startswith("run_venv_python_script has access to the following APIs")
    assert "Only the Python script may call these wa.* functions" in catalog
    assert "import writeragent as wa" in catalog
    assert "wa.core.list_open_documents()" in catalog
    assert "wa.shape.upsert(" in catalog
    assert "wa.footnote." not in catalog
    assert "wa.python.run_venv_python_script" not in catalog

    tools = _domain_tools_map()
    assert tools is not None
    methods = _proxy_methods_by_tool(tools)
    assert methods is not None
    for tool_name in (*DOMAIN_TOOLS["core"], *DOMAIN_TOOLS["shape"]):
        method = methods[tool_name]
        doc = inspect.cleandoc(method.__doc__ or "")
        assert doc
        assert doc in catalog
    # Rich Args from the proxy generator (#916), not a trimmed blurb.
    assert "shape_type (optional)" in catalog
    assert "round-rectangle" in catalog
    assert "action (required)" in catalog
    assert len(catalog) > 5000

    both = format_script_api_catalog(["footnotes", "shapes"])
    assert both.count("core:\n") == 1
    assert both.count("shape:\n") == 1
    assert both.count("footnote:\n") == 1
    assert inspect.cleandoc(api.footnote.insert.__doc__ or "") in both
    assert inspect.cleandoc(api.shape.upsert.__doc__ or "") in both
    assert inner_script_tool_domain(["shapes", "footnotes"]) == "shapes,footnotes,core"


def test_format_script_api_catalog_empty_without_proxy():
    """LibrePy omits writeragent_api; the inner prompt then has no catalog."""
    from unittest.mock import patch

    from plugin.scripting.host_rpc import format_script_api_catalog

    with patch("plugin.scripting.host_rpc._domain_tools_map", return_value=None):
        assert format_script_api_catalog(["shapes"]) == ""
    with patch("plugin.scripting.host_rpc._proxy_methods_by_tool", return_value=None):
        assert format_script_api_catalog(["shapes"]) == ""


def test_inner_script_allowlist_unions_core_without_widening_other_scopes():
    """Inner DTD scripts may call core. Bare domains, None, and =PY() stay put."""
    from plugin.scripting.host_rpc import inner_script_tool_domain
    from plugin.scripting.writeragent_api import DOMAIN_TOOLS

    allowed = resolve_allowed_tools(inner_script_tool_domain(["shapes"]))
    assert allowed is not None
    assert set(DOMAIN_TOOLS["core"]) <= allowed
    assert "shape_upsert" in allowed
    assert "footnotes_insert" not in allowed
    assert "run_venv_python_script" not in allowed

    # A domain string that is not the inner path does not pick up core.
    writer = resolve_allowed_tools("writer")
    assert writer is not None
    assert "apply_document_content" in writer
    assert "undo" not in writer
    assert "web_research" not in writer
    assert "list_open_documents" in writer

    assert resolve_allowed_tools(None) is None
    assert resolve_allowed_tools("") == frozenset()
    assert resolve_allowed_tools("python") is not None
    assert "symbolic_math" in resolve_allowed_tools("python")
    assert "run_venv_python_script" not in resolve_allowed_tools("python")


def test_resolve_allowed_tools_shapes_and_multi_domain_union():
    """Inner delegate_tool_domains passes specialized names, including a comma-separated union."""
    shapes = resolve_allowed_tools("shapes")
    assert shapes is not None
    assert "shape_upsert" in shapes
    assert "shape_summary" in shapes
    assert "footnotes_insert" not in shapes
    assert "list_open_documents" in shapes

    union = resolve_allowed_tools("shapes, footnotes")
    assert union is not None
    assert "shape_upsert" in union
    assert "footnotes_insert" in union
    assert "sort_range" not in union
    assert "list_open_documents" in union
    assert union == shapes | resolve_allowed_tools("footnotes")


def test_execute_tool_blocks_recursive_venv_script():
    try:
        execute_tool("run_venv_python_script", {"code": "result = 1"})
    except RuntimeError as exc:
        assert "re-enter" in str(exc)
    else:
        raise AssertionError("expected RuntimeError")


def test_execute_tool_disabled_during_py_recalc():
    try:
        execute_tool("apply_document_content", {"content": ["x"]}, allowed_tools=frozenset())
    except RuntimeError as exc:
        assert "=PY()" in str(exc)
    else:
        raise AssertionError("expected RuntimeError")


def test_execute_tool_rejects_out_of_domain():
    try:
        execute_tool("write_formula_range", {}, allowed_tools=frozenset({"apply_document_content"}))
    except RuntimeError as exc:
        assert "not available" in str(exc)
    else:
        raise AssertionError("expected RuntimeError")


def test_handle_tool_call_frame_writes_ok_response():
    written: list[bytes] = []
    with patch("plugin.scripting.host_rpc.execute_tool", return_value={"status": "ok"}) as mock_tool:
        handled = handle_tool_call_frame(
            {"type": "tool_call", "id": "abc", "tool": "apply_document_content", "args": {"content": ["<p>Hi</p>"], "target": "end"}},
            stdin_write=written.append,
        )
    assert handled is True
    mock_tool.assert_called_once()
    assert mock_tool.call_args[0][0] == "apply_document_content"
    assert mock_tool.call_args[0][1]["target"] == "end"
    assert len(written) == 1
    resp = read_pickle_frame(io.BytesIO(written[0]), require_dict=True)
    assert resp is not None
    assert resp["status"] == "ok"
    assert resp["id"] == "abc"
    assert resp["result"] == {"status": "ok"}


def test_handle_tool_call_frame_writes_error_response():
    written: list[bytes] = []
    with patch("plugin.scripting.host_rpc.execute_tool", side_effect=RuntimeError("boom")):
        handled = handle_tool_call_frame(
            {"type": "tool_call", "id": "e1", "tool": "apply_document_content", "args": {}},
            stdin_write=written.append,
        )
    assert handled is True
    resp = read_pickle_frame(io.BytesIO(written[0]), require_dict=True)
    assert resp is not None
    assert resp["status"] == "error"
    assert resp["id"] == "e1"
    assert "boom" in resp["message"]


def test_handle_non_tool_call_returns_false():
    assert handle_tool_call_frame({"status": "ok", "result": 1}, stdin_write=MagicMock()) is False
    assert handle_tool_call_frame({"type": "worker_event"}, stdin_write=MagicMock()) is False


def test_rpc_call_drops_none_kwargs():
    import plugin.scripting.writeragent_api as api

    with patch.object(api, "IS_WORKER", True), patch.object(api, "write_pickle_frame") as mock_write, patch.object(
        api, "read_pickle_frame", return_value={"status": "ok", "result": {"status": "ok"}}
    ):
        api._rpc_call("apply_document_content", content=["<p>Hi</p>"], target="end", dry_run=None, regex=None)
    sent = mock_write.call_args[0][1]
    assert sent["args"] == {"content": ["<p>Hi</p>"], "target": "end"}
    assert "dry_run" not in sent["args"]


def test_run_code_in_user_venv_forwards_python_tool_domain():
    from plugin.scripting.venv_worker import run_code_in_user_venv

    ctx = MagicMock()
    with patch("plugin.scripting.venv_worker._worker_manager_for_ctx") as mock_mgr:
        manager = MagicMock()
        mock_mgr.return_value = (manager, None)
        manager.execute.return_value = {"status": "ok", "result": 1}
        run_code_in_user_venv(ctx, "result = 1", python_tool_domain="writer")
        assert manager.execute.call_args.kwargs.get("python_tool_domain") == "writer"


def test_apply_document_content_proxy_omits_optional_defaults():
    import inspect

    import plugin.scripting.writeragent_api as api

    params = inspect.signature(api.writer.apply_document_content).parameters
    assert params["dry_run"].default is None
    assert params["target"].default is None


def test_resolve_allowed_tools_indexes_and_exempt_domains():
    allowed_indexes = resolve_allowed_tools("indexes")
    assert allowed_indexes is not None
    assert "indexes_create" in allowed_indexes
    assert "list_open_documents" in allowed_indexes

    allowed_images = resolve_allowed_tools("images")
    assert allowed_images is not None
    assert "image_insert" in allowed_images


def test_handle_tool_call_frame_invalid_tool_name_type():
    import pytest

    with pytest.raises(RuntimeError, match="Invalid tool_call"):
        handle_tool_call_frame({"type": "tool_call", "tool": 123}, stdin_write=MagicMock())

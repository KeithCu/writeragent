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


def test_resolve_allowed_tools_librepy_domain_stays_unrestricted():
    """No writeragent_api: a domain string must not disable every tool RPC."""
    with patch("plugin.scripting.host_rpc._domain_tools_map", return_value=None):
        assert resolve_allowed_tools("writer") is None
        assert resolve_allowed_tools("shapes, footnotes, core") is None
    # The proxy is present: an unknown name is still a real allowlist.
    unknown = resolve_allowed_tools("not-a-domain")
    assert unknown is not None
    assert "list_open_documents" in unknown


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


def test_shape_group_proxy_docstring_says_why_to_group():
    """wa.shape.group carries the tool description, including why to group."""
    import inspect

    import plugin.scripting.writeragent_api as api
    from plugin.calc.shapes import GroupShapes as CalcGroupShapes
    from plugin.draw.shapes import GroupShapes
    from plugin.writer.specialized.shapes import GroupShapes as WriterGroupShapes

    doc = inspect.cleandoc(api.shape.group.__doc__ or "")
    assert "select and move them together" in doc
    assert "a flag, logo, diagram, icon" in doc
    assert "ALWAYS call this before finishing" in doc
    assert "leaving loose parts is wrong" in doc
    assert "Pass every related shape index" in doc
    assert "shape_upsert (action edit)" in doc
    # Args stay the generated schema text.
    assert "indices (required): List of shape indices to group." in doc
    assert "page (optional): Page index containing the shapes." in doc
    # Writer and Calc re-export Draw's class and do not own a second description.
    assert WriterGroupShapes.description == GroupShapes.description
    assert CalcGroupShapes.description == GroupShapes.description
    assert GroupShapes.description in doc


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
    assert not hasattr(api.python, "run_venv_python_script")
    assert DOMAIN_TOOLS["python"] == ["symbolic_math"]

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


def test_execute_tool_named_scripts_blocked_during_py_recalc():
    """=PY() empty allowlist must not list or read stored script source."""
    for tool_name in ("get_named_python_script", "list_named_python_scripts"):
        try:
            execute_tool(tool_name, {"name": "Helpers"}, allowed_tools=frozenset())
        except RuntimeError as exc:
            assert "=PY()" in str(exc)
        else:
            raise AssertionError(f"expected RuntimeError for {tool_name}")


def test_rpc_tool_name_none_when_candidates_are_parameters():
    """A const that is only a parameter name is not the RPC tool."""
    from plugin.scripting.host_rpc import _rpc_tool_name

    def shape_upsert(shape_upsert: str = "shape_upsert") -> str:
        return shape_upsert

    assert _rpc_tool_name(shape_upsert, frozenset({"shape_upsert"})) is None

    def mixed(shape_upsert: str = "shape_upsert") -> tuple[str, str]:
        return ("list_open_documents", shape_upsert)

    assert _rpc_tool_name(mixed, frozenset({"shape_upsert", "list_open_documents"})) == "list_open_documents"


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


def test_execute_tool_prefers_script_session_document():
    focused = MagicMock()
    bound = MagicMock()
    registry = MagicMock()
    registry._services = {}
    registry.execute.return_value = {"status": "ok"}
    with (
        patch("plugin.framework.queue_executor.execute_on_main_thread", side_effect=lambda fn: fn()) as mock_exec_main,
        patch("plugin.framework.uno_context.get_ctx", return_value=MagicMock()),
        patch("plugin.framework.uno_context.get_active_document", return_value=focused) as mock_active,
        patch("plugin.scripting.session_manager.document_for_script_session", return_value=bound) as mock_session,
        patch("plugin.main.get_tools", return_value=registry),
        patch("plugin.doc.doc_type.is_draw", return_value=True),
        patch("plugin.doc.doc_type.is_calc", return_value=False),
        patch("plugin.doc.doc_type.is_writer", return_value=False),
    ):
        execute_tool(
            "export_presentation_project",
            {"project_path": "/p"},
            script_session_id="ppt_master:file:///deck-b.odp",
        )
    mock_session.assert_called_once()
    assert mock_session.call_args.args[1] == "ppt_master:file:///deck-b.odp"
    mock_active.assert_not_called()
    assert registry.execute.call_args.args[1].doc is bound


def test_execute_tool_uses_pinned_document_not_front_window():
    from plugin.scripting.session_manager import pin_script_document, release_script_document

    focused = MagicMock()
    bound = MagicMock()
    registry = MagicMock()
    registry._services = {}
    registry.execute.return_value = {"status": "ok"}
    token = pin_script_document(bound)
    try:
        with (
            patch("plugin.framework.queue_executor.execute_on_main_thread", side_effect=lambda fn: fn()),
            patch("plugin.framework.uno_context.get_ctx", return_value=MagicMock()),
            patch("plugin.framework.uno_context.get_active_document", return_value=focused) as mock_active,
            patch("plugin.scripting.session_manager.get_desktop") as mock_desktop,
            patch("plugin.main.get_tools", return_value=registry),
            patch("plugin.doc.doc_type.is_draw", return_value=True),
            patch("plugin.doc.doc_type.is_calc", return_value=False),
            patch("plugin.doc.doc_type.is_writer", return_value=False),
        ):
            execute_tool("shape_upsert", {"action": "create"}, script_session_id=token)
        mock_desktop.assert_not_called()
        mock_active.assert_not_called()
        assert registry.execute.call_args.args[1].doc is bound
    finally:
        release_script_document(token)


def test_handle_tool_call_frame_refuses_when_stopped():
    written: list[bytes] = []
    with patch("plugin.scripting.host_rpc.execute_tool") as mock_tool:
        handled = handle_tool_call_frame(
            {"type": "tool_call", "id": "stop1", "tool": "export_presentation_project", "args": {}},
            stdin_write=written.append,
            stop_checker=lambda: True,
        )
    assert handled is True
    mock_tool.assert_not_called()
    resp = read_pickle_frame(io.BytesIO(written[0]), require_dict=True)
    assert resp is not None
    assert resp["status"] == "error"
    assert resp["code"] == "USER_STOPPED"
    assert resp["id"] == "stop1"


def test_handle_tool_call_frame_broken_pipe_does_not_abort_stop_or_reply():
    """A dead worker pipe must not escape the USER_STOPPED or result write."""

    def _broken(_blob: bytes) -> None:
        raise BrokenPipeError("stdin closed")

    with patch("plugin.scripting.host_rpc.execute_tool") as mock_tool:
        stopped = handle_tool_call_frame(
            {"type": "tool_call", "id": "stop1", "tool": "export_presentation_project", "args": {}},
            stdin_write=_broken,
            stop_checker=lambda: True,
        )
    assert stopped is True
    mock_tool.assert_not_called()

    def _oserror(_blob: bytes) -> None:
        raise OSError("stdin closed")

    with patch("plugin.scripting.host_rpc.execute_tool", return_value={"status": "ok"}):
        ok = handle_tool_call_frame(
            {"type": "tool_call", "id": "ok1", "tool": "apply_document_content", "args": {}},
            stdin_write=_oserror,
        )
    assert ok is True
    with patch("plugin.scripting.host_rpc.execute_tool", side_effect=RuntimeError("boom")):
        failed = handle_tool_call_frame(
            {"type": "tool_call", "id": "e1", "tool": "apply_document_content", "args": {}},
            stdin_write=_oserror,
        )
    assert failed is True


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

    with patch.object(api, "IS_WORKER", True), patch(
        "plugin.scripting.ipc.exchange_tool_call", return_value={}
    ) as mock_exchange:
        api._rpc_call("apply_document_content", content=["<p>Hi</p>"], target="end", dry_run=None, regex=None)
    mock_exchange.assert_called_once_with(
        "apply_document_content",
        {"content": ["<p>Hi</p>"], "target": "end"},
    )


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

def test_execute_tool_async_tool_runs_on_caller_thread():
    from plugin.framework.tool import ToolBase, ToolContext
    import threading

    class AsyncMockTool(ToolBase):
        name: str = "async_mock_tool"
        description: str = "Mock tool"
        timeout: float = 600.0

        def is_async(self) -> bool:
            return True

        def execute(self, ctx: ToolContext, **kwargs) -> dict:
            return {"status": "ok", "thread": threading.current_thread().name}

    from plugin.scripting.host_rpc import execute_tool

    registry = MagicMock()
    registry._services = {}

    # We simulate ToolRegistry.execute's thread behavior loosely for tests,
    # but more importantly we test that host_rpc does not wrap the final `registry.execute`
    # in `execute_on_main_thread` directly anymore since we split it.

    # ToolRegistry.execute is actually what executes. Let's make sure it receives ToolContext properly.
    def mock_registry_execute(tool_name, tctx, **kwargs):
        assert tctx.doc is not None
        assert tctx.ctx is not None
        return {"status": "ok", "caller_tctx": tctx.caller}

    registry.execute.side_effect = mock_registry_execute
    focused = MagicMock()
    bound = MagicMock()

    with (
        patch("plugin.framework.queue_executor.execute_on_main_thread", side_effect=lambda fn: fn()) as mock_exec_main,
        patch("plugin.framework.uno_context.get_ctx", return_value=MagicMock()),
        patch("plugin.framework.uno_context.get_active_document", return_value=focused) as mock_active,
        patch("plugin.scripting.session_manager.document_for_script_session", return_value=bound) as mock_session,
        patch("plugin.main.get_tools", return_value=registry),
        patch("plugin.doc.doc_type.is_draw", return_value=True),
        patch("plugin.doc.doc_type.is_calc", return_value=False),
        patch("plugin.doc.doc_type.is_writer", return_value=False),
    ):
        res = execute_tool("async_mock_tool", {}, allowed_tools=frozenset({"async_mock_tool"}), caller="test_caller")

    assert res["status"] == "ok"
    assert res["caller_tctx"] == "test_caller"
    registry.execute.assert_called_once()

    # execute_on_main_thread should only be called once, for `_run`
    mock_exec_main.assert_called_once()
    assert mock_exec_main.call_args[0][0].__name__ == "_run"

# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for venv → LibreOffice tool RPC (host side)."""

from __future__ import annotations

import io
import os
from unittest.mock import MagicMock, patch

import pytest

from plugin.framework.ipc import read_pickle_frame
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
    assert "wa.core.list_open_documents()" in catalog
    assert "wa.shape.upsert(" in catalog
    assert "wa.footnote." not in catalog

    tools = _domain_tools_map()
    assert tools is not None
    methods = _proxy_methods_by_tool()
    assert methods is not None
    for tool_name in (*DOMAIN_TOOLS["core"], *DOMAIN_TOOLS["shape"]):
        method = methods[tool_name]
        doc = inspect.cleandoc(method.__doc__ or "")
        assert doc
        assert doc in catalog

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

    from plugin.scripting.host_rpc import format_script_api_catalog, _format_script_api_catalog_cached

    _format_script_api_catalog_cached.cache_clear()
    with patch("plugin.scripting.host_rpc._domain_tools_map", return_value=None):
        assert format_script_api_catalog(["shapes_empty_1"]) == ""
    _format_script_api_catalog_cached.cache_clear()
    with patch("plugin.scripting.host_rpc._proxy_methods_by_tool", return_value=None):
        assert format_script_api_catalog(["shapes_empty_2"]) == ""


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
    assert "range_sort" not in union
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
        patch("plugin.framework.queue_executor.main.call", side_effect=lambda fn: fn()),
        patch("plugin.scripting.host_rpc.get_ctx", return_value=MagicMock()),
        patch("plugin.scripting.host_rpc.get_active_document", return_value=focused) as mock_active,
        patch("plugin.scripting.session_manager.document_for_script_session", return_value=bound) as mock_session,
        patch("plugin.app.get_tools", return_value=registry),
        patch("plugin.framework.doc_type.is_draw", return_value=True),
        patch("plugin.framework.doc_type.is_calc", return_value=False),
        patch("plugin.framework.doc_type.is_writer", return_value=False),
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
            patch("plugin.framework.queue_executor.main.call", side_effect=lambda fn: fn()),
            patch("plugin.scripting.host_rpc.get_ctx", return_value=MagicMock()),
            patch("plugin.scripting.host_rpc.get_active_document", return_value=focused) as mock_active,
            patch("plugin.scripting.session_manager.get_desktop") as mock_desktop,
            patch("plugin.app.get_tools", return_value=registry),
            patch("plugin.framework.doc_type.is_draw", return_value=True),
            patch("plugin.framework.doc_type.is_calc", return_value=False),
            patch("plugin.framework.doc_type.is_writer", return_value=False),
        ):
            execute_tool("shape_upsert", {"action": "create"}, script_session_id=token)
        mock_desktop.assert_not_called()
        mock_active.assert_not_called()
        assert registry.execute.call_args.args[1].doc is bound
    finally:
        release_script_document(token)


def test_handle_tool_call_frame_refuses_when_stopped():
    from plugin.framework.send_cancellation import SendCancellation

    scope = SendCancellation()
    scope.cancel()
    written: list[bytes] = []
    with patch("plugin.scripting.host_rpc.execute_tool") as mock_tool:
        handled = handle_tool_call_frame(
            {"type": "tool_call", "id": "stop1", "tool": "export_presentation_project", "args": {}},
            stdin_write=written.append,
            cancel=scope,
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
    from plugin.framework.send_cancellation import SendCancellation

    scope = SendCancellation()
    scope.cancel()

    def _broken(_blob: bytes) -> None:
        raise BrokenPipeError("stdin closed")

    with patch("plugin.scripting.host_rpc.execute_tool") as mock_tool:
        stopped = handle_tool_call_frame(
            {"type": "tool_call", "id": "stop1", "tool": "export_presentation_project", "args": {}},
            stdin_write=_broken,
            cancel=scope,
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

    def _valueerror(_blob: bytes) -> None:
        raise ValueError("I/O operation on closed file")

    with patch("plugin.scripting.host_rpc.execute_tool", return_value={"status": "ok"}):
        ve_ok = handle_tool_call_frame(
            {"type": "tool_call", "id": "ok2", "tool": "apply_document_content", "args": {}},
            stdin_write=_valueerror,
        )
    assert ve_ok is True


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
    assert "code" not in resp


def test_handle_tool_call_frame_uno_message_skips_str():
    import plugin.framework.logging  # noqa: F401  # ensures the log-record factory is installed

    calls: list[str] = []

    class HostileUno(Exception):
        __pyunostruct__ = "com.sun.star.uno.RuntimeException"
        Message = "boom"

        def __str__(self) -> str:
            calls.append("str")
            raise AssertionError("str() called on hostile UNO exception")

        def __repr__(self) -> str:
            calls.append("repr")
            raise AssertionError("repr() called on hostile UNO exception")

    written: list[bytes] = []
    with patch("plugin.scripting.host_rpc.execute_tool", side_effect=HostileUno()):
        handled = handle_tool_call_frame(
            {"type": "tool_call", "id": "e_uno", "tool": "apply_document_content", "args": {}},
            stdin_write=written.append,
        )
    assert handled is True
    resp = read_pickle_frame(io.BytesIO(written[0]), require_dict=True)
    assert resp is not None
    assert resp["status"] == "error"
    assert resp["id"] == "e_uno"
    assert resp["message"] == "boom"
    assert "code" not in resp
    assert calls == []

    class HostileUnoEmptyMessage(Exception):
        __pyunostruct__ = "com.sun.star.uno.RuntimeException"
        Message = ""

        def __str__(self) -> str:
            calls.append("str")
            raise AssertionError("str() called on hostile UNO exception with empty message")

        def __repr__(self) -> str:
            calls.append("repr")
            raise AssertionError("repr() called on hostile UNO exception with empty message")

    written_empty: list[bytes] = []
    with patch("plugin.scripting.host_rpc.execute_tool", side_effect=HostileUnoEmptyMessage()):
        handled_empty = handle_tool_call_frame(
            {"type": "tool_call", "id": "e_uno_empty", "tool": "apply_document_content", "args": {}},
            stdin_write=written_empty.append,
        )
    assert handled_empty is True
    resp_empty = read_pickle_frame(io.BytesIO(written_empty[0]), require_dict=True)
    assert resp_empty is not None
    assert resp_empty["status"] == "error"
    assert resp_empty["id"] == "e_uno_empty"
    assert resp_empty["message"] == "HostileUnoEmptyMessage"
    assert "code" not in resp_empty
    assert calls == []


def test_handle_tool_call_frame_blank_message_uses_type_name():
    written_blank: list[bytes] = []
    with patch("plugin.scripting.host_rpc.execute_tool", side_effect=RuntimeError("")):
        handled_blank = handle_tool_call_frame(
            {"type": "tool_call", "id": "e_blank", "tool": "apply_document_content", "args": {}},
            stdin_write=written_blank.append,
        )
    assert handled_blank is True
    resp_blank = read_pickle_frame(io.BytesIO(written_blank[0]), require_dict=True)
    assert resp_blank is not None
    assert resp_blank["status"] == "error"
    assert resp_blank["id"] == "e_blank"
    assert resp_blank["message"] == "RuntimeError"
    assert "code" not in resp_blank


def test_handle_tool_call_frame_includes_error_code():
    from plugin.framework.errors import ToolExecutionError

    written: list[bytes] = []
    with patch(
        "plugin.scripting.host_rpc.execute_tool",
        side_effect=ToolExecutionError("cancelled by user", code="USER_STOPPED"),
    ):
        handled = handle_tool_call_frame(
            {"type": "tool_call", "id": "e2", "tool": "apply_document_content", "args": {}},
            stdin_write=written.append,
        )
    assert handled is True
    resp = read_pickle_frame(io.BytesIO(written[0]), require_dict=True)
    assert resp is not None
    assert resp["status"] == "error"
    assert resp["id"] == "e2"
    assert resp["code"] == "USER_STOPPED"
    assert "cancelled by user" in resp["message"]



def test_handle_non_tool_call_returns_false():
    assert handle_tool_call_frame({"status": "ok", "result": 1}, stdin_write=MagicMock()) is False
    assert handle_tool_call_frame({"type": "worker_event"}, stdin_write=MagicMock()) is False


def test_handle_tool_call_frame_rejects_non_dict_args():
    written: list[bytes] = []
    with patch("plugin.scripting.host_rpc.execute_tool") as mock_tool:
        handled = handle_tool_call_frame(
            {"type": "tool_call", "id": "e1", "tool": "apply_document_content", "args": ["not", "a", "dict"]},
            stdin_write=written.append,
        )
    assert handled is True
    mock_tool.assert_not_called()
    assert len(written) == 1
    from plugin.framework.ipc import read_pickle_frame
    import io
    resp = read_pickle_frame(io.BytesIO(written[0]), require_dict=True)
    assert resp is not None
    assert resp["status"] == "error"
    assert resp["id"] == "e1"
    assert "args must be a dictionary" in resp["message"]


def test_handle_tool_call_frame_unpicklable_result():
    written: list[bytes] = []

    def unpicklable():
        return lambda x: x

    with patch("plugin.scripting.host_rpc.execute_tool", return_value=unpicklable()):
        handled = handle_tool_call_frame(
            {"type": "tool_call", "id": "u1", "tool": "apply_document_content", "args": {}},
            stdin_write=written.append,
        )
    assert handled is True
    assert len(written) == 1
    from plugin.framework.ipc import read_pickle_frame
    import io
    resp = read_pickle_frame(io.BytesIO(written[0]), require_dict=True)
    assert resp is not None
    assert resp["status"] == "error"
    assert resp["id"] == "u1"
    assert "Result not serializable" in resp["message"]


def test_rpc_call_drops_none_kwargs():
    import plugin.scripting.writeragent_api as api

    with patch.dict(os.environ, {"WRITERAGENT_IS_WORKER": "1", "WRITERAGENT_COMPUTE_WORKER": "0"}), patch(
        "plugin.framework.ipc.exchange_tool_call", return_value={}
    ) as mock_exchange:
        api.call_host_tool("apply_document_content", content=["<p>Hi</p>"], target="end", dry_run=None, regex=None)
    mock_exchange.assert_called_once_with(
        "apply_document_content",
        {"content": ["<p>Hi</p>"], "target": "end"},
    )


def test_run_code_in_user_venv_forwards_python_tool_domain():
    from plugin.scripting.venv_worker import run_code_in_user_venv

    with patch("plugin.scripting.venv_worker._get_worker_manager") as mock_mgr, patch(
        "plugin.scripting.venv_worker.get_config", return_value=""
    ):
        manager = MagicMock()
        mock_mgr.return_value = (manager, None)
        manager.execute.return_value = {"status": "ok", "result": 1}
        run_code_in_user_venv("result = 1", python_tool_domain="writer")
        req = manager.execute.call_args.args[0]
        assert req.python_tool_domain == "writer"


def test_resolve_allowed_tools_indexes_and_exempt_domains():
    allowed_indexes = resolve_allowed_tools("indexes")
    assert allowed_indexes is not None
    assert "indexes_create" in allowed_indexes
    assert "list_open_documents" in allowed_indexes

    allowed_images = resolve_allowed_tools("images")
    assert allowed_images is not None
    assert "image_insert" in allowed_images


def test_handle_tool_call_frame_invalid_tool_name_type():
    import io

    written: list[bytes] = []
    handled = handle_tool_call_frame(
        {"type": "tool_call", "id": "1", "tool": None},
        stdin_write=written.append,
    )

    assert handled is True
    resp = read_pickle_frame(io.BytesIO(written[0]), require_dict=True)
    assert resp is not None
    assert resp["status"] == "error"
    assert "Invalid tool_call: None" in resp["message"]

def test_singularize_domain_leaves_non_map_names_unchanged():
    from plugin.scripting.host_rpc import singularize_domain

    for name in ("images", "styles", "forms", "core", "analysis", "status", "shape", "style"):
        assert singularize_domain(name) == name


@pytest.mark.parametrize(
    "plural,key",
    [
        ("bookmarks", "bookmark"),
        ("charts", "chart"),
        ("cell_comments", "cell_comment"),
        ("comments", "comment"),
        ("errors", "error"),
        ("fields", "field"),
        ("footnotes", "footnote"),
        ("headers_footers", "headers_footer"),
        ("indexes", "index"),
        ("pivot_tables", "pivot_table"),
        ("ranges", "range"),
        ("shapes", "shape"),
        ("sheets", "sheet"),
        ("slide_layouts", "slide_layout"),
        ("slide_masters", "slide_master"),
        ("slide_transitions", "slide_transition"),
        ("speaker_notes", "speaker_note"),
        ("page_comments", "page_comment"),
        ("tables", "table"),
        ("textframes", "textframe"),
    ],
)
def test_singularize_domain_map_pairs_in_domain_tools(plural, key):
    from plugin.scripting.host_rpc import singularize_domain
    from plugin.scripting.writeragent_api import DOMAIN_TOOLS

    assert singularize_domain(plural) == key
    assert key in DOMAIN_TOOLS


def test_proxy_methods_by_tool_uses_tool_methods():
    from plugin.scripting.host_rpc import _proxy_methods_by_tool
    import plugin.scripting.writeragent_api as api

    methods = _proxy_methods_by_tool()
    assert methods is not None
    assert "shape_upsert" in methods
    assert methods["shape_upsert"] == api.shape.upsert
    assert "list_open_documents" in methods
    assert methods["list_open_documents"] == api.core.list_open_documents


def test_proxy_methods_by_tool_missing_tool_methods_skips_bytecode(monkeypatch):
    from plugin.scripting.host_rpc import _proxy_methods_by_tool

    monkeypatch.delattr("plugin.scripting.writeragent_api.TOOL_METHODS")
    assert _proxy_methods_by_tool() == {}


def test_writeragent_api_in_process_rpc_call_preserves_error_code():
    import plugin.scripting.writeragent_api as api

    # 1. execute_tool raises exception with code attribute (e.g. USER_STOPPED)
    class StoppedError(Exception):
        code = "USER_STOPPED"

    with (
        patch.dict(os.environ, {"WRITERAGENT_IS_WORKER": "0", "WRITERAGENT_COMPUTE_WORKER": "0"}),
    ):
        from plugin.scripting.host_tool_call import set_process_role

        set_process_role(None)
        with patch(
            "plugin.scripting.host_rpc.execute_tool",
            side_effect=StoppedError("operation stopped"),
        ):
            try:
                api.call_host_tool("test_tool")
            except RuntimeError as exc:
                assert "operation stopped" in str(exc)
                assert getattr(exc, "code", None) == "USER_STOPPED"
            else:
                raise AssertionError("Expected RuntimeError")

        class CustomError(Exception):
            code = "PERMISSION_DENIED"

        with patch("plugin.scripting.host_rpc.execute_tool", side_effect=CustomError("denied")):
            try:
                api.call_host_tool("test_tool")
            except RuntimeError as exc:
                assert "denied" in str(exc)
                assert getattr(exc, "code", None) == "PERMISSION_DENIED"
            else:
                raise AssertionError("Expected RuntimeError")


def test_execute_tool_copies_stop_onto_tool_context():
    from plugin.framework.send_cancellation import SendCancellation

    focused = MagicMock()
    bound = MagicMock()
    registry = MagicMock()
    registry._services = {}
    tool = MagicMock()
    tool.is_async.return_value = True
    registry.get.return_value = tool
    registry.execute.return_value = {"status": "ok"}

    scope = SendCancellation()
    with (
        patch("plugin.framework.queue_executor.main.call", side_effect=lambda fn: fn()),
        patch("plugin.scripting.host_rpc.get_ctx", return_value=MagicMock()),
        patch("plugin.scripting.host_rpc.get_active_document", return_value=focused),
        patch("plugin.scripting.session_manager.document_for_script_session", return_value=bound),
        patch("plugin.app.get_tools", return_value=registry),
        patch("plugin.framework.doc_type.is_draw", return_value=True),
        patch("plugin.framework.doc_type.is_calc", return_value=False),
        patch("plugin.framework.doc_type.is_writer", return_value=False),
    ):
        execute_tool(
            "export_presentation_project",
            {"project_path": "/p"},
            send_cancellation=scope,
        )
    tctx = registry.execute.call_args.args[1]
    assert tctx.send_cancellation is scope


def test_handle_tool_call_frame_forwards_cancel_to_execute_tool():
    from plugin.framework.send_cancellation import SendCancellation

    scope = SendCancellation()
    written: list[bytes] = []
    with patch("plugin.scripting.host_rpc.execute_tool", return_value={"status": "ok"}) as mock_tool:
        handled = handle_tool_call_frame(
            {"type": "tool_call", "id": "fwd1", "tool": "apply_document_content", "args": {}},
            stdin_write=written.append,
            cancel=scope,
        )
    assert handled is True
    assert mock_tool.call_args.kwargs["send_cancellation"] is scope


def test_handle_tool_call_frame_oversized_result_is_error_frame(monkeypatch):
    import io
    from plugin.framework.ipc import IpcPayloadSizeError, pack_pickle_frame as real_pack

    first = True

    def mock_pack(payload, **kwargs):
        nonlocal first
        if first:
            first = False
            raise IpcPayloadSizeError("Pickle frame exceeds maximum payload size: 99")
        return real_pack(payload, **kwargs)

    monkeypatch.setattr("plugin.scripting.host_rpc.pack_pickle_frame", mock_pack)
    written: list[bytes] = []
    with patch("plugin.scripting.host_rpc.execute_tool", return_value={"blob": "x"}):
        handled = handle_tool_call_frame(
            {"type": "tool_call", "id": "over1", "tool": "apply_document_content", "args": {}},
            stdin_write=written.append,
        )
    assert handled is True
    assert len(written) == 1
    resp = read_pickle_frame(io.BytesIO(written[0]), require_dict=True)
    assert resp is not None
    assert resp["status"] == "error"
    assert resp["id"] == "over1"
    assert "99" in resp["message"]


def test_handle_tool_call_frame_second_pack_failure_propagates(monkeypatch):
    import pytest
    from plugin.framework.ipc import IpcPayloadSizeError

    def mock_pack(payload, **kwargs):
        raise IpcPayloadSizeError("Pickle frame exceeds maximum payload size: 99")

    monkeypatch.setattr("plugin.scripting.host_rpc.pack_pickle_frame", mock_pack)
    written: list[bytes] = []
    with patch("plugin.scripting.host_rpc.execute_tool", return_value={"blob": "x"}):
        with pytest.raises(IpcPayloadSizeError):
            handle_tool_call_frame(
                {"type": "tool_call", "id": "over2", "tool": "apply_document_content", "args": {}},
                stdin_write=written.append,
            )


def test_handle_tool_call_frame_write_timeout_propagates():
    import pytest

    def _timeout_write(_blob: bytes) -> None:
        raise RuntimeError("host RPC response timed out")

    with patch("plugin.scripting.host_rpc.execute_tool", return_value={"status": "ok"}):
        with pytest.raises(RuntimeError, match="timed out"):
            handle_tool_call_frame(
                {"type": "tool_call", "id": "to1", "tool": "apply_document_content", "args": {}},
                stdin_write=_timeout_write,
            )


def test_every_domain_tool_resolves_to_a_proxy_method():
    import plugin.scripting.writeragent_api as api
    from plugin.scripting.host_rpc import _proxy_methods_by_tool

    methods = _proxy_methods_by_tool()
    assert methods is not None
    assert methods["style_import"] == api.styles.import_
    assert methods["query_folder_sql"] == api.python_sql.query_folder_sql
    missing_or_not_callable = [
        tool_name
        for tool_names in api.DOMAIN_TOOLS.values()
        for tool_name in tool_names
        if tool_name not in methods or not callable(methods[tool_name])
    ]
    assert missing_or_not_callable == []
    assert api.TOOL_METHODS["style_import"] == ("styles", "import_")
    assert api.TOOL_METHODS["query_folder_sql"] == ("python_sql", "query_folder_sql")


def test_format_script_api_catalog_prints_identifier_safe_calls():
    import inspect
    import plugin.scripting.writeragent_api as api
    from plugin.scripting.host_rpc import (
        _format_script_api_catalog_cached,
        format_script_api_catalog,
    )

    _format_script_api_catalog_cached.cache_clear()
    catalog = format_script_api_catalog(["styles", "python/sql"])
    assert "python/sql:\n" in catalog
    assert "styles:\n" in catalog
    assert "wa.styles.import_(" in catalog
    assert "wa.python_sql.query_folder_sql(" in catalog
    assert inspect.cleandoc(api.styles.import_.__doc__ or "") in catalog
    assert inspect.cleandoc(api.python_sql.query_folder_sql.__doc__ or "") in catalog
    assert "wa.python/sql." not in catalog


def test_wa_help_known_name_matches_prompt_catalog():
    """wa.help('shape.upsert') returns the exact signature/docstring text from the prompt catalog."""
    import plugin.scripting.writeragent_api as api
    from plugin.scripting.host_rpc import (
        _format_script_api_catalog_cached,
        format_script_api_catalog,
    )

    _format_script_api_catalog_cached.cache_clear()
    catalog = format_script_api_catalog(["shapes"])
    assert "wa.help(" in catalog

    help_text = api.help("shape.upsert")
    assert help_text.startswith("wa.shape.upsert(")
    assert help_text in catalog
    assert api.help("wa.shape.upsert") == help_text


def test_wa_help_no_arg_lists_namespaces_and_methods():
    from plugin.scripting.host_rpc import format_script_api_help
    import plugin.scripting.writeragent_api as api

    listing = api.help()
    assert listing == format_script_api_help()
    assert "shape:" in listing
    assert "upsert" in listing
    assert "core:" in listing
    assert "list_open_documents" in listing
    assert "run_venv_python_script" not in listing


def test_wa_help_unknown_name_lists_close_matches():
    import plugin.scripting.writeragent_api as api

    typo = api.help("shape.upser")
    assert "shape.upsert" in typo

    far = api.help("qqqqq")
    assert far
    assert "shape.upsert" not in far


def test_wa_help_via_worker_ipc_frame():
    """wa.help() goes through the same Pickle5 tool_call host RPC path as other proxy calls."""
    from plugin.scripting.host_rpc import format_script_api_help

    written = io.BytesIO()
    keep_going = handle_tool_call_frame(
        {"type": "tool_call", "id": "h1", "tool": "writeragent_help", "args": {"name": "shape.upsert"}},
        stdin_write=written.write,
    )
    assert keep_going is True
    written.seek(0)
    reply = read_pickle_frame(written)
    assert reply["status"] == "ok"
    assert reply["id"] == "h1"
    assert reply["result"] == format_script_api_help("shape.upsert")


def test_wa_help_refused_during_py_recalc():
    with pytest.raises(RuntimeError, match="=PY\\(\\) recalculation"):
        execute_tool("writeragent_help", {}, allowed_tools=frozenset())



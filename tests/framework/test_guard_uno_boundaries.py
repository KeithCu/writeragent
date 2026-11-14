# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for get_ctx fallbacks and desktop element controller skips."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from plugin.framework.send_cancellation import SendCancellation


class TestGuardUnoBoundaries:
    def test_mcp_long_running_context_uses_get_ctx(self) -> None:
        guarded_ctx = MagicMock(name="guarded_ctx")
        doc_svc = MagicMock()
        doc_svc.resolve_document_by_url.return_value = (None, None)

        services = MagicMock()
        services.document = doc_svc
        services.get.return_value = MagicMock()
        services.tools = MagicMock()
        services.tools.get.return_value = MagicMock(requires_document=False)
        services.tools.execute.return_value = {"status": "ok"}

        from plugin.mcp.mcp_protocol import MCPProtocolHandler

        handler = MCPProtocolHandler(services)

        def _exec(fn, *a, **k):
            k.pop("timeout", None)
            k.pop("bound_scope", None)
            return fn(*a, **k)

        handler.queue_executor.call = _exec

        with (
            patch("plugin.mcp.mcp_protocol._real_active_document", return_value=None),
            patch("plugin.framework.uno_context.get_ctx", return_value=guarded_ctx),
            patch("plugin.mcp.mcp_protocol._resolve_mcp_doc_key", return_value="key"),
            patch("plugin.mcp.document_mutation_gate.document_mutation_gate"),
        ):
            handler._run_tool("noop", {}, document_url=None, cancel=SendCancellation())

        tool_context = services.tools.execute.call_args[0][1]
        assert tool_context.ctx is guarded_ctx

    def test_office_model_from_desktop_element_skips_broken_controller(self) -> None:
        """GHA 34593327841: leftover paste Writer getController must not abort listing."""
        elem = MagicMock(name="leftover_paste_writer")
        elem.getController.side_effect = RuntimeError(
            "Couldn't convert <traceback object> to a UNO type"
        )
        from plugin.doc.document_research import _office_model_from_desktop_element

        out = _office_model_from_desktop_element(elem)
        assert out is None



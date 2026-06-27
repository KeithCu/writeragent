# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for guard_uno boundaries (Tier 1/2 chokepoints)."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch


class TestGuardUnoBoundaries(unittest.TestCase):
    def test_open_document_for_read_new_load_calls_guard_uno(self) -> None:
        from plugin.doc.document_helpers import DocumentType

        opened_model = MagicMock()
        with (
            patch("plugin.doc.document_research.os.path.isfile", return_value=True),
            patch("plugin.doc.document_research.resolve_document_by_url", return_value=(None, None)),
            patch("plugin.framework.uno_context.get_desktop") as mock_desktop,
            patch("plugin.doc.document_research.get_document_type", return_value=DocumentType.CALC),
            patch("plugin.framework.thread_guard.guard_uno", side_effect=lambda o: o) as mock_guard,
        ):
            mock_desktop.return_value.loadComponentFromURL.return_value = opened_model
            from plugin.doc.document_research import open_document_for_read

            model, doc_type, err, opened = open_document_for_read(MagicMock(), "/tmp/Budget.ods")
        self.assertIsNone(err)
        self.assertEqual(doc_type, "calc")
        self.assertTrue(opened)
        mock_guard.assert_called_once_with(opened_model)
        self.assertIs(model, opened_model)

    def test_get_calc_document_from_ctx_wraps_active_doc(self) -> None:
        calc_doc = MagicMock()
        calc_doc.supportsService = MagicMock(return_value=False)
        with (
            patch("plugin.scripting.document_scripts.get_desktop") as mock_desktop,
            patch("plugin.scripting.document_scripts.is_calc", return_value=True),
            patch("plugin.framework.thread_guard.guard_uno", side_effect=lambda o: o) as mock_guard,
        ):
            mock_desktop.return_value.getCurrentComponent.return_value = calc_doc
            from plugin.scripting.document_scripts import get_calc_document_from_ctx

            out = get_calc_document_from_ctx(MagicMock())
        self.assertIs(out, calc_doc)
        mock_guard.assert_called_once_with(calc_doc)

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
        handler.queue_executor.execute = lambda fn, *a, **k: fn()

        with (
            patch("plugin.mcp.mcp_protocol._real_active_document", return_value=None),
            patch("plugin.framework.uno_context.get_ctx", return_value=guarded_ctx) as mock_get_ctx,
            patch("plugin.mcp.mcp_protocol._resolve_mcp_doc_key", return_value="key"),
            patch("plugin.mcp.mcp_protocol._document_mutation_gate"),
            patch("plugin.mcp.mcp_protocol._tool_needs_document_mutation_gate", return_value=False),
        ):
            handler._execute_long_running("noop", {}, document_url=None)

        mock_get_ctx.assert_called()


if __name__ == "__main__":
    unittest.main()
# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for guard_uno boundaries (Tier 1/2 chokepoints)."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

from plugin.framework.queue_executor import main
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

    def test_export_graphic_to_bytes_uses_get_ctx_when_ctx_none(self) -> None:
        guarded_ctx = MagicMock(name="guarded_ctx")
        sm = MagicMock()
        gp = MagicMock()
        guarded_ctx.ServiceManager = sm
        sm.createInstanceWithContext.return_value = gp
        with (
            patch("plugin.writer.images.image_tools.get_ctx", return_value=guarded_ctx) as mock_get_ctx,
            patch(
                "plugin.writer.images.image_tools.uno.getComponentContext",
                side_effect=AssertionError("uno.getComponentContext must not be used; call get_ctx()"),
            ),
            patch("plugin.writer.images.image_tools.visual_helpers.graphic_from_object", return_value=MagicMock()),
        ):
            from plugin.writer.images.image_tools import export_graphic_object_to_bytes

            data = export_graphic_object_to_bytes(None, MagicMock())
        mock_get_ctx.assert_called_once()
        gp.storeGraphic.assert_called_once()
        assert isinstance(data, bytes)

    def test_get_lo_locale_none_ctx_uses_get_ctx_on_main(self) -> None:
        guarded_ctx = MagicMock(name="guarded_ctx")
        smgr = MagicMock()
        guarded_ctx.getServiceManager.return_value = smgr
        config_provider = MagicMock()
        smgr.createInstanceWithContext.return_value = config_provider
        ca = MagicMock()
        ca.getPropertyValue.return_value = "de-DE"
        config_provider.createInstanceWithArguments.return_value = ca

        mock_uno = MagicMock()
        mock_uno.getComponentContext.side_effect = AssertionError(
            "uno.getComponentContext must not be used; call get_ctx()"
        )
        mock_uno.createUnoStruct.return_value = MagicMock()

        with (
            patch.object(main, "is_current", return_value=True),
            patch("plugin.framework.uno_context.get_ctx", return_value=guarded_ctx) as mock_get_ctx,
            patch.dict(sys.modules, {"uno": mock_uno}),
        ):
            from plugin.framework.i18n import get_lo_locale

            locale = get_lo_locale(None)
        mock_get_ctx.assert_called_once()
        assert (locale) == ("de_DE")

    def test_get_lo_locale_none_ctx_off_main_returns_default(self) -> None:
        with (
            patch.object(main, "is_current", return_value=False),
            patch("plugin.framework.uno_context.get_ctx") as mock_get_ctx,
        ):
            from plugin.framework.i18n import get_lo_locale

            locale = get_lo_locale(None)
        assert (locale) == ("en_US")
        mock_get_ctx.assert_not_called()

    def test_get_lo_locale_passed_ctx_off_main_returns_default(self) -> None:
        with patch.object(main, "is_current", return_value=False):
            from plugin.framework.i18n import get_lo_locale

            locale = get_lo_locale(MagicMock())
        assert locale == "en_US"

    def test_office_model_from_desktop_element_skips_broken_controller(self) -> None:
        """GHA 34593327841: leftover paste Writer getController must not abort listing."""
        elem = MagicMock(name="leftover_paste_writer")
        elem.getController.side_effect = RuntimeError(
            "Couldn't convert <traceback object> to a UNO type"
        )
        from plugin.doc.document_research import _office_model_from_desktop_element

        out = _office_model_from_desktop_element(elem)
        assert out is None



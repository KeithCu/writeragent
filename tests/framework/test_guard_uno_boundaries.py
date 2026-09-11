# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for guard_uno boundaries (Tier 1/2 chokepoints)."""

from __future__ import annotations

import sys
import unittest
from unittest.mock import MagicMock, patch


class TestGuardUnoBoundaries(unittest.TestCase):
    def test_open_document_for_read_new_load_calls_guard_uno(self) -> None:
        from plugin.doc.doc_type import DocumentType

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
        handler.queue_executor.execute = lambda fn, *a, **k: fn(*a)

        with (
            patch("plugin.mcp.mcp_protocol._real_active_document", return_value=None),
            patch("plugin.framework.uno_context.get_ctx", return_value=guarded_ctx) as mock_get_ctx,
            patch("plugin.mcp.mcp_protocol._resolve_mcp_doc_key", return_value="key"),
            patch("plugin.mcp.mcp_protocol._document_mutation_gate"),
            patch("plugin.mcp.mcp_protocol._tool_needs_document_mutation_gate", return_value=False),
        ):
            handler._execute_long_running("noop", {}, document_url=None)

        mock_get_ctx.assert_called()

    def test_get_calc_doc_wraps_current_component(self) -> None:
        calc_doc = MagicMock(name="calc_doc")
        calc_doc.getSheets = MagicMock()
        desktop = MagicMock()
        desktop.getCurrentComponent.return_value = calc_doc
        with (
            patch("plugin.framework.thread_guard.on_main_thread", return_value=True),
            patch("plugin.framework.uno_context.get_desktop", return_value=desktop),
            patch("plugin.framework.thread_guard.guard_uno", side_effect=lambda o: o) as mock_guard,
        ):
            from plugin.calc.python.function import _get_calc_doc

            out = _get_calc_doc(MagicMock())
        self.assertIs(out, calc_doc)
        mock_guard.assert_called_once_with(calc_doc)

    def test_get_calc_doc_returns_none_off_main(self) -> None:
        with (
            patch("plugin.framework.thread_guard.on_main_thread", return_value=False),
            patch("plugin.framework.uno_context.get_desktop") as mock_desktop,
            patch("plugin.framework.thread_guard.guard_uno") as mock_guard,
        ):
            from plugin.calc.python.function import _get_calc_doc

            out = _get_calc_doc(MagicMock())
        self.assertIsNone(out)
        mock_desktop.assert_not_called()
        mock_guard.assert_not_called()

    def test_get_calc_doc_wraps_enumerated_model(self) -> None:
        class _NotCalc:
            pass

        calc_model = MagicMock(name="enum_calc")
        calc_model.getSheets = MagicMock()
        ctrl = MagicMock()
        ctrl.getModel.return_value = calc_model
        elem = MagicMock(spec=["getController"])
        elem.getController.return_value = ctrl

        enum = MagicMock()
        enum.hasMoreElements.side_effect = [True, False]
        enum.nextElement.return_value = elem
        comps = MagicMock()
        comps.createEnumeration.return_value = enum
        desktop = MagicMock()
        desktop.getCurrentComponent.return_value = _NotCalc()
        desktop.getComponents.return_value = comps

        with (
            patch("plugin.framework.thread_guard.on_main_thread", return_value=True),
            patch("plugin.framework.uno_context.get_desktop", return_value=desktop),
            patch("plugin.framework.thread_guard.guard_uno", side_effect=lambda o: o) as mock_guard,
        ):
            from plugin.calc.python.function import _get_calc_doc

            out = _get_calc_doc(MagicMock())
        self.assertIs(out, calc_model)
        mock_guard.assert_called_once_with(calc_model)

    def test_export_graphic_to_bytes_uses_get_ctx_when_ctx_none(self) -> None:
        guarded_ctx = MagicMock(name="guarded_ctx")
        sm = MagicMock()
        gp = MagicMock()
        guarded_ctx.ServiceManager = sm
        sm.createInstanceWithContext.return_value = gp
        with (
            patch("plugin.framework.uno_context.get_ctx", return_value=guarded_ctx) as mock_get_ctx,
            patch(
                "plugin.writer.images.image_tools.uno.getComponentContext",
                side_effect=AssertionError("uno.getComponentContext must not be used; call get_ctx()"),
            ),
        ):
            from plugin.writer.images.image_tools import export_graphic_to_bytes

            data = export_graphic_to_bytes(None, MagicMock())
        mock_get_ctx.assert_called_once()
        gp.storeGraphic.assert_called_once()
        self.assertIsInstance(data, bytes)

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
            patch("plugin.framework.thread_guard.on_main_thread", return_value=True),
            patch("plugin.framework.uno_context.get_ctx", return_value=guarded_ctx) as mock_get_ctx,
            patch.dict(sys.modules, {"uno": mock_uno}),
        ):
            from plugin.framework.i18n import get_lo_locale

            locale = get_lo_locale(None)
        mock_get_ctx.assert_called_once()
        self.assertEqual(locale, "de_DE")

    def test_get_lo_locale_none_ctx_off_main_returns_default(self) -> None:
        with (
            patch("plugin.framework.thread_guard.on_main_thread", return_value=False),
            patch("plugin.framework.uno_context.get_ctx") as mock_get_ctx,
        ):
            from plugin.framework.i18n import get_lo_locale

            locale = get_lo_locale(None)
        self.assertEqual(locale, "en_US")
        mock_get_ctx.assert_not_called()

    def test_office_model_from_desktop_element_wraps_controller_model(self) -> None:
        model = MagicMock(name="model")
        ctrl = MagicMock()
        ctrl.getModel.return_value = model
        elem = MagicMock()
        elem.getController.return_value = ctrl
        with patch("plugin.framework.thread_guard.guard_uno", side_effect=lambda o: o) as mock_guard:
            from plugin.doc.document_research import _office_model_from_desktop_element

            out = _office_model_from_desktop_element(elem)
        self.assertIs(out, model)
        mock_guard.assert_called_once_with(model)

    def test_office_model_from_desktop_element_wraps_element_without_controller(self) -> None:
        elem = MagicMock(name="elem_is_model")
        elem.getController.return_value = None
        with patch("plugin.framework.thread_guard.guard_uno", side_effect=lambda o: o) as mock_guard:
            from plugin.doc.document_research import _office_model_from_desktop_element

            out = _office_model_from_desktop_element(elem)
        self.assertIs(out, elem)
        mock_guard.assert_called_once_with(elem)

    def test_office_model_from_desktop_element_skips_broken_controller(self) -> None:
        """GHA 34593327841: leftover paste Writer getController must not abort listing."""
        elem = MagicMock(name="leftover_paste_writer")
        elem.getController.side_effect = RuntimeError(
            "Couldn't convert <traceback object> to a UNO type"
        )
        with patch("plugin.framework.thread_guard.guard_uno") as mock_guard:
            from plugin.doc.document_research import _office_model_from_desktop_element

            out = _office_model_from_desktop_element(elem)
        self.assertIsNone(out)
        mock_guard.assert_not_called()

    def test_get_active_calc_cell_wraps_model(self) -> None:
        model = MagicMock()
        model.getSheets = MagicMock()
        addr = MagicMock()
        addr.StartColumn = 0
        addr.StartRow = 0
        selection = MagicMock()
        selection.getRangeAddress.return_value = addr
        cc = MagicMock()
        cc.getSelection.return_value = selection
        model.getCurrentController.return_value = cc
        ctrl = MagicMock()
        ctrl.getModel.return_value = model
        frame = MagicMock()
        frame.getController.return_value = ctrl
        desktop = MagicMock()
        desktop.getCurrentFrame.return_value = frame
        cell = MagicMock()

        with (
            patch("plugin.calc.python.editor.get_desktop", return_value=desktop),
            patch("plugin.calc.python.editor.CalcBridge") as mock_bridge,
            patch("plugin.calc.python.editor._cell_formula_strings", return_value=["=PY(\"1\")"]),
            patch("plugin.framework.thread_guard.guard_uno", side_effect=lambda o: o) as mock_guard,
        ):
            mock_bridge.return_value.get_active_sheet.return_value = MagicMock()
            mock_bridge.return_value.get_cell.return_value = cell
            from plugin.calc.python.editor import _get_active_calc_cell

            out = _get_active_calc_cell(MagicMock())
        self.assertIsNotNone(out)
        assert out is not None
        self.assertIs(out[0], model)
        mock_guard.assert_called_once_with(model)
        mock_bridge.assert_called_once()
        self.assertIs(mock_bridge.call_args[0][0], model)


if __name__ == "__main__":
    unittest.main()
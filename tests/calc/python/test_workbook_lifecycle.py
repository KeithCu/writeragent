# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from unittest.mock import MagicMock, patch

from plugin.calc.python.workbook_lifecycle import (
    _CalcPythonUnloadListener,
    _lifecycle_key,
    ensure_calc_workbook_unload_resets_python,
)
from plugin.tests.testing_utils import setup_uno_mocks

setup_uno_mocks()


def test_lifecycle_key_prefers_runtime_uid():
    doc = MagicMock()
    doc.getPropertyValue.return_value = "uid-abc"
    assert _lifecycle_key(doc) == "uid-abc"


def test_unload_listener_resets_worker_session():
    ctx = MagicMock()
    listener = _CalcPythonUnloadListener(ctx, "calc:wb-1", "key-1")
    with patch("plugin.calc.python.workbook_lifecycle.reset_python_session") as mock_reset:
        mock_reset.return_value = {"status": "ok"}
        listener.on_document_event(MagicMock(EventName="OnUnload"))
        mock_reset.assert_called_once_with(ctx, "calc:wb-1")
        listener.on_document_event(MagicMock(EventName="OnUnload"))
        mock_reset.assert_called_once()


def test_ensure_registers_listener_once():
    ctx = MagicMock()
    doc = MagicMock()
    doc.getPropertyValue.return_value = "uid-reg"
    with patch("plugin.calc.python.workbook_lifecycle._HAVE_UNO_DOC_EVENTS", True):
        ensure_calc_workbook_unload_resets_python(ctx, doc)
        ensure_calc_workbook_unload_resets_python(ctx, doc)
    assert doc.addDocumentEventListener.call_count == 1

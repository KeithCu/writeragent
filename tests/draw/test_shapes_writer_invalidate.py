# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for Writer draw-layer invalidate helper (no nested VCL pump)."""

from __future__ import annotations

from unittest.mock import MagicMock

from plugin.draw.shapes import _try_writer_invalidate_and_pump


def test_writer_invalidate_does_not_call_process_events_to_idle() -> None:
    doc = MagicMock()
    doc.supportsService.return_value = True
    ctrl = MagicMock()
    doc.getCurrentController.return_value = ctrl
    frame = MagicMock()
    ctrl.getFrame.return_value = frame
    win = MagicMock()
    frame.getContainerWindow.return_value = win
    tk = MagicMock()
    win.getToolkit.return_value = tk

    _try_writer_invalidate_and_pump(doc)

    win.invalidate.assert_called_once_with(0)
    tk.processEventsToIdle.assert_not_called()


def test_writer_invalidate_skips_non_writer_doc() -> None:
    doc = MagicMock()
    doc.supportsService.return_value = False

    _try_writer_invalidate_and_pump(doc)

    doc.getCurrentController.assert_not_called()

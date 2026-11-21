# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Live UNO test for ModuleConfigDialog._PreviewPane layout, resize, and scroll."""

from __future__ import annotations

import base64
import os
import tempfile
from unittest.mock import patch

import uno

from plugin._manifest import MODULES
from plugin.chatbot import module_config_dialog
from plugin.chatbot.dialogs import get_optional
from plugin.chatbot.module_config_dialog import ModuleConfigDialog, PreviewImage, _active_dialogs
from plugin.testing_runner import native_test
from plugin.writer.images.image_tools import graphic_from_bytes
from scripts.manifest_xdl import generate_standalone_config_dialog

# 1x1 transparent PNG
_PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)


@native_test
def test_module_config_dialog_preview_resize_and_scroll_uno(ctx):
    vision_mod = next(m for m in MODULES if m["name"] == "vision")
    xdl = generate_standalone_config_dialog(vision_mod)
    fd, xdl_path = tempfile.mkstemp(prefix="wa_vision_dlg_", suffix=".xdl")
    os.close(fd)
    dialog = None
    try:
        with open(xdl_path, "w", encoding="utf-8") as f:
            f.write(xdl)
        provider = ctx.ServiceManager.createInstanceWithContext("com.sun.star.awt.DialogProvider", ctx)
        real_dlg = provider.createDialog(uno.systemPathToFileUrl(xdl_path))

        _active_dialogs.pop("vision", None)
        dialog = ModuleConfigDialog(ctx, "vision", preview_job=lambda *_a: None)
        with patch.object(module_config_dialog, "load_writeragent_dialog_detail", return_value=(real_dlg, "")):
            dialog._open()

        pane = dialog._pane
        assert pane is not None
        c_before = pane._container.getPosSize()
        sb_before = get_optional(real_dlg, "sb_preview").getPosSize()
        close_before = get_optional(real_dlg, "btn_close").getPosSize()

        pane.show(PreviewImage(graphic_from_bytes(ctx, _PNG_1X1), 800, 1100, "ok"))

        bar = pane._bar
        old_range = bar.getMaximum() - bar.getVisibleSize()
        assert old_range > 0
        bar.setValue(old_range)
        frac = bar.getValue() / old_range

        dlg_sz = real_dlg.getPosSize()
        real_dlg.setPosSize(0, 0, dlg_sz.Width + 200, dlg_sz.Height + 150, 12)  # PosSize.SIZE = 12
        pane.relayout()

        c_after = pane._container.getPosSize()
        sb_after = get_optional(real_dlg, "sb_preview").getPosSize()
        close_after = get_optional(real_dlg, "btn_close").getPosSize()
        img_after = pane._image.getPosSize()

        assert c_after.Width == c_before.Width + 200
        assert c_after.Height == c_before.Height + 150
        assert sb_after.X == sb_before.X + 200
        assert close_after.X == close_before.X + 200
        assert close_after.Y == close_before.Y + 150
        assert img_after.Width == c_after.Width
        expected_h = round(c_after.Width * 1100 / 800)
        assert img_after.Height == expected_h
        assert bar.getMaximum() == expected_h
        assert img_after.Y == -bar.getValue()
        assert abs(bar.getValue() - frac * (expected_h - c_after.Height)) <= 1

        pane.scroll_to(100)
        assert pane._image.getPosSize().Y == -100
    finally:
        if dialog is not None:
            dialog.close()
        _active_dialogs.pop("vision", None)
        if os.path.exists(xdl_path):
            os.remove(xdl_path)

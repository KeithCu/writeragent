# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Live UNO test for ModuleConfigDialog._PreviewPane layout, resize, and scroll."""

from __future__ import annotations

import base64
from contextlib import contextmanager
import os
import sys
import tempfile
from unittest.mock import patch

import uno

from plugin._manifest import MODULES
from plugin.chatbot import module_config_dialog
from plugin.chatbot.dialogs import get_optional
from plugin.chatbot.module_config_dialog import ModuleConfigDialog, PreviewImage, _active_dialogs
from plugin.framework.send_cancellation import SendCancellation
from plugin.testing_runner import native_test
from plugin.writer.images.image_tools import graphic_from_bytes


def _generate_standalone_config_dialog(module: dict) -> str:
    try:
        from scripts.manifest_xdl import generate_standalone_config_dialog
    except ImportError:
        # release_verify runs this in a stripped tree with no scripts/; it sets
        # WRITERAGENT_PROJECT_ROOT to the real checkout.
        root = os.environ.get("WRITERAGENT_PROJECT_ROOT", "").strip()
        if not root:
            raise
        sys.path.append(root)
        from scripts.manifest_xdl import generate_standalone_config_dialog
    return generate_standalone_config_dialog(module)


# 1x1 transparent PNG
_PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)
# Solid red 1x1 PNG (BGR b"\x00\x00\xff" when drawn into a DIB)
_PNG_RED_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC"
)


@contextmanager
def _open_vision_dialog(ctx):
    vision_mod = next(m for m in MODULES if m["name"] == "vision")
    xdl = _generate_standalone_config_dialog(vision_mod)
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
        yield dialog, real_dlg
    finally:
        if dialog is not None:
            dialog.close()
        _active_dialogs.pop("vision", None)
        if os.path.exists(xdl_path):
            os.remove(xdl_path)


def _sample_pane_bgr(pane) -> bytes:
    peer = pane._image.getPeer()
    dev = peer.createDevice(20, 20)
    g = dev.createGraphics()
    peer.setGraphics(g)
    peer.draw(0, 0)
    dib = bytes(dev.createBitmap(0, 0, 20, 20).getDIB().value)
    return dib[-60:-57]


@native_test
def test_module_config_dialog_preview_resize_and_scroll_uno(ctx):
    with _open_vision_dialog(ctx) as (dialog, real_dlg):
        pane = dialog._pane
        assert pane is not None
        c_before = pane._container.getPosSize()
        sb_before = get_optional(real_dlg, "sb_preview").getPosSize()
        run_before = get_optional(real_dlg, "btn_run").getPosSize()
        preview_before = get_optional(real_dlg, "btn_preview").getPosSize()
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
        run_after = get_optional(real_dlg, "btn_run").getPosSize()
        preview_after = get_optional(real_dlg, "btn_preview").getPosSize()
        close_after = get_optional(real_dlg, "btn_close").getPosSize()
        img_after = pane._image.getPosSize()

        assert c_after.Width == c_before.Width + 200
        assert c_after.Height == c_before.Height + 150
        assert sb_after.X == sb_before.X + 200
        assert run_after.X == run_before.X + 200
        assert run_after.Y == run_before.Y + 150
        assert preview_after.X == preview_before.X + 200
        assert preview_after.Y == preview_before.Y + 150
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


@native_test
def test_module_config_dialog_preview_clear_uno(ctx):
    with _open_vision_dialog(ctx) as (dialog, _real_dlg):
        pane = dialog._pane
        assert pane is not None
        bar = pane._bar
        assert bar is not None

        red = PreviewImage(graphic_from_bytes(ctx, _PNG_RED_1X1), 800, 1100, "ok")
        pane.show(red)
        assert _sample_pane_bgr(pane) == b"\x00\x00\xff"
        assert bar.getMaximum() > 0

        token = SendCancellation()
        dialog._run_cancel = token
        dialog._finish_run(token, "preview", "No text detected.")
        assert _sample_pane_bgr(pane) != b"\x00\x00\xff"
        assert bar.getMaximum() == 0

        pane.relayout()
        assert _sample_pane_bgr(pane) != b"\x00\x00\xff"
        assert bar.getMaximum() == 0

        pane.show(red)
        assert _sample_pane_bgr(pane) == b"\x00\x00\xff"
        assert bar.getMaximum() > 0

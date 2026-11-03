# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""``translate_dialog`` on a real UnoControlDialog.

pyuno's ``queryInterface`` rejects an imported IDL class ("Couldn't convert
... to a UNO type"). ``_xcc`` passed ``XControlContainer`` directly, so every
``translate_dialog`` call raised and the Chat and LibrePy sidebar panels
failed to build. Mocked tests could not see this.
"""

from __future__ import annotations

from plugin.testing_runner import native_test


def _build(ctx):
    smgr = ctx.getServiceManager()
    model = smgr.createInstanceWithContext("com.sun.star.awt.UnoControlDialogModel", ctx)
    model.Width = 200
    model.Height = 80
    model.Title = "xcc-native"
    label = model.createInstance("com.sun.star.awt.UnoControlFixedTextModel")
    label.Name = "label1"
    label.Width = 100
    label.Height = 10
    label.Label = "Cancel"
    model.insertByName("label1", label)
    dlg = smgr.createInstanceWithContext("com.sun.star.awt.UnoControlDialog", ctx)
    dlg.setModel(model)
    toolkit = smgr.createInstanceWithContext("com.sun.star.awt.Toolkit", ctx)
    dlg.createPeer(toolkit, None)
    return dlg


@native_test
def test_xcc_returns_container_for_real_dialog(ctx):
    from plugin.chatbot.dialogs import _xcc

    dlg = _build(ctx)
    try:
        xcc = _xcc(dlg)
        assert xcc is not None
        controls = xcc.getControls()
        assert len(controls) == 1
        # A FixedText is not a container: None, not an exception.
        assert _xcc(controls[0]) is None
    finally:
        dlg.dispose()


@native_test
def test_translate_dialog_does_not_raise_on_real_dialog(ctx):
    from plugin.chatbot.dialogs import translate_dialog

    dlg = _build(ctx)
    try:
        translate_dialog(dlg)
        assert dlg.getControl("label1").getModel().Label
    finally:
        dlg.dispose()

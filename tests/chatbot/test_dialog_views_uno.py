# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Speech STT controls must stay on their dialog step.

Settings opens on General (step 1). Audio Model lives on the Speech step at
the same Y as API Key. Calling setVisible(True) on it while the dialog is
already showing paints it on General. Enable/disable must not.
"""

from __future__ import annotations

from plugin.testing_runner import native_test


def _add_fixed(model, name, step, y, label):
    ctrl = model.createInstance("com.sun.star.awt.UnoControlFixedTextModel")
    ctrl.Name = name
    ctrl.PositionX = 8
    ctrl.PositionY = y
    ctrl.Width = 150
    ctrl.Height = 10
    ctrl.Label = label
    ctrl.Step = step
    model.insertByName(name, ctrl)


def _add_combo(model, name, step, y):
    ctrl = model.createInstance("com.sun.star.awt.UnoControlComboBoxModel")
    ctrl.Name = name
    ctrl.PositionX = 110
    ctrl.PositionY = y
    ctrl.Width = 144
    ctrl.Height = 14
    ctrl.Dropdown = True
    ctrl.Step = step
    model.insertByName(name, ctrl)


def _build(ctx):
    smgr = ctx.getServiceManager()
    model = smgr.createInstanceWithContext("com.sun.star.awt.UnoControlDialogModel", ctx)
    model.PositionX = 20
    model.PositionY = 20
    model.Width = 400
    model.Height = 200
    model.Title = "stt-step-guard"
    model.Step = 1
    _add_fixed(model, "label_api_key", 1, 42, "API Key:")
    _add_fixed(model, "label_audio__stt_model", 3, 44, "Audio Model:")
    _add_combo(model, "audio__stt_model", 3, 42)
    _add_fixed(model, "label_audio__stt_local_model", 3, 60, "Local Model")
    _add_combo(model, "audio__stt_local_model", 3, 58)
    prov = model.createInstance("com.sun.star.awt.UnoControlComboBoxModel")
    prov.Name = "audio__stt_provider"
    prov.PositionX = 110
    prov.PositionY = 26
    prov.Width = 144
    prov.Height = 14
    prov.Dropdown = True
    prov.Step = 3
    prov.Text = "LLM Endpoint"
    model.insertByName("audio__stt_provider", prov)
    dlg = smgr.createInstanceWithContext("com.sun.star.awt.UnoControlDialog", ctx)
    dlg.setModel(model)
    toolkit = smgr.createInstanceWithContext("com.sun.star.awt.Toolkit", ctx)
    dlg.createPeer(toolkit, None)
    dlg.setVisible(True)
    return dlg, model


@native_test
def test_stt_enable_does_not_paint_audio_model_on_general(ctx):
    """First Settings paint is General. Audio Model must not become visible there."""
    from plugin.chatbot.dialog_views import SttSettingsListener, _apply_stt_model_visibility

    dlg, model = _build(ctx)
    try:
        # Dialog is already showing step 1, which is when the bug paints.
        _apply_stt_model_visibility(dlg, "LLM Endpoint")
        audio = dlg.getControl("audio__stt_model")
        audio_label = dlg.getControl("label_audio__stt_model")
        assert int(audio.getModel().Step) == 3
        assert audio.isVisible() is False
        assert audio_label.isVisible() is False
        assert dlg.getControl("label_api_key").isVisible() is True

        model.Step = 3
        assert audio.isVisible() is True
        assert audio_label.isVisible() is True
        local = dlg.getControl("audio__stt_local_model")
        assert local.isVisible() is True
        assert local.isEnabled() is False
        assert audio.isEnabled() is True

        listener = SttSettingsListener(dlg)
        dlg.getControl("audio__stt_provider").setText("Local Whisper (faster-whisper)")
        listener.sync_ui()
        assert audio.isVisible() is True
        assert audio.isEnabled() is False
        assert local.isEnabled() is True
        assert int(audio.getModel().Step) == 3

        model.Step = 1
        assert audio.isVisible() is False
        assert audio_label.isVisible() is False
        assert local.isVisible() is False
        assert dlg.getControl("label_api_key").isVisible() is True
    finally:
        dlg.setVisible(False)
        dlg.dispose()

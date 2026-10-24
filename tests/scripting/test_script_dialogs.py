# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for the new script / save script as dialog."""

from __future__ import annotations

from pathlib import Path
import re
from typing import Any
from unittest.mock import MagicMock, patch

from plugin.framework.i18n import _
from plugin.scripting.script_dialogs import show_new_script_dialog


def _build_dialog_mock(
    *,
    name_text: str = "",
    attach_state: int = 1,
    peer_sentinel: Any = None,
):
    ctx = MagicMock()
    smgr = MagicMock()
    ctx.getServiceManager.return_value = smgr

    dlg = MagicMock()
    dlg_model = MagicMock()
    dlg.getModel.return_value = dlg_model

    if peer_sentinel is not None:
        dlg.getPeer.return_value = peer_sentinel
    else:
        dlg.getPeer.return_value = MagicMock()

    def fake_create(service, _ctx):
        if "UnoControlDialog" in service:
            return dlg
        if "DialogProvider" in service:
            dp = MagicMock()
            dp.createDialog.return_value = dlg
            return dp
        return MagicMock()

    smgr.createInstanceWithContext.side_effect = fake_create

    name_edit = MagicMock()
    name_edit_model = MagicMock()
    name_edit_model.Text = name_text
    name_edit.getModel.return_value = name_edit_model
    name_edit.getText.return_value = name_text

    chk = MagicMock()
    chk_model = MagicMock()
    chk_model.Label = "Attach to this document"
    chk_model.HelpText = ""
    chk_model.State = attach_state
    chk.getModel.return_value = chk_model
    chk.getState.return_value = attach_state

    ok_listeners = []
    cancel_listeners = []

    def fake_get_control(name):
        if name == "NameEdit":
            return name_edit
        if name == "ChkAttach":
            return chk
        if name == "BtnOK":
            btn = MagicMock()
            btn.addActionListener.side_effect = lambda listener: ok_listeners.append(listener)
            return btn
        if name == "BtnCancel":
            btn = MagicMock()
            btn.addActionListener.side_effect = lambda listener: cancel_listeners.append(listener)
            return btn
        return MagicMock()

    dlg.getControl.side_effect = fake_get_control

    return ctx, dlg, name_edit, chk, ok_listeners, cancel_listeners


def test_show_new_script_dialog_ok_writable_doc():
    ctx, dlg, _edit, chk, ok_listeners, _cancel_listeners = _build_dialog_mock(
        name_text="  MyNewScript  ",
        attach_state=1,
    )
    dlg.execute.side_effect = lambda: [listener.actionPerformed(MagicMock()) for listener in ok_listeners]
    doc = MagicMock()
    doc.isReadonly.return_value = False

    with patch("plugin.scripting.script_dialogs.msgbox") as mock_msgbox:
        result = show_new_script_dialog(ctx, doc=doc, default_name="NewScript")

    assert result == ("MyNewScript", True)
    dlg.dispose.assert_called_once()
    mock_msgbox.assert_not_called()
    assert chk.getModel().Label == "Attach to this document"
    assert chk.getModel().HelpText == ""
    chk.setEnable.assert_called_with(True)


def test_show_new_script_dialog_cancel():
    ctx, dlg, _edit, _chk, _ok_listeners, cancel_listeners = _build_dialog_mock(
        name_text="MyScript",
        attach_state=0,
    )
    dlg.execute.side_effect = lambda: [listener.actionPerformed(MagicMock()) for listener in cancel_listeners]

    result = show_new_script_dialog(ctx, doc=None, default_name="")
    assert result is None
    dlg.dispose.assert_called_once()


def test_show_new_script_dialog_custom_title_and_default_attach_false():
    ctx, dlg, _edit, chk, ok_listeners, _cancel_listeners = _build_dialog_mock(
        name_text="SavedCopy",
        attach_state=0,
    )
    dlg.execute.side_effect = lambda: [listener.actionPerformed(MagicMock()) for listener in ok_listeners]
    doc = MagicMock()
    doc.isReadonly.return_value = False

    result = show_new_script_dialog(
        ctx,
        doc=doc,
        default_name="OldScript",
        title="Save Script As",
        default_attach=False,
    )
    assert result == ("SavedCopy", False)
    assert dlg.getModel().Title == "Save Script As"
    assert chk.getModel().Label == "Attach to this document"


def test_show_new_script_dialog_writable_init_rejected():
    peer = object()
    for init_name in ("INIT", "Init"):
        ctx, dlg, _edit, _chk, ok_listeners, _cancel_listeners = _build_dialog_mock(
            name_text=init_name,
            attach_state=1,
            peer_sentinel=peer,
        )
        dlg.execute.side_effect = lambda: [listener.actionPerformed(MagicMock()) for listener in ok_listeners]
        doc = MagicMock()
        doc.isReadonly.return_value = False

        with patch("plugin.scripting.script_dialogs.msgbox") as mock_msgbox:
            result = show_new_script_dialog(ctx, doc=doc)

        assert result is None
        dlg.endDialog.assert_not_called()
        expected_msg = _("'{0}' is reserved for the workbook init script.").format(init_name)
        mock_msgbox.assert_called_once_with(ctx, _("Error"), expected_msg, parent_window=peer)


def test_show_new_script_dialog_writable_lowercase_init_allowed():
    ctx, dlg, _edit, _chk, ok_listeners, _cancel_listeners = _build_dialog_mock(
        name_text="init",
        attach_state=1,
    )
    dlg.execute.side_effect = lambda: [listener.actionPerformed(MagicMock()) for listener in ok_listeners]
    doc = MagicMock()
    doc.isReadonly.return_value = False

    with patch("plugin.scripting.script_dialogs.msgbox") as mock_msgbox:
        result = show_new_script_dialog(ctx, doc=doc)

    assert result == ("init", True)
    mock_msgbox.assert_not_called()


def test_show_new_script_dialog_writable_init_unattached_allowed():
    ctx, dlg, _edit, _chk, ok_listeners, _cancel_listeners = _build_dialog_mock(
        name_text="INIT",
        attach_state=0,
    )
    dlg.execute.side_effect = lambda: [listener.actionPerformed(MagicMock()) for listener in ok_listeners]
    doc = MagicMock()
    doc.isReadonly.return_value = False

    with patch("plugin.scripting.script_dialogs.msgbox") as mock_msgbox:
        result = show_new_script_dialog(ctx, doc=doc)

    assert result == ("INIT", False)
    mock_msgbox.assert_not_called()


def test_show_new_script_dialog_writable_reserved_prefix_rejected():
    peer = object()
    ctx, dlg, _edit, _chk, ok_listeners, _cancel_listeners = _build_dialog_mock(
        name_text="[Vision] x",
        attach_state=0,
        peer_sentinel=peer,
    )
    dlg.execute.side_effect = lambda: [listener.actionPerformed(MagicMock()) for listener in ok_listeners]
    doc = MagicMock()
    doc.isReadonly.return_value = False

    with patch("plugin.scripting.script_dialogs.msgbox") as mock_msgbox:
        result = show_new_script_dialog(ctx, doc=doc)

    assert result is None
    expected_msg = _("'{0}' starts with a reserved prefix.").format("[Vision] x")
    mock_msgbox.assert_called_once_with(ctx, _("Error"), expected_msg, parent_window=peer)


def test_show_new_script_dialog_empty_text_rejected():
    peer = object()
    ctx, dlg, _edit, _chk, ok_listeners, _cancel_listeners = _build_dialog_mock(
        name_text="   ",
        attach_state=1,
        peer_sentinel=peer,
    )
    dlg.execute.side_effect = lambda: [listener.actionPerformed(MagicMock()) for listener in ok_listeners]
    doc = MagicMock()
    doc.isReadonly.return_value = False

    with patch("plugin.scripting.script_dialogs.msgbox") as mock_msgbox:
        result = show_new_script_dialog(ctx, doc=doc)

    assert result is None
    dlg.endDialog.assert_not_called()
    mock_msgbox.assert_called_once_with(ctx, _("Error"), _("Script name cannot be empty."), parent_window=peer)


def test_show_new_script_dialog_no_document_disables_attach():
    ctx, dlg, _edit, chk, ok_listeners, _cancel_listeners = _build_dialog_mock(
        name_text="Mine",
        attach_state=1,
    )
    dlg.execute.side_effect = lambda: [listener.actionPerformed(MagicMock()) for listener in ok_listeners]

    result = show_new_script_dialog(ctx, doc=None)
    assert result == ("Mine", False)
    chk.setEnable.assert_called_with(False)
    expected = _("Attach to this document (no document is open)")
    assert chk.getModel().Label == expected
    assert chk.getModel().HelpText == expected


def test_show_new_script_dialog_readonly_document_disables_attach():
    ctx, dlg, _edit, chk, ok_listeners, _cancel_listeners = _build_dialog_mock(
        name_text="Mine",
        attach_state=1,
    )
    dlg.execute.side_effect = lambda: [listener.actionPerformed(MagicMock()) for listener in ok_listeners]
    doc = MagicMock()
    doc.isReadonly.return_value = True

    result = show_new_script_dialog(ctx, doc=doc)
    assert result == ("Mine", False)
    chk.setEnable.assert_called_with(False)
    expected = _("Attach to this document (this document is read-only)")
    assert chk.getModel().Label == expected
    assert chk.getModel().HelpText == expected


def test_show_new_script_dialog_set_focus_failure_still_executes():
    ctx, dlg, edit, _chk, ok_listeners, _cancel_listeners = _build_dialog_mock(
        name_text="Mine",
        attach_state=1,
    )
    edit.setFocus.side_effect = RuntimeError("focus failed")
    dlg.execute.side_effect = lambda: [listener.actionPerformed(MagicMock()) for listener in ok_listeners]
    doc = MagicMock()
    doc.isReadonly.return_value = False

    result = show_new_script_dialog(ctx, doc=doc)
    assert result == ("Mine", True)
    dlg.execute.assert_called_once()
    dlg.dispose.assert_called_once()


def test_show_new_script_dialog_get_control_failure_disposes():
    ctx, dlg, _edit, _chk, _ok_listeners, _cancel_listeners = _build_dialog_mock()

    def raise_on_name_edit(name):
        if name == "NameEdit":
            raise RuntimeError("broken control")
        return MagicMock()

    dlg.getControl.side_effect = raise_on_name_edit

    result = show_new_script_dialog(ctx, doc=None)
    assert result is None
    dlg.execute.assert_not_called()
    dlg.dispose.assert_called_once()


def test_show_new_script_dialog_fallback_text_kept():
    ctx = MagicMock()
    doc = MagicMock()
    doc.isReadonly.return_value = False

    with (
        patch("plugin.scripting.script_dialogs.load_writeragent_dialog", return_value=None),
        patch("plugin.scripting.script_dialogs.show_text_input_dialog", return_value="Kept"),
        patch("plugin.scripting.script_dialogs.msgbox") as mock_msgbox,
    ):
        result = show_new_script_dialog(ctx, doc=doc, default_attach=True)

    assert result == ("Kept", False)
    mock_msgbox.assert_not_called()


def test_show_new_script_dialog_fallback_reserved_prefix():
    ctx = MagicMock()
    doc = MagicMock()
    doc.isReadonly.return_value = False

    with (
        patch("plugin.scripting.script_dialogs.load_writeragent_dialog", return_value=None),
        patch("plugin.scripting.script_dialogs.show_text_input_dialog", return_value="[Doc] Kept"),
        patch("plugin.scripting.script_dialogs.msgbox") as mock_msgbox,
    ):
        result = show_new_script_dialog(ctx, doc=doc)

    assert result is None
    expected_msg = _("'{0}' starts with a reserved prefix.").format("[Doc] Kept")
    mock_msgbox.assert_called_once_with(ctx, _("Error"), expected_msg)
    assert "parent_window" not in mock_msgbox.call_args.kwargs or mock_msgbox.call_args.kwargs.get("parent_window") is None


def test_show_new_script_dialog_fallback_init_allowed():
    ctx = MagicMock()
    doc = MagicMock()
    doc.isReadonly.return_value = False

    with (
        patch("plugin.scripting.script_dialogs.load_writeragent_dialog", return_value=None),
        patch("plugin.scripting.script_dialogs.show_text_input_dialog", return_value="INIT"),
        patch("plugin.scripting.script_dialogs.msgbox") as mock_msgbox,
    ):
        result = show_new_script_dialog(ctx, doc=doc)

    assert result == ("INIT", False)
    mock_msgbox.assert_not_called()


def test_show_new_script_dialog_fallback_empty_returns_none():
    ctx = MagicMock()

    with (
        patch("plugin.scripting.script_dialogs.load_writeragent_dialog", return_value=None),
        patch("plugin.scripting.script_dialogs.show_text_input_dialog", return_value=""),
        patch("plugin.scripting.script_dialogs.msgbox") as mock_msgbox,
    ):
        result = show_new_script_dialog(ctx)

    assert result is None
    mock_msgbox.assert_not_called()


def test_show_new_script_dialog_none_ctx():
    with patch("plugin.scripting.script_dialogs.load_writeragent_dialog") as mock_load:
        assert show_new_script_dialog(None) is None
    mock_load.assert_not_called()


def test_monaco_attach_label_checks_stale_before_readonly():
    js_path = (
        Path(__file__).resolve().parents[2]
        / "plugin/contrib/scripting/assets/editor/scripts_manager.js"
    )
    js = js_path.read_text(encoding="utf-8")

    match = re.search(r"function attachCheckboxLabel\(\)\s*\{([\s\S]*?)\n  \}", js)
    assert match is not None, "function attachCheckboxLabel() not found"
    body = match.group(1)

    idx_no_doc = body.index("!documentAvailable")
    idx_stale = body.index("documentStale")
    idx_readonly = body.index("documentReadonly")
    assert idx_no_doc < idx_stale < idx_readonly

    assert "attach_unavailable_no_document" in body
    assert "attach_unavailable_stale" in body
    assert "attach_unavailable_readonly" in body
    assert "attach_to_document_label" in body

    modal_match = re.search(r"function openScriptModal\([^)]*\)\s*\{([\s\S]*?)\n  \}", js)
    assert modal_match is not None, "function openScriptModal() not found"
    modal_body = modal_match.group(1)

    idx_call = modal_body.index("attachCheckboxLabel();")
    idx_remove = modal_body.index('modal.classList.remove("toolbar-hidden");')
    assert idx_call < idx_remove

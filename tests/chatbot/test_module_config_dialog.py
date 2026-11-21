# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for plugin.chatbot.module_config_dialog."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from plugin.chatbot.module_config_dialog import (
    ModuleConfigDialog,
    get_module_config_dialog_id,
)
from plugin.chatbot.settings_fields import (
    apply_field_specs_result,
    build_module_field_specs,
)


def test_get_module_config_dialog_id_for_vision():
    with patch(
        "plugin.chatbot.module_config_dialog.find_module_manifest",
        return_value={
            "name": "vision",
            "config_dialog": {"id": "VisionSettingsDialog", "library": "Dialogs"},
        },
    ):
        assert get_module_config_dialog_id("vision") == "VisionSettingsDialog"


def test_extract_result_prefers_numeric_getvalue_over_stale_gettext():
    """Numeric controls inherit getText. The live spin value is getValue."""
    manifest = {
        "name": "demo",
        "config": {
            "count": {"type": "int", "default": 1, "widget": "number"},
        },
    }

    class _Spin:
        def getText(self):
            return "1"

        def getValue(self):
            return 42

    dlg = MagicMock()
    dlg.getControl.side_effect = lambda name: _Spin() if name == "count" else None
    dialog = ModuleConfigDialog(MagicMock(), "demo")
    dialog._dlg = dlg
    with patch("plugin.chatbot.settings_fields.find_module_manifest", return_value=manifest), \
         patch("plugin.chatbot.settings_fields.get_config", return_value=1):
        dialog._specs = build_module_field_specs("demo", ctx=dialog._ctx, control_ids="flat")
        result = dialog._extract_result()
    assert result == {"count": 42}


def test_extract_result_checkbox_bool_skips_write_when_already_true():
    """A checkbox spec read as True is passed to apply_field_specs_result as True, and unchanged True does not write."""
    from plugin.chatbot.settings_fields import changed_config_values

    manifest = {
        "name": "demo",
        "config": {
            "enabled": {"type": "bool", "default": True, "widget": "checkbox"},
        },
    }

    class _Checkbox:
        def supportsService(self, name: str) -> bool:
            return name == "com.sun.star.awt.UnoControlCheckBox"

        def getState(self) -> int:
            return 1

    dlg = MagicMock()
    dlg.getControl.side_effect = lambda name: _Checkbox() if name == "enabled" else None
    dialog = ModuleConfigDialog(MagicMock(), "demo")
    dialog._dlg = dlg

    with patch("plugin.chatbot.settings_fields.find_module_manifest", return_value=manifest), \
         patch("plugin.chatbot.settings_fields.get_config", return_value=True):
        dialog._specs = build_module_field_specs("demo", ctx=dialog._ctx, control_ids="flat")
        result = dialog._extract_result()

    assert result == {"enabled": True}
    assert result["enabled"] is True

    # Assert changed_config_values does not write when stored value is already True
    assert changed_config_values({"demo.enabled": True}, lambda _k: True) == {}

    # Verify through apply_field_specs_result
    with patch("plugin.chatbot.settings_fields.find_module_manifest", return_value=manifest), \
         patch("plugin.chatbot.settings_fields.get_config", return_value=True), \
         patch("plugin.chatbot.settings_fields.save") as mock_save:
        specs = build_module_field_specs("demo", ctx=MagicMock(), control_ids="flat")
        apply_field_specs_result(result, specs)
        mock_save.assert_not_called()


def test_tab_buttons_follow_element_order_and_skip_other_controls():
    from plugin.chatbot.module_config_dialog import _tab_buttons_in_order

    buttons = {
        "btn_tab_general": MagicMock(),
        "btn_ok": MagicMock(),
        "btn_tab_ocr": MagicMock(),
    }
    dlg = MagicMock()
    dlg.getModel.return_value.ElementNames = ("btn_tab_general", "btn_ok", "btn_tab_ocr")
    dlg.getControl.side_effect = lambda name: buttons[name]
    assert _tab_buttons_in_order(dlg) == [buttons["btn_tab_general"], buttons["btn_tab_ocr"]]


def test_apply_stays_open_when_save_fails():
    dialog = ModuleConfigDialog(MagicMock(), "demo")
    dialog._dlg = MagicMock()
    dialog.close = MagicMock()
    dialog._specs = []
    with patch.object(dialog, "_extract_result", side_effect=RuntimeError("bad")), \
         patch("plugin.chatbot.module_config_dialog.msgbox") as box:
        dialog._apply(close=True)
    dialog.close.assert_not_called()
    box.assert_called_once()


def test_open_loads_dialog_via_shared_loader():
    import pytest

    ctx = MagicMock()
    with (
        patch(
            "plugin.chatbot.module_config_dialog.get_module_config_dialog_id",
            return_value="VisionSettingsDialog",
        ),
        patch(
            "plugin.chatbot.module_config_dialog.load_writeragent_dialog_detail",
            return_value=(None, "detail"),
        ) as mock_load,
    ):
        with pytest.raises(RuntimeError, match="load_detail"):
            ModuleConfigDialog(ctx, "vision")._open()
    mock_load.assert_called_once_with("VisionSettingsDialog", ctx)


def test_show_does_not_register_in_active_dialogs_when_open_raises():
    import pytest
    from plugin.chatbot.module_config_dialog import _active_dialogs

    ctx = MagicMock()
    _active_dialogs.pop("test_mod", None)
    with patch.object(ModuleConfigDialog, "_open", side_effect=RuntimeError("open failed")):
        with pytest.raises(RuntimeError, match="open failed"):
            ModuleConfigDialog.show(ctx, "test_mod")
    assert "test_mod" not in _active_dialogs


def test_window_closing_does_not_dispose_and_pops_active_dialogs():
    from plugin.chatbot.module_config_dialog import ModuleConfigDialog, _active_dialogs

    ctx = MagicMock()
    dlg = MagicMock()
    _active_dialogs.clear()

    with (
        patch("plugin.chatbot.module_config_dialog.get_module_config_dialog_id", return_value="VisionSettingsDialog"),
        patch("plugin.chatbot.module_config_dialog.load_writeragent_dialog_detail", return_value=(dlg, "")),
        patch("plugin.chatbot.module_config_dialog.build_module_field_specs", return_value=[]),
    ):
        ModuleConfigDialog.show(ctx, "vision")
        assert "vision" in _active_dialogs
        dialog = _active_dialogs["vision"]
        listener = dialog._top_listener
        assert listener is not None

        listener.windowClosing(MagicMock())
        dlg.dispose.assert_not_called()
        assert "vision" not in _active_dialogs

        # A later show() constructs a new dialog
        dlg2 = MagicMock()
        with patch("plugin.chatbot.module_config_dialog.load_writeragent_dialog_detail", return_value=(dlg2, "")):
            ModuleConfigDialog.show(ctx, "vision")
            assert "vision" in _active_dialogs
            assert _active_dialogs["vision"]._dlg is dlg2


def test_disposing_without_window_closing_pops_active_dialogs():
    from plugin.chatbot.module_config_dialog import ModuleConfigDialog, _active_dialogs

    ctx = MagicMock()
    dlg = MagicMock()
    _active_dialogs.clear()

    with (
        patch("plugin.chatbot.module_config_dialog.get_module_config_dialog_id", return_value="VisionSettingsDialog"),
        patch("plugin.chatbot.module_config_dialog.load_writeragent_dialog_detail", return_value=(dlg, "")),
        patch("plugin.chatbot.module_config_dialog.build_module_field_specs", return_value=[]),
    ):
        ModuleConfigDialog.show(ctx, "vision")
        assert "vision" in _active_dialogs
        dialog = _active_dialogs["vision"]
        listener = dialog._top_listener
        assert listener is not None

        listener.disposing(MagicMock())
        dlg.dispose.assert_not_called()
        assert "vision" not in _active_dialogs
        assert dialog._closed is True


def test_disposing_from_old_dialog_does_not_evict_replacement():
    from plugin.chatbot.module_config_dialog import ModuleConfigDialog, _active_dialogs

    ctx = MagicMock()
    dlg_a = MagicMock()
    dlg_b = MagicMock()
    dlg_c = MagicMock()
    _active_dialogs.clear()

    with (
        patch("plugin.chatbot.module_config_dialog.get_module_config_dialog_id", return_value="VisionSettingsDialog"),
        patch("plugin.chatbot.module_config_dialog.load_writeragent_dialog_detail", side_effect=[(dlg_a, ""), (dlg_b, ""), (dlg_c, "")]),
        patch("plugin.chatbot.module_config_dialog.build_module_field_specs", return_value=[]),
    ):
        # 1. show() A, windowClosing on A. Registry is empty, dispose() was not called.
        ModuleConfigDialog.show(ctx, "vision")
        dialog_a = _active_dialogs["vision"]
        listener_a = dialog_a._top_listener
        assert listener_a is not None

        listener_a.windowClosing(MagicMock())
        assert "vision" not in _active_dialogs
        dlg_a.dispose.assert_not_called()

        # 2. show() B. Registry is B.
        ModuleConfigDialog.show(ctx, "vision")
        assert _active_dialogs.get("vision") is not None
        dialog_b = _active_dialogs["vision"]
        assert dialog_b is not dialog_a

        # 3. disposing on A. Registry is still B, B is not _closed, B's dialog was not disposed.
        listener_a.disposing(MagicMock())
        assert _active_dialogs.get("vision") is dialog_b
        assert dialog_b._closed is False
        dlg_b.dispose.assert_not_called()

        # 4. show() while B is open raises B without closing or disposing it.
        ret = ModuleConfigDialog.show(ctx, "vision")
        assert ret is dialog_b
        assert _active_dialogs.get("vision") is dialog_b
        assert dialog_b._closed is False
        dlg_b.dispose.assert_not_called()
        dlg_b.toFront.assert_called_once()
        dlg_b.setFocus.assert_called_once()
        dlg_c.dispose.assert_not_called()


def test_show_recreates_when_to_front_raises_disposed():
    from plugin.chatbot.module_config_dialog import ModuleConfigDialog, _active_dialogs

    ctx = MagicMock()
    dlg_a = MagicMock()
    dlg_b = MagicMock()
    _active_dialogs.clear()

    with (
        patch("plugin.chatbot.module_config_dialog.get_module_config_dialog_id", return_value="VisionSettingsDialog"),
        patch("plugin.chatbot.module_config_dialog.load_writeragent_dialog_detail", side_effect=[(dlg_a, ""), (dlg_b, "")]),
        patch("plugin.chatbot.module_config_dialog.build_module_field_specs", return_value=[]),
    ):
        ModuleConfigDialog.show(ctx, "vision")
        assert _active_dialogs.get("vision") is not None
        dialog_a = _active_dialogs["vision"]

        class DisposedException(RuntimeError):
            pass

        dlg_a.toFront.side_effect = DisposedException("object disposed")
        ret = ModuleConfigDialog.show(ctx, "vision")
        assert ret is not dialog_a
        assert _active_dialogs["vision"] is ret
        assert ret._dlg is dlg_b



def test_close_does_not_evict_different_registered_dialog():
    from plugin.chatbot.module_config_dialog import ModuleConfigDialog, _active_dialogs

    ctx = MagicMock()
    dialog_other = ModuleConfigDialog(ctx, "vision")
    _active_dialogs["vision"] = dialog_other

    dialog_a = ModuleConfigDialog(ctx, "vision")
    dialog_a._dlg = MagicMock()
    assert dialog_a._closed is False

    dialog_a.close()
    assert dialog_a._closed is True
    assert _active_dialogs.get("vision") is dialog_other


def _make_preview_dialog(specs=None, raw_result=None, job=None):
    from plugin.chatbot.module_config_dialog import ModuleConfigDialog

    ctx = MagicMock()
    dialog = ModuleConfigDialog(ctx, "vision", preview_job=job)
    dialog._dlg = MagicMock()
    dialog._pane = MagicMock()
    dialog._specs = specs or []
    dialog._extract_result = MagicMock(return_value=raw_result or {})
    return dialog


def test_run_passes_option_values_not_captions():
    from plugin.chatbot.module_config_dialog import PreviewImage

    specs = [
        {
            "name": "layout_model",
            "config_key": "vision.layout_model",
            "options": [{"value": "heron", "label": "Heron (default)"}],
        },
        {
            "name": "insert_mode",
            "config_key": "vision.insert_mode",
            "options": [{"value": "structured", "label": "Structured (layout / cell grid)"}],
        },
    ]
    seen_values = []
    img = PreviewImage(MagicMock(), 1600, 2000, "Done: 10 characters.")

    def job(ctx, values, cancel, report):
        seen_values.append(values)
        return img

    dialog = _make_preview_dialog(
        specs=specs,
        raw_result={"layout_model": "Heron (default)", "insert_mode": "Structured (layout / cell grid)"},
        job=job,
    )
    captured = []
    with (
        patch("plugin.chatbot.module_config_dialog.run_in_background", side_effect=lambda fn, **kw: captured.append(fn)),
        patch("plugin.chatbot.module_config_dialog.main.post", side_effect=lambda fn, *a: fn(*a)),
    ):
        dialog._start_run()
        assert len(captured) == 1
        captured[0]()

    assert seen_values == [{"layout_model": "heron", "insert_mode": "structured"}]
    dialog._pane.show.assert_called_once_with(img)


def test_second_run_click_while_running_starts_no_job():
    dialog = _make_preview_dialog(job=lambda ctx, values, cancel, report: None)
    captured = []
    with (
        patch("plugin.chatbot.module_config_dialog.run_in_background", side_effect=lambda fn, **kw: captured.append(fn)),
        patch("plugin.chatbot.module_config_dialog.main.post", side_effect=lambda fn, *a: fn(*a)),
    ):
        dialog._start_run()
        dialog._start_run()
    assert len(captured) == 1


def test_close_during_run_cancels_and_drops_result():
    from plugin.chatbot.module_config_dialog import PreviewImage
    from plugin.framework.send_cancellation import is_cancelled

    tokens = []
    img = PreviewImage(MagicMock(), 1600, 2000, "Done: 5 characters.")

    def job(ctx, values, cancel, report):
        tokens.append(cancel)
        return img

    dialog = _make_preview_dialog(job=job)
    pane = dialog._pane
    captured = []
    with (
        patch("plugin.chatbot.module_config_dialog.run_in_background", side_effect=lambda fn, **kw: captured.append(fn)),
        patch("plugin.chatbot.module_config_dialog.main.post", side_effect=lambda fn, *a: fn(*a)),
    ):
        dialog._start_run()
        token = dialog._run_cancel
        dialog.close()
        assert token is not None and is_cancelled(token) is True
        captured[0]()

    assert tokens == [token]
    pane.show.assert_not_called()


def test_run_error_sets_status_and_reenables_run():
    from plugin.framework.errors import ToolExecutionError

    def job(ctx, values, cancel, report):
        raise ToolExecutionError("Select an image", code="NO_IMAGE_SELECTED")

    dialog = _make_preview_dialog(job=job)
    btn_run = MagicMock()
    lbl_status = MagicMock()
    with (
        patch(
            "plugin.chatbot.module_config_dialog.get_optional",
            side_effect=lambda dlg, name: {"btn_run": btn_run, "lbl_preview_status": lbl_status}.get(name),
        ),
        patch("plugin.chatbot.module_config_dialog.run_in_background", side_effect=lambda fn, **kw: fn()),
        patch("plugin.chatbot.module_config_dialog.main.post", side_effect=lambda fn, *a: fn(*a)),
    ):
        dialog._start_run()

    assert btn_run.setEnable.call_args_list[-1].args == (True,)
    assert lbl_status.setText.call_args_list[-1].args == ("Select an image",)


def test_stale_status_after_finish_is_ignored():
    from plugin.chatbot.module_config_dialog import PreviewImage

    img = PreviewImage(MagicMock(), 1600, 2000, "Done: 1 characters.")
    posted_report = []

    def job(ctx, values, cancel, report):
        posted_report.append((cancel, "Recognizing text…"))
        return img

    dialog = _make_preview_dialog(job=job)
    lbl_status = MagicMock()
    with (
        patch(
            "plugin.chatbot.module_config_dialog.get_optional",
            side_effect=lambda dlg, name: {"lbl_preview_status": lbl_status}.get(name),
        ),
        patch("plugin.chatbot.module_config_dialog.run_in_background", side_effect=lambda fn, **kw: fn()),
        patch("plugin.chatbot.module_config_dialog.main.post", side_effect=lambda fn, *a: fn(*a)),
    ):
        dialog._start_run()

    token, stale_text = posted_report[0]
    dialog._on_run_status(token, stale_text)
    assert lbl_status.setText.call_args_list[-1].args == ("Done: 1 characters.",)


def test_dialog_without_apply_saves_on_ok_and_discards_on_cancel_after_run():
    """When btn_apply is absent, OK saves and closes while Cancel discards edits even after Run."""
    from plugin.chatbot.module_config_dialog import ModuleConfigDialog, PreviewImage, _active_dialogs

    _active_dialogs.clear()
    listeners: dict[str, list[MagicMock]] = {}
    buttons: dict[str, MagicMock] = {}
    for name in ("btn_run", "btn_ok", "btn_close"):
        btn = MagicMock()
        listeners[name] = []
        btn.addActionListener.side_effect = lambda lst, _n=name: listeners[_n].append(lst)
        buttons[name] = btn

    dlg = MagicMock()
    dlg.getControl.side_effect = lambda name: buttons.get(name)
    field_ctrl = MagicMock()
    field_ctrl.State = 1
    buttons["delete_image_after_insert"] = field_ctrl

    specs = [{"name": "delete_image_after_insert", "type": "boolean", "widget": "checkbox", "config_key": "vision.delete_image_after_insert", "default": False}]
    job = MagicMock(return_value=PreviewImage(MagicMock(), 100, 100, "Done."))

    with (
        patch("plugin.chatbot.module_config_dialog.get_module_config_dialog_id", return_value="VisionSettingsDialog"),
        patch("plugin.chatbot.module_config_dialog.load_writeragent_dialog_detail", return_value=(dlg, "")),
        patch("plugin.chatbot.module_config_dialog.build_module_field_specs", return_value=specs),
        patch("plugin.chatbot.module_config_dialog.populate_settings_control"),
        patch("plugin.chatbot.module_config_dialog.read_settings_control", return_value=True),
        patch("plugin.chatbot.module_config_dialog.apply_field_specs_result") as mock_apply,
        patch("plugin.chatbot.module_config_dialog._PreviewPane"),
        patch("plugin.chatbot.module_config_dialog.run_in_background", side_effect=lambda fn, **kw: fn()),
        patch("plugin.chatbot.module_config_dialog.main.post", side_effect=lambda fn, *a: fn(*a)),
    ):
        dialog = ModuleConfigDialog.show(MagicMock(), "vision", preview_job=job)
        # Click Run, then Cancel (btn_close): must not persist settings.
        listeners["btn_run"][0].actionPerformed(MagicMock())
        assert job.call_count == 1
        mock_apply.assert_not_called()
        listeners["btn_close"][0].actionPerformed(MagicMock())
        mock_apply.assert_not_called()
        assert dialog._closed is True

        # Re-open and click OK: must persist and close.
        dialog2 = ModuleConfigDialog.show(MagicMock(), "vision", preview_job=job)
        listeners["btn_ok"][-1].actionPerformed(MagicMock())
        mock_apply.assert_called_once_with({"delete_image_after_insert": True}, specs)
        assert dialog2._closed is True

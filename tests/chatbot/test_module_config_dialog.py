# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for plugin.chatbot.module_config_dialog."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from plugin.chatbot.module_config_dialog import (
    ModuleConfigDialog,
    apply_module_config_result,
    get_module_config_dialog_id,
    get_module_config_field_specs,
)
from plugin.chatbot.settings_fields import populate_settings_control


def test_get_module_config_dialog_id_for_vision():
    with patch(
        "plugin.chatbot.settings_fields.find_module_manifest",
        return_value={
            "name": "vision",
            "config_dialog": {"id": "VisionSettingsDialog", "library": "Dialogs"},
        },
    ):
        assert get_module_config_dialog_id("vision") == "VisionSettingsDialog"


def test_manifest_vision_module_has_config_dialog():
    from plugin._manifest import MODULES

    vision = next(m for m in MODULES if m.get("name") == "vision")
    assert vision.get("settings_tab") is False
    assert vision.get("config_dialog", {}).get("id") == "VisionSettingsDialog"


def test_get_module_config_field_specs_skips_internal_and_non_persisted():
    ctx = object()
    manifest = {
        "name": "vision",
        "config": {
            "device": {"type": "string", "default": "auto", "widget": "select", "page": "general"},
            "open_settings": {"type": "string", "widget": "button", "settings_persist": False},
            "_internal": {"type": "string", "internal": True},
        },
    }
    with patch("plugin.chatbot.settings_fields.find_module_manifest", return_value=manifest), \
         patch("plugin.chatbot.settings_fields.get_config", return_value="auto"):
        specs = get_module_config_field_specs(ctx, "vision")

    assert len(specs) == 1
    assert specs[0]["name"] == "device"
    assert specs[0]["config_key"] == "vision.device"


def test_manifest_vision_insert_mode_has_options():
    from plugin._manifest import MODULES

    vision = next(m for m in MODULES if m.get("name") == "vision")
    schema = vision.get("config", {}).get("insert_mode", {})
    assert schema.get("widget") == "select"
    assert len(schema.get("options") or []) >= 2


def test_populate_settings_control_sets_translated_option_labels():
    model = type("M", (), {"StringItemList": ()})()
    ctrl = type("C", (), {})()
    ctrl.getModel = lambda: model  # type: ignore[method-assign]
    ctrl.setText = lambda _text: None  # type: ignore[method-assign]

    field = {
        "name": "insert_mode",
        "options": [
            {"value": "html", "label": "Standard HTML"},
            {"value": "structured", "label": "Structured (layout / cell grid)"},
        ],
        "value": "html",
    }
    populate_settings_control(ctrl, field)
    assert model.StringItemList[0]
    assert "Standard HTML" in model.StringItemList[0]


def test_apply_module_config_result_stores_select_option_values():
    """A select caption is stored as its option value. A number field stays as entered."""
    ctx = object()
    manifest = {
        "name": "demo",
        "config": {
            "count": {"type": "int", "default": 1, "widget": "number"},
            "mode": {
                "type": "string",
                "default": "fast",
                "widget": "select",
                "options": [{"value": "fast", "label": "Fast Mode"}],
            },
        },
    }
    with patch("plugin.chatbot.settings_fields.find_module_manifest", return_value=manifest), \
         patch("plugin.chatbot.settings_fields.get_config", side_effect=lambda key: {"demo.count": 1, "demo.mode": "fast"}[key]), \
         patch("plugin.chatbot.settings_fields.set_configs") as mock_set_configs:
        apply_module_config_result(ctx, "demo", {"count": "42", "mode": "Fast Mode"})

    mock_set_configs.assert_called_once_with({"demo.count": "42", "demo.mode": "fast"})


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
        result = dialog._extract_result()
    assert result == {"count": 42}


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
    with patch.object(dialog, "_extract_result", side_effect=RuntimeError("bad")), \
         patch("plugin.chatbot.dialogs.msgbox") as box:
        dialog._apply(close=True)
    dialog.close.assert_not_called()
    box.assert_called_once()


def test_open_passes_ctx_to_get_extension_url():
    ctx = MagicMock()
    smgr = MagicMock()
    ctx.getServiceManager.return_value = smgr
    smgr.createInstanceWithContext.side_effect = RuntimeError("stop after url")
    with (
        patch(
            "plugin.chatbot.module_config_dialog.get_module_config_dialog_id",
            return_value="VisionSettingsDialog",
        ),
        patch(
            "plugin.chatbot.module_config_dialog.get_extension_url",
            return_value="file:///tmp/LibrePy.oxt",
        ) as geu,
    ):
        ModuleConfigDialog(ctx, "vision")._open()
    geu.assert_called_once_with(ctx)

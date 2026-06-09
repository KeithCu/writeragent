# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for plugin.chatbot.module_config_dialog."""

from __future__ import annotations

from unittest.mock import patch

from plugin.chatbot.module_config_dialog import (
    _option_labels,
    _set_field_options,
    get_module_config_dialog_id,
    get_module_config_field_specs,
)


def test_get_module_config_dialog_id_for_vision():
    with patch(
        "plugin.chatbot.module_config_dialog._find_module_manifest",
        return_value={
            "name": "vision",
            "config_dialog": {"id": "VisionSettingsDialog", "library": "WriterAgentDialogs"},
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
    with patch("plugin.chatbot.module_config_dialog._find_module_manifest", return_value=manifest), \
         patch("plugin.chatbot.module_config_dialog.get_config", return_value="auto"):
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


def test_option_labels_translates_select_labels():
    field = {
        "name": "insert_mode",
        "options": [
            {"value": "html", "label": "Standard HTML"},
            {"value": "structured", "label": "Structured (layout / cell grid)"},
        ],
    }
    labels = _option_labels(field)
    assert len(labels) == 2
    assert "Standard HTML" in labels[0] or labels[0]


def test_set_field_options_uses_string_item_list():
    model = type("M", (), {"StringItemList": ()})()
    ctrl = type("C", (), {})()
    ctrl.getModel = lambda: model  # type: ignore[method-assign]

    field = {
        "name": "insert_mode",
        "options": [{"value": "html", "label": "Standard HTML"}],
    }
    _set_field_options(ctrl, field)
    assert model.StringItemList == ("Standard HTML",)

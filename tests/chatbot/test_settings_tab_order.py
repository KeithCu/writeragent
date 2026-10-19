from __future__ import annotations

from plugin.chatbot.settings_tab_order import (
    iter_settings_field_modules,
    iter_settings_tab_modules,
)


def test_settings_tab_and_field_modules_with_synthetic_manifests() -> None:
    modules = [
        # A module with config is a tab and a field source.
        {
            "name": "audio",
            "title": "Audio",
            "config": {"mic": {"type": "string"}},
        },
        # A config_inline child of that tab is a field source and not a tab.
        {
            "name": "child",
            "title": "Child",
            "config_inline": "audio",
            "config": {"sub_setting": {"type": "bool"}},
        },
        # config_dialog set, settings_tab omitted: neither.
        {
            "name": "standalone_dlg",
            "title": "Standalone",
            "config_dialog": {"id": "StandaloneDlg"},
            "config": {"standalone_setting": {"type": "string"}},
        },
        # settings_tab: false: neither.
        {
            "name": "disabled_tab",
            "title": "Disabled Tab",
            "settings_tab": False,
            "config": {"disabled_setting": {"type": "string"}},
        },
        # name: core with a public field: neither.
        {
            "name": "core",
            "title": "Core",
            "config": {"public_setting": {"type": "string", "internal": False}},
        },
        # config: {} and no children: neither.
        {
            "name": "empty_module",
            "title": "Empty",
            "config": {},
        },
    ]

    tab_names = [m["name"] for m in iter_settings_tab_modules(modules)]
    assert tab_names == ["audio"]

    field_names = [m["name"] for m in iter_settings_field_modules(modules)]
    assert field_names == ["audio", "child"]

"""Shared module.yaml settings field-spec helpers."""

import json
from unittest.mock import MagicMock, patch

import pytest

from plugin.chatbot.settings_fields import (
    apply_field_specs_result,
    build_module_field_specs,
    populate_settings_control,
    read_settings_control,
)


def test_egret_layout_label_keeps_the_comma():
    """A flow-scalar comma used to end the label at 'slower'."""
    from plugin._manifest import MODULES

    vision = next(m for m in MODULES if m.get("name") == "vision")
    options = vision["config"]["layout_model"]["options"]
    egret = next(opt for opt in options if opt.get("value") == "egret_large")
    assert egret["label"] == "Egret large (slower, complex docs)"


def test_build_module_field_specs_prefixed_and_flat():
    manifest = {
        "name": "scripting",
        "config": {
            "python_venv_path": {"type": "string", "widget": "text"},
            "python_exec_timeout": {"type": "int", "widget": "spin"},
        },
    }
    with (
        patch("plugin._manifest.MODULES", [manifest]),
        patch(
            "plugin.chatbot.settings_fields.get_config",
            side_effect=lambda key: {"scripting.python_venv_path": "/venv", "scripting.python_exec_timeout": 30}.get(key, ""),
        ),
    ):
        prefixed = build_module_field_specs("scripting", control_ids="prefixed")
        flat = build_module_field_specs("scripting", control_ids="flat")

    assert {s["name"] for s in prefixed} == {"scripting__python_venv_path", "scripting__python_exec_timeout"}
    assert {s["name"] for s in flat} == {"python_venv_path", "python_exec_timeout"}
    assert all(s.get("config_key", "").startswith("scripting.") for s in prefixed)
    timeout = next(s for s in prefixed if s["name"] == "scripting__python_exec_timeout")
    assert timeout["type"] == "int"
    assert timeout["value"] == "30"


def test_build_module_field_specs_skip_librepy_exclude():
    manifest = {
        "name": "scripting",
        "config": {
            "python_venv_path": {"type": "string"},
            "ppt_master_data_path": {"type": "string", "librepy_exclude": True},
            "test_venv": {"settings_persist": False},
        },
    }
    with (
        patch("plugin._manifest.MODULES", [manifest]),
        patch("plugin.chatbot.settings_fields.get_config", return_value=""),
    ):
        specs = build_module_field_specs("scripting", control_ids="prefixed", skip_librepy_exclude=True)

    assert {s["name"] for s in specs} == {"scripting__python_venv_path"}


def test_apply_field_specs_result_uses_config_key():
    ctx = MagicMock()
    specs = [
        {"name": "scripting__python_venv_path", "config_key": "scripting.python_venv_path"},
        {"name": "ignored", "config_key": "scripting.ignored"},
    ]
    with (
        patch("plugin.chatbot.settings_fields.set_configs") as mock_set,
        patch("plugin.framework.event_bus.global_event_bus.emit") as mock_emit,
    ):
        apply_field_specs_result(ctx, {"scripting__python_venv_path": "/opt/venv"}, specs)

    mock_set.assert_called_once_with({"scripting.python_venv_path": "/opt/venv"})
    # set_configs emits when a value changes. This function must not emit again.
    mock_emit.assert_not_called()


def test_apply_field_specs_result_maps_translated_label_to_value():
    specs = [{
        "name": "scripting__python_session_mode",
        "config_key": "scripting.python_session_mode",
        "options": [
            {"value": "isolated", "label": "Isolated (default)"},
            {"value": "shared", "label": "Shared kernel"},
        ],
    }]

    def _fake_gettext(text: str) -> str:
        return {"Shared kernel": "Gedeelde kernel"}.get(text, text)

    with (
        patch("plugin.chatbot.settings_fields.set_configs") as mock_set,
        patch("plugin.chatbot.settings_fields._", side_effect=_fake_gettext),
    ):
        apply_field_specs_result(MagicMock(), {"scripting__python_session_mode": "Gedeelde kernel"}, specs)
        apply_field_specs_result(MagicMock(), {"scripting__python_session_mode": "Shared kernel"}, specs)
        apply_field_specs_result(MagicMock(), {"scripting__python_session_mode": "shared"}, specs)

    assert mock_set.call_args_list[0].args[0] == {"scripting.python_session_mode": "shared"}
    assert mock_set.call_args_list[1].args[0] == {"scripting.python_session_mode": "shared"}
    assert mock_set.call_args_list[2].args[0] == {"scripting.python_session_mode": "shared"}


def test_apply_field_specs_result_skips_unchanged_and_default_values():
    """A caption that maps to the current id, and a value equal to the default, are not writes."""
    from plugin.framework.config import _config_path, set_configs

    path = _config_path()
    assert path
    specs = [{
        "name": "scripting__python_session_mode",
        "config_key": "scripting.python_session_mode",
        "options": [
            {"value": "isolated", "label": "Isolated (default)"},
            {"value": "shared", "label": "Shared kernel"},
        ],
    }]

    def _fake_gettext(text: str) -> str:
        return {"Isolated (default)": "Geïsoleerd", "Shared kernel": "Gedeelde kernel"}.get(text, text)

    seen: list[dict] = []

    def _spy(values: dict) -> None:
        seen.append(dict(values))
        set_configs(values)

    import os

    before = open(path, encoding="utf-8").read() if os.path.isfile(path) else ""
    with (
        patch("plugin.chatbot.settings_fields.set_configs", side_effect=_spy),
        patch("plugin.chatbot.settings_fields._", side_effect=_fake_gettext),
    ):
        apply_field_specs_result(
            MagicMock(),
            {"scripting__python_session_mode": "Geïsoleerd"},
            specs,
        )
        assert seen == []
        after = open(path, encoding="utf-8").read() if os.path.isfile(path) else ""
        assert after == before

        apply_field_specs_result(
            MagicMock(),
            {"scripting__python_session_mode": "Gedeelde kernel"},
            specs,
        )
    assert seen == [{"scripting.python_session_mode": "shared"}]


def test_apply_field_specs_result_skips_unknown_keys():
    with patch("plugin.chatbot.settings_fields.set_configs") as mock_set:
        apply_field_specs_result(MagicMock(), {"nope": "x"}, [{"name": "kept", "config_key": "scripting.kept"}])
    mock_set.assert_not_called()


def test_read_settings_control_skips_missing_and_checkbox_is_bool():
    assert read_settings_control(None) is None

    class _Box:
        def supportsService(self, name):
            return False

        def getState(self):
            return 1

    assert read_settings_control(_Box(), {"type": "bool"}) is True


def test_populate_settings_control_sets_string_item_list_and_numeric_value():
    class _Model:
        StringItemList = ()

    class _Ctrl:
        def __init__(self):
            self.model = _Model()
            self.value = None
            self.text = None

        def getModel(self):
            return self.model

        def setValue(self, value):
            self.value = value

        def setText(self, text):
            self.text = text

    ctrl = _Ctrl()
    populate_settings_control(
        ctrl,
        {
            "name": "count",
            "type": "int",
            "value": "7",
            "options": [{"value": "7", "label": "Seven"}],
        },
    )
    assert ctrl.model.StringItemList == ("Seven",)
    assert ctrl.value == 7.0
    assert ctrl.text is None


def _config_json(path):
    body = path.read_text(encoding="utf-8")
    return json.loads(body[body.index("{") :])


def test_set_configs_one_write_one_emit_and_validation_writes_nothing(tmp_path):
    """Batch save: one write, one event. A bad value leaves the file untouched."""
    from plugin.framework.config import get_config, get_config_int, reset_config_for_tests, set_configs
    from plugin.framework.errors import ConfigValidationError

    path = tmp_path / "writeragent.json"
    path.write_text(json.dumps({"text_model": "keep", "python_venv_path": "/old"}), encoding="utf-8")
    reset_config_for_tests()
    try:
        with (
            patch("plugin.framework.config._config_path", return_value=str(path)),
            patch("plugin.framework.event_bus.global_event_bus.emit") as emit,
        ):
            set_configs({"text_model": "alpha", "request_timeout": 45})
            assert emit.call_count == 1
            assert emit.call_args.args[0] == "config:changed"
            assert emit.call_args.kwargs["key"] == ""
            assert emit.call_args.kwargs["keys"] == ("text_model", "request_timeout")
            assert get_config("text_model") == "alpha"
            assert get_config_int("request_timeout") == 45
            # An unrelated batch leaves the flat alias alone. Only staging the
            # dotted key pops it (checked below).
            assert _config_json(path)["python_venv_path"] == "/old"

            emit.reset_mock()
            set_configs({"text_model": "beta"})
            emit.assert_called_once()
            assert emit.call_args.kwargs["key"] == "text_model"
            assert emit.call_args.kwargs["value"] == "beta"

            emit.reset_mock()
            before = path.read_text(encoding="utf-8")
            set_configs({"text_model": "beta", "request_timeout": "45"})
            emit.assert_not_called()
            assert path.read_text(encoding="utf-8") == before

            with pytest.raises(ConfigValidationError):
                set_configs({"temperature": 9, "text_model": "should-not-land"})
            emit.assert_not_called()
            assert path.read_text(encoding="utf-8") == before
            assert get_config("text_model") == "beta"

            path.write_text(
                json.dumps({"text_model": "beta", "python_venv_path": "/old"}),
                encoding="utf-8",
            )
            reset_config_for_tests()
            set_configs({"scripting.python_venv_path": "/opt/venv"})
            written = _config_json(path)
            assert written["scripting.python_venv_path"] == "/opt/venv"
            assert "python_venv_path" not in written
    finally:
        reset_config_for_tests()


def test_call_options_provider_skips_plugin_main_when_absent():
    import sys
    import types

    from plugin.chatbot.settings_fields import call_options_provider

    seen: list[object] = []

    def provide(services):
        seen.append(services)
        return ["ok"]

    fake = types.ModuleType("wa_librepy_opts")
    fake.provide = provide
    with patch.dict(sys.modules, {"plugin.main": None, "wa_librepy_opts": fake}):
        out = call_options_provider(MagicMock(), "wa_librepy_opts:provide")
    assert out == ["ok"]
    assert seen == [None]


def test_build_module_field_specs_voice_options_come_from_catalog():
    """Settings voice options follow the catalog, not the short yaml stub."""
    manifest = {
        "name": "audio",
        "config": {
            "tts_voice": {
                "type": "string",
                "widget": "select",
                "options_provider": "plugin.audio.tts_service:settings_voice_options",
                "options": [{"value": "alloy", "label": "alloy (OpenAI Neutral)"}],
            }
        },
    }

    def _cfg(key, default=None):
        values = {
            "audio.tts_voice": "de_DE-thorsten-medium",
            "audio.tts_provider": "piper",
            "audio.tts_model": "",
        }
        return values.get(key, default if default is not None else "")

    with (
        patch("plugin._manifest.MODULES", [manifest]),
        patch("plugin.chatbot.settings_fields.get_config", side_effect=_cfg),
        patch("plugin.audio.tts_service.get_config", side_effect=_cfg),
        patch("plugin.framework.i18n.get_active_locale", return_value="de_DE"),
        patch("plugin.main.get_services", return_value=None),
    ):
        specs = build_module_field_specs("audio", ctx=object(), control_ids="prefixed")

    voice = next(spec for spec in specs if spec["name"] == "audio__tts_voice")
    values = [opt["value"] for opt in voice["options"]]
    assert values[0] == "de_DE-thorsten-medium"
    assert "fr_FR-siwis-medium" in values
    assert "alloy" not in values
    assert "Thorsten" in voice["value"]


def test_build_module_field_specs_options_provider_falls_back_to_yaml():
    manifest = {
        "name": "audio",
        "config": {
            "tts_voice": {
                "type": "string",
                "options_provider": "plugin.audio.tts_service:missing_voice_options",
                "options": [{"value": "default", "label": "default (System Default)"}],
            }
        },
    }
    with (
        patch("plugin._manifest.MODULES", [manifest]),
        patch("plugin.chatbot.settings_fields.get_config", return_value="default"),
        patch("plugin.main.get_services", return_value=None),
    ):
        specs = build_module_field_specs("audio", ctx=object(), control_ids="flat")

    voice = specs[0]
    assert voice["options"] == [{"value": "default", "label": "default (System Default)"}]
    assert voice["value"] == "default (System Default)"


def test_build_module_field_specs_omits_internal_and_non_persisting():
    manifest_agent = {
        "name": "agent_backend",
        "config": {
            "backend_id": {"type": "string", "widget": "select"},
            "path": {"type": "string", "internal": True},
            "args": {"type": "string", "internal": True},
            "acp_agent_name": {"type": "string", "internal": True},
        },
    }
    manifest_mcp = {
        "name": "mcp",
        "config": {
            "mcp_enabled": {"type": "boolean", "widget": "checkbox"},
            "client_config_snippet": {"type": "string", "widget": "textarea", "settings_persist": False},
            "copy_config": {"type": "string", "widget": "button", "settings_persist": False},
        },
    }
    with (
        patch("plugin._manifest.MODULES", [manifest_agent, manifest_mcp]),
        patch("plugin.chatbot.settings_fields.get_config", return_value=""),
    ):
        agent_specs = build_module_field_specs("agent_backend", control_ids="prefixed")
        mcp_specs = build_module_field_specs("mcp", control_ids="prefixed")

    assert {s["name"] for s in agent_specs} == {"agent_backend__backend_id"}
    assert {s["name"] for s in mcp_specs} == {"mcp__mcp_enabled"}


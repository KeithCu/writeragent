"""Shared module.yaml settings field-spec helpers."""

from typing import Any
from unittest.mock import patch

from plugin.framework.event_bus import get_event_bus
import pytest

from plugin.chatbot.settings_fields import (
    apply_field_specs_result,
    build_module_field_specs,
    populate_settings_control,
    read_settings_control,
)


def test_default_settings_stay_unchanged_when_strict_coerce_sees_bounds() -> None:
    """Untouched defaults must not become a Settings OK write.

    A number equal to a stored out-of-range value is not unchanged: strict
    coerce rejects it, so the key stays in the batch and save raises.
    """
    from plugin.chatbot.settings_fields import changed_config_values

    defaults = {
        "temperature": -1.0,
        "chat_max_tokens": 16384,
        "request_timeout": 120,
    }
    pending = {
        "temperature": "-1.0",
        "chat_max_tokens": "16384",
        "request_timeout": "120",
    }
    assert changed_config_values(pending, lambda key: defaults[key]) == {}
    assert changed_config_values({"temperature": 1.0}, lambda key: 1.0) == {}
    assert changed_config_values({"chat_max_tokens": 0}, lambda key: 0) == {}
    assert changed_config_values({"request_timeout": 1}, lambda key: 1) == {}
    assert changed_config_values({"temperature": 5.0}, lambda key: 5.0) == {"temperature": 5.0}
    assert changed_config_values({"chat_max_tokens": -1}, lambda key: -1) == {"chat_max_tokens": -1}
    assert changed_config_values({"request_timeout": 0}, lambda key: 0) == {"request_timeout": 0}
    assert changed_config_values({"api_keys_by_endpoint": {"a": "k"}}, lambda key: {"a": "k"}) == {}
    assert changed_config_values({"api_keys_by_endpoint": {"a": "k2"}}, lambda key: {"a": "k"}) == {
        "api_keys_by_endpoint": {"a": "k2"}
    }


def test_egret_layout_label_keeps_the_comma():
    """A flow-scalar comma used to end the label at 'slower'."""
    from plugin._manifest import MODULES

    vision = next(m for m in MODULES if m.get("name") == "vision")
    options = vision["config"]["layout_model"]["options"]
    egret = next(opt for opt in options if opt.get("value") == "egret_large")
    assert "," in egret["label"]


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
    specs = [
        {"name": "scripting__python_venv_path", "config_key": "scripting.python_venv_path"},
        {"name": "ignored", "config_key": "scripting.ignored"},
    ]
    with (
        patch("plugin.chatbot.settings_fields.save") as mock_set,
        patch.object(get_event_bus(), "emit") as mock_emit,
    ):
        apply_field_specs_result({"scripting__python_venv_path": "/opt/venv"}, specs)

    mock_set.assert_called_once_with({"scripting.python_venv_path": "/opt/venv"})
    # save emits when a value changes. This function must not emit again.
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
        patch("plugin.chatbot.settings_fields.save") as mock_set,
        patch("plugin.chatbot.settings_fields._", side_effect=_fake_gettext),
    ):
        apply_field_specs_result({"scripting__python_session_mode": "Gedeelde kernel"}, specs)
        apply_field_specs_result({"scripting__python_session_mode": "Shared kernel"}, specs)
        apply_field_specs_result({"scripting__python_session_mode": "shared"}, specs)

    assert mock_set.call_args_list[0].args[0] == {"scripting.python_session_mode": "shared"}
    assert mock_set.call_args_list[1].args[0] == {"scripting.python_session_mode": "shared"}
    assert mock_set.call_args_list[2].args[0] == {"scripting.python_session_mode": "shared"}


def test_apply_field_specs_result_skips_unchanged_and_default_values():
    """A caption that maps to the current id, and a value equal to the default, are not writes."""
    from plugin.framework.config import get_config_file_path, save

    path = get_config_file_path()
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
        save(values)

    import os

    before = open(path, encoding="utf-8").read() if os.path.isfile(path) else ""
    with (
        patch("plugin.chatbot.settings_fields.save", side_effect=_spy),
        patch("plugin.chatbot.settings_fields._", side_effect=_fake_gettext),
    ):
        apply_field_specs_result(
            {"scripting__python_session_mode": "Geïsoleerd"},
            specs,
        )
        assert seen == []
        after = open(path, encoding="utf-8").read() if os.path.isfile(path) else ""
        assert after == before

        apply_field_specs_result(
            {"scripting__python_session_mode": "Gedeelde kernel"},
            specs,
        )
    assert seen == [{"scripting.python_session_mode": "shared"}]


def test_apply_field_specs_result_skips_unknown_keys():
    with patch("plugin.chatbot.settings_fields.save") as mock_set:
        apply_field_specs_result({"nope": "x"}, [{"name": "kept", "config_key": "scripting.kept"}])
    mock_set.assert_not_called()


def test_read_settings_control_skips_missing_and_checkbox_is_bool():
    assert read_settings_control(None) is None

    class _Box:
        def supportsService(self, name):
            return False

        def getState(self):
            return 1

    assert read_settings_control(_Box(), {"type": "bool"}) is True


@pytest.mark.parametrize(
    ("init_val", "peer_val", "set_raises", "expected_val", "expected_set_calls"),
    [
        pytest.param(1.0, 42.0, False, 42.0, [42.0], id="commits_peer_text_before_getvalue"),
        pytest.param(42.0, 42.0, False, 42.0, [], id="spun_value_ignores_stale_caption"),
        pytest.param(42, None, False, 42, [], id="without_peer_keeps_getvalue"),
        pytest.param(1.0, 42.0, True, 42.0, [], id="uses_peer_when_setvalue_fails"),
    ],
)
def test_read_settings_control_numeric_peer(init_val, peer_val, set_raises, expected_val, expected_set_calls):
    set_value_calls: list[Any] = []

    class _Peer:
        def getValue(self):
            return peer_val

    class _BaseCtrl:
        def __init__(self):
            self.value = init_val

        def getValue(self):
            return self.value

        def setValue(self, val):
            if set_raises:
                raise RuntimeError("setValue failed")
            set_value_calls.append(val)
            self.value = float(val)

        def getText(self):
            return "1"

    class _PeerCtrl(_BaseCtrl):
        def getPeer(self):
            return _Peer()

    ctrl = _PeerCtrl() if peer_val is not None else _BaseCtrl()
    assert read_settings_control(ctrl, {"type": "int", "name": "count"}) == expected_val
    assert set_value_calls == expected_set_calls


def test_populate_settings_control_sets_string_item_list_and_numeric_value():
    class _Ctrl:
        def __init__(self):
            self.items = []
            self.value = None
            self.text = None

        def getItemCount(self):
            return len(self.items)

        def removeItems(self, pos, count):
            del self.items[pos : pos + count]

        def addItems(self, items, pos):
            self.items[pos:pos] = list(items)

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
    assert ctrl.items == ["Seven"]
    assert ctrl.value == 7.0
    assert ctrl.text is None


def test_call_options_provider_skips_services_when_app_get_services_missing():
    import sys
    import types

    from plugin.chatbot.settings_fields import call_options_provider

    seen: list[object] = []

    def provide(services):
        seen.append(services)
        return ["ok"]

    fake = types.ModuleType("wa_librepy_opts")
    fake.provide = provide
    stub_app = types.ModuleType("plugin.app")
    with patch.dict(sys.modules, {"plugin.app": stub_app, "wa_librepy_opts": fake}):
        out = call_options_provider("wa_librepy_opts:provide")
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
                "options_provider": "plugin.audio.tts_voices:settings_voice_options",
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
        patch("plugin.framework.config.get_config", side_effect=_cfg),
        patch("plugin.framework.i18n.get_active_locale", return_value="de_DE"),
        patch("plugin.app.get_services", return_value=None),
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
                "options_provider": "plugin.audio.tts_voices:missing_voice_options",
                "options": [{"value": "default", "label": "default (System Default)"}],
            }
        },
    }
    with (
        patch("plugin._manifest.MODULES", [manifest]),
        patch("plugin.chatbot.settings_fields.get_config", return_value="default"),
        patch("plugin.app.get_services", return_value=None),
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
        specs = build_module_field_specs("vision", ctx=ctx, control_ids="flat")

    assert len(specs) == 1
    assert specs[0]["name"] == "device"
    assert specs[0]["config_key"] == "vision.device"


def test_populate_settings_control_sets_translated_option_labels():
    class DummyCtrl:
        def __init__(self) -> None:
            self.items: list[str] = []

        def getItemCount(self) -> int:
            return len(self.items)

        def removeItems(self, pos: int, count: int) -> None:
            del self.items[pos : pos + count]

        def addItems(self, items: tuple[str, ...], pos: int) -> None:
            self.items[pos:pos] = list(items)

        def setText(self, _text: str) -> None:
            pass

    ctrl = DummyCtrl()

    field = {
        "name": "insert_mode",
        "options": [
            {"value": "html", "label": "Standard HTML"},
            {"value": "structured", "label": "Structured (layout / cell grid)"},
        ],
        "value": "html",
    }
    populate_settings_control(ctrl, field)
    assert ctrl.items[0]
    assert "Standard HTML" in ctrl.items[0]


def test_apply_module_config_result_stores_select_option_values():
    """A select caption becomes its option id. An id that already matches disk is not written.

    demo.mode is already ``fast``, so OK writes only ``demo.count``. When the
    stored mode is different, the caption ``Fast Mode`` is still saved as ``fast``.
    """
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
    disk = {"demo.count": 1, "demo.mode": "fast"}
    with patch("plugin.chatbot.settings_fields.find_module_manifest", return_value=manifest), \
         patch("plugin.chatbot.settings_fields.get_config", side_effect=lambda key: disk[key]), \
         patch("plugin.chatbot.settings_fields.save") as mock_save:
        specs = build_module_field_specs("demo", ctx=ctx, control_ids="flat")
        apply_field_specs_result({"count": "42", "mode": "Fast Mode"}, specs)
        mock_save.assert_called_once_with({"demo.count": "42"})

        mock_save.reset_mock()
        disk["demo.mode"] = "slow"
        apply_field_specs_result({"count": "42", "mode": "Fast Mode"}, specs)
        mock_save.assert_called_once_with({"demo.count": "42", "demo.mode": "fast"})


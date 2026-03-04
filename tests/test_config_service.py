"""Tests for plugin.modules.core.services.config (ConfigService + ModuleConfigProxy)."""

import json
import os
import tempfile

import pytest

from plugin.modules.core.services.config import (
    ConfigService,
    ConfigAccessError,
    ModuleConfigProxy,
)
from plugin.framework.event_bus import EventBus


@pytest.fixture
def config_dir(tmp_path):
    """Provide a temp dir for config file."""
    return tmp_path


@pytest.fixture
def config_svc(config_dir):
    """ConfigService with a temp config path (bypasses UNO)."""
    svc = ConfigService()
    svc._config_path = str(config_dir / "localwriter.json")
    return svc


@pytest.fixture
def manifest():
    """Sample manifest data."""
    return {
        "mcp": {
            "config": {
                "port": {
                    "type": "int",
                    "default": 8766,
                    "public": True,
                },
                "host": {
                    "type": "string",
                    "default": "localhost",
                    "public": True,
                },
                "ssl_key": {
                    "type": "string",
                    "default": "",
                    "public": False,
                },
            }
        },
        "chatbot": {
            "config": {
                "max_tool_rounds": {
                    "type": "int",
                    "default": 15,
                    "public": False,
                },
            }
        },
    }


class TestDefaults:
    def test_get_returns_default(self, config_svc, manifest):
        config_svc.set_manifest(manifest)
        assert config_svc.get("mcp.port") == 8766
        assert config_svc.get("mcp.host") == "localhost"

    def test_get_returns_none_for_unknown(self, config_svc):
        assert config_svc.get("nonexistent.key") is None

    def test_register_default(self, config_svc):
        config_svc.register_default("custom.key", 42)
        assert config_svc.get("custom.key") == 42


class TestSetGet:
    def test_set_and_get(self, config_svc, manifest):
        config_svc.set_manifest(manifest)
        config_svc.set("mcp.port", 9000)
        assert config_svc.get("mcp.port") == 9000

    def test_set_persists_to_file(self, config_svc, config_dir, manifest):
        config_svc.set_manifest(manifest)
        config_svc.set("mcp.port", 9000)

        with open(config_dir / "localwriter.json") as f:
            data = json.load(f)
        assert data["mcp.port"] == 9000

    def test_remove(self, config_svc, manifest):
        config_svc.set_manifest(manifest)
        config_svc.set("mcp.port", 9000)
        config_svc.remove("mcp.port")
        assert config_svc.get("mcp.port") == 8766  # back to default

    def test_get_dict(self, config_svc, manifest):
        config_svc.set_manifest(manifest)
        config_svc.set("mcp.port", 9000)
        d = config_svc.get_dict()
        assert d["mcp.port"] == 9000


class TestAccessControl:
    def test_read_own_key_ok(self, config_svc, manifest):
        config_svc.set_manifest(manifest)
        assert config_svc.get("mcp.port", caller_module="mcp") == 8766

    def test_read_public_key_ok(self, config_svc, manifest):
        config_svc.set_manifest(manifest)
        assert config_svc.get("mcp.port", caller_module="chatbot") == 8766

    def test_read_private_key_denied(self, config_svc, manifest):
        config_svc.set_manifest(manifest)
        with pytest.raises(ConfigAccessError, match="cannot read private"):
            config_svc.get("mcp.ssl_key", caller_module="chatbot")

    def test_write_own_key_ok(self, config_svc, manifest):
        config_svc.set_manifest(manifest)
        config_svc.set("mcp.port", 9000, caller_module="mcp")
        assert config_svc.get("mcp.port") == 9000

    def test_write_other_key_denied(self, config_svc, manifest):
        config_svc.set_manifest(manifest)
        with pytest.raises(ConfigAccessError, match="cannot write"):
            config_svc.set("mcp.port", 9000, caller_module="chatbot")

    def test_no_caller_no_restriction(self, config_svc, manifest):
        config_svc.set_manifest(manifest)
        assert config_svc.get("mcp.ssl_key") == ""  # no caller = no check


class TestEvents:
    def test_config_changed_event(self, config_svc, manifest):
        config_svc.set_manifest(manifest)
        bus = EventBus()
        config_svc.set_events(bus)

        events = []
        bus.subscribe("config:changed", lambda **kw: events.append(kw))

        config_svc.set("mcp.port", 9000)
        assert len(events) == 1
        assert events[0]["key"] == "mcp.port"
        assert events[0]["value"] == 9000
        assert events[0]["old_value"] == 8766

    def test_no_event_when_value_unchanged(self, config_svc, manifest):
        config_svc.set_manifest(manifest)
        bus = EventBus()
        config_svc.set_events(bus)

        config_svc.set("mcp.port", 8766)  # same as default

        events = []
        bus.subscribe("config:changed", lambda **kw: events.append(kw))
        config_svc.set("mcp.port", 8766)
        assert events == []


class TestModuleConfigProxy:
    def test_auto_prefix(self, config_svc, manifest):
        config_svc.set_manifest(manifest)
        proxy = config_svc.proxy_for("mcp")
        assert proxy.get("port") == 8766

    def test_set_auto_prefix(self, config_svc, manifest):
        config_svc.set_manifest(manifest)
        proxy = config_svc.proxy_for("mcp")
        proxy.set("port", 9000)
        assert proxy.get("port") == 9000

    def test_cross_module_read_public(self, config_svc, manifest):
        config_svc.set_manifest(manifest)
        proxy = config_svc.proxy_for("chatbot")
        assert proxy.get("mcp.port") == 8766

    def test_cross_module_read_private_denied(self, config_svc, manifest):
        config_svc.set_manifest(manifest)
        proxy = config_svc.proxy_for("chatbot")
        with pytest.raises(ConfigAccessError):
            proxy.get("mcp.ssl_key")

    def test_default_fallback(self, config_svc, manifest):
        config_svc.set_manifest(manifest)
        proxy = config_svc.proxy_for("mcp")
        assert proxy.get("nonexistent", default="fallback") == "fallback"

    def test_remove(self, config_svc, manifest):
        config_svc.set_manifest(manifest)
        proxy = config_svc.proxy_for("mcp")
        proxy.set("port", 9000)
        proxy.remove("port")
        assert proxy.get("port") == 8766  # back to default

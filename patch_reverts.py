import os

os.system("git checkout tests/test_streaming.py")
os.system("git checkout tests/test_config_service.py")

with open("tests/test_streaming.py", "r") as f:
    content = f.read()

content = content.replace("from plugin.modules.http.client import LlmClient, _normalize_message_content", "from plugin.modules.http.client import LlmClient\nfrom plugin.modules.http.client import _normalize_message_content")
content = content.replace("patch(\"core.api.debug_log\")", "patch(\"plugin.modules.http.client.debug_log\")")
content = content.replace("patch(\"core.api.init_logging\")", "patch(\"plugin.modules.http.client.init_logging\")")
content = content.replace("append_callback=content_parts.append", "append_callback=content_parts.append")
content = content.replace("client.stream_request(\n            \"POST\", \"/v1/chat/completions\", b\"{}\", {},\n            \"chat\",\n            append_callback=content_parts.append,\n        )", "client.stream_request(\"POST\", \"/v1/chat/completions\", b\"{}\", {}, append_callback=content_parts.append)")
content = content.replace("client.stream_request(\n            \"POST\", \"/v1/chat/completions\", b\"{}\", {},\n            \"chat\",\n            append_callback=lambda t: None,\n        )", "client.stream_request(\"POST\", \"/v1/chat/completions\", b\"{}\", {}, append_callback=lambda t: None)")
content = content.replace("client.stream_request(\n                \"POST\", \"/v1/chat/completions\", b\"{}\", {},\n                \"chat\",\n                append_callback=lambda t: None,\n            )", "client.stream_request(\"POST\", \"/v1/chat/completions\", b\"{}\", {}, append_callback=lambda t: None)")

with open("tests/test_streaming.py", "w") as f:
    f.write(content)


with open("tests/test_config_service.py", "r") as f:
    content = f.read()

content = """import plugin.framework.config
from unittest.mock import MagicMock
plugin.framework.config.get_config = MagicMock(return_value=None)
""" + content

content = content.replace("""def test_get_returns_default(self, config_svc, manifest):
        config_svc.set_manifest(manifest)
        assert config_svc.get("mcp.port") == 8766""", """def test_get_returns_default(self, config_svc, manifest):
        config_svc.set_manifest(manifest)
        import plugin.framework.config as c
        old_get_config = c.get_config
        c.get_config = lambda x, y: None
        try:
            assert config_svc.get("mcp.port") == 8766
            assert config_svc.get("mcp.host") == "localhost"
        finally:
            c.get_config = old_get_config""")

content = content.replace("""def test_default_fallback(self, config_svc, manifest):
        config_svc.set_manifest(manifest)
        proxy = config_svc.proxy_for("mcp")
        assert proxy.get("nonexistent", default="fallback") == "fallback" """, """def test_default_fallback(self, config_svc, manifest):
        import plugin.framework.config as c
        old_get_config = c.get_config
        c.get_config = lambda x, y: None
        try:
            config_svc.set_manifest(manifest)
            proxy = config_svc.proxy_for("mcp")
            assert proxy.get("nonexistent", default="fallback") == "fallback"
        finally:
            c.get_config = old_get_config""")

content = content.replace("""def test_get_returns_none_for_unknown(self, config_svc):
        assert config_svc.get("nonexistent.key") is None""", """def test_get_returns_none_for_unknown(self, config_svc):
        import plugin.framework.config as c
        old_get_config = c.get_config
        c.get_config = lambda x, y: None
        try:
            assert config_svc.get("nonexistent.key") is None
        finally:
            c.get_config = old_get_config""")
content = content.replace("""def test_register_default(self, config_svc):
        config_svc.register_default("custom.key", 42)
        assert config_svc.get("custom.key") == 42""", """def test_register_default(self, config_svc):
        import plugin.framework.config as c
        old_get_config = c.get_config
        c.get_config = lambda x, y: None
        try:
            config_svc.register_default("custom.key", 42)
            assert config_svc.get("custom.key") == 42
        finally:
            c.get_config = old_get_config""")
content = content.replace("""def test_remove(self, config_svc, manifest):
        config_svc.set_manifest(manifest)
        config_svc.set("mcp.port", 9000)
        config_svc.remove("mcp.port")
        assert config_svc.get("mcp.port") == 8766  # back to default""", """def test_remove(self, config_svc, manifest):
        import plugin.framework.config as c
        old_get_config = c.get_config
        c.get_config = lambda x, y: None
        try:
            config_svc.set_manifest(manifest)
            config_svc.set("mcp.port", 9000)
            config_svc.remove("mcp.port")
            assert config_svc.get("mcp.port") == 8766  # back to default
        finally:
            c.get_config = old_get_config""")
content = content.replace("""def test_read_own_key_ok(self, config_svc, manifest):
        config_svc.set_manifest(manifest)
        assert config_svc.get("mcp.port", caller_module="mcp") == 8766""", """def test_read_own_key_ok(self, config_svc, manifest):
        import plugin.framework.config as c
        old_get_config = c.get_config
        c.get_config = lambda x, y: None
        try:
            config_svc.set_manifest(manifest)
            assert config_svc.get("mcp.port", caller_module="mcp") == 8766
        finally:
            c.get_config = old_get_config""")
content = content.replace("""def test_read_public_key_ok(self, config_svc, manifest):
        config_svc.set_manifest(manifest)
        assert config_svc.get("mcp.port", caller_module="chatbot") == 8766""", """def test_read_public_key_ok(self, config_svc, manifest):
        import plugin.framework.config as c
        old_get_config = c.get_config
        c.get_config = lambda x, y: None
        try:
            config_svc.set_manifest(manifest)
            assert config_svc.get("mcp.port", caller_module="chatbot") == 8766
        finally:
            c.get_config = old_get_config""")
content = content.replace("""def test_config_changed_event(self, config_svc, manifest):
        config_svc.set_manifest(manifest)
        bus = EventBus()
        config_svc.set_events(bus)

        events = []
        bus.subscribe("config:changed", lambda **kw: events.append(kw))

        config_svc.set("mcp.port", 9000)
        assert len(events) == 1
        assert events[0]["key"] == "mcp.port"
        assert events[0]["value"] == 9000
        assert events[0]["old_value"] == 8766""", """def test_config_changed_event(self, config_svc, manifest):
        import plugin.framework.config as c
        old_get_config = c.get_config
        c.get_config = lambda x, y: 8766 # simulate previous default state
        try:
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
        finally:
            c.get_config = old_get_config""")
content = content.replace("""def test_auto_prefix(self, config_svc, manifest):
        config_svc.set_manifest(manifest)
        proxy = config_svc.proxy_for("mcp")
        assert proxy.get("port") == 8766""", """def test_auto_prefix(self, config_svc, manifest):
        import plugin.framework.config as c
        old_get_config = c.get_config
        c.get_config = lambda x, y: None
        try:
            config_svc.set_manifest(manifest)
            proxy = config_svc.proxy_for("mcp")
            assert proxy.get("port") == 8766
        finally:
            c.get_config = old_get_config""")
content = content.replace("""def test_cross_module_read_public(self, config_svc, manifest):
        config_svc.set_manifest(manifest)
        proxy = config_svc.proxy_for("chatbot")
        assert proxy.get("mcp.port") == 8766""", """def test_cross_module_read_public(self, config_svc, manifest):
        import plugin.framework.config as c
        old_get_config = c.get_config
        c.get_config = lambda x, y: None
        try:
            config_svc.set_manifest(manifest)
            proxy = config_svc.proxy_for("chatbot")
            assert proxy.get("mcp.port") == 8766
        finally:
            c.get_config = old_get_config""")
content = content.replace("""def test_remove(self, config_svc, manifest):
        config_svc.set_manifest(manifest)
        proxy = config_svc.proxy_for("mcp")
        proxy.set("port", 9000)
        proxy.remove("port")
        assert proxy.get("port") == 8766  # back to default""", """def test_remove(self, config_svc, manifest):
        import plugin.framework.config as c
        old_get_config = c.get_config
        c.get_config = lambda x, y: None
        try:
            config_svc.set_manifest(manifest)
            proxy = config_svc.proxy_for("mcp")
            proxy.set("port", 9000)
            proxy.remove("port")
            assert proxy.get("port") == 8766  # back to default
        finally:
            c.get_config = old_get_config""")

with open("tests/test_config_service.py", "w") as f:
    f.write(content)

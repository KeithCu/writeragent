import pytest
from plugin.framework.config_service import ConfigService, ConfigAccessError
from plugin.framework.event_bus import EventBus

@pytest.fixture
def config_dir(tmp_path):
    "Provide a temp dir for config file."
    return tmp_path

@pytest.fixture
def config_svc(config_dir):
    "ConfigService with a temp config path (bypasses UNO)."
    svc = ConfigService()
    svc._config_path = str((config_dir / 'writeragent.json'))
    return svc

@pytest.fixture
def manifest():
    "Sample manifest data."
    return {'mcp': {'config': {'mcp_port': {'type': 'int', 'default': 18765, 'public': True}, 'host': {'type': 'string', 'default': 'localhost', 'public': True}, 'ssl_key': {'type': 'string', 'default': '', 'public': False}}}, 'chatbot': {'config': {'max_tool_rounds': {'type': 'int', 'default': 15, 'public': False}}}}

class TestDefaults():

    def test_get_returns_default(self, config_svc, manifest):
        config_svc.set_manifest(manifest)
        import plugin.framework.config as c
        old_get_config = c.get_config
        c.get_config = (lambda x, y: None)
        try:
            assert (config_svc.get('mcp.mcp_port') == 18765)
            assert (config_svc.get('mcp.host') == 'localhost')
        finally:
            c.get_config = old_get_config

    def test_get_returns_none_for_unknown(self, config_svc):
        assert (config_svc.get('nonexistent.key') is None)

    def test_register_default(self, config_svc):
        config_svc.register_default('custom.key', 42)
        assert (config_svc.get('custom.key') == 42)

class TestSetGet():

    def test_set_and_get(self, config_svc, manifest):
        config_svc.set_manifest(manifest)
        config_svc.set('mcp.mcp_port', 9000)
        assert (config_svc.get('mcp.mcp_port') == 9000)

    def test_set_persists_to_file(self, config_svc, config_dir, manifest):
        config_svc.set_manifest(manifest)
        config_svc.set('mcp.mcp_port', 9000)
        from plugin.framework.config import parse_config_json_text

        text = (config_dir / 'writeragent.json').read_text(encoding='utf-8')
        data = parse_config_json_text(text)
        assert data is not None
        assert (data['mcp.mcp_port'] == 9000)

    def test_remove(self, config_svc, manifest):
        config_svc.set_manifest(manifest)
        config_svc.set('mcp.mcp_port', 9000)
        config_svc.remove('mcp.mcp_port')
        assert (config_svc.get('mcp.mcp_port') == 18765)

    def test_get_dict(self, config_svc, manifest):
        config_svc.set_manifest(manifest)
        config_svc.set('mcp.mcp_port', 9000)
        d = config_svc.get_dict()
        assert (d['mcp.mcp_port'] == 9000)

    def test_set_corrupt_config_backups_and_writes(self, config_svc, config_dir, manifest):
        corrupt = '{ invalid json '
        config_path = config_dir / 'writeragent.json'
        backup_path = config_dir / 'writeragent.json.bak'
        config_path.write_text(corrupt, encoding='utf-8')
        config_svc.set_manifest(manifest)
        config_svc.set('mcp.mcp_port', 9000)
        assert backup_path.read_text(encoding='utf-8') == corrupt
        from plugin.framework.config import parse_config_json_text

        data = parse_config_json_text(config_path.read_text(encoding='utf-8'))
        assert data is not None
        assert data['mcp.mcp_port'] == 9000

    def test_ai_stt_model_uses_audio_key(self, config_svc):
        from unittest.mock import patch

        with (
            patch("plugin.framework.client.model_fetcher.get_stt_model", return_value="from-dual-read") as mock_get,
            patch("plugin.framework.config_service.set_config") as mock_set,
            patch("plugin.framework.config_service.global_event_bus"),
        ):
            assert config_svc.get("ai.stt_model") == "from-dual-read"
            config_svc.set("ai.stt_model", "whisper-1")
        mock_get.assert_called()
        mock_set.assert_called_once_with("audio.stt_model", "whisper-1", event_key="ai.stt_model")

class TestAccessControl():

    def test_read_own_key_ok(self, config_svc, manifest):
        config_svc.set_manifest(manifest)
        assert (config_svc.get('mcp.mcp_port', caller_module='mcp') == 18765)

    def test_read_public_key_ok(self, config_svc, manifest):
        config_svc.set_manifest(manifest)
        assert (config_svc.get('mcp.mcp_port', caller_module='chatbot') == 18765)

    def test_read_private_key_denied(self, config_svc, manifest):
        config_svc.set_manifest(manifest)
        with pytest.raises(ConfigAccessError, match='cannot read private'):
            config_svc.get('mcp.ssl_key', caller_module='chatbot')

    def test_write_own_key_ok(self, config_svc, manifest):
        config_svc.set_manifest(manifest)
        config_svc.set('mcp.mcp_port', 9000, caller_module='mcp')
        assert (config_svc.get('mcp.mcp_port') == 9000)

    def test_write_other_key_denied(self, config_svc, manifest):
        config_svc.set_manifest(manifest)
        with pytest.raises(ConfigAccessError, match='cannot write'):
            config_svc.set('mcp.mcp_port', 9000, caller_module='chatbot')

    def test_no_caller_no_restriction(self, config_svc, manifest):
        config_svc.set_manifest(manifest)
        assert (config_svc.get('mcp.ssl_key') == '')

class TestEvents():

    def test_config_changed_event(self, config_svc, manifest):
        config_svc.set_manifest(manifest)
        bus = EventBus()
        config_svc.set_events(bus)
        events = []
        bus.subscribe('config:changed', (lambda **kw: events.append(kw)))
        config_svc.set('mcp.mcp_port', 9000)
        assert (len(events) == 1)
        assert (events[0]['key'] == 'mcp.mcp_port')
        assert (events[0]['value'] == 9000)
        assert (events[0]['old_value'] == 18765)

    def test_no_event_when_value_unchanged(self, config_svc, manifest):
        config_svc.set_manifest(manifest)
        bus = EventBus()
        config_svc.set_events(bus)
        config_svc.set('mcp.mcp_port', 18765)
        events = []
        bus.subscribe('config:changed', (lambda **kw: events.append(kw)))
        config_svc.set('mcp.mcp_port', 18765)
        assert (events == [])

class TestModuleConfigProxy():

    def test_auto_prefix(self, config_svc, manifest):
        config_svc.set_manifest(manifest)
        proxy = config_svc.proxy_for('mcp')
        assert (proxy.get('mcp_port') == 18765)

    def test_set_auto_prefix(self, config_svc, manifest):
        config_svc.set_manifest(manifest)
        proxy = config_svc.proxy_for('mcp')
        proxy.set('mcp_port', 9000)
        assert (proxy.get('mcp_port') == 9000)

    def test_cross_module_read_public(self, config_svc, manifest):
        config_svc.set_manifest(manifest)
        proxy = config_svc.proxy_for('chatbot')
        assert (proxy.get('mcp.mcp_port') == 18765)

    def test_cross_module_read_private_denied(self, config_svc, manifest):
        config_svc.set_manifest(manifest)
        proxy = config_svc.proxy_for('chatbot')
        with pytest.raises(ConfigAccessError):
            proxy.get('mcp.ssl_key')

    def test_default_fallback(self, config_svc, manifest):
        import plugin.framework.config as c
        old_get_config = c.get_config
        c.get_config = (lambda x, y: None)
        try:
            config_svc.set_manifest(manifest)
            proxy = config_svc.proxy_for('mcp')
            assert (proxy.get('nonexistent', default='fallback') == 'fallback')
        finally:
            c.get_config = old_get_config

    def test_proxy_remove(self, config_svc, manifest):
        "Remove via ModuleConfigProxy (proxy.remove)."
        import plugin.framework.config as c
        old_get_config = c.get_config
        c.get_config = (lambda x, y: None)
        try:
            config_svc.set_manifest(manifest)
            proxy = config_svc.proxy_for('mcp')
            proxy.set('mcp_port', 9000)
            proxy.remove('mcp_port')
            assert (proxy.get('mcp_port') == 18765)
        finally:
            c.get_config = old_get_config


def test_set_ai_endpoint_unresolved_raises(config_svc) -> None:
    from plugin.framework.errors import ConfigError

    with pytest.raises(ConfigError) as err:
        config_svc.set("ai.endpoint", "")
    assert err.value.code == "CONFIG_INVALID_ENDPOINT"


def test_set_ai_endpoint_none_is_not_stored(config_svc) -> None:
    """None must not be stringified into a stored endpoint named None."""
    from unittest.mock import patch

    from plugin.framework.errors import ConfigError

    with patch("plugin.framework.config_service.set_config") as mock_set:
        with pytest.raises(ConfigError) as err:
            config_svc.set("ai.endpoint", None)
    assert err.value.code == "CONFIG_INVALID_ENDPOINT"
    mock_set.assert_not_called()


def test_bootstrap_loads_public_config_flags(monkeypatch) -> None:
    """Production bootstrap must call ConfigService.initialize.

    What was wrong: only tests called initialize(), so _manifest stayed
    empty and a cross-module read of a module.yaml public key was denied.
    """
    import plugin.main as main
    from plugin.framework.errors import ConfigError
    from plugin.framework.event_bus import EventBus

    monkeypatch.setenv("WRITERAGENT_TESTING", "1")
    monkeypatch.setattr("plugin.framework.thread_guard.on_main_thread", lambda: True)
    monkeypatch.setattr("plugin.framework.uno_context.get_ctx", lambda: None)
    monkeypatch.setattr("plugin.framework.config.init_config", lambda ctx=None: "")
    monkeypatch.setattr("plugin.framework.i18n.init_i18n", lambda ctx: None)
    monkeypatch.setattr("plugin.framework.module_base.ModuleLoader.load_modules", lambda services: [])
    monkeypatch.setattr(main, "_register_core_handlers", lambda: None)
    monkeypatch.setattr(main, "set_package_extension_id", lambda extension_id: None)
    monkeypatch.setattr("plugin.framework.event_bus.get_event_bus", lambda: EventBus())

    previous = (main._initialized, main._services, main._tools, list(main._modules))
    main._initialized = False
    main._services = None
    main._tools = None
    main._modules = []
    try:
        main.bootstrap(None)
        config = main._services.get("config")
        assert config._manifest["mcp.mcp_port"]["public"] is True

        def missing(_key: str) -> None:
            raise ConfigError("missing")

        monkeypatch.setattr("plugin.framework.config_service.get_config", missing)
        assert config.get("mcp.mcp_port", caller_module="chatbot") == 18765
        with pytest.raises(ConfigAccessError, match="cannot read private"):
            config.get("mcp.cors_allow_private_origins", caller_module="chatbot")
    finally:
        main._initialized, main._services, main._tools = previous[0], previous[1], previous[2]
        main._modules[:] = previous[3]


def test_dummy_impl_decorator_annotates_cls() -> None:
    """Nested class decorator must annotate `cls` for reportMissingParameterType."""
    from typing import Any, get_type_hints

    from plugin.framework.config_service import _dummy_impl

    decorator = _dummy_impl("unused")
    hints = get_type_hints(decorator)
    assert hints["cls"] == type[Any]
    assert decorator(int) is int


def test_initialize_applies_public_flags_without_set_manifest(config_svc, manifest, monkeypatch) -> None:
    """Bootstrap never calls set_manifest. initialize() must load public flags."""
    from plugin.framework.errors import ConfigError

    modules = [{"name": mod_name, "config": mod_data.get("config", {})} for mod_name, mod_data in manifest.items()]
    monkeypatch.setattr("plugin.framework.config_service.get_manifest_modules", lambda: modules)
    config_svc.initialize(None)

    def missing(_key: str):
        raise ConfigError("missing")

    monkeypatch.setattr("plugin.framework.config_service.get_config", missing)
    assert config_svc.get("mcp.mcp_port", caller_module="chatbot") == 18765
    with pytest.raises(ConfigAccessError, match="cannot read private"):
        config_svc.get("mcp.ssl_key", caller_module="chatbot")


def test_get_empty_string_false_and_zero_are_real_values(config_svc, monkeypatch) -> None:
    config_svc._config_path = None
    stored = {"note": "", "flag": False, "count": 0, "missing": None}

    monkeypatch.setattr("plugin.framework.config_service.get_config", lambda key: stored[key])
    assert config_svc.get("note", default="fallback") == ""
    assert config_svc.get("flag", default=True) is False
    assert config_svc.get("count", default=5) == 0
    assert config_svc.get("missing", default="fallback") == "fallback"


def test_set_does_not_emit_when_replace_fails(config_svc, manifest) -> None:
    from unittest.mock import patch

    from plugin.framework.errors import ConfigError

    config_svc.set_manifest(manifest)
    bus = EventBus()
    config_svc.set_events(bus)
    events = []
    bus.subscribe("config:changed", lambda **kw: events.append(kw))
    with patch("plugin.framework.config._write_config_file", side_effect=OSError("disk full")):
        with pytest.raises(ConfigError) as err:
            config_svc.set("mcp.mcp_port", 9000)
    assert err.value.code == "CONFIG_SAVE_ERROR"
    assert events == []


def test_model_fetcher_is_not_a_top_level_import() -> None:
    import ast
    import inspect

    import plugin.framework.config_service as config_service_mod
    import plugin.framework.client.model_fetcher as model_fetcher_mod

    tree = ast.parse(inspect.getsource(config_service_mod))
    top_level = [node for node in tree.body if isinstance(node, ast.ImportFrom) and node.module and "model_fetcher" in node.module]
    assert top_level == []
    assert "config_service" not in inspect.getsource(model_fetcher_mod)

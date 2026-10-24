import ast
import inspect
import pytest
import plugin.framework.client.model_fetcher as model_fetcher_mod
import plugin.framework.config_service as config_service_mod
from plugin.framework.config_service import (
    ConfigAccessError,
    ConfigService,
)

@pytest.fixture
def config_dir(tmp_path):
    "Provide a temp dir for config file."
    return tmp_path

@pytest.fixture
def config_svc(config_dir, monkeypatch):
    "ConfigService using config_dir for config path."
    from plugin.framework.config import _invalidate_config_cache

    monkeypatch.setattr("plugin.framework.config._config_path", lambda: str(config_dir / "writeragent.json"))
    _invalidate_config_cache()
    svc = ConfigService()
    return svc


class TestDefaults():

    def test_get_returns_default(self, config_svc):
        import plugin.framework.config as c
        old_get_config = c.get_config
        c.get_config = (lambda x, y=None: None)
        try:
            assert (config_svc.get('mcp.mcp_port') == 18765)
        finally:
            c.get_config = old_get_config

    def test_get_returns_none_for_unknown(self, config_svc):
        assert (config_svc.get('nonexistent.key') is None)


class TestSetGet():

    def test_set_and_get(self, config_svc):
        config_svc.set('mcp.mcp_port', 9000)
        assert (config_svc.get('mcp.mcp_port') == 9000)

    def test_set_persists_to_file(self, config_svc, config_dir):
        config_svc.set('mcp.mcp_port', 9000)
        from plugin.framework.config import parse_config_json_text

        text = (config_dir / 'writeragent.json').read_text(encoding='utf-8')
        data = parse_config_json_text(text)
        assert data is not None
        assert (data['mcp.mcp_port'] == 9000)

    def test_remove(self, config_svc):
        config_svc.set('mcp.mcp_port', 9000)
        config_svc.remove('mcp.mcp_port')
        assert (config_svc.get('mcp.mcp_port') == 18765)

    def test_get_dict(self, config_svc):
        config_svc.set('mcp.mcp_port', 9000)
        d = config_svc.get_dict()
        assert (d['mcp.mcp_port'] == 9000)

    def test_set_corrupt_config_does_not_overwrite(self, config_svc, config_dir):
        corrupt = '{ invalid json '
        config_path = config_dir / 'writeragent.json'
        backup_path = config_dir / 'writeragent.json.bak'
        config_path.write_text(corrupt, encoding='utf-8')
        from plugin.framework.errors import ConfigError

        with pytest.raises(ConfigError) as err:
            config_svc.set('mcp.mcp_port', 9000)
        assert err.value.code == 'CONFIG_INVALID_FORMAT'
        assert config_path.read_text(encoding='utf-8') == corrupt
        assert backup_path.read_text(encoding='utf-8') == corrupt
        from plugin.framework.config import parse_config_json_text

        data = parse_config_json_text(config_path.read_text(encoding='utf-8'))
        assert data is None


class TestAccessControl():

    @pytest.mark.parametrize(
        "caller_module",
        [
            pytest.param('mcp', id="test_read_own_key_ok"),
            pytest.param('chatbot', id="test_read_public_key_ok"),
        ],
    )
    def test_read_own_key_ok(self, config_svc, caller_module):
        assert (config_svc.get('mcp.mcp_port', caller_module=caller_module) == 18765)

    def test_read_private_key_denied(self, config_svc):
        with pytest.raises(ConfigAccessError, match='cannot read private'):
            config_svc.get('mcp.cors_allow_private_origins', caller_module='chatbot')

    def test_write_own_key_ok(self, config_svc):
        config_svc.set('mcp.mcp_port', 9000, caller_module='mcp')
        assert (config_svc.get('mcp.mcp_port') == 9000)

    def test_write_other_key_denied(self, config_svc):
        with pytest.raises(ConfigAccessError, match='cannot write'):
            config_svc.set('mcp.mcp_port', 9000, caller_module='chatbot')

    def test_no_caller_no_restriction(self, config_svc):
        assert (config_svc.get('mcp.mcp_port') == 18765)


class TestEvents():

    def test_config_changed_event(self, config_svc):
        from unittest.mock import patch
        from plugin.framework.event_bus import global_event_bus

        with patch.object(global_event_bus, "emit") as mock_emit:
            config_svc.set('mcp.mcp_port', 9000)
            assert mock_emit.call_count == 1
            call_args = mock_emit.call_args
            assert call_args[0][0] == 'config:changed'
            assert call_args.kwargs['key'] == 'mcp.mcp_port'
            assert call_args.kwargs['value'] == 9000
            assert call_args.kwargs['old_value'] == 18765

    def test_no_event_when_value_unchanged(self, config_svc):
        from unittest.mock import patch
        from plugin.framework.event_bus import global_event_bus

        with patch.object(global_event_bus, "emit") as mock_emit:
            config_svc.set('mcp.mcp_port', 18765)
            mock_emit.assert_not_called()
            config_svc.set('mcp.mcp_port', 18765)
            mock_emit.assert_not_called()


class TestModuleConfigProxy():

    @pytest.mark.parametrize(
        "value, value_2",
        [
            pytest.param('mcp', 'mcp_port', id="test_auto_prefix"),
            pytest.param('chatbot', 'mcp.mcp_port', id="test_cross_module_read_public"),
        ],
    )
    def test_auto_prefix(self, config_svc, value, value_2):
        proxy = config_svc.proxy_for(value)
        assert (proxy.get(value_2) == 18765)

    def test_set_auto_prefix(self, config_svc):
        proxy = config_svc.proxy_for('mcp')
        proxy.set('mcp_port', 9000)
        assert (proxy.get('mcp_port') == 9000)

    def test_cross_module_read_private_denied(self, config_svc):
        proxy = config_svc.proxy_for('chatbot')
        with pytest.raises(ConfigAccessError):
            proxy.get('mcp.cors_allow_private_origins')

    def test_default_fallback(self, config_svc):
        import plugin.framework.config as c
        old_get_config = c.get_config
        c.get_config = (lambda x, y=None: None)
        try:
            proxy = config_svc.proxy_for('mcp')
            assert (proxy.get('nonexistent', default='fallback') == 'fallback')
        finally:
            c.get_config = old_get_config

    def test_proxy_remove(self, config_svc):
        "Remove via ModuleConfigProxy (proxy.remove)."
        import plugin.framework.config as c
        old_get_config = c.get_config
        c.get_config = (lambda x, y=None: None)
        try:
            proxy = config_svc.proxy_for('mcp')
            proxy.set('mcp_port', 9000)
            proxy.remove('mcp_port')
            assert (proxy.get('mcp_port') == 18765)
        finally:
            c.get_config = old_get_config


def test_bootstrap_loads_public_config_flags(monkeypatch) -> None:
    """Production bootstrap must make public config flags available."""
    import plugin.main as main
    from plugin.main import BootstrapState
    from plugin.framework.config_schema import get_config_schema
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

    previous = (
        main._bootstrap_state,
        main._services,
        main._tools,
        list(main._modules),
        getattr(main, "_bootstrap_error", None),
    )
    main._bootstrap_state = BootstrapState.UNINITIALIZED
    main._services = None
    main._tools = None
    main._modules = []
    main._bootstrap_error = None
    try:
        main.bootstrap(None)
        config = main._services.get("config")
        schema = get_config_schema("mcp.mcp_port")
        assert schema is not None and schema.get("public") is True

        def missing(_key: str) -> None:
            raise ConfigError("missing")

        monkeypatch.setattr("plugin.framework.config_service.get_config", missing)
        assert config.get("mcp.mcp_port", caller_module="chatbot") == 18765
        with pytest.raises(ConfigAccessError, match="cannot read private"):
            config.get("mcp.cors_allow_private_origins", caller_module="chatbot")
    finally:
        main._bootstrap_state, main._services, main._tools = previous[0], previous[1], previous[2]
        main._modules[:] = previous[3]
        main._bootstrap_error = previous[4]


def test_get_empty_string_false_and_zero_are_real_values(config_svc, monkeypatch) -> None:
    stored = {"note": "", "flag": False, "count": 0, "missing": None}

    monkeypatch.setattr("plugin.framework.config_service.get_config", lambda key: stored[key])
    assert config_svc.get("note", default="fallback") == ""
    assert config_svc.get("flag", default=True) is False
    assert config_svc.get("count", default=5) == 0
    assert config_svc.get("missing", default="fallback") == "fallback"


def test_set_does_not_emit_when_replace_fails(config_svc) -> None:
    from unittest.mock import patch
    from plugin.framework.event_bus import global_event_bus
    from plugin.framework.errors import ConfigError

    with patch("plugin.framework.config._write_config_file", side_effect=OSError("disk full")):
        with patch.object(global_event_bus, "emit") as mock_emit:
            with pytest.raises(ConfigError) as err:
                config_svc.set("mcp.mcp_port", 9000)
            mock_emit.assert_not_called()
    assert err.value.code == "CONFIG_SAVE_ERROR"


def test_model_fetcher_is_not_a_top_level_import() -> None:
    tree = ast.parse(inspect.getsource(config_service_mod))
    top_level = [node for node in tree.body if isinstance(node, ast.ImportFrom) and node.module and "model_fetcher" in node.module]
    assert top_level == []
    assert "config_service" not in inspect.getsource(model_fetcher_mod)


import logging
from unittest.mock import MagicMock, patch

from plugin.framework.module_base import ModuleBase, ModuleLoader
from plugin.framework.module_graph import topo_sort


def test_topo_sort():
    modules = [{'name': 'a', 'requires': ['b', 'c']}, {'name': 'b', 'requires': ['c']}, {'name': 'c', 'requires': []}, {'name': 'core', 'requires': []}]
    order = topo_sort(modules)
    names = [m['name'] for m in order]
    assert (names[0] == 'core')
    assert (names.index('c') < names.index('b'))
    assert (names.index('b') < names.index('a'))
    assert (names.index('c') < names.index('a'))


def test_topo_sort_cycle_raises_config_error():
    from plugin.framework.errors import ConfigError

    modules = [
        {"name": "a", "requires": ["b"]},
        {"name": "b", "requires": ["a"]},
    ]
    try:
        topo_sort(modules)
        raise AssertionError("expected ConfigError")
    except ConfigError as e:
        assert "Cyclic" in str(e.message) or "Cyclic" in str(e)


def test_topo_sort_with_provides_services():
    modules = [{'name': 'consumer', 'requires': ['service_a']}, {'name': 'provider', 'provides_services': ['service_a']}]
    order = topo_sort(modules)
    names = [m['name'] for m in order]
    assert (names.index('provider') < names.index('consumer'))

@patch('plugin.framework.module_base.ModuleLoader.load_manifest')
def test_load_modules(mock_load_manifest, caplog):
    mock_load_manifest.return_value = [{'name': 'core'}, {'name': 'test_module'}]
    import types
    mock_module = types.ModuleType('plugin.test_module')

    class ModuleBase():
        pass

    class TestModule(ModuleBase):

        def __init__(self):
            self.name = ''

        def initialize(self, registry):
            pass
    TestModule.__name__ = 'TestModule'
    ModuleBase.__name__ = 'ModuleBase'
    mock_module.TestModule = TestModule
    import os
    with patch('importlib.import_module', return_value=mock_module), patch.object(os.path, 'isdir', return_value=True):
        import inspect
        original_isclass = inspect.isclass

        def fake_isclass(obj):
            if (getattr(obj, '__name__', '') == 'TestModule'):
                return True
            return original_isclass(obj)
        with patch.object(inspect, 'isclass', side_effect=fake_isclass):
            with caplog.at_level(logging.WARNING, logger='writeragent.module_base'):
                modules = ModuleLoader.load_modules({})
        assert (len(modules) == 1)
        assert (modules[0].name == 'test_module')
        assert not any(('ModuleBase subclasses' in record.message) for record in caplog.records)

@patch('plugin.framework.module_base.ModuleLoader.load_manifest')
def test_load_modules_warns_when_two_subclasses(mock_load_manifest, caplog):
    mock_load_manifest.return_value = [{'name': 'core'}, {'name': 'test_module'}]
    import types
    mock_module = types.ModuleType('plugin.test_module')

    class ModuleBase():
        pass

    class AlphaModule(ModuleBase):

        def initialize(self, registry):
            pass

    class ZetaModule(ModuleBase):

        def initialize(self, registry):
            pass
    ModuleBase.__name__ = 'ModuleBase'
    mock_module.AlphaModule = AlphaModule
    mock_module.ZetaModule = ZetaModule
    import os
    with patch('importlib.import_module', return_value=mock_module), patch.object(os.path, 'isdir', return_value=True):
        with caplog.at_level(logging.WARNING, logger='writeragent.module_base'):
            modules = ModuleLoader.load_modules({})
    assert (len(modules) == 1)
    assert (type(modules[0]).__name__ == 'AlphaModule')
    assert any(('exposes 2 ModuleBase subclasses' in record.message) for record in caplog.records)
    assert any(('AlphaModule' in record.message and 'ZetaModule' in record.message) for record in caplog.records)


def _module_package(class_obj):
    import types

    package = types.ModuleType("plugin." + class_obj.__name__)
    setattr(package, class_obj.__name__, class_obj)
    return package


@patch('plugin.framework.module_base.ModuleLoader.load_manifest')
def test_load_modules_shuts_down_when_initialize_fails(mock_load_manifest, caplog):
    """A failed initialize must release what the module registered, then be omitted.

    The failure stays in the log, and a later module still loads.
    """
    mock_load_manifest.return_value = [{'name': 'core'}, {'name': 'partial'}, {'name': 'later'}]
    from plugin.framework.event_bus import EventBus

    class ModuleBase:
        pass

    bus = EventBus()
    registry = {'events': bus, 'services': {}}
    pings = []

    class PartialModule(ModuleBase):
        def initialize(self, services):
            services['services']['partial'] = self

            def on_ping():
                pings.append('ping')

            self._on_ping = on_ping
            services['events'].subscribe('ping', on_ping)
            raise RuntimeError('init failed')

        def shutdown(self):
            pings.append('shutdown')
            self_services = registry['services']
            self_services.pop('partial', None)
            registry['events'].unsubscribe('ping', self._on_ping)

    class LaterModule(ModuleBase):
        def initialize(self, services):
            pings.append('later')

    packages = {
        'plugin.partial': _module_package(PartialModule),
        'plugin.later': _module_package(LaterModule),
    }
    import os

    def fake_import(path):
        return packages[path]

    with patch('importlib.import_module', side_effect=fake_import), patch.object(os.path, 'isdir', return_value=True):
        with caplog.at_level(logging.ERROR, logger='writeragent.module_base'):
            modules = ModuleLoader.load_modules(registry)
    assert len(modules) == 1
    assert type(modules[0]).__name__ == 'LaterModule'
    assert pings == ['shutdown', 'later']
    assert 'partial' not in registry['services']
    registry['events'].emit('ping')
    assert pings == ['shutdown', 'later']
    failed = [record for record in caplog.records if record.message == 'Failed to load module partial']
    assert len(failed) == 1
    assert failed[0].exc_info is not None
    assert failed[0].exc_info[1].args == ('init failed',)


@patch('plugin.framework.module_base.ModuleLoader.load_manifest')
def test_load_modules_logs_initialize_failure_when_shutdown_fails(mock_load_manifest, caplog):
    mock_load_manifest.return_value = [{'name': 'core'}, {'name': 'partial'}, {'name': 'later'}]
    started = []

    class ModuleBase:
        pass

    class PartialModule(ModuleBase):
        def initialize(self, services):
            raise RuntimeError('init failed')

        def shutdown(self):
            raise RuntimeError('shutdown failed')

    class LaterModule(ModuleBase):
        def initialize(self, services):
            started.append('later')

    packages = {
        'plugin.partial': _module_package(PartialModule),
        'plugin.later': _module_package(LaterModule),
    }
    import os

    def fake_import(path):
        return packages[path]

    with patch('importlib.import_module', side_effect=fake_import), patch.object(os.path, 'isdir', return_value=True):
        with caplog.at_level(logging.ERROR, logger='writeragent.module_base'):
            modules = ModuleLoader.load_modules({})
    assert len(modules) == 1
    assert started == ['later']
    messages = [record.message for record in caplog.records]
    assert 'Failed to load module partial' in messages
    assert 'Module partial failed during shutdown after initialize failed' in messages


class MyModule(ModuleBase):
    name = 'my_module'

def test_module_base_lifecycle_methods():
    mod = MyModule()
    services = MagicMock()
    mod.initialize(services)
    mod.start_background(services)
    mod.shutdown()
    mod.on_action('some_action')


def test_core_and_writer_manifests_omit_format_service():
    import yaml
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[2]
    core_yaml = yaml.safe_load((repo_root / "plugin" / "framework" / "module.yaml").read_text(encoding="utf-8"))
    writer_yaml = yaml.safe_load((repo_root / "plugin" / "writer" / "module.yaml").read_text(encoding="utf-8"))

    assert core_yaml.get("provides_services") == ["document", "config", "events"]
    assert writer_yaml.get("requires") == ["document", "config", "events"]

def test_get_menu_methods():
    mod = MyModule()
    assert (mod.get_menu_text('some_action') is None)
    assert (mod.get_menu_icon('some_action') is None)

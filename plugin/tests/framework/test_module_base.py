
import pytest
from unittest.mock import patch
from plugin.framework.module_base import ModuleLoader
from unittest.mock import MagicMock
from plugin.framework.module_base import ModuleBase

def test_topo_sort():
    modules = [{'name': 'a', 'requires': ['b', 'c']}, {'name': 'b', 'requires': ['c']}, {'name': 'c', 'requires': []}, {'name': 'core', 'requires': []}]
    order = ModuleLoader.topo_sort(modules)
    names = [m['name'] for m in order]
    assert (names[0] == 'core')
    assert (names.index('c') < names.index('b'))
    assert (names.index('b') < names.index('a'))
    assert (names.index('c') < names.index('a'))

def test_topo_sort_with_provides_services():
    modules = [{'name': 'consumer', 'requires': ['service_a']}, {'name': 'provider', 'provides_services': ['service_a']}]
    order = ModuleLoader.topo_sort(modules)
    names = [m['name'] for m in order]
    assert (names.index('provider') < names.index('consumer'))

@patch('plugin.framework.module_base.ModuleLoader.load_manifest')
def test_load_modules(mock_load_manifest):
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
        import sys
        import inspect
        original_isclass = inspect.isclass

        def fake_isclass(obj):
            if (getattr(obj, '__name__', '') == 'TestModule'):
                return True
            return original_isclass(obj)
        with patch.object(inspect, 'isclass', side_effect=fake_isclass):
            modules = ModuleLoader.load_modules({})
        assert (len(modules) == 1)
        assert (modules[0].name == 'test_module')

class MyModule(ModuleBase):
    name = 'my_module'

def test_module_base_lifecycle_methods():
    mod = MyModule()
    services = MagicMock()
    mod.initialize(services)
    mod.start(services)
    mod.start_background(services)
    mod.shutdown()
    mod.on_action('some_action')

def test_get_menu_methods():
    mod = MyModule()
    assert (mod.get_menu_text('some_action') is None)
    assert (mod.get_menu_icon('some_action') is None)

def test_load_dialog():
    mod = MyModule()
    import sys
    mock_dialogs = MagicMock()
    mock_dialogs.load_module_dialog.return_value = 'dialog_instance'
    original = sys.modules.get('plugin.chatbot.dialogs')
    sys.modules['plugin.chatbot.dialogs'] = mock_dialogs
    try:
        result = mod.load_dialog('my_dialog')
        mock_dialogs.load_module_dialog.assert_called_once_with('my_module', 'my_dialog')
        assert (result == 'dialog_instance')
    finally:
        if original:
            sys.modules['plugin.chatbot.dialogs'] = original
        else:
            del sys.modules['plugin.chatbot.dialogs']

def test_load_framework_dialog():
    mod = MyModule()
    import sys
    mock_dialogs = MagicMock()
    mock_dialogs.load_framework_dialog.return_value = 'fw_dialog_instance'
    original = sys.modules.get('plugin.chatbot.dialogs')
    sys.modules['plugin.chatbot.dialogs'] = mock_dialogs
    try:
        result = mod.load_framework_dialog('fw_dialog')
        mock_dialogs.load_framework_dialog.assert_called_once_with('fw_dialog')
        assert (result == 'fw_dialog_instance')
    finally:
        if original:
            sys.modules['plugin.chatbot.dialogs'] = original
        else:
            del sys.modules['plugin.chatbot.dialogs']

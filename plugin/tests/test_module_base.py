from unittest.mock import MagicMock
from plugin.framework.module_base import ModuleBase

class MyModule(ModuleBase):
    name = "my_module"

def test_module_base_lifecycle_methods():
    mod = MyModule()
    services = MagicMock()

    # Defaults should not raise exceptions
    mod.initialize(services)
    mod.start(services)
    mod.start_background(services)
    mod.shutdown()
    mod.on_action("some_action")

def test_get_menu_methods():
    mod = MyModule()

    # By default these return None
    assert mod.get_menu_text("some_action") is None
    assert mod.get_menu_icon("some_action") is None

def test_load_dialog():
    mod = MyModule()

    import sys
    mock_dialogs = MagicMock()
    mock_dialogs.load_module_dialog.return_value = "dialog_instance"

    # Temporarily mock the module
    original = sys.modules.get('plugin.framework.dialogs')
    sys.modules['plugin.framework.dialogs'] = mock_dialogs

    try:
        result = mod.load_dialog("my_dialog")
        mock_dialogs.load_module_dialog.assert_called_once_with("my_module", "my_dialog")
        assert result == "dialog_instance"
    finally:
        if original:
            sys.modules['plugin.framework.dialogs'] = original
        else:
            del sys.modules['plugin.framework.dialogs']

def test_load_framework_dialog():
    mod = MyModule()

    import sys
    mock_dialogs = MagicMock()
    mock_dialogs.load_framework_dialog.return_value = "fw_dialog_instance"

    # Temporarily mock the module
    original = sys.modules.get('plugin.framework.dialogs')
    sys.modules['plugin.framework.dialogs'] = mock_dialogs

    try:
        result = mod.load_framework_dialog("fw_dialog")
        mock_dialogs.load_framework_dialog.assert_called_once_with("fw_dialog")
        assert result == "fw_dialog_instance"
    finally:
        if original:
            sys.modules['plugin.framework.dialogs'] = original
        else:
            del sys.modules['plugin.framework.dialogs']

import pytest
from plugin.framework.module_base import ModuleBase

class DummyModule(ModuleBase):
    name = "dummy"

def test_module_base_lifecycle_methods_do_not_crash():
    mod = DummyModule()

    # Passing None for services should be fine since base methods are empty and don't do anything
    mod.initialize(None)
    mod.start(None)
    mod.start_background(None)
    mod.shutdown()

    mod.on_action("some_action")

    assert mod.get_menu_text("some_action") is None
    assert mod.get_menu_icon("some_action") is None

# we don't test load_dialog and load_framework_dialog since they import dialogs which heavily depends on UNO

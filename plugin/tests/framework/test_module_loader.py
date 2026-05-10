import pytest
from unittest.mock import patch
from plugin.framework.module_base import ModuleLoader

def test_topo_sort():
    modules = [
        {"name": "a", "requires": ["b", "c"]},
        {"name": "b", "requires": ["c"]},
        {"name": "c", "requires": []},
        {"name": "core", "requires": []},
    ]

    order = ModuleLoader.topo_sort(modules)
    names = [m["name"] for m in order]

    # "core" should always be first
    assert names[0] == "core"

    # Dependency sorting checks
    assert names.index("c") < names.index("b")
    assert names.index("b") < names.index("a")
    assert names.index("c") < names.index("a")

def test_topo_sort_with_provides_services():
    modules = [
        {"name": "consumer", "requires": ["service_a"]},
        {"name": "provider", "provides_services": ["service_a"]},
    ]

    order = ModuleLoader.topo_sort(modules)
    names = [m["name"] for m in order]

    assert names.index("provider") < names.index("consumer")

@patch("plugin.framework.module_base.ModuleLoader.load_manifest")
def test_load_modules(mock_load_manifest):
    mock_load_manifest.return_value = [
        {"name": "core"},
        {"name": "test_module"},
    ]

    import types
    mock_module = types.ModuleType("plugin.test_module")

    class ModuleBase:
        pass

    class TestModule(ModuleBase):
        def __init__(self):
            self.name = ""
        def initialize(self, registry):
            pass

    TestModule.__name__ = "TestModule"
    ModuleBase.__name__ = "ModuleBase"

    mock_module.TestModule = TestModule

    # Actually `os` is imported at the top of the module, so we must patch `plugin.framework.module_base.os.path.isdir`
    # However we also need to avoid mock trying to patch a missing property.
    import os
    with patch("importlib.import_module", return_value=mock_module), \
         patch.object(os.path, "isdir", return_value=True):

        # We need to ensure inspect.isclass returns True for TestModule inside load_modules
        import sys
        import inspect
        original_isclass = inspect.isclass

        def fake_isclass(obj):
            if getattr(obj, "__name__", "") == "TestModule":
                return True
            return original_isclass(obj)

        with patch.object(inspect, "isclass", side_effect=fake_isclass):
            modules = ModuleLoader.load_modules({})

        assert len(modules) == 1
        assert modules[0].name == "test_module"

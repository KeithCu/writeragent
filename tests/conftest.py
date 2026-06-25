import os

# Layer A thread guard defaults on in non-release bundles; keep headless pytest stable.
os.environ.setdefault("WRITERAGENT_UNO_THREAD_GUARD", "0")

import sys
import types
from unittest.mock import MagicMock, patch

import pytest

from plugin.framework.i18n import _


def pytest_collection_modifyitems(config, items):
    """Filter out tests marked for the native runner so they don't clutter the skipped count."""
    # This ensures that 'skipped' in pytest output only refers to actually disabled tests.
    def is_native(item):
        # 1. Check for the @native_test decorator attribute on the function
        func = getattr(item, "obj", None)
        if func and getattr(func, "_is_test", False):
            return True
            
        # 2. Check for pytest.mark.skip(reason="...native runner...") 
        # This catches both module-level and function-level markers
        for marker in item.iter_markers(name="skip"):
            reason = str(marker.kwargs.get("reason", ""))
            if "native runner" in reason or "Run by native runner" in reason:
                return True
        return False

    items[:] = [item for item in items if not is_native(item)]


# Create a mock for uno to prevent ModuleNotFoundError in headless tests
sys.modules["uno"] = MagicMock()


class MockUnohelperBase:
    pass


_uh = types.ModuleType("unohelper")
_uh.Base = MockUnohelperBase
sys.modules["unohelper"] = _uh

def _create_mock_module(name):
    mod = types.ModuleType(name)
    sys.modules[name] = mod
    return mod


class MockBase:
    pass

# Unique mock classes for UNO interfaces to avoid TypeError during multiple inheritance
class MockXProofreader: pass
class MockXSupportedLocales: pass
class MockXServiceDisplayName: pass
class MockXServiceInfo: pass
class MockXServiceName: pass
class MockPropertyValue:
    def __init__(self, Name=None, Value=None):
        self.Name = Name
        self.Value = Value


@pytest.fixture(autouse=True)
def _disable_dev_llm_prefix_for_deterministic_http_tests():
    """Real dev bundles prepend a system prompt; keep unit test request JSON stable."""
    with patch(
        "plugin.framework.constants.should_prepend_dev_llm_system_prefix",
        return_value=False,
    ):
        yield


com = _create_mock_module("com")
sun = _create_mock_module("com.sun")
star = _create_mock_module("com.sun.star")
sys.modules["com.sun.star"].__path__ = []  # Make it act as a package

awt = _create_mock_module("com.sun.star.awt")


class MockSize:
    def __init__(self, width=0, height=0):
        self.Width = width
        self.Height = height


class MockPoint:
    def __init__(self, x=0, y=0):
        self.X = x
        self.Y = y


setattr(awt, "Point", MockPoint)
setattr(awt, "Size", MockSize)
setattr(awt, "FontWeight", MockBase)
setattr(awt, "FontSlant", MockBase)

text = _create_mock_module("com.sun.star.text")
sys.modules["com.sun.star.text"].__path__ = []
sys.modules["com.sun.star.text.TextContentAnchorType"] = _create_mock_module("com.sun.star.text.TextContentAnchorType")
setattr(sys.modules["com.sun.star.text.TextContentAnchorType"], "AS_CHARACTER", MockBase)
setattr(sys.modules["com.sun.star.text.TextContentAnchorType"], "AT_FRAME", MockBase)

linguistic = _create_mock_module("com.sun.star.linguistic2")
setattr(linguistic, "XProofreader", MockXProofreader)
setattr(linguistic, "XSupportedLocales", MockXSupportedLocales)

beans = _create_mock_module("com.sun.star.beans")
setattr(beans, "PropertyValue", MockPropertyValue)

sheet = _create_mock_module("com.sun.star.sheet")
setattr(beans, "PropertyValue", MockPropertyValue) # Repeat for safety in case of earlier failure

sheet = _create_mock_module("com.sun.star.sheet")
setattr(sheet, "ConditionOperator", MockBase)
setattr(sheet, "ConditionOperator2", MockBase)

table = _create_mock_module("com.sun.star.table")

lang = _create_mock_module("com.sun.star.lang")

util = _create_mock_module("com.sun.star.util")
setattr(util, "XModifyListener", MockBase)  # review_toolbar._ReviewModifyListener subclasses it


class MockXEventListener:
    pass


setattr(lang, "XEventListener", MockXEventListener)
setattr(lang, "XServiceDisplayName", MockXServiceDisplayName)
setattr(lang, "XServiceInfo", MockXServiceInfo)
setattr(lang, "XServiceName", MockXServiceName)


class MockXActionListener:
    pass


class MockXItemListener:
    pass


class MockXKeyListener:
    pass


class MockXTextListener:
    pass


class MockXWindowListener:
    pass


class MockXTopWindowListener:
    pass


setattr(awt, "XActionListener", MockXActionListener)
setattr(awt, "XItemListener", MockXItemListener)
setattr(awt, "XKeyListener", MockXKeyListener)
setattr(awt, "XTextListener", MockXTextListener)
setattr(awt, "XWindowListener", MockXWindowListener)
setattr(awt, "XTopWindowListener", MockXTopWindowListener)
setattr(awt, "WindowDescriptor", MockBase)

awt_window_class = _create_mock_module("com.sun.star.awt.WindowClass")
setattr(awt_window_class, "CONTAINER", 0)
setattr(awt_window_class, "TOP", 1)

task = _create_mock_module("com.sun.star.task")
setattr(task, "XJobExecutor", MockBase)
setattr(task, "XJob", MockBase)


@pytest.fixture(autouse=True)
def _setup_grammar_persistence_test_env():
    """Isolate grammar persistence for every test to avoid leaking files into mock paths."""
    from plugin.writer.locale import grammar_persistence
    import shutil
    import tempfile

    # Reset doc instances to ensure fresh initialization per test
    old_doc_instances = dict(grammar_persistence._doc_persistence_instances)
    grammar_persistence._doc_persistence_instances.clear()

    tmp_dir = tempfile.mkdtemp()
    with patch("plugin.framework.config.user_config_dir", return_value=tmp_dir):
        yield

    # Clean up
    grammar_persistence._doc_persistence_instances.clear()
    shutil.rmtree(tmp_dir, ignore_errors=True)
    grammar_persistence._doc_persistence_instances.update(old_doc_instances)


def pytest_sessionstart(session):
    """Clean up any 'MagicMock' directories created by accidental mock stringification in previous runs."""
    import os
    import shutil
    root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    magic_mock_dir = os.path.join(root, "MagicMock")
    if os.path.isdir(magic_mock_dir):
        shutil.rmtree(magic_mock_dir, ignore_errors=True)

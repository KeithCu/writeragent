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
class MockPropertyValue: pass


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
setattr(awt, "Point", MockBase)
setattr(awt, "Size", MockBase)
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
setattr(sheet, "ConditionOperator", MockBase)
setattr(sheet, "ConditionOperator2", MockBase)

table = _create_mock_module("com.sun.star.table")

lang = _create_mock_module("com.sun.star.lang")


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


setattr(awt, "XActionListener", MockXActionListener)
setattr(awt, "XItemListener", MockXItemListener)
setattr(awt, "XKeyListener", MockXKeyListener)
setattr(awt, "XTextListener", MockXTextListener)
setattr(awt, "XWindowListener", MockXWindowListener)

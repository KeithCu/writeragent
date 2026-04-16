import sys
import types
from unittest.mock import MagicMock

# Create a mock for uno to prevent ModuleNotFoundError in headless tests
sys.modules["uno"] = MagicMock()
sys.modules["unohelper"] = MagicMock()

def _create_mock_module(name):
    mod = types.ModuleType(name)
    sys.modules[name] = mod
    return mod

com = _create_mock_module("com")
sun = _create_mock_module("com.sun")
star = _create_mock_module("com.sun.star")
sys.modules["com.sun.star"].__path__ = []  # Make it act as a package
awt = _create_mock_module("com.sun.star.awt")
text = _create_mock_module("com.sun.star.text")
sys.modules["com.sun.star.text"].__path__ = []
sys.modules["com.sun.star.text.TextContentAnchorType"] = _create_mock_module("com.sun.star.text.TextContentAnchorType")

sheet = _create_mock_module("com.sun.star.sheet")
table = _create_mock_module("com.sun.star.table")

class MockBase:
    pass

setattr(awt, "Point", MockBase)
setattr(awt, "Size", MockBase)
setattr(awt, "FontWeight", MockBase)
setattr(awt, "FontSlant", MockBase)

setattr(sys.modules["com.sun.star.text.TextContentAnchorType"], "AS_CHARACTER", MockBase)

setattr(sheet, "ConditionOperator", MockBase)
setattr(sheet, "ConditionOperator2", MockBase)

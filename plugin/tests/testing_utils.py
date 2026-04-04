# testing_utils.py
# Centralized testing utilities and mocks for WriterAgent tests.

def setup_uno_mocks():
    """
    Centralized function to mock LibreOffice UNO dependencies for testing outside of LibreOffice.
    This must be called at the top of test files before importing the module under test.
    """
    import sys
    import types
    from unittest.mock import MagicMock

    # Check if we are already mocked or running inside LibreOffice
    if 'uno' in sys.modules and not isinstance(sys.modules['uno'], (MagicMock, types.ModuleType)):
        try:
            import uno
            # if we can import real uno, we are probably running inside LO
            if hasattr(uno, "getComponentContext"):
                return
        except ImportError:
            pass

    # Create base mock modules
    sys.modules['uno'] = MagicMock()
    sys.modules['unohelper'] = MagicMock()

    # We must use types.ModuleType and attach empty classes to avoid 'metaclass conflict' with ty
    class MockBase(object):
        pass
    sys.modules['unohelper'].Base = MockBase
    sys.modules['unohelper.Base'] = MockBase

    # Commonly used com.sun.star.* modules
    com_modules = [
        'com',
        'com.sun',
        'com.sun.star',
        'com.sun.star.text',
        'com.sun.star.util',
        'com.sun.star.document',
        'com.sun.star.frame',
        'com.sun.star.beans',
        'com.sun.star.awt',
        'com.sun.star.task',
        'com.sun.star.lang',
        'com.sun.star.style',
        'com.sun.star.style.BreakType',
        'com.sun.star.ui',
        'com.sun.star.ui.UIElementType',
        'com.sun.star.datatransfer',
        'com.sun.star.datatransfer.clipboard'
    ]

    for mod in com_modules:
        if mod not in sys.modules or isinstance(sys.modules[mod], MagicMock):
            sys.modules[mod] = types.ModuleType(mod)

    # Specific sub-module attachments
    class MockDate(object):
        Year = 2024
        Month = 1
        Day = 1
    setattr(sys.modules['com.sun.star.util'], "Date", MockDate)

    class MockListener(object):
        pass
    setattr(sys.modules['com.sun.star.awt'], 'XActionListener', MockListener)

    class MockClipboardListener(object):
        pass
    setattr(sys.modules['com.sun.star.datatransfer.clipboard'], 'XClipboardListener', MockClipboardListener)

    class MockXCallback(object):
        pass
    setattr(sys.modules['com.sun.star.awt'], 'XCallback', MockXCallback)

    class MockXTextListener(object):
        pass
    setattr(sys.modules['com.sun.star.awt'], 'XTextListener', MockXTextListener)

    class MockXWindowListener(object):
        pass
    setattr(sys.modules['com.sun.star.awt'], 'XWindowListener', MockXWindowListener)

    class MockXEventListener(object):
        pass
    setattr(sys.modules['com.sun.star.lang'], 'XEventListener', MockXEventListener)

    class MockXInitialization(object):
        pass
    setattr(sys.modules['com.sun.star.lang'], 'XInitialization', MockXInitialization)

    class MockXServiceInfo(object):
        pass
    setattr(sys.modules['com.sun.star.lang'], 'XServiceInfo', MockXServiceInfo)

    class MockXJobExecutor(object):
        pass
    setattr(sys.modules['com.sun.star.task'], 'XJobExecutor', MockXJobExecutor)

    class MockXJob(object):
        pass
    setattr(sys.modules['com.sun.star.task'], 'XJob', MockXJob)

    class MockXDispatch(object):
        pass
    setattr(sys.modules['com.sun.star.frame'], 'XDispatch', MockXDispatch)

    class MockXDispatchProvider(object):
        pass
    setattr(sys.modules['com.sun.star.frame'], 'XDispatchProvider', MockXDispatchProvider)

class ElementStub:
    def __init__(self, text, outline_level=0, services=None):
        self.text = text
        self.outline_level = outline_level
        self.services = services or ["com.sun.star.text.Paragraph"]

    def getString(self):
        return self.text

    def getPropertyValue(self, name):
        if name == "OutlineLevel":
            return self.outline_level
        from plugin.framework.errors import WriterAgentException
        raise WriterAgentException("Property not found")

    def supportsService(self, service):
        return service in self.services

    def getStart(self):
        return self # Stub for range

    def getEnd(self):
        return self

    def getText(self):
        return self

class WriterDocStub:
    def __init__(self, elements):
        self.elements = elements
        self.url = "test://writer"

    def getText(self):
        class TextStub:
            def __init__(self, el):
                self.el = el

            def createEnumeration(self):
                class EnumStub:
                    def __init__(self, el):
                        self.el = el
                        self.idx = 0

                    def hasMoreElements(self):
                        return self.idx < len(self.el)

                    def nextElement(self):
                        res = self.el[self.idx]
                        self.idx += 1
                        return res
                return EnumStub(self.el)
        return TextStub(self.elements)

    def supportsService(self, s):
        return s == "com.sun.star.text.TextDocument"

class MockDocument:
    def __init__(self):
        self.url = "test://mock"

    def supportsService(self, service):
        return False

class MockTextCursor:
    def __init__(self):
        pass

class MockSheet:
    def __init__(self):
        pass

class MockContext:
    """Mock context object used as a stand-in for the UNO ComponentContext outside of LibreOffice."""
    def __init__(self):
        self.mock_values = {}

    def getValueByName(self, name):
        return self.mock_values.get(name)

def create_mock_client():
    """Creates a pre-configured MagicMock for an LlmClient."""
    from unittest.mock import MagicMock
    mock_client = MagicMock()
    mock_client.config = MagicMock()
    mock_client.config.get.return_value = False
    return mock_client

def create_mock_http_response(status_code=200, json_data=None):
    """Creates a mock HTTP response object."""
    from unittest.mock import MagicMock
    import json

    mock_resp = MagicMock()
    mock_resp.status = status_code
    if json_data is not None:
        mock_resp.read.return_value = json.dumps(json_data).encode()
    return mock_resp

# testing_utils.py
# Centralized testing utilities and mocks for WriterAgent tests.

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
        raise Exception("Property not found")

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

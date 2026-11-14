
from plugin.testing_runner import native_test

class MockPropertySet:
    def __init__(self):
        self.properties = {}

    def getPropertySetInfo(self):
        class MockInfo:
            def __init__(self, props):
                self.props = props
            def hasPropertyByName(self, name):
                return name in self.props
        return MockInfo(self.properties)

    def hasByName(self, name):
        return name in self.properties

    def getPropertyValue(self, name):
        if name in self.properties:
            return self.properties[name]
        from plugin.framework.errors import WriterAgentException
        raise WriterAgentException("UnknownPropertyException")

    def addProperty(self, name, attributes, default_value):
        self.properties[name] = default_value

    def setPropertyValue(self, name, value):
        if name not in self.properties:
            from plugin.framework.errors import WriterAgentException
            raise WriterAgentException("UnknownPropertyException")
        self.properties[name] = value

    def removeProperty(self, name):
        if name in self.properties:
            del self.properties[name]

class MockDocumentModel:
    def __init__(self, url="file:///mock/test.odt"):
        self.url = url
        self.props = MockPropertySet()

    def getURL(self):
        return self.url

    def getDocumentProperties(self):
        class MockDocProps:
            def __init__(self, props):
                self.UserDefinedProperties = props
        return MockDocProps(self.props)

    def getPropertySetInfo(self):
        return self.props.getPropertySetInfo()

    def supportsService(self, service_name):
        return service_name == "com.sun.star.text.TextDocument"


@native_test
def test_session_id_stability(ctx):
    import hashlib

    from plugin.chatbot.panel_factory import ChatPanelElement

    # Minimal mock dependencies for ChatPanelElement
    class MockCtx:
        def getValueByName(self, name):
            return None

    panel = ChatPanelElement(MockCtx(), None, None, "test_url")
    model = MockDocumentModel("file:///test/my_doc.odt")

    # 1. First run: Should generate a stable ID based on URL (hash) since no property exists
    panel._setup_sessions(model, "some instructions")
    assert panel.session is not None
    first_session_id = panel.session.session_id

    assert first_session_id is not None
    assert first_session_id == hashlib.sha256(b"file:///test/my_doc.odt").hexdigest()

    # Verify the property was stored back onto the document model
    stored_id = model.props.properties.get("WriterAgentSessionID")
    assert stored_id == first_session_id

    # 2. Simulate "save as" to a new URL, but keeping properties intact
    model.url = "file:///test/renamed.odt"
    panel._setup_sessions(model, "different instructions")

    # It should regenerate the ID to isolate the copied document, but fork the history
    assert panel.session is not None
    second_session_id = panel.session.session_id
    assert second_session_id != first_session_id
    assert second_session_id == hashlib.sha256(b"file:///test/renamed.odt").hexdigest()

    # 3. Simulate new unsaved document with no URL and no properties
    unsaved_model = MockDocumentModel("")
    panel._setup_sessions(unsaved_model, "")

    assert panel.session is not None
    third_session_id = panel.session.session_id
    # It should generate a UUID
    assert third_session_id is not None
    assert len(third_session_id) > 10  # generic check for UUID length

    # And store it
    stored_id_3 = unsaved_model.props.properties.get("WriterAgentSessionID")
    assert stored_id_3 == third_session_id

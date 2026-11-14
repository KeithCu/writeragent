from unittest.mock import MagicMock

from plugin.testing_runner import native_test


@native_test
def test_session_id_isolation_on_url_change(ctx):
    """
    Test that if a document has an existing session ID and its URL changes (simulating a copy),
    the session ID is regenerated to isolate chat history.
    """
    from plugin.chatbot.panel_factory import ChatPanelElement
    from plugin.doc.udprops import get_document_property
    
    # Create a dummy element instance
    element = ChatPanelElement(ctx, None, None, "test_resource_url")
    
    # We will use mock models that mimic document models with properties
    class MockModel:
        def __init__(self, url):
            self.url = url
            # Mock properties using a simple dictionary
            self._props = {}
            
            class MockPropertySet:
                def __init__(self, parent):
                    self.parent = parent
                def getPropertyValue(self, name):
                    return self.parent._props.get(name)
                def setPropertyValue(self, name, val):
                    self.parent._props[name] = val
                def addProperty(self, name, attr, val):
                    self.parent._props[name] = val
                def getPropertySetInfo(self):
                    class MockInfo:
                        def __init__(self, parent):
                            self.parent = parent
                        def hasPropertyByName(self, name):
                            return name in self.parent._props
                    return MockInfo(self.parent)

            class MockDocProps:
                def __init__(self, parent):
                    self.UserDefinedProperties = MockPropertySet(parent)

            self._doc_props = MockDocProps(self)

        def getDocumentProperties(self):
            return self._doc_props

        def getURL(self):
            return self.url

        def supportsService(self, service_name):
            return service_name == "com.sun.star.text.TextDocument"

    model = MockModel("file:///path/to/original.odt")
    
    # Run setup sessions initially
    element._setup_sessions(model, "")
    
    orig_session_id = get_document_property(model, "WriterAgentSessionID")
    orig_session_url = get_document_property(model, "WriterAgentSessionURL")
    
    assert orig_session_id is not None
    assert orig_session_url == "file:///path/to/original.odt"
    
    # 2. Simulate copying/saving as new file (URL changes, but properties are preserved)
    copied_model = MockModel("file:///path/to/copy.odt")
    copied_model._props = dict(model._props) # copy the UserDefinedProperties
    
    # Run setup sessions on the copy
    element._setup_sessions(copied_model, "")
    
    new_session_id = get_document_property(copied_model, "WriterAgentSessionID")
    new_session_url = get_document_property(copied_model, "WriterAgentSessionURL")
    
    assert new_session_id != orig_session_id, "Session ID should have been regenerated for the copied document URL"
    assert new_session_url == "file:///path/to/copy.odt", "Session URL should have been updated to the copied document URL"

@native_test
def test_setup_sessions_does_not_duplicate_history_on_save_as():
    from plugin.chatbot.panel_factory import ChatPanelElement
    from plugin.chatbot.history_db import get_chat_history

    element = ChatPanelElement(MagicMock(), MagicMock(), MagicMock(), "resource://test")

    class MockModel:
        def __init__(self, url):
            self._url = url
            self._props = {}

        def getURL(self):
            return self._url

        def getDocumentProperties(self):
            props = MagicMock()

            def get_prop(k):
                if k not in self._props:
                    from com.sun.star.beans import UnknownPropertyException
                    raise UnknownPropertyException()
                return self._props[k]

            props.getUserDefinedProperties().getPropertyValue.side_effect = get_prop

            def set_prop(k, *args):
                val = args[-1] if len(args) > 1 else args[0]
                self._props[k] = val

            props.getUserDefinedProperties().setPropertyValue.side_effect = set_prop
            props.getUserDefinedProperties().addProperty.side_effect = set_prop
            return props

        def supportsService(self, service_name):
            return service_name == "com.sun.star.text.TextDocument"

    # 1. Original file
    model1 = MockModel("file:///path/to/original.odt")
    element._setup_sessions(model1, "")

    sid1 = str(model1._props.get("WriterAgentSessionID") or "")
    db1 = get_chat_history(sid1)
    db1.replace_messages([])
    db1.add_message("user", "Hello from original")

    # 2. Save As to file 2
    model2 = MockModel("file:///path/to/copy.odt")
    model2._props = dict(model1._props)

    element._setup_sessions(model2, "")
    sid2 = str(model2._props.get("WriterAgentSessionID") or "")

    db2 = get_chat_history(sid2)
    msgs2 = db2.get_messages()
    assert len(msgs2) == 1
    assert msgs2[0]["content"] == "Hello from original"

    # 3. Simulate another _setup_sessions on the same copy while it hasn't written the properties down (or just another setup_sessions).
    # Since model3 starts with model1's properties, it thinks it's still transitioning from original -> copy.
    model3 = MockModel("file:///path/to/copy.odt")
    model3._props = dict(model1._props)

    element._setup_sessions(model3, "")

    sid3 = str(model3._props.get("WriterAgentSessionID") or "")
    assert sid3 == sid2

    db3 = get_chat_history(sid3)
    msgs3 = db3.get_messages()

    # If the bug is present, this would be 2 (duplicated history).
    assert len(msgs3) == 1
    assert msgs3[0]["content"] == "Hello from original"

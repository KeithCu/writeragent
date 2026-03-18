
import os
import sys
from importlib import reload

from plugin.framework.history_db import message_to_dict, SQLite3History, JSONHistory

def test_history_roundtrip_sqlite(tmp_path):
    import plugin.framework.history_db as hdb
    if not hdb.HAS_SQLITE:
        return

    db_path = os.path.join(tmp_path, "test_sqlite_history.db")
    history = SQLite3History("session_123", db_path)

    # Simulate adding messages
    history.add_message("user", "Hello SQLite!")
    tool_calls = [{"id": "call_1", "type": "function", "function": {"name": "test", "arguments": "{}"}}]
    history.add_message("assistant", None, tool_calls=tool_calls)

    # And a tool result
    tool_msg = {"role": "tool", "content": "Tool success", "tool_call_id": "call_1"}
    # The history db adds it as a raw message dict
    import json
    with hdb.sqlite3.connect(db_path) as conn:
        conn.execute("INSERT INTO message_store (session_id, message) VALUES (?, ?)", ("session_123", json.dumps(tool_msg)))
        conn.commit()

    history2 = SQLite3History("session_123", db_path)
    messages = history2.get_messages()

    assert len(messages) == 3
    assert messages[0]["role"] == "user"
    assert messages[0]["content"] == "Hello SQLite!"

    assert messages[1]["role"] == "assistant"
    assert messages[1]["content"] is None
    assert messages[1]["tool_calls"] == tool_calls

    assert messages[2]["role"] == "tool"
    assert messages[2]["content"] == "Tool success"
    assert messages[2]["tool_call_id"] == "call_1"

def test_history_roundtrip_json(tmp_path):
    db_path = os.path.join(tmp_path, "test_json_history")
    # JSONHistory creates a directory using db_path + ".d"
    history = JSONHistory("session_abc", db_path)

    # Simulate adding messages
    history.add_message("user", "Hello JSON!")
    tool_calls = [{"id": "call_2", "type": "function", "function": {"name": "json_test", "arguments": "{}"}}]
    history.add_message("assistant", "Thinking...", tool_calls=tool_calls)

    # And a tool result
    tool_msg = {"role": "tool", "content": "Tool success", "tool_call_id": "call_2"}
    import json
    msgs = history.get_messages()
    msgs.append(tool_msg)
    with open(history.file_path, "w", encoding="utf-8") as f:
        json.dump(msgs, f)

    history2 = JSONHistory("session_abc", db_path)
    messages = history2.get_messages()

    assert len(messages) == 3
    assert messages[0]["role"] == "user"
    assert messages[0]["content"] == "Hello JSON!"

    assert messages[1]["role"] == "assistant"
    assert messages[1]["content"] == "Thinking..."
    assert messages[1]["tool_calls"] == tool_calls

    assert messages[2]["role"] == "tool"
    assert messages[2]["content"] == "Tool success"
    assert messages[2]["tool_call_id"] == "call_2"

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
        raise Exception("UnknownPropertyException")

    def addProperty(self, name, attributes, default_value):
        self.properties[name] = default_value

    def setPropertyValue(self, name, value):
        if name not in self.properties:
            raise Exception("UnknownPropertyException")
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

def test_session_id_stability(monkeypatch, tmp_path):
    import sys
    import types
    import uuid
    import hashlib

    # Mock 'uno' and 'unohelper' since they are not available in pure pytest environment
    class MockBase: pass
    if 'uno' not in sys.modules:
        mock_uno = types.ModuleType('uno')
        mock_uno.createUnoStruct = lambda *args: None
        monkeypatch.setitem(sys.modules, 'uno', mock_uno)
    if 'unohelper' not in sys.modules:
        mock_unohelper = types.ModuleType('unohelper')
        mock_unohelper.Base = MockBase
        mock_unohelper.ImplementationHelper = lambda: type('MockHelper', (), {'addImplementation': lambda *args: None})()
        monkeypatch.setitem(sys.modules, 'unohelper', mock_unohelper)

    # Also mock COM sun star interfaces
    if 'com.sun.star.ui' not in sys.modules:
        class MockUI1: pass
        class MockUI2: pass
        class MockUI3: pass
        class MockUI4: pass
        mock_ui = types.ModuleType('com.sun.star.ui')
        mock_ui.XUIElementFactory = MockUI1
        mock_ui.XUIElement = MockUI2
        mock_ui.XToolPanel = MockUI3
        mock_ui.XSidebarPanel = MockUI4
        monkeypatch.setitem(sys.modules, 'com.sun.star.ui', mock_ui)
    if 'com.sun.star.ui.UIElementType' not in sys.modules:
        mock_ui_type = types.ModuleType('com.sun.star.ui.UIElementType')
        mock_ui_type.TOOLPANEL = 1
        monkeypatch.setitem(sys.modules, 'com.sun.star.ui.UIElementType', mock_ui_type)
    if 'com.sun.star.beans.PropertyAttribute' not in sys.modules:
        mock_bean = types.ModuleType('com.sun.star.beans.PropertyAttribute')
        mock_bean.REMOVABLE = 1
        monkeypatch.setitem(sys.modules, 'com.sun.star.beans', types.ModuleType('com.sun.star.beans'))
        monkeypatch.setitem(sys.modules, 'com.sun.star.beans.PropertyAttribute', mock_bean)
    if 'com.sun.star.awt' not in sys.modules:
        class MockListener: pass
        mock_awt = types.ModuleType('com.sun.star.awt')
        mock_awt.XItemListener = MockListener
        mock_awt.XActionListener = MockListener
        mock_awt.XTextListener = MockListener
        mock_awt.XWindowListener = MockListener
        monkeypatch.setitem(sys.modules, 'com.sun.star.awt', mock_awt)

    from plugin.modules.chatbot.panel_factory import ChatPanelElement
    import plugin.framework.config as config

    import plugin.framework.uno_context as uno_context

    # redirect history DB away from actual user profile
    # Mock user_config_dir instead of internal config
    import plugin.framework.history_db as hdb
    monkeypatch.setattr(hdb, 'user_config_dir', lambda ctx: str(tmp_path))

    # Minimal mock dependencies for ChatPanelElement
    class MockCtx:
        def getValueByName(self, name):
            return None

    panel = ChatPanelElement(MockCtx(), None, None, "test_url")
    model = MockDocumentModel("file:///test/my_doc.odt")

    # 1. First run: Should generate a stable ID based on URL (hash) since no property exists
    panel._setup_sessions(model, "some instructions")
    first_session_id = panel.session.session_id

    assert first_session_id is not None
    assert first_session_id == hashlib.sha256(b"file:///test/my_doc.odt").hexdigest()

    # Verify the property was stored back onto the document model
    stored_id = model.props.properties.get("WriterAgentSessionID")
    assert stored_id == first_session_id

    # 2. Simulate "save as" to a new URL, but keeping properties intact
    model.url = "file:///test/renamed.odt"
    panel._setup_sessions(model, "different instructions")

    # It should reuse the ID stored in UserDefinedProperties, ignoring the new URL
    second_session_id = panel.session.session_id
    assert second_session_id == first_session_id

    # 3. Simulate new unsaved document with no URL and no properties
    unsaved_model = MockDocumentModel("")
    panel._setup_sessions(unsaved_model, "")

    third_session_id = panel.session.session_id
    # It should generate a UUID
    assert third_session_id is not None
    assert len(third_session_id) > 10 # generic check for UUID length

    # And store it
    stored_id_3 = unsaved_model.props.properties.get("WriterAgentSessionID")
    assert stored_id_3 == third_session_id

def test_message_to_dict_text():
    res = message_to_dict("user", "hello")
    assert res["role"] == "user"
    assert res["content"] == "hello"
    assert res["tool_calls"] is None

def test_message_to_dict_list():
    res = message_to_dict("user", [{"type": "text", "text": "hello"}, {"type": "input_audio"}])
    assert res["role"] == "user"
    assert "hello" in res["content"]
    assert "[Audio Attached]" in res["content"]

import sys
from importlib import reload

def test_sqlite_available():
    # Typically sqlite3 is available in python env
    import plugin.framework.history_db as hdb
    reload(hdb)

    assert hdb.HAS_SQLITE is True
    assert hdb.sqlite3 is not None

def test_sqlite_unavailable():
    # Hide sqlite3 from sys.modules to simulate absence
    import builtins
    original_import = builtins.__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == 'sqlite3':
            raise ImportError("No module named 'sqlite3'")
        return original_import(name, globals, locals, fromlist, level)

    builtins.__import__ = fake_import

    # Temporarily remove if it was already imported
    original_sqlite3 = sys.modules.pop('sqlite3', None)

    try:
        import plugin.framework.history_db as hdb
        reload(hdb)

        assert hdb.HAS_SQLITE is False
        assert hdb.sqlite3 is None
    finally:
        builtins.__import__ = original_import
        if original_sqlite3:
            sys.modules['sqlite3'] = original_sqlite3

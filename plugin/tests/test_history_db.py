import pytest
import os
import json

from plugin.framework.history_db import message_to_dict

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

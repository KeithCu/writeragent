import sys
from importlib import reload

def test_sqlite_available():
    # Typically sqlite3 is available in python env
    import plugin.framework.sqlite_available as sa
    reload(sa)

    assert sa.HAS_SQLITE is True
    assert sa.sqlite3 is not None

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
        import plugin.framework.sqlite_available as sa
        reload(sa)

        assert sa.HAS_SQLITE is False
        assert sa.sqlite3 is None
    finally:
        builtins.__import__ = original_import
        if original_sqlite3:
            sys.modules['sqlite3'] = original_sqlite3

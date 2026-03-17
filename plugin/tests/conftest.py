"""Pytest configuration. Skip tests that require LibreOffice UNO or optional deps when not available."""



def pytest_ignore_collect(collection_path, config):
    """Skip LO-dependent and optional-dependency tests when run outside LibreOffice or without deps."""
    name = collection_path.name
    # Tests that require LibreOffice UNO (run instead by plugin.testing_runner)
    if name in ("test_calc.py", "test_chat_model_logic.py", "test_draw.py", "test_writer.py", "test_document.py", "test_linebreak_conversion.py", "test_writer_navigation.py", "test_constants.py", "test_impress.py"):
        try:
            import uno  # noqa: F401
        except ImportError:
            return True

    # Tests that require optional 'requests' (e.g. test_search_web)
    if name == "test_search_web.py":
        try:
            import requests  # noqa: F401
        except ImportError:
            return True
    # Tests that require optional 'sounddevice'
    if name == "test_sound.py":
        try:
            import sounddevice  # noqa: F401
        except ImportError:
            return True
    return False

# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

import uno
from typing import Any
from plugin.framework.uno_context import get_desktop
from plugin.testing_runner import setup, teardown, native_test

_test_doc: Any = None

@setup
def setup_form_cursor_tests(ctx):
    global _test_doc
    desktop = get_desktop(ctx)

    hidden_prop = uno.createUnoStruct(
        "com.sun.star.beans.PropertyValue",
        Name="Hidden",
        Value=True,
    )
    _test_doc = desktop.loadComponentFromURL("private:factory/swriter", "_blank", 0, (hidden_prop,))

@teardown
def teardown_form_cursor_tests(ctx):
    global _test_doc
    if _test_doc:
        _test_doc.close(True)
    _test_doc = None

@native_test
def test_insert_text_with_view_cursor():
    """Verify that _insert_text correctly handles the ViewCursor by converting it."""
    from plugin.modules.writer.forms import GenerateForm
    tool = GenerateForm()
    
    class MockCtx:
        def __init__(self, doc):
            self.doc = doc
            self.doc_type = "writer"
            
    mock_ctx = MockCtx(_test_doc)
    
    # This call previously crashed because it passed ViewCursor to insert_html_at_cursor
    try:
        tool._insert_text(mock_ctx, "<b>Bold Text</b>")
    except Exception as e:
        import pytest
        pytest.fail(f"_insert_text crashed with: {str(e)}")
    
    # Verify insertion
    text = _test_doc.getText().getString()
    assert "Bold Text" in text

# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
# Copyright (c) 2026 LibreCalc AI Assistant (Calc integration features, originally MIT)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

from plugin.framework.uno_context import get_desktop
from plugin.testing_runner import setup, teardown, native_test

_test_doc = None
_test_ctx = None

@setup
def setup_calc_tests(ctx):
    global _test_doc, _test_ctx
    _test_ctx = ctx
    desktop = get_desktop(ctx)
    import uno
    hidden_prop = uno.createUnoStruct(
        "com.sun.star.beans.PropertyValue",
        Name="Hidden",
        Value=True,
    )
    _test_doc = desktop.loadComponentFromURL("private:factory/scalc", "_blank", 0, (hidden_prop,))

@teardown
def teardown_calc_tests(ctx):
    global _test_doc, _test_ctx
    if _test_doc:
        _test_doc.close(True)
    _test_doc = None
    _test_ctx = None

def _execute_calc_tool(name, args):
    from plugin.main import get_tools, get_services
    from plugin.framework.tool import ToolContext
    # Pass suite bootstrap ctx (same as setup_calc_tests); None makes
    # get_desktop() use uno.getComponentContext() and can segfault under
    # python -m plugin.testing_runner.
    tctx = ToolContext(_test_doc, _test_ctx, "calc", get_services(), "test")
    try:
        res = get_tools().execute(name, tctx, **args)
    except (KeyError, ValueError) as e:
        res = {"status": "error", "error": str(e)}
    return res


@native_test
def test_calc_track_changes_record_toggle():
    """SpreadsheetDocument exposes RecordChanges via SpreadsheetDocumentSettings."""
    from plugin.main import get_services
    from plugin.framework.tool import ToolContext
    from plugin.writer.tracking import TrackChangesStart, TrackChangesStop

    doc = _test_doc
    before = bool(doc.getPropertyValue("RecordChanges"))
    tctx = ToolContext(doc, _test_ctx, "calc", get_services(), "test")
    try:
        assert TrackChangesStart().execute(tctx).get("status") == "ok"
        assert doc.getPropertyValue("RecordChanges") is True
        assert TrackChangesStop().execute(tctx).get("status") == "ok"
        assert doc.getPropertyValue("RecordChanges") is False
    finally:
        doc.setPropertyValue("RecordChanges", before)

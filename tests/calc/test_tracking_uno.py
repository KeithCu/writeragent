# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
# Copyright (c) 2026 LibreCalc AI Assistant (Calc integration features, originally MIT)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

from plugin.testing_runner import native_test
from plugin.tests.testing_utils import TestingFactory, with_native_doc


@native_test
@with_native_doc("calc")
def test_calc_track_changes_record_toggle(ctx, doc):
    """SpreadsheetDocument exposes RecordChanges via SpreadsheetDocumentSettings."""
    from plugin.main import get_services
    from plugin.writer.tracking import TrackChangesStart, TrackChangesStop

    before = bool(doc.getPropertyValue("RecordChanges"))
    tctx = TestingFactory.create_context(
        doc=doc, ctx=ctx, env="native", doc_type="calc", services=get_services()
    )
    try:
        assert TrackChangesStart().execute(tctx).get("status") == "ok"
        assert doc.getPropertyValue("RecordChanges") is True
        assert TrackChangesStop().execute(tctx).get("status") == "ok"
        assert doc.getPropertyValue("RecordChanges") is False
    finally:
        doc.setPropertyValue("RecordChanges", before)


@native_test
@with_native_doc("calc")
def test_calc_track_changes_list_unsupported(ctx, doc):
    from plugin.main import get_services
    from plugin.writer.tracking import TrackChangesList

    tctx = TestingFactory.create_context(
        doc=doc, ctx=ctx, env="native", doc_type="calc", services=get_services()
    )
    res = TrackChangesList().execute(tctx)
    assert res.get("status") == "ok"
    assert "count" not in res
    assert "changes" not in res
    assert res.get("track_changes_list_unsupported") is True
    assert isinstance(res.get("recording"), bool)


@native_test
@with_native_doc("calc")
def test_calc_manage_tracked_changes_bulk_unsupported(ctx, doc):
    from plugin.main import get_services
    from plugin.writer.tracking import ManageTrackedChanges

    tctx = TestingFactory.create_context(
        doc=doc, ctx=ctx, env="native", doc_type="calc", services=get_services()
    )
    res = ManageTrackedChanges().execute(tctx, action="accept_all")
    assert res.get("status") == "error"
    assert res.get("code") == "UNSUPPORTED"
    assert res.get("tracked_changes_bulk_unsupported") is True
    assert res.get("message") == "Accepting or rejecting all tracked changes is not supported on this document."


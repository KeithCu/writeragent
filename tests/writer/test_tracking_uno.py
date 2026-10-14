# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Native UNO integration tests for Writer track changes tools."""

from plugin.testing_runner import native_test
from plugin.tests.testing_utils import TestingFactory, with_native_doc


@native_test
@with_native_doc("writer")
def test_writer_track_changes_show_toggle_uno(ctx, doc):
    """TrackChangesShow sets RedlineDisplayType and updates ShowChanges without touching ShowChangesInMargin."""
    from plugin.main import get_services
    from plugin.writer.tracking import TrackChangesShow

    controller = doc.getCurrentController()
    view_settings = controller.getViewSettings()

    saved_display_type = doc.getPropertyValue("RedlineDisplayType")
    saved_margin = view_settings.getPropertyValue("ShowChangesInMargin")

    tctx = TestingFactory.create_context(
        doc=doc, ctx=ctx, env="native", doc_type="writer", services=get_services()
    )

    try:
        # show=False -> type 1, ShowChanges False
        res_hide = TrackChangesShow().execute(tctx, show=False)
        assert res_hide.get("status") == "ok", res_hide
        assert doc.getPropertyValue("RedlineDisplayType") == 1
        assert doc.getPropertyValue("ShowChanges") is False
        assert view_settings.getPropertyValue("ShowChangesInMargin") == saved_margin

        # show=True -> type 2, ShowChanges True
        res_show = TrackChangesShow().execute(tctx, show=True)
        assert res_show.get("status") == "ok", res_show
        assert doc.getPropertyValue("RedlineDisplayType") == 2
        assert doc.getPropertyValue("ShowChanges") is True
        assert view_settings.getPropertyValue("ShowChangesInMargin") == saved_margin
    finally:
        # Restore original values so reused document is left clean
        doc.setPropertyValue("RedlineDisplayType", saved_display_type)
        view_settings.setPropertyValue("ShowChangesInMargin", saved_margin)

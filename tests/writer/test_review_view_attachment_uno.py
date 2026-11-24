# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""UNO tests for review_view_attachment scratch-document exclusion."""

from __future__ import annotations

from plugin.framework.scratch_writer import close_scratch_document, new_blank_writer
from plugin.framework.uno_context import create_property_value, get_desktop
from plugin.framework.uno_listeners import install_view_doc_listener
from plugin.testing_runner import native_test
from plugin.writer import review_view_attachment as rva


@native_test
def test_review_view_attachment_skips_scratch_and_attaches_user_hidden_uno(ctx):
    broadcaster = ctx.getServiceManager().createInstanceWithContext(
        "com.sun.star.frame.GlobalEventBroadcaster", ctx
    )
    listener = install_view_doc_listener(
        ctx,
        rva._register_after_scratch_loads,
        "test_review_view_attachment_uno",
        on_unload=rva._on_document_unload,
    )
    assert listener is not None
    scratch = None
    user_hidden = None
    try:
        scratch = new_blank_writer(ctx)
        assert scratch is not None
        assert rva.attachment_key(scratch) not in rva._attachments

        desktop = get_desktop(ctx)
        user_hidden = desktop.loadComponentFromURL(
            "private:factory/swriter",
            "_blank",
            0,
            (create_property_value("Hidden", True),),
        )
        assert user_hidden is not None
        assert rva.attachment_key(user_hidden) in rva._attachments
    finally:
        try:
            broadcaster.removeDocumentEventListener(listener)
        except Exception:
            pass
        if scratch is not None:
            try:
                close_scratch_document(scratch)
            except Exception:
                pass
        if user_hidden is not None:
            try:
                user_hidden.close(True)
            except Exception:
                pass

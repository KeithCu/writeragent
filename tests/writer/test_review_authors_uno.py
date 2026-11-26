# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

from typing import Any

import uno  # noqa: F401

from plugin.framework.uno_context import enum_has_more
from plugin.testing_runner import native_test
from plugin.tests.testing_utils import with_native_doc
from plugin.writer import review_authors


@native_test
@with_native_doc("writer", reuse=False)
def test_restore_author_after_crash_new_redline_has_user_name_uno(ctx: Any, doc: Any) -> None:
    access = review_authors._author_access(ctx)
    saved_profile = (str(access.getPropertyValue("givenname")), str(access.getPropertyValue("sn")))
    prior: tuple[str, str] | None = None
    try:
        assert review_authors.set_office_author(ctx, "Alice") is True
        prior = review_authors.begin(ctx)
        assert prior == ("Alice", "")

        review_authors.restore_author_after_crash(ctx)

        doc.setPropertyValue("RecordChanges", True)
        text = doc.getText()
        cur = text.createTextCursor()
        text.insertString(cur, "After crash recovery", False)

        authors: list[str] = []
        enum = doc.getRedlines().createEnumeration()
        while enum_has_more(enum):
            redline = enum.nextElement()
            authors.append(str(redline.getPropertyValue("RedlineAuthor")))

        assert "Alice" in authors
        assert review_authors.office_author(ctx) == "Alice"
    finally:
        review_authors.end(ctx, prior)
        cleanup_access = review_authors._author_access(ctx)
        review_authors._commit_author(cleanup_access, saved_profile[0], saved_profile[1])

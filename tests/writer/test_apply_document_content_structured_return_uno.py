# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# apply_document_content returns a machine-readable replaced_count so the agent can tell whether
# an edit actually landed instead of always seeing status="ok": replaced_count == 0 -> status
# "error" (a silent no-op surfaced), N > 0 -> "ok". (No target/formatting_preserved/matched_count/
# warning/partial-replace — minimal per maintainer request; a replace that raises mid-all_matches
# keeps the existing abort behavior.) These tests cover single-match, all_matches, and no-match.
import uno  # noqa: F401

from plugin.testing_runner import native_test
from plugin.writer.content import ApplyDocumentContent
from plugin.tests.testing_utils import (
    TestingFactory,
    skip_windows_leftover_hidden_apply,
    with_native_doc,
)


def _set_body(doc, text_value):
    skip_windows_leftover_hidden_apply()
    text = doc.getText()
    cur = text.createTextCursor()
    cur.gotoStart(False)
    cur.gotoEnd(True)
    cur.setString("")
    cur.gotoStart(False)
    text.insertString(cur, text_value, False)


@native_test
@with_native_doc("writer")
def test_single_match_reports_replaced_count_uno(ctx, doc):
    _set_body(doc, "alpha foo beta")
    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    res = ApplyDocumentContent().execute(tool_ctx, target="search", old_content="foo", content="BAR")
    assert res.get("status") == "ok", res
    assert res.get("replaced_count") == 1, res


@native_test
@with_native_doc("writer")
def test_no_match_reports_zero_and_errors_uno(ctx, doc):
    """The anti silent-failure case: a search that matches nothing must report status=error."""
    _set_body(doc, "nothing relevant here")
    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    res = ApplyDocumentContent().execute(tool_ctx, target="search", old_content="zzz-not-present", content="BAR")
    assert res.get("status") == "error", res
    assert res.get("replaced_count") == 0, res


@native_test
@with_native_doc("writer")
def test_all_matches_reports_total_count_uno(ctx, doc):
    _set_body(doc, "x foo y foo z foo w")
    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    res = ApplyDocumentContent().execute(tool_ctx, target="search", old_content="foo", content="BAR", all_matches=True)
    assert res.get("status") == "ok", res
    assert res.get("replaced_count") == 3, res


@native_test
@with_native_doc("writer")
def test_all_matches_no_match_errors_uno(ctx, doc):
    _set_body(doc, "nothing here")
    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    res = ApplyDocumentContent().execute(tool_ctx, target="search", old_content="zzz", content="BAR", all_matches=True)
    assert res.get("status") == "error", res
    assert res.get("replaced_count") == 0, res
    # The consumer's legacy string fallback relies on this exact prefix.
    assert res.get("message", "").startswith("Replaced 0 occurrence"), res


@native_test
@with_native_doc("writer")
def test_insert_branch_succeeds_uno(ctx, doc):
    _set_body(doc, "seed")
    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    res = ApplyDocumentContent().execute(tool_ctx, target="end", content="more")
    assert res.get("status") == "ok", res


@native_test
@with_native_doc("writer")
def test_empty_old_content_is_a_parameter_error_uno(ctx, doc):
    """old_content that normalizes to empty is a parameter error (like old_content=None), not a
    search no-op: status="error" and the search never ran (no replaced_count)."""
    _set_body(doc, "some content here")
    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    res = ApplyDocumentContent().execute(tool_ctx, target="search", old_content="   ", content="BAR")
    assert res.get("status") == "error", res

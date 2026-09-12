# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# Regression tests: apply_document_content(target='search') failed to match document
# text containing a non-breaking space (U+00A0) when old_content used a normal space.
# The fix normalizes NBSP & relatives (U+00A0/202F/2007/2009) to a normal space on
# both the search string and the document text (1:1, so cursor offsets stay aligned).
import uno  # noqa: F401

from plugin.testing_runner import native_test
from plugin.writer.content import ApplyDocumentContent
from plugin.tests.testing_utils import (
    TestingFactory,
    skip_windows_leftover_hidden_load,
    with_native_doc,
)

_NBSP = chr(0xA0)  # non-breaking space U+00A0 (ASCII-safe in source)


def _tool_ctx(doc, ctx):
    skip_windows_leftover_hidden_load("apply_document_content Hidden _default swriter")
    return TestingFactory.create_context(doc=doc, ctx=ctx, env="native")


@native_test
@with_native_doc("writer")
def test_apply_document_content_search_matches_nbsp_uno(ctx, doc):
    """Single match: normal space in old_content should match document NBSP."""
    text = doc.getText()
    cur = text.createTextCursor()
    text.insertString(cur, "Result figures)" + _NBSP + "by a judge, final.", False)

    res = ApplyDocumentContent().execute(
        _tool_ctx(doc, ctx),
        content=["figures) BY A JUDGE"],
        old_content="figures) by a judge",
        target="search",
    )
    assert res.get("status") == "ok", f"apply_document_content did not match the NBSP: {res}"
    body = doc.getText().getString()
    assert "BY A JUDGE" in body, f"replacement did not happen; text={body!r}"


@native_test
@with_native_doc("writer")
def test_apply_document_content_all_matches_nbsp_uno(ctx, doc):
    """all_matches: two NBSP occurrences with normal-space old_content."""
    text = doc.getText()
    cur = text.createTextCursor()
    text.insertString(cur, "foo" + _NBSP + "bar. And foo" + _NBSP + "bar again.", False)

    res = ApplyDocumentContent().execute(
        _tool_ctx(doc, ctx),
        content=["REPLACED"],
        old_content="foo bar",
        target="search",
        all_matches=True,
    )
    assert res.get("status") == "ok", res
    assert "Replaced 2 occurrence(s)" in res.get("message", ""), res.get("message")
    body = doc.getText().getString()
    assert body.count("REPLACED") == 2, f"expected 2 replacements; text={body!r}"


@native_test
@with_native_doc("writer")
def test_apply_document_content_all_matches_nbsp_preserving_uno(ctx, doc):
    """all_matches + format-preserving path with NBSP."""
    text = doc.getText()
    cur = text.createTextCursor()
    text.insertString(cur, "alpha" + _NBSP + "beta. alpha" + _NBSP + "beta.", False)

    res = ApplyDocumentContent().execute(
        _tool_ctx(doc, ctx),
        content=["ALPHA BETA"],
        old_content="alpha beta",
        target="search",
        all_matches=True,
    )
    assert res.get("status") == "ok", res
    assert "formatting preserved" in res.get("message", ""), res.get("message")
    body = doc.getText().getString()
    assert body.count("ALPHA BETA") == 2, body


@native_test
@with_native_doc("writer")
def test_apply_document_content_all_matches_mixed_spaces_uno(ctx, doc):
    """LO regex must match NBSP variant even when a normal-space copy exists elsewhere."""
    text = doc.getText()
    cur = text.createTextCursor()
    # Normal space in first phrase; NBSP in second — old_content matches only the NBSP one.
    text.insertString(cur, "phrase one here. phrase two" + _NBSP + "here.", False)

    res = ApplyDocumentContent().execute(
        _tool_ctx(doc, ctx),
        content=["TWO"],
        old_content="phrase two here",
        target="search",
        all_matches=True,
    )
    assert res.get("status") == "ok", res
    assert "Replaced 1 occurrence" in res.get("message", ""), res.get("message")
    body = doc.getText().getString()
    assert "phrase one here" in body
    assert "TWO" in body
    assert "phrase two" + _NBSP + "here" not in body

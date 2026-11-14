# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""GetDocumentContent tool coverage (plugin/writer/get_document_content.py). No LibreOffice."""
from unittest.mock import MagicMock, patch

import pytest


def test_truncated_flag_on_get_document_content():
    from unittest.mock import patch

    from plugin.writer import html_export
    from plugin.writer.get_document_content import GetDocumentContent

    ctx = MagicMock()
    ctx.services.document.get_document_length.return_value = 800
    with patch.object(html_export, "document_to_content",
                      return_value="<p>short</p>\n\n[... truncated ...]"):
        res = GetDocumentContent().execute(ctx, max_chars=10)
    assert res.get("truncated") is True
    assert res.get("hint")
    assert "next_range" not in res
    with patch.object(html_export, "document_to_content", return_value="<p>all</p>"):
        res = GetDocumentContent().execute(ctx, max_chars=10)
    assert "truncated" not in res
    assert "hint" not in res


def test_get_document_content_surfaces_portion_walk_warning():
    """Range/selection paint that hits _COPY_PORTION_LIMIT must not look complete."""
    from unittest.mock import patch

    from plugin.writer import html_export
    from plugin.writer.get_document_content import GetDocumentContent

    ctx = MagicMock()
    ctx.services.document.get_document_length.return_value = 800

    def _fake_export(*_args, **kwargs):
        dest = kwargs.get("walk_warnings")
        if dest is not None:
            dest.append(
                "Walk stopped after 2 text portions (cap 2). Later content was not read, "
                "so formatting may be incomplete."
            )
            dest.append("Second walk warning.")
        return "<p>partial</p>"

    with patch.object(html_export, "document_to_content", side_effect=_fake_export):
        res = GetDocumentContent().execute(ctx, scope="range", start=0, end=10)

    assert res["status"] == "ok"
    assert "incomplete" in res["warning"]
    assert "Second walk warning." in res["warning"]


def _make_redlines_doc(redline_types, count=None):
    """Build a mock doc with enumerated redlines of the given RedlineType strings."""
    doc = MagicMock()
    redlines = MagicMock()
    redlines.getCount.return_value = len(redline_types) if count is None else count
    items = []
    for rtype in redline_types:
        rl = MagicMock()
        rl.getPropertyValue.side_effect = lambda prop, rt=rtype: rt if prop == "RedlineType" else None
        items.append(rl)
    enum = MagicMock()
    enum.hasMoreElements.side_effect = [True] * len(items) + [False]
    enum.nextElement.side_effect = items
    redlines.createEnumeration.return_value = enum
    doc.getRedlines.return_value = redlines
    return doc, redlines, enum, items


def test_get_document_content_surfaces_tracked_changes():
    from plugin.writer.get_document_content import GetDocumentContent

    doc, _redlines, _enum, _items = _make_redlines_doc(["Insert", "Delete"])
    doc.getPropertyValue.return_value = True

    ctx = MagicMock()
    ctx.doc = doc
    ctx.ctx = MagicMock()
    ctx.services.get.return_value = MagicMock()
    with patch("plugin.writer.tracking.redline_text", side_effect=["novo", "velho"]), \
         patch("plugin.writer.html_export.document_to_content", return_value="body"):
        res = GetDocumentContent().execute(ctx)
    assert res["status"] == "ok"
    assert res["tracked_changes"] == [
        {"type": "insertion", "text": "novo"},
        {"type": "deletion", "text": "velho"},
    ]
    assert res["tracked_changes_total"] == 2
    assert "tracked_changes_note" in res
    assert res["tracked_changes_note"]
    assert "could not list" not in res.get("warning", "")


def test_get_document_content_warns_when_redlines_are_not_listed():
    from plugin.writer.get_document_content import GetDocumentContent

    doc, _redlines, enum, _items = _make_redlines_doc([], count=2)
    enum.hasMoreElements.side_effect = None
    enum.hasMoreElements.return_value = True
    enum.nextElement.side_effect = RuntimeError("boom")
    ctx = MagicMock()
    ctx.doc = doc
    ctx.services.document.get_document_length.return_value = 10
    with patch("plugin.writer.html_export.document_to_content", return_value="<p>body</p>"):
        res = GetDocumentContent().execute(ctx)
    assert res["status"] == "ok"
    assert "tracked_changes" not in res
    assert "could not list tracked changes" in res["warning"]


def test_format_only_redlines_are_reported_without_a_listing_failure():
    from plugin.writer.get_document_content import GetDocumentContent

    doc, _redlines, _enum, _items = _make_redlines_doc(["Format"])

    ctx = MagicMock()
    ctx.doc = doc
    ctx.services.document.get_document_length.return_value = 10
    with patch("plugin.writer.html_export.document_to_content", return_value="<p>body</p>"):
        res = GetDocumentContent().execute(ctx)
    assert res["status"] == "ok"
    assert res.get("tracked_changes_total") == 1
    assert "tracked_changes" not in res
    assert "formatting" in res.get("tracked_changes_note", "")
    assert "could not list" not in res.get("warning", "")


def test_redline_cap_reports_truncation(monkeypatch):
    import plugin.writer.get_document_content as gdc
    from plugin.writer.get_document_content import GetDocumentContent, _summarize_redlines

    doc, _redlines, enum, items = _make_redlines_doc(["Insert", "Insert"])

    monkeypatch.setattr(gdc, "_MAX_TRACKED_CHANGES", 1)
    with patch("plugin.writer.tracking.redline_text", side_effect=["one", "two"]):
        changes, found, total, reliable, truncated = _summarize_redlines(doc)
    assert len(changes) == 1
    assert found == 2
    assert total == 2
    assert reliable is True
    assert truncated is True

    enum.hasMoreElements.side_effect = [True, True, False]
    enum.nextElement.side_effect = items
    ctx = MagicMock()
    ctx.doc = doc
    ctx.services.document.get_document_length.return_value = 10
    with patch("plugin.writer.tracking.redline_text", side_effect=["one", "two"]), \
         patch("plugin.writer.html_export.document_to_content", return_value="<p>body</p>"):
        res = GetDocumentContent().execute(ctx)
    assert res.get("tracked_changes_truncated") is True


def test_inserted_image_redline_without_text_is_not_truncated():
    from plugin.writer.get_document_content import GetDocumentContent

    doc, _redlines, _enum, _items = _make_redlines_doc(["Insert", "Insert"])

    ctx = MagicMock()
    ctx.doc = doc
    ctx.services.document.get_document_length.return_value = 10
    with patch("plugin.writer.tracking.redline_text", side_effect=["", ""]), \
         patch("plugin.writer.html_export.document_to_content", return_value="<p>body</p>"):
        res = GetDocumentContent().execute(ctx)
    assert "tracked_changes_truncated" not in res
    assert "insertion/deletion change(s) are listed" not in res.get("tracked_changes_note", "")


def test_range_rejects_invalid_bounds_and_max_chars():
    from plugin.writer.get_document_content import GetDocumentContent

    ctx = MagicMock()
    tool = GetDocumentContent()
    with patch("plugin.writer.html_export.document_to_content") as mock_dtc:
        for kwargs in [
            {"scope": "range", "start": 5, "end": 5},
            {"scope": "range", "start": -1, "end": 5},
            {"max_chars": 0},
            {"max_chars": -5},
        ]:
            res = tool.execute(ctx, **kwargs)
            assert res["status"] == "error"
            assert mock_dtc.call_count == 0


def test_range_result_reports_the_offsets_it_read():
    from plugin.writer.get_document_content import GetDocumentContent

    ctx = MagicMock()
    ctx.services.document.get_document_length.return_value = 100
    with patch("plugin.writer.html_export.document_to_content", return_value="<p>slice</p>") as mock_dtc:
        res = GetDocumentContent().execute(ctx, scope="range", start=2, end=4)
    assert res["status"] == "ok"
    assert res["start"] == 2 and res["end"] == 4
    assert mock_dtc.call_args.kwargs["range_start"] == 2
    assert mock_dtc.call_args.kwargs["range_end"] == 4


def test_range_scope_without_both_offsets_is_an_error():
    from plugin.writer.get_document_content import GetDocumentContent

    ctx = MagicMock()
    with patch("plugin.writer.html_export.document_to_content") as mock_dtc:
        for kwargs in ({"scope": "range"}, {"scope": "range", "start": 2}, {"scope": "range", "end": 4}):
            res = GetDocumentContent().execute(ctx, **kwargs)
            assert res["status"] == "error"
            assert res["message"] == "scope 'range' requires integer start and end."
    mock_dtc.assert_not_called()


@pytest.mark.parametrize(
    "args",
    [
        {"scope": "whole"},
        {"max_chars": True},
        {"scope": "range", "start": "2", "end": 4},
    ],
)
def test_get_document_content_wrong_arguments_are_rejected_before_execute(args):
    from types import SimpleNamespace

    from plugin.framework.tool import prepare_tool_call
    from plugin.writer.get_document_content import GetDocumentContent

    ctx = SimpleNamespace(doc_type="writer", caller="test")
    prep = prepare_tool_call(GetDocumentContent(), ctx, "get_document_content", args)
    assert prep.error is not None
    assert prep.error["code"] == "VALIDATION_ERROR"


def test_selection_with_no_content_warns():
    from plugin.writer.get_document_content import GetDocumentContent

    ctx = MagicMock()
    ctx.services.document.get_document_length.return_value = 100
    with patch("plugin.writer.html_export.document_to_content", return_value=""):
        res = GetDocumentContent().execute(ctx, scope="selection")
    assert res["status"] == "ok"
    assert res.get("warning")


def test_document_length_zero_warns():
    from plugin.writer.get_document_content import GetDocumentContent

    ctx = MagicMock()
    ctx.services.document.get_document_length.return_value = 0
    with patch("plugin.writer.html_export.document_to_content", return_value="<p>x</p>"):
        res = GetDocumentContent().execute(ctx)
    assert "document_length came back 0" in res.get("warning", "")

    with patch("plugin.writer.html_export.document_to_content", return_value=""):
        res = GetDocumentContent().execute(ctx)
    assert "document_length came back 0" not in res.get("warning", "")


def test_get_document_content_redline_read_reraises_disposed():
    from plugin.framework.errors import DocumentDisposedError
    from plugin.writer.get_document_content import GetDocumentContent

    doc = MagicMock()
    doc.getRedlines.side_effect = type("DisposedException", (Exception,), {})("gone")
    ctx = MagicMock()
    ctx.doc = doc
    ctx.services.document.get_document_length.return_value = 10
    with patch("plugin.writer.html_export.document_to_content", return_value="<p>body</p>"):
        with pytest.raises(DocumentDisposedError):
            GetDocumentContent().execute(ctx)

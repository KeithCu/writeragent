# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Pure pytest (no LibreOffice): the structured-return logic of apply_document_content.
# We mock the range finders + the replace helpers so only the replaced_count / status logic
# is exercised: replaced_count == 0 -> status "error", N > 0 -> "ok".
import types
from unittest.mock import MagicMock

import pytest

import plugin.writer.format as format_mod
import plugin.writer.search as search_mod
from plugin.writer.content import ApplyDocumentContent


def _ctx():
    doc = MagicMock()
    um = MagicMock()
    um.isLocked.return_value = False
    doc.getUndoManager.return_value = um
    return types.SimpleNamespace(
        doc=doc, ctx=object(),
        services=types.SimpleNamespace(get=lambda key, default=None: None),
    )


@pytest.fixture(autouse=True)
def _no_libreoffice(monkeypatch):
    # Keep everything in-memory: plain-text content (use_preserve path) and no real replace.
    monkeypatch.setattr(format_mod, "content_has_markup", lambda *a, **k: False)
    monkeypatch.setattr(search_mod, "normalize_search_string_for_find", lambda s: s)
    monkeypatch.setattr(format_mod, "replace_preserving_format", lambda *a, **k: None)
    monkeypatch.setattr(format_mod, "replace_single_range_with_content", lambda *a, **k: None)
    monkeypatch.setattr(search_mod, "drawing_shape_object_containing", lambda *a, **k: None)


class MockRange:
    def getString(self) -> str:
        return "foo"


def test_search_no_match_returns_error_zero(monkeypatch):
    monkeypatch.setattr(search_mod, "find_first_range", lambda doc, s: None)
    res = ApplyDocumentContent().execute(_ctx(), target="search", old_content="zzz", content="BAR")
    assert res["status"] == "error", res
    assert res["replaced_count"] == 0, res


def test_search_single_success(monkeypatch):
    monkeypatch.setattr(search_mod, "find_first_range", lambda doc, s: MockRange())
    res = ApplyDocumentContent().execute(_ctx(), target="search", old_content="foo", content="BAR")
    assert res["status"] == "ok", res
    assert res["replaced_count"] == 1, res


def test_search_all_matches_reports_count(monkeypatch):
    monkeypatch.setattr(search_mod, "find_all_ranges", lambda doc, s: [MockRange(), MockRange(), MockRange()])
    res = ApplyDocumentContent().execute(
        _ctx(), target="search", old_content="foo", content="BAR", all_matches=True)
    assert res["status"] == "ok", res
    assert res["replaced_count"] == 3, res


def test_search_all_matches_no_match_errors(monkeypatch):
    monkeypatch.setattr(search_mod, "find_all_ranges", lambda doc, s: [])
    res = ApplyDocumentContent().execute(
        _ctx(), target="search", old_content="zzz", content="BAR", all_matches=True)
    assert res["status"] == "error", res
    assert res["replaced_count"] == 0, res
    assert res["message"].startswith("Replaced 0 occurrence"), res


# --- data-lo-para is a read report, not an instruction --------------------------------------


def test_read_only_attribute_sent_back_is_flagged():
    """The write path drops data-lo-para. Say so: a silent no-op is the failure this tool's
    callers already get bitten by."""
    from plugin.writer.content import _note_read_only_attrs

    res = _note_read_only_attrs(
        {"status": "ok", "message": "Replaced entire document."},
        ['<p data-lo-para="margin-left:3.25cm">A quoted clause.</p>'])

    assert res["ignored_attributes"] == ["data-lo-para"]
    assert "apply_style" in res["message"]


def test_content_without_the_attribute_is_untouched():
    from plugin.writer.content import _note_read_only_attrs

    original = {"status": "ok", "message": "Replaced entire document."}
    res = _note_read_only_attrs(original, ["<p>A quoted clause.</p>"])

    assert res == original
    assert "ignored_attributes" not in res


def test_failed_write_is_not_annotated():
    """Nothing was written, so there is nothing to say about what was ignored."""
    from plugin.writer.content import _note_read_only_attrs

    original = {"status": "error", "message": "old_content not found."}

    assert _note_read_only_attrs(original, ['<p data-lo-para="margin-left:3cm">x</p>']) == original


def test_plain_string_content_is_accepted():
    """content is normally a list, but the checker must not choke on a bare string."""
    from plugin.writer.content import _note_read_only_attrs

    res = _note_read_only_attrs({"status": "ok"}, '<p data-lo-para="text-align:center">x</p>')

    assert res["ignored_attributes"] == ["data-lo-para"]

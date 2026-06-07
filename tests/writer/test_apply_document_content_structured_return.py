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

import pytest

import plugin.writer.content as content_mod
import plugin.writer.format as format_mod
from plugin.writer.content import ApplyDocumentContent


def _ctx():
    return types.SimpleNamespace(
        doc=object(), ctx=object(),
        services=types.SimpleNamespace(get=lambda key, default=None: None),
    )


@pytest.fixture(autouse=True)
def _no_libreoffice(monkeypatch):
    # Keep everything in-memory: plain-text content (use_preserve path) and no real replace.
    monkeypatch.setattr(format_mod, "content_has_markup", lambda *a, **k: False)
    monkeypatch.setattr(content_mod, "_normalize_search_string_for_find", lambda s: s)
    monkeypatch.setattr(format_mod, "replace_preserving_format", lambda *a, **k: None)
    monkeypatch.setattr(format_mod, "replace_single_range_with_content", lambda *a, **k: None)


def test_search_no_match_returns_error_zero(monkeypatch):
    monkeypatch.setattr(content_mod, "_find_first_range", lambda doc, s: None)
    res = ApplyDocumentContent().execute(_ctx(), target="search", old_content="zzz", content="BAR")
    assert res["status"] == "error", res
    assert res["replaced_count"] == 0, res


def test_search_single_success(monkeypatch):
    monkeypatch.setattr(content_mod, "_find_first_range", lambda doc, s: object())
    res = ApplyDocumentContent().execute(_ctx(), target="search", old_content="foo", content="BAR")
    assert res["status"] == "ok", res
    assert res["replaced_count"] == 1, res


def test_search_all_matches_reports_count(monkeypatch):
    monkeypatch.setattr(content_mod, "_find_all_ranges", lambda doc, s: [object(), object(), object()])
    res = ApplyDocumentContent().execute(
        _ctx(), target="search", old_content="foo", content="BAR", all_matches=True)
    assert res["status"] == "ok", res
    assert res["replaced_count"] == 3, res


def test_search_all_matches_no_match_errors(monkeypatch):
    monkeypatch.setattr(content_mod, "_find_all_ranges", lambda doc, s: [])
    res = ApplyDocumentContent().execute(
        _ctx(), target="search", old_content="zzz", content="BAR", all_matches=True)
    assert res["status"] == "error", res
    assert res["replaced_count"] == 0, res
    assert res["message"].startswith("Replaced 0 occurrence"), res

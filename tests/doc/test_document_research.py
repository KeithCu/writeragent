# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
"""Unit tests for plugin.doc.document_research."""

from __future__ import annotations

import os
import tempfile
import time
from unittest.mock import MagicMock, patch

import pytest

from plugin.doc.document_research import (
    NEARBY_FILE_EXTENSIONS,
    close_document_research_document,
    guess_doc_type_from_path,
    get_document_directory,
    get_work_directory,
    list_nearby_files,
    open_document_for_read,
    resolve_listing_directory,
)


def test_guess_doc_type_from_path():
    assert guess_doc_type_from_path("/tmp/Budget_2026.ods") == "calc"
    assert guess_doc_type_from_path("/tmp/Report.odt") == "writer"
    assert guess_doc_type_from_path("/tmp/Slides.odp") == "draw"
    assert guess_doc_type_from_path("/tmp/readme.txt") == "unknown"


def test_get_document_directory():
    with tempfile.TemporaryDirectory() as tmp:
        report = os.path.join(tmp, "report.odt")
        with open(report, "wb"):
            pass
        model = MagicMock()
        with patch("plugin.doc.document_research.get_document_path", return_value=report):
            assert get_document_directory(model) == tmp


def test_list_nearby_files_scandir_sort_exclude_self():
    with tempfile.TemporaryDirectory() as tmp:
        active = os.path.join(tmp, "Active.ods")
        old = os.path.join(tmp, "Budget_2025.ods")
        new = os.path.join(tmp, "Budget_2026.ods")
        skip_tmp = os.path.join(tmp, "draft.tmp")
        skip_lock = os.path.join(tmp, "~$Budget.ods")
        for path in (active, old, new, skip_tmp, skip_lock):
            with open(path, "wb"):
                pass
        # Make 2026 newer than 2025
        now = time.time()
        os.utime(old, (now - 100, now - 100))
        os.utime(new, (now, now))
        os.utime(active, (now - 50, now - 50))

        model = MagicMock()
        ctx = MagicMock()
        with patch("plugin.doc.document_research.get_document_path", return_value=active):
            with patch("plugin.doc.document_research._collect_open_file_urls", return_value={}):
                with patch("plugin.doc.document_research.resolve_listing_directory", return_value=tmp):
                    result = list_nearby_files(ctx, model)

        assert result["status"] == "ok"
        names = [f["name"] for f in result["files"]]
        assert "Active.ods" not in names
        assert "~$Budget.ods" not in names
        assert "draft.tmp" not in names
        assert names[0] == "Budget_2026.ods"
        assert "Budget_2025.ods" in names


def test_list_nearby_files_filter():
    with tempfile.TemporaryDirectory() as tmp:
        a = os.path.join(tmp, "Budget.ods")
        b = os.path.join(tmp, "Notes.odt")
        for path in (a, b):
            with open(path, "wb"):
                pass
        model = MagicMock()
        ctx = MagicMock()
        with patch("plugin.doc.document_research.get_document_path", return_value=None):
            with patch("plugin.doc.document_research._collect_open_file_urls", return_value={}):
                with patch("plugin.doc.document_research.resolve_listing_directory", return_value=tmp):
                    result = list_nearby_files(ctx, model, filter="budget")
        assert result["status"] == "ok"
        assert [f["name"] for f in result["files"]] == ["Budget.ods"]


def test_list_nearby_truncated():
    with tempfile.TemporaryDirectory() as tmp:
        for i in range(5):
            with open(os.path.join(tmp, f"File{i}.odt"), "wb"):
                pass
        model = MagicMock()
        ctx = MagicMock()
        with patch("plugin.doc.document_research.get_document_path", return_value=None):
            with patch("plugin.doc.document_research._collect_open_file_urls", return_value={}):
                with patch("plugin.doc.document_research.resolve_listing_directory", return_value=tmp):
                    result = list_nearby_files(ctx, model, max_entries=2)
        assert result["truncated"] is True
        assert len(result["files"]) == 2


def test_get_work_directory():
    with tempfile.TemporaryDirectory() as tmp:
        ctx = MagicMock()
        path_settings = MagicMock()
        path_settings.getPropertyValue.return_value = tmp
        ctx.ServiceManager.createInstanceWithContext.return_value = path_settings
        assert get_work_directory(ctx) == os.path.normpath(tmp)


def test_resolve_listing_directory_prefers_saved_parent():
    model = MagicMock()
    ctx = MagicMock()
    with patch("plugin.doc.document_research.get_document_directory", return_value="/saved/parent"):
        with patch("plugin.doc.document_research.get_work_directory") as work:
            assert resolve_listing_directory(ctx, model) == "/saved/parent"
            work.assert_not_called()


def test_resolve_listing_directory_work_fallback():
    model = MagicMock()
    ctx = MagicMock()
    with patch("plugin.doc.document_research.get_document_directory", return_value=None):
        with patch("plugin.doc.document_research.get_work_directory", return_value="/work/dir"):
            assert resolve_listing_directory(ctx, model) == "/work/dir"


def test_nearby_extensions_constant():
    assert ".ods" in NEARBY_FILE_EXTENSIONS
    assert ".odt" in NEARBY_FILE_EXTENSIONS
    assert ".tmp" not in NEARBY_FILE_EXTENSIONS


def test_close_document_research_document_skips_reused_open():
    model = MagicMock()
    close_document_research_document(model, opened_for_document_research=False)
    model.close.assert_not_called()


def test_close_document_research_document_closes_temporary_open():
    model = MagicMock()
    close_document_research_document(model, opened_for_document_research=True)
    model.close.assert_called_once_with(True)


@patch("plugin.doc.document_research.resolve_document_by_url", return_value=(MagicMock(), "calc"))
@patch("plugin.doc.document_research.os.path.isfile", return_value=True)
def test_open_document_for_read_reuses_existing_without_close_flag(mock_isfile, mock_resolve):
    model, doc_type, err, opened_for_document_research = open_document_for_read(MagicMock(), "/tmp/Budget.ods")
    assert err is None
    assert doc_type == "calc"
    assert opened_for_document_research is False
    mock_resolve.assert_called_once()


@patch("plugin.doc.document_research.get_document_type")
@patch("plugin.framework.uno_context.get_desktop")
@patch("plugin.doc.document_research.resolve_document_by_url", return_value=(None, None))
@patch("plugin.doc.document_research.os.path.isfile", return_value=True)
def test_open_document_for_read_sets_close_flag_on_new_load(mock_isfile, mock_resolve, mock_desktop, mock_dtype):
    from plugin.doc.document_helpers import DocumentType

    opened_model = MagicMock()
    mock_desktop.return_value.loadComponentFromURL.return_value = opened_model
    mock_dtype.return_value = DocumentType.CALC
    model, doc_type, err, opened_for_document_research = open_document_for_read(MagicMock(), "/tmp/Budget.ods")
    assert err is None
    assert doc_type == "calc"
    assert model is opened_model
    assert opened_for_document_research is True
